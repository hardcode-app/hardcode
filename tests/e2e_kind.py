#!/usr/bin/env python3
"""
End to end on a local kind cluster.

Installs the chart with the admin image built from this checkout, signs in
through the API (password and TOTP), creates profiles through it and checks
that the operator builds them. The VS Code image isn't loaded (it is 12 GB),
so vscode pods stay Pending, but squid and CoreDNS come up for real, and a
small probe pod in a VS Code container's network position checks that the
NetworkPolicies are enforced (kind's own network plugin enforces them).

    tests/e2e_kind.py                create the cluster "hardcode-e2e", test, delete it
    KEEP_CLUSTER=1 tests/e2e_kind.py leave the cluster for inspection
    CLUSTER=name tests/e2e_kind.py   use a kind cluster you already have
    KIND_NODE_MEMORY=8g ...          the hard memory cap on the kind node container
    SQUID_IMAGE=name ...             a squid image you built, instead of building one

The suite uses a kubeconfig of its own, so your contexts are never touched.
"""

from __future__ import annotations

import base64
import contextlib
import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, Optional

import harness
from harness import FIXTURES, ROOT, Docker, Suite

CLUSTER = os.environ.get("CLUSTER", "hardcode-e2e")
NAMESPACE = "hardcode-e2e"
NODE_MEMORY = os.environ.get("KIND_NODE_MEMORY", "8g")
KEEP_CLUSTER = os.environ.get("KEEP_CLUSTER") == "1"

ADMIN_IMAGE = "hardcode-admin:e2e"
SQUID_IMAGE = os.environ.get("SQUID_IMAGE", "hardcode-squid:test")
PROBE_IMAGE = "hardcode-probe:test"

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "e2e-bootstrap-password"
API_PORT = 18081
API = f"http://127.0.0.1:{API_PORT}"

# Everything the chart and the operator create for the profile "e2e".
PROFILE_RESOURCES = [
    "networkpolicy/hc-hardcode-sandbox-default-deny",
    "secret/e2e-proxy-ca",
    "secret/e2e-vscode-token",
    "configmap/e2e-allowlist",
    "configmap/e2e-vscode-extensions",
    "configmap/e2e-proxy-ca-cert",
    "service/e2e-squid",
    "service/e2e-dns",
    "service/e2e-vscode",
    "deployment/e2e-squid",
    "deployment/e2e-coredns",
    "statefulset/e2e-vscode",
    "networkpolicy/e2e-vscode",
    "networkpolicy/e2e-squid",
    "networkpolicy/e2e-coredns",
]

# The probe image: Ubuntu with Python, built here rather than pulled, because
# kind can't import a multi-platform image from Docker's image store, only
# one built locally for this platform.
PROBE_DOCKERFILE = """\
FROM ubuntu:26.04
RUN apt-get update \\
 && apt-get install -y --no-install-recommends python3-minimal \\
 && rm -rf /var/lib/apt/lists/*
"""


class Kubectl:
    """
    kubectl and helm against the test cluster, through the suite's own kubeconfig.
    """

    def __init__(self, kubeconfig: Path):
        self.environment = {**os.environ, "KUBECONFIG": str(kubeconfig)}

    def __call__(
        self,
        *arguments: object,
        namespace: str = NAMESPACE,
        input: Optional[str] = None,
        timeout: float = 300,
    ) -> harness.Output:
        command = ["kubectl", "-n", namespace, *arguments]
        return harness.run(command, input=input, env=self.environment, timeout=timeout)

    def helm(self, *arguments: object) -> harness.Output:
        return harness.run(["helm", *arguments], env=self.environment, timeout=600)

    def exists(self, resource: str) -> bool:
        return self("get", resource).ok

    def field(self, resource: str, path: str, namespace: str = NAMESPACE) -> str:
        """
        One field of a resource, by JSONPath: ``{.spec.clusterIP}``.
        """
        output = self("get", resource, "-o", f"jsonpath={path}", namespace=namespace)
        return output.text.strip() if output.ok else ""

    def rolled_out(self, deployment: str) -> harness.Output:
        """
        Wait for a Deployment's newest pods to be ready.
        """
        return self("rollout", "status", f"deploy/{deployment}", "--timeout=180s", timeout=200)

    def pod_ip(self, profile: str, component: str) -> str:
        selector = f"hardcode-app.github.io/profile={profile},hardcode-app.github.io/component={component}"
        output = self("get", "pods", "-l", selector, "-o", "jsonpath={.items[0].status.podIP}")
        return output.text.strip()


