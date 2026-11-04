"""
The operator: watches Profile objects and keeps their Kubernetes resources in
step with them, by server-side apply.

Deleting a Profile deletes everything it owns, its volumes included, through
owner references and the StatefulSet's retention policy.

A change of a profile's allowlist restarts its proxy and DNS server, so
changes are gathered first, as the command gathers them on one computer:
the profile is reconciled once its allowlist has gone ``QUIET_SECONDS``
without a change, and a change made while the last one's restart is under
way waits for that restart to end, then as long again.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from hardcode_cli import allowlist

from . import manifests
from .k8s import ApiError, K8s

log = logging.getLogger("hardcode.operator")

# The component whose pods carry each hash: a pod is current only when its
# template's hash is this reconcile's.
HASH_ANNOTATIONS = {
    "squid": manifests.ANNOTATION_ALLOWLIST_HASH,
    "coredns": manifests.ANNOTATION_ALLOWLIST_HASH,
    "vscode": manifests.ANNOTATION_TOKEN_HASH,
}

# How long a profile's allowlist must go without a change before its proxy
# and DNS server are restarted with it; how often a restart under way is
# looked at again, and how long it is waited for at most (one that takes
# longer is stuck, and the change may be what puts it right).
QUIET_SECONDS = 5.0
ROLLOUT_POLL_SECONDS = 2.0
ROLLOUT_WAIT_SECONDS = 120.0

# The components an allowlist change restarts.
POLICY_COMPONENTS = ("squid", "coredns")


def profiles_path(namespace: str) -> str:
    """
    The REST path of the Profiles in a namespace.
    """
    return f"/apis/{manifests.GROUP}/{manifests.VERSION}/namespaces/{namespace}/profiles"


def allowlist_hash(text: str) -> str:
    """
    A short fingerprint of a text, for pod template annotations.
    """
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def generate_ca(name: str, openssl: str = "openssl") -> tuple[str, str]:
    """
    A new proxy CA for one profile: the certificate and the key.

    The parameters are the same as in images/squid/make-ca.sh.
    """
    with tempfile.TemporaryDirectory(prefix="hc-ca-") as directory:
        cert = Path(directory) / "ca.pem"
        key = Path(directory) / "ca.key"

        subprocess.run(
            [
                openssl, "req", "-x509",
                "-newkey", "rsa:3072", "-sha256", "-days", "3650", "-nodes",
                "-subj", f"/CN=HardCode proxy CA ({name})",
                "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                "-keyout", str(key),
                "-out", str(cert),
            ],
            check=True,
            capture_output=True,
        )  # fmt: skip

        return cert.read_text(encoding="utf-8"), key.read_text(encoding="utf-8")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _unb64(text: str) -> str:
    return base64.b64decode(text).decode()


def _status_only(obj: dict) -> bool:
    """
    Whether a watch event is for a change that didn't touch the spec,
    typically our own status write: the generation is the one already observed.
    """
    generation = obj.get("metadata", {}).get("generation")
    observed = (obj.get("status") or {}).get("observedGeneration")

    return generation is not None and generation == observed


def _without_timestamp(status: dict) -> dict:
    return {key: value for key, value in status.items() if key != "lastReconciled"}


@dataclass
class _Waiting:
    """
    A profile's allowlist change that waits to be given to its proxy:
    the allowlist's hash, when it last changed (or the restart it waited
    for ended), and since when a restart under way has been waited for.
    """

    allowlist_hash: str
    since: float
    rollout_since: Optional[float] = None


class Operator:
    """
    Reconciles every Profile in the client's namespace.
    """

    def __init__(
        self,
        k8s: K8s,
        defaults: Optional[dict] = None,
        openssl: str = "openssl",
        clock: Callable[[], float] = time.monotonic,
    ):
        self.k8s = k8s
        self.defaults = defaults or {}
        self.openssl = openssl
        self.clock = clock
        self.stop = threading.Event()

        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._waiting: dict[str, _Waiting] = {}

    # -- lifecycle -------------------------------------------------------------

    def start(self, resync_seconds: float = 60) -> None:
        """
        Watch the Profiles in a background thread, and reconcile all of them
        every *resync_seconds*. The resync is also how pods becoming ready
        reach a profile's status.
        """
        namespace = self.k8s.namespace
        log.info("watching profiles in %s", namespace)

        watch = threading.Thread(
            target=self.k8s.watch,
            args=(profiles_path(namespace), self._on_event, self.stop),
            daemon=True,
            name="profile-watch",
        )
        resync = threading.Thread(
            target=self._resync_loop,
            args=(resync_seconds,),
            daemon=True,
            name="profile-resync",
        )
        watch.start()
        resync.start()

    def _resync_loop(self, interval: float) -> None:
        while not self.stop.wait(interval):
            try:
                self.resync_all()
            except Exception as error:  # noqa: BLE001 - one failed pass mustn't end the loop
                log.error("resync: %s", error)

    def resync_all(self) -> None:
        """
        Queue every Profile for a reconcile.
        """
        for profile in self.k8s.list(profiles_path(self.k8s.namespace)):
            self.enqueue(profile["metadata"]["name"])

    def _on_event(self, kind: str, obj: dict) -> None:
        name = obj.get("metadata", {}).get("name")
        if not name:
            return

        if kind == "DELETED":
            log.info("profile %s: deleted (garbage collection removes its resources)", name)
            return

        if kind == "MODIFIED" and _status_only(obj):
            return  # our own status write echoing back

        self.enqueue(name)

    def enqueue(self, name: str, delay: float = 0.3) -> None:
        """
        Reconcile *name* shortly. Events arriving within *delay* of each other
        coalesce into one reconcile.
        """
        with self._lock:
            timer = self._timers.pop(name, None)
            if timer:
                timer.cancel()

            timer = threading.Timer(delay, self.reconcile_by_name, args=(name,))
            timer.daemon = True
            self._timers[name] = timer
            timer.start()

    def reconcile_by_name(self, name: str) -> None:
        """
        Reconcile one Profile, one thread at a time, and record a failure in
        its status.
        """
        with self._lock:
            busy = name in self._inflight
            if not busy:
                self._inflight.add(name)

        if busy:
            # Another thread is on it. Look again once that one is done (from
            # outside the lock, since enqueue takes it too).
            self.enqueue(name, 2)
            return

        profile = None
        try:
            profile = self.k8s.get(f"{profiles_path(self.k8s.namespace)}/{name}")
            if profile and not profile["metadata"].get("deletionTimestamp"):
                hold = self.allowlist_hold(profile)
                if hold:
                    self.enqueue(name, hold)
                else:
                    self.reconcile(profile)

        except Exception as error:  # noqa: BLE001 - the failure is reported on the object
            log.error("profile %s: %s", name, error)
            self._report_failure(name, profile, error)

        finally:
            with self._lock:
                self._inflight.discard(name)

    def _report_failure(self, name: str, profile: Optional[dict], error: Exception) -> None:
        """
        Put a failed reconcile into the profile's status.

        The status carries the observed generation and is only written when
        it changed, so the error doesn't become an event that triggers itself
        again; the resync retries it.
        """
        previous = (profile or {}).get("status") or {}
        status = {
            **_without_timestamp(previous),
            "phase": "Error",
            "message": str(error),
            "observedGeneration": (profile or {}).get("metadata", {}).get("generation"),
        }

        try:
            self.set_status(name, status, previous=previous)
        except Exception:  # noqa: BLE001 - nothing more to do; the next resync tries again
            pass

    # -- gathering allowlist changes ---------------------------------------------

    def allowlist_hold(self, profile: dict) -> float:
        """
        How many seconds to hold off reconciling *profile*, so that its proxy
        and DNS server are restarted once for a burst of allowlist changes;
        0 to go ahead. Its status says what it waits for meanwhile.

        It waits until the allowlist the proxy is to be given has gone
        ``QUIET_SECONDS`` without a change, and while the restart for the
        last change is under way, then as long again; not for a new profile,
        whose proxy has nothing yet. The whole reconcile waits, the rest of
        an edit made with the allowlist included: the policy and the pods
        it is applied with stay one.
        """
        name = profile["metadata"]["name"]
        namespace = profile["metadata"]["namespace"]
        names = manifests.Names(name)

        wanted = manifests.effective_spec(profile, self.defaults)["proxyAllowlist"]
        current = self.k8s.get(f"/api/v1/namespaces/{namespace}/configmaps/{names.allowlist}")
        given = ((current or {}).get("data") or {}).get("allowlist.txt")

        if given is None or given == wanted:
            self._waiting.pop(name, None)
            return 0

        # The wait starts again whenever the allowlist changes.
        now = self.clock()
        wanted_hash = allowlist_hash(wanted)
        waiting = self._waiting.get(name)
        if waiting is None or waiting.allowlist_hash != wanted_hash:
            waiting = _Waiting(wanted_hash, now)
            self._waiting[name] = waiting

        # A restart for the last change, still under way: the wait starts
        # again once it is over, or has been waited for long enough.
        if self._restarting(profile, allowlist_hash(given)):
            if waiting.rollout_since is None:
                waiting.rollout_since = now

            if now - waiting.rollout_since < ROLLOUT_WAIT_SECONDS:
                waiting.since = now
                self._say_waiting(profile, "allowlist changes wait for the proxy's restart to finish")
                return ROLLOUT_POLL_SECONDS
        else:
            waiting.rollout_since = None

        remaining = QUIET_SECONDS - (now - waiting.since)
        if remaining > 0:
            self._say_waiting(profile, "allowlist changes apply a few seconds after the last one")
            return remaining

        return 0

    def _restarting(self, profile: dict, given_hash: str) -> bool:
        """
        Whether the profile's proxy or DNS server is being restarted with
        the policy it was last given (*given_hash*): one of their pods is on
        another policy, on its way out, or not ready yet.
        """
        namespace = profile["metadata"]["namespace"]
        selector = f"{manifests.LABEL_PROFILE}={profile['metadata']['name']}"
        pods = self.k8s.list(f"/api/v1/namespaces/{namespace}/pods", {"labelSelector": selector})

        for pod in pods:
            labels = pod["metadata"].get("labels") or {}
            if labels.get(manifests.LABEL_COMPONENT) not in POLICY_COMPONENTS:
                continue

            annotations = pod["metadata"].get("annotations") or {}
            ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
            if annotations.get(manifests.ANNOTATION_ALLOWLIST_HASH) != given_hash:
                return True
            if pod["metadata"].get("deletionTimestamp") or not ready:
                return True

        return False

    def _say_waiting(self, profile: dict, message: str) -> None:
        """
        Put what a held profile waits for in its status: Pending, as its
        policy isn't what its spec says yet. The generation observed stays
        the last one reconciled.
        """
        previous = profile.get("status") or {}
        status = {**_without_timestamp(previous), "phase": "Pending", "message": message}
        self.set_status(profile["metadata"]["name"], status, previous=previous)

    # -- reconcile -------------------------------------------------------------

    def ensure_secret(self, profile: dict, name: str, make_data: Callable[[], dict]) -> dict:
        """
        The profile's Secret called *name*, created from ``make_data()`` when
        it doesn't exist. An existing Secret is never regenerated.
        """
        namespace = profile["metadata"]["namespace"]

        existing = self.k8s.get(f"/api/v1/namespaces/{namespace}/secrets/{name}")
        if existing:
            return existing

        data = make_data()
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": manifests.labels(profile, "secret"),
                "ownerReferences": [manifests.owner_reference(profile)],
            },
            "data": {key: _b64(value) for key, value in data.items()},
        }

        return self.k8s.create(f"/api/v1/namespaces/{namespace}/secrets", secret)

    def _proxy_ca(self, profile: dict, names: manifests.Names) -> str:
        """
        The profile's proxy CA certificate, made the first time.
        """

        def new_ca() -> dict:
            cert, key = generate_ca(profile["metadata"]["name"], self.openssl)
            return {"ca.pem": cert, "ca.key": key}

        secret = self.ensure_secret(profile, names.proxy_ca, new_ca)
        return _unb64(secret["data"]["ca.pem"])

    def _vscode_token(self, profile: dict, names: manifests.Names, spec: dict) -> str:
        """
        The VS Code connection token: the one the spec fixes, or a random one
        made the first time.
        """
        fixed = spec["vscodeToken"]

        secret = self.ensure_secret(profile, names.token, lambda: {"token": fixed or secrets.token_urlsafe(24)})
        token = _unb64(secret["data"].get("token", ""))

        # A token set in the spec after the Secret was made replaces it.
        if fixed and token != fixed:
            path = f"/api/v1/namespaces/{profile['metadata']['namespace']}/secrets/{names.token}"
            self.k8s.merge_patch(path, {"data": {"token": _b64(fixed)}})
            token = fixed

        return token

    def reconcile(self, profile: dict) -> dict:
        """
        Bring one profile's resources in line with its spec, and return the
        status written for it.
        """
        name = profile["metadata"]["name"]
        namespace = profile["metadata"]["namespace"]
        names = manifests.Names(name)
        spec = manifests.effective_spec(profile, self.defaults)
        notes: list[str] = []

        ca_cert = self._proxy_ca(profile, names)
        token = self._vscode_token(profile, names, spec)

        # The DNS Service's address goes into the VS Code container's dnsConfig, so it is
        # looked up first. An existing StatefulSet's claim templates are kept
        # as they are, since they can't change.
        dns_path = f"/api/v1/namespaces/{namespace}/services/{names.dns}"
        dns_service = self.k8s.get(dns_path)
        statefulset = self.k8s.get(f"/apis/apps/v1/namespaces/{namespace}/statefulsets/{names.vscode}")

        self._log_allowlist_changes(name, namespace, names, spec["proxyAllowlist"])

        ctx = {
            "dnsClusterIP": dns_service["spec"].get("clusterIP") if dns_service else None,
            "caCert": ca_cert,
            "allowlistHash": allowlist_hash(spec["proxyAllowlist"]),
            "tokenHash": allowlist_hash(token),
            "existingClaims": (statefulset or {}).get("spec", {}).get("volumeClaimTemplates"),
        }
        objects = manifests.render(profile, self.defaults, ctx)

        # The first time, create the DNS Service on its own to learn the
        # address Kubernetes gives it, then render again with that address.
        if not ctx["dnsClusterIP"]:
            service = next(obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"] == names.dns)
            self.k8s.apply(dns_path, service)
            ctx["dnsClusterIP"] = self.k8s.get(dns_path)["spec"]["clusterIP"]
            objects = manifests.render(profile, self.defaults, ctx)

        for obj in objects:
            if obj["kind"] != "Ingress":
                self.k8s.apply(manifests.path_for(obj), obj)

        # The Ingress last, and on its own: the API may refuse it for reasons
        # of its own (a host name it doesn't like), and that mustn't stop the
        # policies and pods above from being applied.
        for obj in objects:
            if obj["kind"] == "Ingress":
                try:
                    self.k8s.apply(manifests.path_for(obj), obj)
                except ApiError as error:
                    notes.append(f"Ingress not applied: {error}")

        self.prune(profile, objects)
        notes += self.resize_claims(profile, names, spec)

        status = self.observe(profile, names, ctx, notes)
        self.set_status(name, status, previous=profile.get("status"))

        return status

    def _log_allowlist_changes(self, name: str, namespace: str, names: manifests.Names, proxy_allowlist: str) -> None:
        """
        Log what the allowlist about to be applied changes, one line per
        entry: an edit, or, after an update of HardCode, the entries it
        provides that are new or gone. Nothing the first time.
        """
        current = self.k8s.get(f"/api/v1/namespaces/{namespace}/configmaps/{names.allowlist}")
        before = ((current or {}).get("data") or {}).get("allowlist.txt")
        if not before or before == proxy_allowlist:
            return

        for change in allowlist.changes(before, proxy_allowlist):
            log.info("profile %s: allowlist %s", name, change)

    def prune(self, profile: dict, desired: list[dict]) -> None:
        """
        Remove the optional objects this profile owns but no longer wants: an
        Ingress after its host name was cleared, policies after they were
        switched off.
        """
        name = profile["metadata"]["name"]
        namespace = profile["metadata"]["namespace"]
        uid = profile["metadata"].get("uid")
        wanted = {(obj["kind"], obj["metadata"]["name"]) for obj in desired}

        for kind in manifests.OPTIONAL_KINDS:
            collection = manifests.collection_path(namespace, "networking.k8s.io/v1", kind)
            found = self.k8s.list(collection, {"labelSelector": f"{manifests.LABEL_PROFILE}={name}"})

            for obj in found:
                references = obj["metadata"].get("ownerReferences") or []
                owned = any(reference.get("uid") == uid for reference in references)

                if owned and (kind, obj["metadata"]["name"]) not in wanted:
                    log.info("profile %s: removing %s %s", name, kind, obj["metadata"]["name"])
                    stub = {"apiVersion": "networking.k8s.io/v1", "kind": kind, "metadata": obj["metadata"]}
                    self.k8s.delete(manifests.path_for(stub))

    def resize_claims(self, profile: dict, names: manifests.Names, spec: dict) -> list[str]:
        """
        Grow existing volumes to the sizes the spec asks for, where the
        StorageClass allows expansion. A smaller size is reported, not
        applied: volumes can't shrink.
        """
        namespace = profile["metadata"]["namespace"]
        notes = []
        sizes = (
            ("home", spec["home"]["size"]),
            ("shared", spec["shared"]["size"]),
            ("docker-data", spec["docker"]["size"]),
        )

        for claim, size in sizes:
            path = f"/api/v1/namespaces/{namespace}/persistentvolumeclaims/{names.pvc(claim)}"
            pvc = self.k8s.get(path)
            if not pvc:
                continue

            current = pvc.get("spec", {}).get("resources", {}).get("requests", {}).get("storage", "")
            if not current or manifests.quantity_bytes(current) == manifests.quantity_bytes(size):
                continue

            if manifests.quantity_bytes(size) < manifests.quantity_bytes(current):
                notes.append(f"{claim} volume stays at {current}: volumes can't shrink to {size}")
                continue

            try:
                self.k8s.merge_patch(path, {"spec": {"resources": {"requests": {"storage": size}}}})
                log.info("profile %s: %s volume %s -> %s", profile["metadata"]["name"], claim, current, size)
            except ApiError as error:
                notes.append(f"{claim} volume stays at {current}: {error}")

        return notes

    def observe(self, profile: dict, names: manifests.Names, ctx: dict, notes: Optional[list[str]] = None) -> dict:
        """
        The profile's status, from its pods: Ready, Pending (and on what),
        Suspended, or Error when something it asked for couldn't be applied.
        """
        namespace = profile["metadata"]["namespace"]
        selector = f"{manifests.LABEL_PROFILE}={profile['metadata']['name']}"
        pods = self.k8s.list(f"/api/v1/namespaces/{namespace}/pods", {"labelSelector": selector})

        wanted_hashes = {
            "squid": ctx["allowlistHash"],
            "coredns": ctx["allowlistHash"],
            "vscode": ctx.get("tokenHash", ""),
        }

        def component_of(pod: dict) -> str:
            return pod["metadata"].get("labels", {}).get(manifests.LABEL_COMPONENT, "")

        def pods_of(component: str) -> list[dict]:
            return [pod for pod in pods if component_of(pod) == component]

        def is_ready(pod: dict) -> bool:
            for condition in pod.get("status", {}).get("conditions", []):
                if condition.get("type") == "Ready":
                    return condition.get("status") == "True"
            return False

        def is_current(pod: dict, component: str) -> bool:
            # Running this reconcile's policy (or token), and not on its way out.
            annotations = pod["metadata"].get("annotations") or {}
            on_this_policy = annotations.get(HASH_ANNOTATIONS[component]) == wanted_hashes[component]
            return on_this_policy and not pod["metadata"].get("deletionTimestamp")

        def ready(component: str) -> bool:
            return any(is_ready(pod) and is_current(pod, component) for pod in pods_of(component))

        spec = manifests.effective_spec(profile, self.defaults)

        if spec["suspended"]:
            phase, message = "Suspended", "vscode scaled to 0"

        elif all(ready(component) for component in ("vscode", "squid", "coredns")):
            phase, message = "Ready", ""

        else:
            phase = "Pending"
            waiting = [component for component in ("vscode", "squid", "coredns") if not ready(component)]
            message = f"waiting for {', '.join(waiting)}"

            # Why the VS Code container is waiting, as Kubernetes puts it
            # (ImagePullBackOff and the like).
            vscode = next(iter(pods_of("vscode")), None)
            statuses = (vscode or {}).get("status", {}).get("containerStatuses", [])
            reasons = [
                status["state"]["waiting"]["reason"]
                for status in statuses
                if status.get("state", {}).get("waiting", {}).get("reason")
            ]
            if reasons:
                message += f" ({', '.join(reasons)})"

            # A pod that is ready but on the old policy doesn't count, and says so.
            stale = [
                component
                for component in waiting
                if any(is_ready(pod) and not is_current(pod, component) for pod in pods_of(component))
            ]
            if stale:
                message += f"; {', '.join(stale)} still on the previous policy"

        if notes:
            message = "; ".join(([message] if message else []) + notes)
            if any(note.startswith("Ingress not applied") for note in notes):
                phase = "Error"

        return {
            "observedGeneration": profile["metadata"].get("generation"),
            "phase": phase,
            "message": message,
            "dnsClusterIP": ctx["dnsClusterIP"],
            "proxyURL": f"http://{names.squid}.{namespace}.svc.cluster.local:3128",
            "vscodeService": names.vscode,
            "tokenSecret": names.token,
        }

    def set_status(self, name: str, status: dict, previous: Optional[dict] = None) -> None:
        """
        Write the profile's status, unless nothing but the timestamp would
        change: every write is a watch event, and a reconcile that triggers
        itself never settles.
        """
        if previous is not None and _without_timestamp(previous) == status:
            return

        stamped = {**status, "lastReconciled": datetime.now(timezone.utc).isoformat()}
        self.k8s.merge_patch(f"{profiles_path(self.k8s.namespace)}/{name}/status", {"status": stamped})
