"""
Tests for what a Profile renders into (hardcode_admin.manifests) and for the
operator that applies it (hardcode_admin.operator), against a fake API server.
"""

import base64
import dataclasses
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hardcode_admin import manifests
from hardcode_admin.operator import Operator, _status_only, allowlist_hash, generate_ca, profiles_path
from hardcode_cli import langs

from .helpers import FakeK8s, profile, ready_pod

PROFILES = profiles_path("test")
STATEFULSETS = "/apis/apps/v1/namespaces/test/statefulsets"
DEPLOYMENTS = "/apis/apps/v1/namespaces/test/deployments"
CONFIGMAPS = "/api/v1/namespaces/test/configmaps"
SECRETS = "/api/v1/namespaces/test/secrets"
INGRESSES = "/apis/networking.k8s.io/v1/namespaces/test/ingresses"
STS = f"{STATEFULSETS}/p3-vscode"


def find(objects, kind, name=None):
    """
    The first rendered object of *kind* (and *name*, when given).
    """
    return next(obj for obj in objects if obj["kind"] == kind and (name is None or obj["metadata"]["name"] == name))


def env_of(container):
    """
    A container's environment as a dict of name to value.
    """
    return {variable["name"]: variable.get("value") for variable in container["env"]}


def pod_spec(obj):
    return obj["spec"]["template"]["spec"]


class EffectiveSpecTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        spec = manifests.effective_spec(
            profile("p", {"image": "mine:1", "docker": {"enabled": False}, "home": {"size": "50Gi"}}),
            {"image": "default:1", "home": {"storageClass": "fast"}},
        )

        self.assertEqual(spec["image"], "mine:1")
        self.assertEqual(spec["home"], {"size": "50Gi", "storageClass": "fast"})
        self.assertFalse(spec["docker"]["enabled"])
        self.assertTrue(spec["docker"]["privileged"])
        self.assertEqual(spec["squidResources"], manifests.SQUID_RESOURCES)
        self.assertEqual(manifests.effective_spec({"metadata": {"name": "x"}})["image"], "hardcode-vscode:latest")

    def test_empty_strings_mean_inherit(self):
        spec = manifests.effective_spec(
            profile("p", {"image": "", "vscodeHost": "", "home": {"size": ""}}),
            {"image": "default:1"},
        )

        self.assertEqual((spec["image"], spec["vscodeHost"], spec["home"]["size"]), ("default:1", "", "20Gi"))

    def test_languages_and_extensions(self):
        def languages(spec, defaults=None):
            return manifests.effective_spec(profile("p", spec), defaults)["languages"]

        self.assertEqual(languages({}), list(langs.LANGS), "a profile that doesn't choose has them all")
        self.assertEqual(languages({}, {"languages": []}), list(langs.LANGS), "an empty chart default means all")
        self.assertEqual(languages({}, {"languages": ["go", "python"]}), ["python", "go"], "the chart's default")
        self.assertEqual(
            languages({"languages": ["docker", "rust", "cobol"]}, {"languages": ["go"]}),
            ["rust", "docker"],
            "the profile's own, in catalogue order, without keys the catalogue doesn't know",
        )
        self.assertEqual(languages({"languages": []}, {"languages": ["go"]}), [], "an empty list of its own is none")

        self.assertEqual(manifests.effective_spec(profile("p"))["extensions"], [])
        self.assertEqual(
            manifests.effective_spec(profile("p", {"extensions": ["eamodio.gitlens"]}))["extensions"],
            ["eamodio.gitlens"],
        )

    def test_every_container_keeps_a_memory_limit(self):
        spec = manifests.effective_spec(profile("p"), {})
        self.assertEqual(spec["resources"], manifests.VSCODE_RESOURCES)
        self.assertEqual(spec["dockerResources"], manifests.DOCKER_RESOURCES)

        spec = manifests.effective_spec(
            profile("p", {"resources": {"requests": {"cpu": "2"}, "limits": {"cpu": "4"}}}),
            {"dockerResources": {"limits": {"memory": "16Gi"}}},
        )
        self.assertEqual(
            spec["resources"],
            {"requests": {"cpu": "2"}, "limits": {"cpu": "4", "memory": "8Gi"}},
            "an override without a memory limit gets the default one",
        )
        self.assertEqual(spec["dockerResources"]["limits"]["memory"], "16Gi")
        self.assertEqual(manifests.with_memory_limit("junk", manifests.SQUID_RESOURCES)["limits"]["memory"], "512Mi")

    def test_the_fallback_images_are_the_ones_compose_and_the_chart_pin(self):
        # Dependabot moves the pins in compose.yaml and the chart's
        # values.yaml together; the operator's fallbacks follow by hand.
        repo = Path(__file__).resolve().parent.parent.parent
        compose = (repo / "compose.yaml").read_text(encoding="utf-8")
        values = (repo / "helm" / "hardcode" / "values.yaml").read_text(encoding="utf-8")
        fallbacks = manifests.effective_spec(profile("p"))

        for key, name in (("corednsImage", "coredns/coredns"), ("dockerImage", "docker")):
            with self.subTest(image=name):
                pinned = fallbacks[key]

                self.assertTrue(pinned.startswith(f"{name}:"), pinned)
                self.assertIn(f"image: {pinned}\n", compose)
                self.assertIn(f"  {key}: {pinned}\n", values)

    def test_a_profile_without_a_memory_limit_inherits_the_charts(self):
        spec = manifests.effective_spec(
            profile("p", {"resources": {"requests": {"memory": "12Gi"}}}),
            {
                "resources": {"requests": {"memory": "1Gi"}, "limits": {"memory": "16Gi"}},
                "dockerResources": {"requests": {"cpu": "1"}},
            },
        )

        self.assertEqual(
            spec["resources"],
            {"requests": {"memory": "12Gi"}, "limits": {"memory": "16Gi"}},
            "the chart's default limit, not the built-in 8Gi",
        )
        self.assertEqual(
            spec["dockerResources"],
            {"requests": {"cpu": "1"}, "limits": {"memory": "4Gi"}},
            "a chart default without a limit falls back to the built-in one",
        )

    def test_quantities(self):
        self.assertEqual(manifests.quantity_bytes("20Gi"), 20 * 2**30)
        self.assertEqual(manifests.quantity_bytes("500M"), 500 * 10**6)
        self.assertEqual(manifests.quantity_bytes("1.5Ti"), 1.5 * 2**40)
        self.assertEqual(manifests.quantity_bytes("1024"), 1024)
        self.assertLess(manifests.quantity_bytes("20Gi"), manifests.quantity_bytes("50Gi"))

        for size in ("20Gi", "500Mi", "1.5T", "1024"):
            self.assertTrue(manifests.is_quantity(size), size)
        for size in ("50GB", "20 Gi", "Gi", "", "-1Gi"):
            self.assertFalse(manifests.is_quantity(size), size)

    def test_names_and_paths(self):
        names = manifests.Names("team-a")
        self.assertEqual(
            (
                names.squid,
                names.dns,
                names.vscode,
                names.vscode_pod,
                names.token,
                names.proxy_ca,
                names.extensions,
                names.pvc("home"),
            ),
            (
                "team-a-squid",
                "team-a-dns",
                "team-a-vscode",
                "team-a-vscode-0",
                "team-a-vscode-token",
                "team-a-proxy-ca",
                "team-a-vscode-extensions",
                "home-team-a-vscode-0",
            ),
        )

        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"namespace": "ns", "name": "s"}}
        self.assertEqual(manifests.path_for(secret), "/api/v1/namespaces/ns/secrets/s")

        statefulset = {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"namespace": "ns", "name": "d"}}
        self.assertEqual(manifests.path_for(statefulset), "/apis/apps/v1/namespaces/ns/statefulsets/d")

        self.assertEqual(
            manifests.collection_path("ns", "networking.k8s.io/v1", "Ingress"),
            "/apis/networking.k8s.io/v1/namespaces/ns/ingresses",
        )


