"""
A small Kubernetes API client on the standard library.

It talks to the cluster in-cluster, as the pod's service account, or to a
plain endpoint such as ``kubectl proxy`` for development. Server-side apply
makes the operator's reconcile idempotent.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

# Where the kubelet mounts a pod's service account credentials.
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")

# The most of a container's log one read returns. The sandbox writes what
# squid logs just by making requests; the kubelet rotates logs at 10 MiB by
# default, so this only cuts in on a node that keeps more.
LOG_BYTES = 16 * 1024 * 1024


class ApiError(Exception):
    """
    A request the API server answered with an error status.
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class K8s:
    """
    The Kubernetes API, for one namespace.

    With no arguments, the configuration comes from the environment:
    ``K8S_API`` (with ``K8S_TOKEN``, ``K8S_CA_FILE`` and ``K8S_NAMESPACE``)
    for a plain endpoint, otherwise the in-cluster service account.
    """

    def __init__(
        self,
        base: Optional[str] = None,
        token: Optional[str] = None,
        ca_file: Optional[str] = None,
        namespace: Optional[str] = None,
        env: Optional[dict] = None,
    ):
        environment = os.environ if env is None else env
        self.token_file: Optional[Path] = None

        if base is None and environment.get("K8S_API"):
            base = environment["K8S_API"]
            token = environment.get("K8S_TOKEN", "")
            ca_file = environment.get("K8S_CA_FILE")
            namespace = namespace or environment.get("K8S_NAMESPACE", "default")

        elif base is None:
            host = environment.get("KUBERNETES_SERVICE_HOST")
            port = environment.get("KUBERNETES_SERVICE_PORT", "443")
            base = f"https://{host}:{port}"

            # The token is projected and rotated by the kubelet, so it is read
            # when it is used, not once at start-up.
            self.token_file = SERVICE_ACCOUNT / "token"
            ca_file = str(SERVICE_ACCOUNT / "ca.crt")
            namespace = (
                namespace
                or environment.get("K8S_NAMESPACE")
                or (SERVICE_ACCOUNT / "namespace").read_text(encoding="utf-8").strip()
            )

        self.base = base.rstrip("/")
        self._token = token or ""
        self._token_stamp: Optional[float] = None
        self.namespace = namespace or "default"
        self.context = ssl.create_default_context(cafile=ca_file) if ca_file else None

    @property
    def token(self) -> str:
        """
        The bearer token: the static one given, or the service account's
        file as it is now, read again whenever the kubelet has rewritten it.
        """
        if self.token_file is not None:
            try:
                stamp = self.token_file.stat().st_mtime
                if stamp != self._token_stamp:
                    self._token = self.token_file.read_text(encoding="utf-8").strip()
                    self._token_stamp = stamp
            except OSError:
                pass

        return self._token

    @token.setter
    def token(self, value: str) -> None:
        self._token = value
        self.token_file = None

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        return headers

    # -- transport -------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        content_type: str = "application/json",
        query: Optional[dict] = None,
        raw: bool = False,
        timeout: float = 30,
    ) -> tuple[int, Any]:
        """
        Send a request and return the status and the body: parsed as JSON,
        or the text as it came with *raw* (or when it isn't JSON).
        """
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({key: value for key, value in query.items() if value is not None})

        headers = self._headers()
        data = None
        if body is not None:
            data = (body if isinstance(body, str) else json.dumps(body)).encode()
            headers["Content-Type"] = content_type

        request = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.context) as response:
                text = response.read().decode()
                status = response.status
        except urllib.error.HTTPError as error:
            text = error.read().decode()
            status = error.code

        if raw:
            return status, text

        try:
            return status, json.loads(text) if text else None
        except json.JSONDecodeError:
            return status, text

    @staticmethod
    def _error(status: int, body: Any, what: str) -> ApiError:
        """
        An ``ApiError`` with the server's own message, when it gave one.
        """
        message = body.get("message") if isinstance(body, dict) else str(body)[:300]
        return ApiError(status, f"{what}: {message or status}")

    # -- objects ---------------------------------------------------------------

    def get(self, path: str) -> Optional[dict]:
        """
        One object, or ``None`` when it doesn't exist.
        """
        status, body = self.request("GET", path)

        if status == 404:
            return None
        if status >= 300:
            raise self._error(status, body, f"GET {path}")

        return body

    def list(self, path: str, query: Optional[dict] = None) -> list[dict]:
        """
        The items of a collection, filtered by *query* (``labelSelector``).
        """
        status, body = self.request("GET", path, query=query)
        if status >= 300:
            raise self._error(status, body, f"GET {path}")

        return body.get("items", []) if isinstance(body, dict) else []

    def apply(self, path: str, obj: dict, field_manager: str = "hardcode") -> dict:
        """
        Server-side apply: create or update *obj*, leaving alone the fields
        other managers own.
        """
        options = {"fieldManager": field_manager, "force": "true"}
        status, body = self.request("PATCH", path, obj, "application/apply-patch+yaml", options)
        if status >= 300:
            raise self._error(status, body, f"APPLY {path}")

        return body

    def merge_patch(self, path: str, patch: dict) -> dict:
        """
        A JSON merge patch. A ``metadata.resourceVersion`` in it makes the
        patch conditional: the server refuses it with 409 if the object moved.
        """
        status, body = self.request("PATCH", path, patch, "application/merge-patch+json")
        if status >= 300:
            raise self._error(status, body, f"PATCH {path}")

        return body

    def create(self, path: str, obj: dict) -> dict:
        """
        Create an object; 409 if one of that name exists.
        """
        status, body = self.request("POST", path, obj)
        if status >= 300:
            raise self._error(status, body, f"POST {path}")

        return body

    def delete(self, path: str, propagation: str = "Foreground") -> Optional[dict]:
        """
        Delete an object and, by default, wait for what it owns to go first;
        ``None`` if it was already gone.
        """
        status, body = self.request("DELETE", path, {"propagationPolicy": propagation})

        if status == 404:
            return None
        if status >= 300:
            raise self._error(status, body, f"DELETE {path}")

        return body

    def logs(
        self,
        pod: str,
        container: str,
        since_seconds: Optional[int] = None,
        tail_lines: Optional[int] = None,
    ) -> str:
        """
        A container's log, or ``""`` when there is none to read (no such pod,
        or a container that hasn't started): all of it, or only what it
        wrote in the last *since_seconds*, or only its last *tail_lines*
        lines, which the kubelet reads from the end of the file; never more
        than ``LOG_BYTES``.
        """
        query = {
            "container": container,
            "sinceSeconds": since_seconds,
            "tailLines": tail_lines,
            "limitBytes": LOG_BYTES,
        }
        status, text = self.request("GET", f"/api/v1/namespaces/{self.namespace}/pods/{pod}/log", query=query, raw=True)
        return text if status == 200 else ""

    # -- watch -----------------------------------------------------------------

    def watch(
        self,
        path: str,
        on_event: Callable[[str, dict], None],
        stop: threading.Event,
        resource_version: Optional[str] = None,
        timeout: float = 300,
    ) -> None:
        """
        Pass each watch event on *path* to *on_event* until *stop* is set.

        The server ends a watch after *timeout* seconds; it is reopened from
        the last resource version seen, or from scratch when the server says
        that version is too old. Connection failures are retried after a pause.
        """
        version = resource_version

        while not stop.is_set():
            query = {"watch": "1", "allowWatchBookmarks": "true", "timeoutSeconds": int(timeout)}
            if version:
                query["resourceVersion"] = version

            url = self.base + path + "?" + urllib.parse.urlencode(query)
            request = urllib.request.Request(url, headers=self._headers())

            try:
                with urllib.request.urlopen(request, timeout=timeout + 10, context=self.context) as response:
                    for line in response:
                        if stop.is_set():
                            return
                        if not line.strip():
                            continue

                        event = json.loads(line)
                        obj = event.get("object") or {}

                        # Usually "410 Gone": the version is too old to resume from.
                        if event.get("type") == "ERROR":
                            version = None
                            break

                        if obj.get("metadata", {}).get("resourceVersion"):
                            version = obj["metadata"]["resourceVersion"]

                        # Bookmarks only move the version forward.
                        if event.get("type") != "BOOKMARK":
                            on_event(event["type"], obj)

            except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
                stop.wait(3)
