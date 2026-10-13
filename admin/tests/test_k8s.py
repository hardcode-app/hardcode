"""
Tests for the Kubernetes API client's credentials and its log reads
(hardcode_admin.k8s).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hardcode_admin import k8s
from hardcode_admin.k8s import LOG_BYTES, K8s


class FakeResponse:
    """
    What ``urllib.request.urlopen`` returns, recording the request's
    Authorization header and URL.
    """

    status = 200

    def __init__(self, request, seen: dict):
        seen["authorization"] = request.get_header("Authorization")
        seen["url"] = request.full_url

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class TokenTests(unittest.TestCase):
    def test_a_static_token_stays(self):
        client = K8s(env={"K8S_API": "http://127.0.0.1:8001", "K8S_TOKEN": "static", "K8S_NAMESPACE": "ns"})

        self.assertEqual((client.token, client.namespace, client.token_file), ("static", "ns", None))

    def test_the_service_account_token_is_read_as_the_kubelet_rotates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            account = Path(tmp)
            (account / "token").write_text("old-token\n", encoding="utf-8")
            (account / "ca.crt").write_text("", encoding="utf-8")
            (account / "namespace").write_text("sandbox", encoding="utf-8")

            with (
                mock.patch.object(k8s, "SERVICE_ACCOUNT", account),
                mock.patch("ssl.create_default_context", return_value=None),
            ):
                client = K8s(env={"KUBERNETES_SERVICE_HOST": "10.0.0.1"})
                self.assertEqual((client.token, client.namespace), ("old-token", "sandbox"))

                # The kubelet rewrites the file; a later mtime, whatever the
                # file system's clock resolution.
                (account / "token").write_text("rotated-token\n", encoding="utf-8")
                os.utime(account / "token", (1, 1 + 10**9))
                self.assertEqual(client.token, "rotated-token")

                seen: dict = {}
                with mock.patch("urllib.request.urlopen", side_effect=lambda request, **_: FakeResponse(request, seen)):
                    client.get("/api/v1/namespaces/sandbox/pods/x")

                self.assertEqual(
                    seen["authorization"],
                    "Bearer rotated-token",
                    "the request carries the token as it is now",
                )


class LogTests(unittest.TestCase):
    """
    A container's log: what is asked of the API server for it.
    """

    def read(self, **options) -> str:
        """
        The URL a log read with *options* asks the API server for.
        """
        client = K8s(env={"K8S_API": "http://127.0.0.1:8001", "K8S_NAMESPACE": "ns"})
        seen: dict = {}

        with mock.patch("urllib.request.urlopen", side_effect=lambda request, **_: FakeResponse(request, seen)):
            client.logs("p-vscode-0", "vscode", **options)

        return seen["url"]

    def test_the_period_or_the_tail_is_what_is_asked_for(self):
        base = "http://127.0.0.1:8001/api/v1/namespaces/ns/pods/p-vscode-0/log?container=vscode"
        limit = f"limitBytes={LOG_BYTES}"

        self.assertEqual(self.read(since_seconds=600), f"{base}&sinceSeconds=600&{limit}")
        self.assertEqual(
            self.read(tail_lines=1000), f"{base}&tailLines=1000&{limit}", "the end of the log, however old"
        )
        self.assertEqual(self.read(), f"{base}&{limit}")

    def test_a_read_has_a_byte_limit(self):
        # The sandbox writes what squid logs just by making requests: however
        # the node rotates logs, a read never holds more than this.
        self.assertIn(f"&limitBytes={LOG_BYTES}", self.read(since_seconds=1800))
        self.assertLessEqual(LOG_BYTES, 16 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