class AdminApi:
    """
    The admin's HTTP API, reached through a port-forward, with a session cookie.
    """

    def __init__(self) -> None:
        cookies = urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        self._opener = urllib.request.build_opener(cookies)

    def call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """
        Send a request and return the JSON reply, error replies included:
        the API explains a refusal in the body.
        """
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{API}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )

        try:
            with self._opener.open(request, timeout=30) as response:
                reply = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            reply = json.loads(error.read() or b"{}")

        # Every route this suite calls answers with an object.
        return reply if isinstance(reply, dict) else {"reply": reply}

    def healthy(self) -> bool:
        try:
            with self._opener.open(f"{API}/healthz", timeout=2):
                return True
        except OSError:
            return False


@contextlib.contextmanager
def kind_cluster(suite: Suite) -> Iterator[Kubectl]:
    """
    The test cluster, created when it doesn't exist and deleted afterwards.

    A cluster that already existed is left in place, with the chart
    uninstalled; KEEP_CLUSTER=1 leaves everything.
    """
    descriptor, name = tempfile.mkstemp(prefix="hc-e2e-kubeconfig-")
    os.close(descriptor)
    kubeconfig = Path(name)
    kubectl = Kubectl(kubeconfig)

    existing = harness.run(["kind", "get", "clusters"]).text.split()
    created = CLUSTER not in existing

    if created:
        suite.section(f"creating kind cluster {CLUSTER}")
        output = harness.run(
            ["kind", "create", "cluster", "--name", CLUSTER, "--wait", "120s", "--kubeconfig", kubeconfig],
            timeout=600,
        )
        if not output.ok:
            suite.abort("kind could not create the cluster", output.text)
    else:
        kubeconfig.write_text(harness.run(["kind", "get", "kubeconfig", "--name", CLUSTER]).text)

    try:
        # Whatever runs in the test cluster, it can't take more than this from
        # the host. A proxy once took 58 GB from inside a test cluster.
        node = f"{CLUSTER}-control-plane"
        capped = harness.run(["docker", "update", "--memory", NODE_MEMORY, "--memory-swap", NODE_MEMORY, node])
        suite.require(f"the kind node container is capped at {NODE_MEMORY}", capped.ok, capped.text)

        yield kubectl
    finally:
        if KEEP_CLUSTER:
            suite.note(f"kept: cluster {CLUSTER}, release hc in {NAMESPACE} (KUBECONFIG={kubeconfig})")
        else:
            if created:
                harness.run(["kind", "delete", "cluster", "--name", CLUSTER, "--kubeconfig", kubeconfig], timeout=300)
            else:
                kubectl.helm("-n", NAMESPACE, "uninstall", "hc")
                kubectl("delete", "namespace", NAMESPACE, "--wait=false")
            kubeconfig.unlink(missing_ok=True)


