"""
An in-memory stand-in for the Kubernetes API, and other fixtures for the
admin's tests.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from hardcode_admin.auth import StoreConflict
from hardcode_admin.k8s import ApiError

VALUES = Path(__file__).resolve().parent.parent.parent / "helm" / "hardcode" / "values.yaml"


def chart_defaults(overrides: dict | None = None) -> dict:
    """
    The chart's profileDefaults, as the operator is given them, with
    *overrides* merged in: a dict into a dict, key by key, anything else
    replaced.
    """

    def merged(base: dict, changes: dict) -> dict:
        result = copy.deepcopy(base)
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merged(result[key], value)
            else:
                result[key] = copy.deepcopy(value)

        return result

    values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))

    return merged(values["profileDefaults"], overrides or {})


class FakeK8s:
    """
    The Kubernetes API, in memory, as far as the operator and the admin use it.

    Objects are kept by path, with resource versions: a merge patch naming a
    stale version is refused with 409, as the real API does. It also has
    label-selected lists, allocated ClusterIPs, the status subresource,
    owner-reference garbage collection on delete, and containers' logs
    (``logs_by_pod``, which tests set as they like).

    ``before_patch(path, patch)`` is a hook for concurrency tests: it runs
    just before a merge patch is checked. ``refuse`` maps a kind to the
    status its applies fail with.
    """

    def __init__(self, namespace: str = "test"):
        self.namespace = namespace
        self.store: dict[str, dict] = {}
        self.pods: list[dict] = []
        self.logs_by_pod: dict[str, str] = {}
        self.log_reads: list[dict] = []
        self.applied: list[str] = []
        self.deleted: list[str] = []
        self.patched: list[str] = []
        self.before_patch = None
        self.refuse: dict[str, int] = {}
        self._ips = 10
        self._uids = 0
        self._versions = 0

    def _stamp(self, obj: dict) -> None:
        """
        Give an object a new resource version, as every write does.
        """
        self._versions += 1
        obj["metadata"]["resourceVersion"] = str(self._versions)

    def _uid(self) -> str:
        self._uids += 1
        return f"uid-{self._uids}"

    def get(self, path: str):
        return copy.deepcopy(self.store.get(path))

    def list(self, path: str, query: dict | None = None) -> list[dict]:
        """
        The objects directly under *path*, filtered by a one-label selector.
        Pods come from ``self.pods``, which tests set as they like.
        """
        key, _, value = (query or {}).get("labelSelector", "").partition("=")

        def matches(obj: dict) -> bool:
            return not key or obj["metadata"].get("labels", {}).get(key) == value

        if path.endswith("/pods"):
            return [copy.deepcopy(pod) for pod in self.pods if matches(pod)]

        prefix = path + "/"
        found = []
        for stored_path, obj in self.store.items():
            directly_under = stored_path.startswith(prefix) and "/" not in stored_path[len(prefix) :]
            if directly_under and matches(obj):
                found.append(copy.deepcopy(obj))

        return found

    def apply(self, path: str, obj: dict, field_manager: str = "hardcode") -> dict:
        """
        Server-side apply, simplified: the object replaces what was there,
        keeping its uid and, for a Service, its allocated ClusterIP.
        """
        kind, name = obj.get("kind"), obj["metadata"]["name"]
        if kind in self.refuse:
            raise ApiError(self.refuse[kind], f'{kind} "{name}" is invalid')

        self.applied.append(path)
        existing = self.store.get(path)
        stored = copy.deepcopy(obj)

        if kind == "Service" and not stored["spec"].get("clusterIP"):
            if existing:
                stored["spec"]["clusterIP"] = existing["spec"]["clusterIP"]
            else:
                stored["spec"]["clusterIP"] = f"10.96.0.{self._ips}"
                self._ips += 1

        stored["metadata"]["uid"] = existing["metadata"].get("uid") if existing else self._uid()
        self._stamp(stored)
        self.store[path] = stored

        return copy.deepcopy(stored)

    def create(self, path: str, obj: dict) -> dict:
        stored = copy.deepcopy(obj)
        key = f"{path}/{stored['metadata']['name']}"
        if key in self.store:
            raise ApiError(409, f"{stored['metadata']['name']} already exists")

        stored["metadata"].update(
            uid=self._uid(),
            creationTimestamp=datetime.now(timezone.utc).isoformat(),
            generation=1,
        )
        self._stamp(stored)
        self.store[key] = stored

        return copy.deepcopy(stored)

    def merge_patch(self, path: str, patch: dict) -> dict:
        """
        A JSON merge patch (``None`` removes a key), conditional on the
        resource version when the patch names one. A spec change bumps the
        generation, as it does on a real server.
        """
        self.patched.append(path)
        if self.before_patch:
            self.before_patch(path, patch)

        key = path[: -len("/status")] if path.endswith("/status") else path
        obj = self.store.get(key)
        if obj is None:
            raise ApiError(404, "not found")

        patch = copy.deepcopy(patch)
        expected = (patch.get("metadata") or {}).pop("resourceVersion", None)
        if expected is not None and expected != obj["metadata"].get("resourceVersion"):
            raise ApiError(
                409,
                "Operation cannot be fulfilled: the object has been modified; "
                "please apply your changes to the latest version and try again",
            )

        def merge(target: dict, changes: dict) -> None:
            for name, value in changes.items():
                if value is None:
                    target.pop(name, None)
                elif isinstance(value, dict) and isinstance(target.get(name), dict):
                    merge(target[name], value)
                else:
                    target[name] = copy.deepcopy(value)

        merge(obj, patch)

        if not path.endswith("/status") and "spec" in patch:
            obj["metadata"]["generation"] = obj["metadata"].get("generation", 1) + 1
        self._stamp(obj)

        return copy.deepcopy(obj)

    def delete(self, path: str, propagation: str = "Foreground"):
        """
        Delete an object, and everything a deleted Profile owned.
        """
        obj = self.store.pop(path, None)
        if obj is None:
            return None

        self.deleted.append(path)
        name = obj["metadata"]["name"]

        for key in list(self.store):
            references = self.store[key]["metadata"].get("ownerReferences") or []
            if any(reference.get("kind") == "Profile" and reference.get("name") == name for reference in references):
                del self.store[key]

        return {}

    def logs(
        self,
        pod: str,
        container: str,
        since_seconds: int | None = None,
        tail_lines: int | None = None,
    ) -> str:
        """
        A container's log from ``logs_by_pod`` (``"<pod>/<container>"``),
        only its last *tail_lines* lines when asked, as the kubelet does.
        Every read is recorded in ``log_reads``.
        """
        self.log_reads.append({"pod": pod, "container": container, "since": since_seconds, "tail": tail_lines})
        text = self.logs_by_pod.get(f"{pod}/{container}", "")

        if tail_lines is None:
            return text

        return "".join(text.splitlines(keepends=True)[-tail_lines:])

    def watch(self, *args, **kwargs) -> None:
        return None


class MemoryStore:
    """
    The admin accounts in memory, versioned like the Secret: a save naming a
    version other than the current one is a ``StoreConflict``.

    ``conflicts`` makes that many of the next saves fail regardless, and
    ``write_externally`` stands for another writer.
    """

    def __init__(self):
        self._users: list[dict] = []
        self._version = 0
        self.conflicts = 0
        self.saves = 0

    def load(self) -> tuple[list[dict], int]:
        return json.loads(json.dumps(self._users)), self._version

    def save(self, users: list[dict], version: int) -> None:
        self.saves += 1

        if self.conflicts:
            self.conflicts -= 1
            raise StoreConflict("moved")
        if version != self._version:
            raise StoreConflict(f"version {version} is not current ({self._version})")

        self.write_externally(users)

    def write_externally(self, users: list[dict]) -> None:
        self._users = json.loads(json.dumps(users))
        self._version += 1


def profile(name: str, spec: dict | None = None, namespace: str = "test") -> dict:
    """
    A Profile object as the API server returns it, with a small allowlist.
    """
    return {
        "apiVersion": "hardcode-app.github.io/v1alpha1",
        "kind": "Profile",
        "metadata": {"name": name, "namespace": namespace, "uid": "u1", "generation": 1},
        "spec": {"allowlist": "pypi.org\n", **(spec or {})},
    }


def ready_pod(profile_name: str, component: str) -> dict:
    """
    A Ready pod of one of a profile's components.
    """
    return {
        "metadata": {
            "name": f"{profile_name}-{component}-x",
            "labels": {"hardcode-app.github.io/profile": profile_name, "hardcode-app.github.io/component": component},
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
