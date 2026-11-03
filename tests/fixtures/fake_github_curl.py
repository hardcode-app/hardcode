#!/usr/bin/env python3
"""
A stand-in for ``curl``, for tests/images.py: answers hardcode-github's
requests as the proxy and GitHub would, from the environment:

- FAKE_REFUSED: addresses the proxy refuses (comma-separated prefixes),
  answered as squid does, 403 with an X-Squid-Error header;
- FAKE_TOKENS: ``token=OWNER/REPO`` pairs (comma-separated), the
  repository each token reaches;
- FAKE_READ_ONLY: tokens (comma-separated) that may read but not push.

It takes the options hardcode-github uses: ``-sS``, ``-o FILE``, ``-D -``,
``-K -`` (the token, as a curl config line on stdin) and the address, and
prints the answer's headers, the proxy's "Connection established" first.
"""

import os
import re
import sys

ESTABLISHED = "HTTP/1.1 200 Connection established\r\n\r\n"


def listed(name: str) -> list[str]:
    """
    A comma-separated variable, as a list.
    """
    return [item for item in os.environ.get(name, "").split(",") if item]


def credentials() -> tuple[str, str]:
    """
    The token curl's config on stdin carries, and how: ``bearer``, ``basic``
    or nothing.
    """
    config = sys.stdin.read()

    bearer = re.search(r'header = "Authorization: Bearer ([^"]+)"', config)
    if bearer:
        return "bearer", bearer.group(1)

    basic = re.search(r'user = "x-access-token:([^"]+)"', config)
    if basic:
        return "basic", basic.group(1)

    return "", ""


def answer(url: str, kind: str, token: str) -> tuple[int, list[str]]:
    """
    GitHub's status code for the address, and its headers.
    """
    tokens = dict(pair.split("=", 1) for pair in listed("FAKE_TOKENS"))

    api = re.match(r"^https://api\.github\.com/repos/([^/]+/[^/?]+)$", url)
    if api:
        if kind != "bearer":
            return 200, []
        if token not in tokens:
            return 401, []
        if tokens[token] != api.group(1):
            return 404, []
        return 200, ["github-authentication-token-expiration: 2026-12-03 10:00:00 UTC"]

    git_service = r"^https://github\.com/([^/]+/[^/?]+)\.git/info/refs\?service=(git-upload-pack|git-receive-pack)$"
    git = re.match(git_service, url)
    if git and git.group(2) == "git-upload-pack":
        return 200, []
    if git:
        may_push = tokens.get(token) == git.group(1) and token not in listed("FAKE_READ_ONLY")
        return (200, []) if kind == "basic" and may_push else (403, [])

    return 404, []


def main() -> int:
    """
    Print the headers for the address on the command line.
    """
    url = sys.argv[-1]
    kind, token = credentials()

    if any(url.startswith(prefix) for prefix in listed("FAKE_REFUSED")):
        refusal = "HTTP/1.1 403 Forbidden\r\nServer: squid\r\nX-Squid-Error: ERR_ACCESS_DENIED 0\r\n\r\n"
        sys.stdout.write(ESTABLISHED + refusal)
        return 0

    code, headers = answer(url, kind, token)
    lines = [f"HTTP/2 {code}", *headers]
    sys.stdout.write(ESTABLISHED + "\r\n".join(lines) + "\r\n\r\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