class RenderTests(unittest.TestCase):
    CTX = {"dnsClusterIP": "10.96.0.5", "caCert": "CERT", "allowlistHash": "h", "tokenHash": "t"}

    def test_full_sandbox_with_owner_references(self):
        objects = manifests.render(
            profile("p1", {"vscodeHost": "p1.example.com"}),
            {"image": "reg/vscode:1", "ingressClassName": "nginx", "ingressTLSSecret": "wild"},
            self.CTX,
        )

        self.assertEqual(
            [f"{obj['kind']}/{obj['metadata']['name']}" for obj in objects],
            [
                "ConfigMap/p1-allowlist",
                "ConfigMap/p1-vscode-extensions",
                "ConfigMap/p1-proxy-ca-cert",
                "Service/p1-squid",
                "Service/p1-dns",
                "Service/p1-vscode",
                "NetworkPolicy/p1-vscode",
                "NetworkPolicy/p1-squid",
                "NetworkPolicy/p1-coredns",
                "Deployment/p1-squid",
                "Deployment/p1-coredns",
                "StatefulSet/p1-vscode",
                "Ingress/p1-vscode",
            ],
            "isolation before workloads, the Ingress last",
        )
        for obj in objects:
            self.assertEqual(obj["metadata"]["ownerReferences"][0]["uid"], "u1")
            self.assertEqual(obj["metadata"]["namespace"], "test")

        for obj in objects:
            if obj["kind"] in ("Deployment", "StatefulSet"):
                self.assertIs(
                    pod_spec(obj)["automountServiceAccountToken"],
                    False,
                    f"{obj['metadata']['name']}: no cluster credentials in a sandbox pod",
                )
            if obj["kind"] == "Deployment":
                self.assertEqual(
                    obj["spec"]["strategy"],
                    {"type": "Recreate"},
                    "the old policy pod goes before the new one starts",
                )

        # The VS Code container: its daemon beside it, its DNS, its limits, its volumes.
        statefulset = find(objects, "StatefulSet")
        pod = pod_spec(statefulset)
        vscode, docker = pod["containers"]

        env = env_of(vscode)
        self.assertEqual((env["ENABLE_DOCKER"], env["DOCKER_HOST"]), ("true", "tcp://localhost:2376"))
        self.assertEqual(env["HTTPS_PROXY"], "http://p1-squid.test.svc.cluster.local:3128")
        self.assertEqual(pod["dnsConfig"]["nameservers"], ["10.96.0.5"])
        self.assertEqual([vscode["name"], docker["name"]], ["vscode", "docker"])
        self.assertEqual(vscode["image"], "reg/vscode:1")
        self.assertEqual(vscode["resources"]["limits"]["memory"], "8Gi")
        self.assertEqual(docker["resources"]["limits"]["memory"], "4Gi", "the sidecar is capped too")

        self.assertEqual(statefulset["spec"]["persistentVolumeClaimRetentionPolicy"]["whenDeleted"], "Delete")
        self.assertEqual(
            [claim["metadata"]["name"] for claim in statefulset["spec"]["volumeClaimTemplates"]],
            ["home", "shared", "docker-data"],
        )
        self.assertEqual(
            statefulset["spec"]["template"]["metadata"]["annotations"], {"hardcode-app.github.io/token-hash": "t"}
        )

        seed = pod["initContainers"][0]
        self.assertEqual(
            (seed["name"], seed["image"], seed["command"][-2:]),
            ("seed-home", "reg/vscode:1", ["/home/dev", "/mnt/home"]),
        )
        self.assertIn("cp -a", seed["command"][2])

        # The Ingress, with the chart's class and TLS secret.
        ingress = find(objects, "Ingress")
        self.assertEqual(ingress["spec"]["rules"][0]["host"], "p1.example.com")
        self.assertEqual(ingress["spec"]["ingressClassName"], "nginx")
        self.assertEqual(ingress["spec"]["tls"][0]["secretName"], "wild")

        # The DNS Service keeps the address it was given; squid is capped,
        # and its renderer answers for the proxy's own name.
        self.assertEqual(find(objects, "Service", "p1-dns")["spec"]["clusterIP"], "10.96.0.5")

        squid_pod = pod_spec(find(objects, "Deployment", "p1-squid"))
        self.assertEqual(env_of(squid_pod["initContainers"][0])["EXTRA_ZONES"], "p1-squid.test.svc.cluster.local")
        self.assertEqual(
            squid_pod["containers"][0]["resources"]["limits"]["memory"],
            "512Mi",
            "the proxy is memory-capped",
        )

    def test_suspended_without_docker_policy_or_ingress(self):
        objects = manifests.render(
            profile("p2", {"suspended": True, "docker": {"enabled": False}, "networkPolicy": {"enabled": False}}),
            {},
            {"caCert": "C", "allowlistHash": "h"},
        )

        statefulset = find(objects, "StatefulSet")
        containers = pod_spec(statefulset)["containers"]
        self.assertEqual(statefulset["spec"]["replicas"], 0)
        self.assertEqual([container["name"] for container in containers], ["vscode"])

        env = env_of(containers[0])
        self.assertEqual(env["ENABLE_DOCKER"], "false")
        self.assertNotIn("DOCKER_HOST", env, "no daemon, no DOCKER_* pointing at one")

        self.assertEqual(
            [claim["metadata"]["name"] for claim in statefulset["spec"]["volumeClaimTemplates"]],
            ["home", "shared", "docker-data"],
            "the claim set never depends on the sidecar",
        )
        self.assertFalse(any(obj["kind"] in ("NetworkPolicy", "Ingress") for obj in objects))
        self.assertTrue(all(obj["spec"]["replicas"] == 0 for obj in objects if obj["kind"] == "Deployment"))

        # Before the DNS Service has an address, nothing resolves.
        self.assertNotIn("clusterIP", find(objects, "Service", "p2-dns")["spec"])
        self.assertEqual(pod_spec(statefulset)["dnsConfig"]["nameservers"], ["127.0.0.1"])

    def test_existing_claims_are_kept_verbatim(self):
        current = manifests.claim_templates(manifests.effective_spec(profile("p2")))

        objects = manifests.render(
            profile("p2", {"home": {"size": "50Gi"}}),
            {},
            {**self.CTX, "existingClaims": current},
        )

        self.assertEqual(
            find(objects, "StatefulSet")["spec"]["volumeClaimTemplates"],
            current,
            "immutable on the API: a size change goes to the PVC instead",
        )

    def test_token_change_rolls_the_vscode(self):
        def template(token_hash):
            objects = manifests.render(profile("p"), {}, {**self.CTX, "tokenHash": token_hash})
            return find(objects, "StatefulSet")["spec"]["template"]

        self.assertNotEqual(template("before"), template("after"))

    def wanted(self, spec, defaults=None):
        """
        The extension list a profile's ConfigMap hands its VS Code container.
        """
        objects = manifests.render(profile("p", spec), defaults or {}, self.CTX)
        return find(objects, "ConfigMap", "p-vscode-extensions")["data"]

    def test_the_extension_list_is_its_languages_and_its_own(self):
        spec = {"languages": ["python", "codex_cli"], "extensions": ["eamodio.gitlens", "foo.bar"]}

        data = self.wanted(spec)
        self.assertEqual(
            data, {"wanted": langs.extension_list(["python", "codex_cli"], ["eamodio.gitlens", "foo.bar"])}
        )
        self.assertIn("openai.chatgpt\n", data["wanted"], "the Codex CLI brings its extension")
        self.assertTrue(data["wanted"].endswith(f"{langs.EXTENSION_LIST_END}\n"), "a complete list")

        # Without languages of its own: the chart's default, or every one.
        self.assertEqual(self.wanted({}, {"languages": ["go"]}), {"wanted": langs.extension_list(["go"], [])})
        self.assertEqual(self.wanted({}), {"wanted": langs.extension_list(list(langs.LANGS), [])})
        self.assertEqual(self.wanted({"languages": []}), {"wanted": langs.extension_list([], [])}, "core only")

    def test_the_extension_list_is_mounted_as_a_directory(self):
        objects = manifests.render(profile("p", {"extensions": ["eamodio.gitlens"]}), {}, self.CTX)
        pod = pod_spec(find(objects, "StatefulSet"))
        vscode = pod["containers"][0]

        mount = next(mount for mount in vscode["volumeMounts"] if mount["name"] == "extensions")
        self.assertEqual(
            mount,
            {"name": "extensions", "mountPath": "/etc/hardcode/extensions", "readOnly": True},
            "a directory: the kubelet updates it in a running pod, which it never does for a subPath",
        )
        volume = next(volume for volume in pod["volumes"] if volume["name"] == "extensions")
        self.assertEqual(volume, {"name": "extensions", "configMap": {"name": "p-vscode-extensions"}})

        env = env_of(vscode)
        self.assertEqual(
            [name for name in env if name.startswith("INSTALL_") or name == "VSCODE_EXTENSIONS"],
            [],
            "the list isn't in the environment, where a change would restart the pod",
        )

    def test_a_profiles_own_variable_wins_without_a_duplicate(self):
        own = {"HTTP_PROXY": "http://mine:3128", "PIP_CERT": "", "VSCODE_DEFAULT_FOLDER": "/shared"}
        spec = {"languages": ["python"], "env": own}
        vscode = pod_spec(find(manifests.render(profile("p", spec), {}, self.CTX), "StatefulSet"))["containers"][0]
        names = [variable["name"] for variable in vscode["env"]]

        # Ours (the proxy, the CA bundle) and the profile's alike:
        # server-side apply refuses a variable twice.
        self.assertEqual(sorted(set(names)), sorted(names))
        for name, value in own.items():
            self.assertEqual(env_of(vscode)[name], value, name)

    def test_new_languages_and_extensions_leave_the_pod_alone(self):
        def rendered(spec):
            objects = manifests.render(profile("p", spec), {}, self.CTX)
            return find(objects, "StatefulSet")["spec"]["template"], self.wanted(spec)

        before, before_list = rendered({"languages": ["python"]})

        for change in ({"languages": ["python", "go"]}, {"languages": ["python"], "extensions": ["eamodio.gitlens"]}):
            after, after_list = rendered(change)
            self.assertEqual(
                after,
                before,
                f"{change}: the pod template is the same, so the VS Code container keeps running",
            )
            self.assertNotEqual(after_list, before_list, f"{change}: the change goes into its extension list")


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.k8s = FakeK8s()
        self.operator = Operator(self.k8s, {"image": "img:1"})
        self.created = self.k8s.create(PROFILES, profile("p3"))

    def current(self, name="p3"):
        return self.k8s.get(f"{PROFILES}/{name}")

    def update(self, spec, name="p3"):
        """
        Change a profile's spec, as an admin would, and return the profile.
        """
        return self.k8s.merge_patch(f"{PROFILES}/{name}", {"spec": spec})

    def status_writes(self):
        return sum(1 for path in self.k8s.patched if path.endswith("/status"))

    def current_pods(self, name="p3", components=("vscode", "squid", "coredns")):
        """
        Ready pods carrying the hashes the current templates carry.
        """
        pods = []

        for component in components:
            if component == "vscode":
                workload = self.k8s.get(f"{STATEFULSETS}/{name}-vscode")
            else:
                workload = self.k8s.get(f"{DEPLOYMENTS}/{name}-{component}")

            pod = ready_pod(name, component)
            pod["metadata"]["annotations"] = dict(workload["spec"]["template"]["metadata"].get("annotations") or {})
            pods.append(pod)

        return pods

    def test_generate_ca(self):
        cert, key = generate_ca("x")

        self.assertIn("BEGIN CERTIFICATE", cert)
        self.assertIn("PRIVATE KEY", key)
        self.assertEqual(allowlist_hash("a"), allowlist_hash("a"))
        self.assertNotEqual(allowlist_hash("a"), allowlist_hash("b"))

    def test_reconcile_creates_everything_and_is_idempotent(self):
        status = self.operator.reconcile(self.created)

        ca = self.k8s.get(f"{SECRETS}/p3-proxy-ca")
        self.assertIn("ca.key", ca["data"])
        self.assertTrue(self.k8s.get(f"{SECRETS}/p3-vscode-token")["data"]["token"])
        self.assertEqual(self.k8s.get("/api/v1/namespaces/test/services/p3-dns")["spec"]["clusterIP"], "10.96.0.10")

        pod = pod_spec(self.k8s.get(STS))
        self.assertEqual(pod["dnsConfig"]["nameservers"], ["10.96.0.10"])
        self.assertEqual(pod["containers"][0]["image"], "img:1")
        self.assertIn(
            "BEGIN CERTIFICATE",
            self.k8s.get("/api/v1/namespaces/test/configmaps/p3-proxy-ca-cert")["data"]["ca.pem"],
        )

        self.assertEqual((status["phase"], status["dnsClusterIP"]), ("Pending", "10.96.0.10"))
        self.assertEqual(self.current()["status"]["phase"], "Pending")
        self.assertTrue(self.current()["status"]["lastReconciled"])

        # Once the pods are up, the profile is Ready, and nothing was regenerated.
        ca_before = ca["data"]["ca.pem"]
        self.k8s.pods = self.current_pods()
        status = self.operator.reconcile(self.current())
        self.assertEqual(status["phase"], "Ready")
        self.assertEqual(
            self.k8s.get(f"{SECRETS}/p3-proxy-ca")["data"]["ca.pem"],
            ca_before,
            "the CA is generated once",
        )

        self.update({"suspended": True})
        self.assertEqual(self.operator.reconcile(self.current())["phase"], "Suspended")
        self.assertEqual(self.k8s.get(STS)["spec"]["replicas"], 0)

    def test_status_is_written_only_when_it_changes(self):
        self.operator.reconcile(self.created)
        self.operator.reconcile(self.current())
        self.operator.reconcile(self.current())
        self.assertEqual(self.status_writes(), 1, "an unchanged profile settles instead of re-triggering itself")

        self.k8s.pods = self.current_pods()
        self.operator.reconcile(self.current())
        self.assertEqual(self.status_writes(), 2, "a real change is written")

    def test_status_only_events_are_not_reconciled(self):
        self.assertFalse(_status_only({"metadata": {"generation": 2}, "status": {"observedGeneration": 1}}))
        self.assertTrue(_status_only({"metadata": {"generation": 2}, "status": {"observedGeneration": 2}}))
        self.assertFalse(_status_only({"metadata": {}}))

        def event(kind, generation):
            self.operator._on_event(
                kind,
                {"metadata": {"name": "p3", "generation": generation}, "status": {"observedGeneration": 2}},
            )

        event("MODIFIED", 2)
        self.assertEqual(self.operator._timers, {})

        event("MODIFIED", 3)
        self.operator._timers.pop("p3").cancel()

        event("ADDED", 2)
        self.operator._timers.pop("p3").cancel()

    def test_clearing_the_host_removes_the_ingress(self):
        created = self.k8s.create(PROFILES, profile("p5", {"vscodeHost": "p5.example.com"}))
        self.operator.reconcile(created)
        ingress = f"{INGRESSES}/p5-vscode"
        self.assertIsNotNone(self.k8s.get(ingress))

        self.operator.reconcile(self.update({"vscodeHost": None}, name="p5"))
        self.assertIsNone(self.k8s.get(ingress), "no host, no route")

        def policies():
            return [path for path in self.k8s.store if "/networkpolicies/p5-" in path]

        self.assertEqual(len(policies()), 3)
        self.operator.reconcile(self.update({"networkPolicy": {"enabled": False}}, name="p5"))
        self.assertEqual(policies(), [])

        # Somebody else's Ingress that carries our label is left alone.
        self.k8s.create(
            INGRESSES,
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": {"name": "theirs", "namespace": "test", "labels": {"hardcode-app.github.io/profile": "p5"}},
                "spec": {},
            },
        )
        self.operator.reconcile(self.current("p5"))
        self.assertIsNotNone(self.k8s.get(f"{INGRESSES}/theirs"))

    def test_volumes_grow_but_never_shrink(self):
        self.operator.reconcile(self.created)
        pvc = "/api/v1/namespaces/test/persistentvolumeclaims/home-p3-vscode-0"
        self.k8s.create(
            "/api/v1/namespaces/test/persistentvolumeclaims",
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "home-p3-vscode-0", "namespace": "test"},
                "spec": {"resources": {"requests": {"storage": "20Gi"}}},
            },
        )

        def requested():
            return self.k8s.get(pvc)["spec"]["resources"]["requests"]["storage"]

        status = self.operator.reconcile(self.update({"home": {"size": "50Gi"}}))
        self.assertEqual(requested(), "50Gi")
        self.assertNotIn("stays", status["message"])

        template = self.k8s.get(STS)["spec"]["volumeClaimTemplates"][0]
        self.assertEqual(
            template["spec"]["resources"]["requests"]["storage"],
            "20Gi",
            "the immutable template is left as it was",
        )

        status = self.operator.reconcile(self.update({"home": {"size": "10Gi"}}))
        self.assertEqual(requested(), "50Gi")
        self.assertIn("can't shrink", status["message"])

    def test_new_languages_and_extensions_reach_the_running_pod_through_its_config_map(self):
        self.operator.reconcile(self.update({"languages": ["python"]}))
        extension_list = "/api/v1/namespaces/test/configmaps/p3-vscode-extensions"
        template = self.k8s.get(STS)["spec"]["template"]

        self.assertEqual(self.k8s.get(extension_list)["data"]["wanted"], langs.extension_list(["python"], []))

        self.operator.reconcile(self.update({"languages": ["python", "go"], "extensions": ["eamodio.gitlens"]}))

        self.assertEqual(
            self.k8s.get(extension_list)["data"]["wanted"],
            langs.extension_list(["python", "go"], ["eamodio.gitlens"]),
        )
        self.assertEqual(
            self.k8s.get(STS)["spec"]["template"],
            template,
            "the pod isn't restarted: it follows its list by itself",
        )

    def test_sidecar_toggle_keeps_the_statefulset_updatable(self):
        self.operator.reconcile(self.created)
        before = self.k8s.get(STS)["spec"]["volumeClaimTemplates"]

        self.operator.reconcile(self.update({"docker": {"enabled": False}}))

        after = self.k8s.get(STS)
        self.assertEqual(after["spec"]["volumeClaimTemplates"], before)
        self.assertEqual(
            [container["name"] for container in pod_spec(after)["containers"]],
            ["vscode"],
            "the privileged sidecar really goes away",
        )

    def test_reconcile_by_name_reports_errors_on_the_object(self):
        self.operator.openssl = "/nonexistent/openssl"

        self.operator.reconcile_by_name("p3")
        self.assertEqual(self.current()["status"]["phase"], "Error")

        self.operator.reconcile_by_name("missing")  # no object: nothing to do, and no error

    def test_a_persistent_error_settles(self):
        self.operator.openssl = "/nonexistent/openssl"
        for _ in range(3):
            self.operator.reconcile_by_name("p3")

        self.assertEqual(self.status_writes(), 1, "the same error isn't written again and again")

        current = self.current()
        self.assertEqual(current["status"]["observedGeneration"], current["metadata"]["generation"])
        self.assertTrue(_status_only(current), "our own error write is recognised as ours, not reconciled again")
        self.operator._on_event("MODIFIED", current)
        self.assertEqual(self.operator._timers, {})

        self.operator.openssl = "openssl"
        self.operator.reconcile_by_name("p3")
        self.assertEqual(self.current()["status"]["phase"], "Pending", "and a later success replaces it")

    def test_pending_message_names_what_is_waiting(self):
        self.operator.reconcile(self.created)
        stuck_vscode = {
            "metadata": {
                "name": "p3-vscode-0",
                "labels": {"hardcode-app.github.io/profile": "p3", "hardcode-app.github.io/component": "vscode"},
            },
            "status": {"conditions": [], "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}]},
        }
        self.k8s.pods = self.current_pods(components=("squid",)) + [stuck_vscode]

        status = self.operator.reconcile(self.current())

        self.assertEqual(status["message"], "waiting for vscode, coredns (ImagePullBackOff)")

    def test_the_proxy_is_given_the_provided_entries_and_the_profiles_own(self):
        self.update({"languages": ["rust"], "allowlist": "mine.example rw\noff crates.io\n"})
        self.operator.reconcile(self.current())

        given = self.k8s.get(f"{CONFIGMAPS}/p3-allowlist")["data"]["allowlist.txt"]
        self.assertIn("\nupdate.code.visualstudio.com\n", given, "every profile's")
        self.assertIn("\nstatic.crates.io\n", given, "the language's")
        self.assertIn("\n#crates.io ", given, "switched off")
        self.assertIn("\nmine.example rw\n", given, "the profile's own")
        self.assertNotIn("\noff ", given)

    def test_an_update_of_the_provided_entries_rolls_the_proxy_and_is_logged(self):
        self.update({"languages": ["rust"]})
        self.operator.reconcile(self.current())
        before = self.k8s.get(f"{DEPLOYMENTS}/p3-squid")["spec"]["template"]["metadata"]["annotations"]

        rust = langs.BY_KEY["rust"]
        newer = dataclasses.replace(rust, domains=(*rust.domains, "newrustsite.com"))
        catalogue = tuple(newer if lang.key == "rust" else lang for lang in langs.CATALOGUE)

        with mock.patch.object(langs, "CATALOGUE", catalogue), self.assertLogs("hardcode.operator") as logged:
            self.operator.reconcile(self.current())

        after = self.k8s.get(f"{DEPLOYMENTS}/p3-squid")["spec"]["template"]["metadata"]["annotations"]
        self.assertNotEqual(before, after, "a new hash rolls the proxy")
        self.assertIn("profile p3: allowlist Rust: + newrustsite.com", "\n".join(logged.output))

    def test_pods_on_the_previous_policy_do_not_make_a_profile_ready(self):
        self.operator.reconcile(self.created)
        self.k8s.pods = self.current_pods()
        self.assertEqual(self.operator.reconcile(self.current())["phase"], "Ready")

        # Switch a provided entry off: until the proxy and DNS pods are
        # replaced, the old policy is the one running.
        status = self.operator.reconcile(self.update({"allowlist": "off update.code.visualstudio.com\n"}))
        self.assertEqual(status["phase"], "Pending", "the old proxy and DNS pods are still the ones running")
        self.assertIn("squid, coredns still on the previous policy", status["message"])
        self.assertEqual(
            status["observedGeneration"],
            self.current()["metadata"]["generation"],
            "the spec was reconciled; the policy isn't running yet",
        )

        self.k8s.pods = self.current_pods()
        self.assertEqual(self.operator.reconcile(self.current())["phase"], "Ready")

        self.k8s.pods[0]["metadata"]["deletionTimestamp"] = "2026-10-08T00:00:00Z"
        self.assertEqual(
            self.operator.reconcile(self.current())["phase"],
            "Pending",
            "a pod on its way out doesn't count",
        )

    def test_a_refused_ingress_does_not_stop_isolation(self):
        self.k8s.create(PROFILES, profile("p6", {"vscodeHost": "p6.example.com"}))
        self.k8s.refuse["Ingress"] = 422

        self.operator.reconcile_by_name("p6")

        policies = [path for path in self.k8s.store if "/networkpolicies/p6-" in path]
        self.assertEqual(len(policies), 3, "the policies were applied before anything else")
        self.assertIsNotNone(self.k8s.get(f"{STATEFULSETS}/p6-vscode"))

        status = self.current("p6")["status"]
        self.assertEqual(status["phase"], "Error")
        self.assertIn("Ingress not applied", status["message"])

        del self.k8s.refuse["Ingress"]
        self.operator.reconcile_by_name("p6")
        self.assertEqual(self.current("p6")["status"]["phase"], "Pending")

    def test_fixed_token_is_honoured_and_rolls_the_vscode(self):
        created = self.k8s.create(PROFILES, profile("p4", {"vscodeToken": "fixed-token"}))
        self.operator.reconcile(created)

        def token():
            secret = self.k8s.get(f"{SECRETS}/p4-vscode-token")
            return base64.b64decode(secret["data"]["token"]).decode()

        def token_hash():
            statefulset = self.k8s.get(f"{STATEFULSETS}/p4-vscode")
            return statefulset["spec"]["template"]["metadata"]["annotations"]["hardcode-app.github.io/token-hash"]

        self.assertEqual(token(), "fixed-token")
        hash_before = token_hash()

        self.operator.reconcile(self.update({"vscodeToken": "rotated"}, name="p4"))
        self.assertEqual(token(), "rotated")
        self.assertNotEqual(
            token_hash(),
            hash_before,
            "the pod template changed, so the server restarts with the new token",
        )

        self.operator.reconcile(self.update({"vscodeToken": ""}, name="p4"))
        self.assertEqual(token(), "rotated", "clearing the fixed token keeps the current one")

    def test_reconcile_while_in_flight_requeues_without_blocking(self):
        self.operator._inflight.add("p3")

        worker = threading.Thread(target=self.operator.reconcile_by_name, args=("p3",))
        worker.start()
        worker.join(2)

        self.assertFalse(worker.is_alive(), "must not wait on the lock it needs to requeue")
        timer = self.operator._timers.pop("p3", None)
        self.assertIsNotNone(timer, "the profile is looked at again later")
        timer.cancel()
        self.assertIn("p3", self.operator._inflight, "the other reconcile is left alone")


@unittest.skipUnless(shutil.which("bash") and os.name != "nt", "needs bash")
class SeedHomeTests(unittest.TestCase):
    """
    The VS Code container's seed-home init script, run for real on temporary directories.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "image-home"
        self.volume = Path(self.tmp.name) / "pvc"

        (self.home / ".local" / "bin").mkdir(parents=True)
        (self.home / ".local" / "bin" / "claude").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.home / ".bashrc").write_text("# rc\n", encoding="utf-8")
        (self.home / "shared").symlink_to("/shared")

        # A fresh ext4 volume isn't empty.
        self.volume.mkdir()
        (self.volume / "lost+found").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self):
        command = ["bash", "-c", manifests.SEED_HOME, "seed-home", str(self.home), str(self.volume)]
        return subprocess.run(command, capture_output=True, text=True)

    def test_fresh_volume_is_seeded_once(self):
        result = self.seed()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.volume / ".local" / "bin" / "claude").exists())
        self.assertEqual((self.volume / ".bashrc").read_text(encoding="utf-8"), "# rc\n")
        self.assertEqual(os.readlink(self.volume / "shared"), "/shared")
        self.assertTrue((self.volume / ".hardcode-seeded").exists())

        (self.volume / ".bashrc").write_text("# mine\n", encoding="utf-8")
        self.assertEqual(self.seed().returncode, 0)
        self.assertEqual(
            (self.volume / ".bashrc").read_text(encoding="utf-8"), "# mine\n", "an established home is left alone"
        )

    def test_an_interrupted_seeding_is_redone(self):
        # Half a copy from last time, that never got as far as the marker.
        (self.volume / ".vscode-server").mkdir()

        self.assertEqual(self.seed().returncode, 0)

        self.assertTrue((self.volume / ".local" / "bin" / "claude").exists())
        self.assertTrue((self.volume / ".hardcode-seeded").exists())
        self.assertEqual(os.readlink(self.volume / "shared"), "/shared")


if __name__ == "__main__":
    unittest.main()
