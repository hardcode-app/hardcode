#!/usr/bin/env python3
"""
Writes the data the documentation screenshots are rendered from:

    <stage>/app/fixture-data.json      for the desktop app's page
    <stage>/admin/fixture-data.json    for the admin UI

It is built with the real parsers, so the shapes match what the CLI and the
admin API return.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "cli"), str(ROOT / "admin")]

from hardcode_admin import manifests, totp  # noqa: E402
from hardcode_cli import allowlist, blocked, langs  # noqa: E402

# A few refusals of each kind: repeated URLs, an upload, refused tunnels.
SQUID_LOG = """\
1759900000.123    0 172.31.253.10 TCP_DENIED/403 3942 GET https://api.github.com/repos/someone-else/tool - HIER_NONE/- text/html
1759900001.456    0 172.31.253.10 TCP_DENIED/403 3942 GET https://api.github.com/repos/someone-else/tool - HIER_NONE/- text/html
1759900002.789    0 172.31.253.10 TCP_DENIED/403 3942 GET https://api.github.com/repos/someone-else/tool - HIER_NONE/- text/html
1759900003.000    0 172.31.253.10 TCP_DENIED/403 3942 POST https://api.anthropic.com/v1/telemetry - HIER_NONE/- text/html
1759900004.000    0 172.31.253.10 TCP_DENIED/403 0 CONNECT registry.npmjs.org:443 - HIER_NONE/- -
1759900005.000    0 172.31.253.10 TCP_DENIED/403 0 CONNECT registry.npmjs.org:443 - HIER_NONE/- -
1759900006.000    0 172.31.253.12 TCP_DENIED/403 3942 PUT https://github.com/ourcompany/app.git/git-receive-pack - HIER_NONE/- text/html
"""

DNS_LOG = """\
[INFO] 172.31.253.10:41234 BLOCKED A telemetry.example.net.
[INFO] 172.31.253.10:41235 BLOCKED AAAA telemetry.example.net.
[INFO] 172.31.253.12:52001 BLOCKED A updates.some-vendor.com.
"""

DEFAULTS = {
    "image": "ghcr.io/ourcompany/hardcode-vscode:1.4.0",
    "ingressClassName": "nginx",
    "ingressTLSSecret": "wildcard-code-tls",
}

TOKEN = "k3Zb7Qp1xJ9vL2mN8rT4wY6aC0eF5gH"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

ADMINS = [
    {"email": "you@example.com", "totpEnrolled": True, "createdAt": "2026-06-01T09:00:00Z"},
    {"email": "sam@example.com", "totpEnrolled": True, "createdAt": "2026-08-14T13:30:00Z"},
    {"email": "new.admin@example.com", "totpEnrolled": False, "createdAt": "2026-10-07T16:45:00Z"},
]


def profile_object(name: str, spec: dict, phase: str, message: str, created: str, dns_ip: str) -> dict:
    """
    A Profile as the API server would return it, status included.
    """
    return {
        "apiVersion": manifests.API_VERSION,
        "kind": "Profile",
        "metadata": {
            "name": name,
            "namespace": "sandbox",
            "uid": f"uid-{name}",
            "generation": 3,
            "creationTimestamp": created,
        },
        "spec": spec,
        "status": {
            "phase": phase,
            "message": message,
            "observedGeneration": 3,
            "dnsClusterIP": dns_ip,
            "proxyURL": f"http://{name}-squid.sandbox.svc.cluster.local:3128",
            "vscodeService": f"{name}-vscode",
            "tokenSecret": f"{name}-vscode-token",
            "lastReconciled": "2026-10-08T09:41:07+00:00",
        },
    }


def summarise(obj: dict) -> dict:
    """
    A profile as the admin's list of profiles shows it.
    """
    spec = manifests.effective_spec(obj, DEFAULTS)
    enabled = [entry for entry in allowlist.effective(spec["allowlist"], spec["languages"]) if entry.enabled]

    return {
        "name": obj["metadata"]["name"],
        "createdAt": obj["metadata"]["creationTimestamp"],
        "image": spec["image"],
        "suspended": spec["suspended"],
        "phase": obj["status"]["phase"],
        "message": obj["status"]["message"],
        "languages": spec["languages"],
        "entries": len(enabled),
        "rw": sum(1 for entry in enabled if entry.flag == "rw"),
        "ssh": sum(1 for entry in enabled if entry.flag == "ssh"),
        "vscodeHost": spec["vscodeHost"],
    }


def enrolment_qr(otpauth: str):
    """
    The enrolment QR code, made in the admin image, where the qrcode package
    is. Without the image, the page shows the secret as text instead.
    """
    program = f"from hardcode_admin.web import qr_data_url; print(qr_data_url({otpauth!r}))"
    command = [
        "docker", "run", "--rm", "--memory", "512m",
        "-e", "PYTHONPATH=",
        "--entrypoint", "python3",
        "hardcode-admin:latest",
        "-c", program,
    ]  # fmt: skip

    try:
        output = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None

    return output.stdout.strip() or None


def main(stage: Path) -> None:
    template = (ROOT / "template" / "allowlist.txt").read_text(encoding="utf-8")

    # The template with a few entries of the profile's own, of each kind,
    # and two provided ones it switches off.
    text = allowlist.add(template, "github.com/ourcompany", "rw")
    text = allowlist.add(text, "ssh.github.com:443", "ssh")
    text = allowlist.add(text, "docs.ourcompany.example")
    text = allowlist.switch_off(text, ".pkg.dev")
    text = allowlist.switch_off(text, "repo.yarnpkg.com")

    entries = allowlist.effective(text, langs.LANGS)
    blocked_items = [item.as_dict() for item in blocked.parse(SQUID_LOG, DNS_LOG)]

    # The desktop app: a profile built around Python, TypeScript and Go, with
    # two extensions of its own, and an image built with exactly those. The
    # second one's publisher has no verified domain: app-fixture.js has the
    # VS Code container refuse it.
    chosen = ["python", "node", "go", "build_essentials", "docker", "claude_cli", "codex_cli"]
    app_env = {lang.env_key: "true" if lang.key in chosen else "false" for lang in langs.CATALOGUE}

    app_langs = langs.enabled_in(app_env)
    for lang in app_langs:
        lang["inImage"] = lang["key"] in chosen

    app_data = {
        "allowlist": allowlist.as_dicts(allowlist.effective(text, chosen)),
        "langs": app_langs,
        "extensions": langs.extension_sources(chosen, ["eamodio.gitlens", "somedev.handy-snippets"]),
        "image": {"built": True, "languages": chosen},
        "rebuildNeeded": False,
        "blocked": blocked_items,
    }
    (stage / "app").mkdir(parents=True, exist_ok=True)
    (stage / "app" / "fixture-data.json").write_text(json.dumps(app_data), encoding="utf-8")

    # The admin: three profiles in different states, one of them in detail.
    # team-a is built around Python, TypeScript and Go, with two extensions of
    # its own, one of which Go brings anyway (it was added while Go was off);
    # team-b has no languages of its own, so it has all of them.
    team_a_languages = ["python", "node", "go", "build_essentials", "docker", "claude_cli", "codex_cli"]
    team_a = profile_object(
        "team-a",
        {
            "allowlist": text,
            "languages": team_a_languages,
            "extensions": ["eamodio.gitlens", "golang.go"],
            "vscodeHost": "team-a.code.example.com",
            "home": {"size": "50Gi"},
        },
        "Ready",
        "",
        "2026-09-12T08:00:00Z",
        "10.96.41.7",
    )
    team_b = profile_object(
        "team-b",
        {"allowlist": template, "image": "ghcr.io/ourcompany/hardcode-vscode:1.3.2"},
        "Pending",
        "waiting for vscode (ContainerCreating)",
        "2026-10-01T15:20:00Z",
        "10.96.88.3",
    )
    research_languages = ["java", "build_essentials"]
    research = profile_object(
        "research",
        {
            "allowlist": template,
            "languages": research_languages,
            "suspended": True,
            "docker": {"enabled": False},
        },
        "Suspended",
        "vscode scaled to 0",
        "2026-07-30T11:05:00Z",
        "10.96.12.9",
    )

    # The shapes GET /api/profiles/<name> and GET /api/languages answer with.
    # team-a's VS Code container is part of the way through installing its
    # extensions.
    effective = manifests.effective_spec(team_a, DEFAULTS)
    wanted = langs.wanted_extensions(effective["languages"], effective["extensions"])
    detail = {
        **summarise(team_a),
        "languages": [langs.describe(lang, lang.key in effective["languages"]) for lang in langs.CATALOGUE],
        "extensions": langs.extension_sources(effective["languages"], effective["extensions"]),
        "extensionStatus": {"state": "installing", "installed": 9, "total": len(wanted), "refused": 0, "failed": 0},
        "spec": {**team_a["spec"], "effective": effective},
        "status": team_a["status"],
        "allowlist": allowlist.as_dicts(allowlist.effective(effective["allowlist"], effective["languages"])),
        "access": {
            "token": TOKEN,
            "url": f"https://team-a.code.example.com/?tkn={TOKEN}",
            "portForward": "kubectl -n sandbox port-forward svc/team-a-vscode 8000:8000",
            "host": "team-a.code.example.com",
        },
    }
    catalogue = [
        {key: value for key, value in langs.describe(lang, False).items() if key != "enabled"}
        for lang in langs.CATALOGUE
    ]

    otpauth = totp.otpauth_url(TOTP_SECRET, "you@example.com")
    qr = enrolment_qr(otpauth)

    admin_data = {
        "profiles": [summarise(obj) for obj in (team_a, team_b, research)],
        "profile": detail,
        "catalogue": catalogue,
        "blocked": blocked_items,
        "template": template,
        "users": ADMINS,
        "otpauth": otpauth,
        "secret": TOTP_SECRET,
        "qr": qr,
    }
    (stage / "admin").mkdir(parents=True, exist_ok=True)
    (stage / "admin" / "fixture-data.json").write_text(json.dumps(admin_data), encoding="utf-8")

    print(
        f"fixture data: {len(entries)} allowlist entries, {len(blocked_items)} blocked targets, "
        f"QR {'yes' if qr else 'no'}"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