@contextlib.contextmanager
def port_forward(kubectl: Kubectl) -> Iterator[None]:
    """
    The admin Service on localhost while the block runs.
    """
    forward = subprocess.Popen(
        ["kubectl", "-n", NAMESPACE, "port-forward", "svc/hc-hardcode-admin", f"{API_PORT}:80"],
        env=kubectl.environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield
    finally:
        forward.terminate()
        forward.wait(timeout=10)


def load_images(suite: Suite, docker: Docker) -> None:
    """
    Build the admin and squid images from the checkout and load them into
    the cluster's node.
    """
    suite.section("building and loading images")

    built = docker.build(ADMIN_IMAGE, ROOT, dockerfile=ROOT / "images" / "admin" / "Dockerfile")
    suite.require(f"{ADMIN_IMAGE} builds", built.ok, built.text)

    # The proxy as it is in the tree, not whatever was built last.
    if "SQUID_IMAGE" not in os.environ:
        built = docker.build(SQUID_IMAGE, ROOT / "images" / "squid")
        suite.require(f"{SQUID_IMAGE} builds", built.ok, built.text)

    for image in (ADMIN_IMAGE, SQUID_IMAGE):
        loaded = harness.run(["kind", "load", "docker-image", image, "--name", CLUSTER], timeout=600)
        suite.require(f"{image} is loaded into the cluster", loaded.ok, loaded.text)


def install_chart(suite: Suite, kubectl: Kubectl) -> None:
    suite.section("installing the chart")

    settings = [
        f"admin.email={ADMIN_EMAIL}",
        f"admin.password={ADMIN_PASSWORD}",
        "admin.image.repository=hardcode-admin",
        "admin.image.tag=e2e",
        "admin.image.pullPolicy=Never",
        # The VS Code image isn't loaded: its pods stay Pending, on purpose.
        "profileDefaults.image=hardcode-vscode:e2e-not-loaded",
        f"profileDefaults.squidImage={SQUID_IMAGE}",
        "profileDefaults.imagePullPolicy=IfNotPresent",
        "profileDefaults.docker.enabled=false",
        "profiles[0].name=e2e",
    ]
    command = ["upgrade", "--install", "hc", ROOT / "helm" / "hardcode", "-n", NAMESPACE]
    command += ["--create-namespace", "--wait", "--timeout", "180s"]
    for setting in settings:
        command += ["--set", setting]

    installed = kubectl.helm(*command)
    suite.require("the chart installs", installed.ok, installed.text)

    rolled_out = kubectl.rolled_out("hc-hardcode-admin")
    suite.require("the admin is running", rolled_out.ok, rolled_out.text)


def sign_in(suite: Suite, kubectl: Kubectl, api: AdminApi) -> None:
    """
    Sign in as the bootstrap admin: the password first, then enrol in TOTP
    with the secret the admin stored, as an authenticator app would.
    """
    suite.section("signing in (password + TOTP)")
    suite.require("the admin answers through the port-forward", harness.wait_until(api.healthy, timeout=30))

    reply = api.call("POST", "/api/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    suite.require("the password is accepted and enrolment is next", reply.get("next") == "enrol", str(reply))

    users = kubectl.field("secret/hardcode-admin-users", r"{.data.users\.json}")
    secret = json.loads(base64.b64decode(users))[0]["totpSecret"]

    # The admin's own TOTP code, imported here because this file first runs
    # outside the build image, where only the standard library is assumed.
    sys.path[:0] = [str(ROOT / "cli"), str(ROOT / "admin")]
    from hardcode_admin.totp import totp

    reply = api.call("POST", "/api/totp", {"code": totp(secret)})
    suite.require("the TOTP code is accepted", reply.get("ok") is True, str(reply))
    suite.note(f"signed in as {api.call('GET', '/api/me')}")


def check_reconciled(suite: Suite, kubectl: Kubectl) -> None:
    """
    The profile the chart created is turned into its resources.
    """
    suite.section("the chart's profile is reconciled")

    def phase() -> str:
        return kubectl.field("profile/e2e", "{.status.phase}")

    harness.wait_until(lambda: phase() != "", timeout=120, interval=2)
    message = kubectl.field("profile/e2e", "{.status.message}")
    suite.require(
        "the operator reports on the profile without an error",
        phase() not in ("", "Error"),
        f"{phase()}: {message}",
    )
    suite.note(f"phase: {phase()} ({message})")

    missing = [resource for resource in PROFILE_RESOURCES if not kubectl.exists(resource)]
    suite.require(
        f"all {len(PROFILE_RESOURCES)} resources exist (the namespace deny-all included)",
        not missing,
        f"missing: {missing}",
    )

    # The VS Code container resolves names only through the profile's own filtering DNS.
    dns_ip = kubectl.field("svc/e2e-dns", "{.spec.clusterIP}")
    nameserver = kubectl.field("sts/e2e-vscode", "{.spec.template.spec.dnsConfig.nameservers[0]}")
    suite.require(
        f"the VS Code container's nameserver is the DNS Service ({dns_ip})",
        dns_ip == nameserver,
        f"nameserver {nameserver}",
    )

    coredns = kubectl.rolled_out("e2e-coredns")
    suite.require("the CoreDNS pod is ready", coredns.ok, coredns.text)

    squid = kubectl.rolled_out("e2e-squid")
    if not squid.ok:
        pods = kubectl("get", "pods", "-l", "hardcode-app.github.io/component=squid").text
        logs = kubectl("logs", "deploy/e2e-squid", "-c", "squid", "--tail=20").text
        suite.abort("the squid pod is not ready", f"{pods}\n{logs}")
    suite.check("the squid pod is ready (rendered allowlist, CA secret, TLS inspection config)", True)


def check_profiles_through_api(suite: Suite, kubectl: Kubectl, api: AdminApi) -> None:
    suite.section("profiles through the API")

    reply = api.call("POST", "/api/profiles", {"name": "team-b", "from": "e2e", "vscodeHost": "b.example.com"})
    suite.require("team-b is created", reply.get("name") == "team-b", str(reply))
    harness.wait_until(lambda: kubectl.exists("sts/team-b-vscode"), timeout=60, interval=2)
    suite.check("team-b gets an Ingress for its host", kubectl.exists("ingress/team-b-vscode"))

    reply = api.call("POST", "/api/allowlist/bulk", {"op": "add", "entry": "github.com/ourcompany", "flag": ""})
    both_changed = reply.get("e2e") == "changed" and reply.get("team-b") == "changed"
    suite.check("a bulk add reaches both profiles", both_changed, str(reply))

    def rolled_into_config_map() -> bool:
        allowlist = kubectl.field("configmap/team-b-allowlist", r"{.data.allowlist\.txt}")
        return any(line.startswith("github.com/ourcompany") for line in allowlist.splitlines())

    suite.check("...and the operator rolls it into the ConfigMap", harness.wait_until(rolled_into_config_map, 60, 2))

    # The entries HardCode provides are the operator's to add: there in the
    # ConfigMap, though not in the spec, and one switched off through the
    # API is commented out there.
    def config_map_lines() -> list[str]:
        return kubectl.field("configmap/team-b-allowlist", r"{.data.allowlist\.txt}").splitlines()

    spec_allowlist = kubectl.field("profile/team-b", "{.spec.allowlist}")
    suite.check(
        "the operator adds the provided entries, which the spec doesn't have",
        "update.code.visualstudio.com" in config_map_lines() and "update.code.visualstudio.com" not in spec_allowlist,
    )

    reply = api.call(
        "POST", "/api/profiles/team-b/allowlist", {"op": "disable", "entry": "update.code.visualstudio.com"}
    )
    suite.check("a provided entry is switched off through the API", reply.get("changed") is True, str(reply))
    suite.check(
        "...and the operator comments it out in the ConfigMap",
        harness.wait_until(
            lambda: any(line.startswith("#update.code.visualstudio.com") for line in config_map_lines()),
            60,
            2,
        ),
    )

    # An extension of the profile's own goes into the VS Code container's
    # extension list, which the running container follows, and nowhere
    # else: the pod template stays as it was, so nothing restarts.
    generation = kubectl.field("sts/team-b-vscode", "{.metadata.generation}")
    reply = api.call("POST", "/api/profiles/team-b/extensions", {"op": "add", "id": "eamodio.gitlens"})
    suite.check("an extension of the profile's own is added", reply.get("changed") is True, str(reply))

    def in_the_extension_list() -> bool:
        wanted = kubectl.field("configmap/team-b-vscode-extensions", "{.data.wanted}")
        return "eamodio.gitlens" in wanted.splitlines()

    suite.check(
        "...and the operator writes it into the extension list",
        harness.wait_until(in_the_extension_list, 60, 2),
    )
    suite.check(
        "...without touching the VS Code container's pod template",
        kubectl.field("sts/team-b-vscode", "{.metadata.generation}") == generation,
        f"generation {generation} before",
    )

    reply = api.call("POST", "/api/profiles/team-b/allowlist", {"op": "add", "entry": "bad entry"})
    suite.check("an invalid entry is refused", "error" in reply, str(reply))

    api.call("PATCH", "/api/profiles/team-b", {"vscodeHost": ""})
    removed = harness.wait_until(lambda: not kubectl.exists("ingress/team-b-vscode"), timeout=60, interval=2)
    suite.check("clearing the host removes the Ingress", removed)


def check_policies_enforced(suite: Suite, kubectl: Kubectl, docker: Docker) -> None:
    """
    A pod in a VS Code container's network position reaches its profile's squid and
    DNS, and nothing else.

    It carries a VS Code container's labels, so the namespace deny-all and the profile's
    vscode policy select it. Targets are addressed by IP, since the only
    nameserver it may reach is the profile's own: the Services, and the pods
    behind them, because policies are written against pod addresses.
    """
    suite.section("the policies are enforced: a probe in the VS Code container's network position")

    built = docker.build_from_text(PROBE_IMAGE, PROBE_DOCKERFILE)
    suite.require(f"{PROBE_IMAGE} builds", built.ok, built.text)
    loaded = harness.run(["kind", "load", "docker-image", PROBE_IMAGE, "--name", CLUSTER], timeout=600)
    suite.require(f"{PROBE_IMAGE} is loaded into the cluster", loaded.ok, loaded.text)

    # The bulk edit above rolled squid and CoreDNS (their strategy is
    # Recreate). Until the new pods are up, the Services have no endpoints
    # and refuse connections rather than route them.
    for deployment in ("e2e-squid", "e2e-coredns"):
        rolled_out = kubectl.rolled_out(deployment)
        suite.require(f"{deployment} rolled out after the allowlist change", rolled_out.ok, rolled_out.text)

    def service_ip(name: str, namespace: str = NAMESPACE) -> str:
        return kubectl.field(f"svc/{name}", "{.spec.clusterIP}", namespace=namespace)

    allowed = {
        "squid-svc": f"{service_ip('e2e-squid')}:3128",
        "squid-pod": f"{kubectl.pod_ip('e2e', 'squid')}:3128",
        "dns-svc": f"{service_ip('e2e-dns')}:53",
        "dns-pod": f"{kubectl.pod_ip('e2e', 'coredns')}:53",
    }
    refused = {
        "other-profile-squid-svc": f"{service_ip('team-b-squid')}:3128",
        "other-profile-squid-pod": f"{kubectl.pod_ip('team-b', 'squid')}:3128",
        "admin": f"{service_ip('hc-hardcode-admin')}:80",
        "cluster-dns": f"{service_ip('kube-dns', namespace='kube-system')}:53",
        "internet": "1.1.1.1:443",
    }
    targets = " ".join(f"{name}={address}" for name, address in {**allowed, **refused}.items())

    probe = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "e2e-probe",
            "labels": {
                "hardcode-app.github.io/profile": "e2e",
                "hardcode-app.github.io/component": "vscode",
                "app.kubernetes.io/managed-by": "hardcode-operator",
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "probe",
                    "image": PROBE_IMAGE,
                    "imagePullPolicy": "Never",
                    "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "64Mi"}},
                    "env": [{"name": "TARGETS", "value": targets}],
                    "command": ["python3", "-c", (FIXTURES / "network_probe.py").read_text()],
                }
            ],
        },
    }
    applied = kubectl("apply", "-f", "-", input=json.dumps(probe))
    suite.require("the probe pod is created", applied.ok, applied.text)

    finished = kubectl(
        "wait", "--for=jsonpath={.status.phase}=Succeeded", "pod/e2e-probe", "--timeout=180s", timeout=200
    )
    if not finished.ok:
        suite.abort("the probe pod didn't finish", kubectl("describe", "pod", "e2e-probe").tail(12))

    report = kubectl("logs", "e2e-probe").text
    suite.note(report)
    lines = report.splitlines()

    unreachable = [name for name in allowed if f"reachable {name}" not in lines]
    suite.check(
        "it reaches its own squid (3128) and its own DNS (53), by Service and by pod",
        not unreachable,
        f"couldn't reach: {unreachable}",
    )
    reached = [name for name in refused if not any(line.startswith(f"unreachable {name} ") for line in lines)]
    suite.check(
        "and nothing else: another profile's proxy, the admin, cluster DNS and the internet are refused",
        not reached,
        f"reached, though the policies must refuse: {reached}",
    )

    kubectl("delete", "pod", "e2e-probe", "--wait=false")


def check_suspend_and_delete(suite: Suite, kubectl: Kubectl, api: AdminApi) -> None:
    suite.section("suspend and delete")

    api.call("PATCH", "/api/profiles/team-b", {"suspended": True})
    scaled_down = harness.wait_until(lambda: kubectl.field("sts/team-b-vscode", "{.spec.replicas}") == "0", 60, 2)
    suite.check("suspend scales the VS Code container to 0", scaled_down)

    api.call("DELETE", "/api/profiles/team-b")
    gone = harness.wait_until(lambda: not kubectl.exists("sts/team-b-vscode"), timeout=120, interval=2)
    suite.check("delete removes team-b and everything it owned", gone)

    suite.note(kubectl("get", "profiles").text)


def run_checks(suite: Suite) -> None:
    with Docker() as docker, kind_cluster(suite) as kubectl:
        load_images(suite, docker)
        install_chart(suite, kubectl)

        api = AdminApi()
        with port_forward(kubectl):
            sign_in(suite, kubectl, api)
            check_reconciled(suite, kubectl)
            check_profiles_through_api(suite, kubectl, api)
            check_policies_enforced(suite, kubectl, docker)
            check_suspend_and_delete(suite, kubectl, api)


if __name__ == "__main__":
    harness.main("e2e_kind", run_checks)
