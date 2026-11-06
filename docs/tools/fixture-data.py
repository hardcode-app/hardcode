#!/usr/bin/env python3
"""
Writes the data the documentation screenshots are rendered from:

    <stage>/app/fixture-data.json      for the desktop app's page
    <stage>/admin/fixture-data.json    for the admin UI

It is built with the real parsers, so the shapes match what the CLI and the
admin API return.
"""

import json
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "cli"), str(ROOT / "admin")]

from hardcode_admin import auth, manifests, totp  # noqa: E402
from hardcode_cli import allowlist, blocked, devices, dockerfile, langs  # noqa: E402

# A few refusals of each kind: repeated URLs, an upload, refused tunnels.
SQUID_LOG = """\
1759900000.123    0 172.31.253.10 TCP_DENIED/403 3942 GET https://api.github.com/repos/someone-else/tool - HIER_NONE/- text/html
1759900001.456    0 172.31.253.10 TCP_DENIED/403 3942 GET https://api.github.com/repos/someone-else/tool - HIER_NONE/- text/html
1759900002.789    0 172.31.253.10 TCP_DENIED/403 3942 GET https://api.github.com/repos/someone-else/tool - HIER_NONE/- text/html
1759900003.000    0 172.31.253.10 TCP_DENIED/403 3942 POST https://api.anthropic.com/v1/telemetry - HIER_NONE/- text/html
1759900004.000    0 172.31.253.10 TCP_DENIED/403 0 CONNECT registry.npmjs.org:443 - HIER_NONE/- -
1759900005.000    0 172.31.253.10 TCP_DENIED/403 0 CONNECT registry.npmjs.org:443 - HIER_NONE/- -
1759900006.000    0 172.31.253.12 TCP_DENIED/403 3942 PUT https://github.com/ourcompany/app.git/git-receive-pack - HIER_NONE/- text/html
1759900007.000    0 172.31.253.10 TCP_DENIED/403 3942 GET https://nas.ourcompany.example/ - HIER_NONE/- text/html internal-address
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
    # The cluster takes names on your network, as the chart can say.
    "lan": {"enabled": True, "clusterRanges": ["10.244.0.0/16", "10.96.0.0/12"]},
}

TOKEN = "k3Zb7Qp1xJ9vL2mN8rT4wY6aC0eF5gH"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

# The people: two admins, a member who asks for entries (read-write ones
# too) and owns team-a, and a newer member whose allowlist is the admins'.
MEMBER = "priya@example.com"
MEMBER_RIGHTS = {"profiles": 2, "allowlist": "ask", "rw": True, "ssh": False, "lan": False}
PEOPLE = [
    {
        "email": "you@example.com",
        "role": "admin",
        "rights": auth.RIGHTS,
        "profiles": 0,
        "totpEnrolled": True,
        "createdAt": "2026-06-01T09:00:00Z",
    },
    {
        "email": "sam@example.com",
        "role": "admin",
        "rights": auth.RIGHTS,
        "profiles": 0,
        "totpEnrolled": True,
        "createdAt": "2026-08-14T13:30:00Z",
    },
    {
        "email": MEMBER,
        "role": "member",
        "rights": MEMBER_RIGHTS,
        "profiles": 1,
        "totpEnrolled": True,
        "createdAt": "2026-09-12T07:55:00Z",
    },
    {
        "email": "lee@example.com",
        "role": "member",
        "rights": {**auth.RIGHTS, "profiles": 1},
        "profiles": 1,
        "totpEnrolled": False,
        "createdAt": "2026-10-07T16:45:00Z",
    },
]


def request(number: int, entry: str, access: str, reason: str, state: str = "pending", by: str = "") -> dict:
    """
    One of the member's requests for team-a, as GET /api/requests answers
    with it: waiting, or decided by *by*.
    """
    return {
        "id": f"r{number}Xk2pQ",
        "profile": "team-a",
        "by": MEMBER,
        "kind": "entry",
        "op": "add",
        "entry": entry,
        "flag": access,
        "access": access,
        "address": "",
        "reason": reason,
        "createdAt": f"2026-10-0{number}T10:15:00+00:00",
        "state": state,
        "decidedBy": by,
        "decidedAt": f"2026-10-0{number}T11:02:00+00:00" if by else "",
        "profileGone": False,
        "notTheirs": False,
    }


REQUESTS = [
    request(8, "pkg.go.dev", "", "the Go module docs"),
    request(7, "github.com/ourcompany/infra", "rw", "we push the deploy scripts there"),
    request(5, "docs.python.org", "", "the standard library's docs", "approved", "you@example.com"),
    request(3, "pastebin.com", "", "", "denied", "sam@example.com"),
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
        "owner": obj["spec"].get("owner", ""),
    }


def device_host() -> devices.Host:
    """
    The computer the app's screenshots show: Linux, with /dev/kvm and an
    AMD or Intel GPU's /dev/dri, and nothing else.
    """
    nodes = {"/dev/kvm": stat.S_IFCHR, "/dev/dri": stat.S_IFDIR}

    def stat_of(path: str) -> SimpleNamespace:
        if path not in nodes:
            raise FileNotFoundError(path)
        return SimpleNamespace(st_mode=nodes[path] | 0o660, st_gid=0)

    return devices.Host(platform="linux", stat=stat_of, listdir=lambda path: [])


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

    # A GitLab on the company's own network, with its address approved.
    text = allowlist.add(text, "gitlab.ourcompany.example:22222", "ssh")
    text, _ = allowlist.add_pin(text, "gitlab.ourcompany.example", "192.168.71.200")
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

    # Two Ubuntu packages of its own, and Dockerfile steps that trust the
    # company's certificate authority, both in the image already.
    packages = ["postgresql-client", "graphviz"]
    steps = (
        "FROM hardcode\n"
        "USER root\n"
        "\n"
        "# Our own certificate authority, so our servers' certificates are trusted.\n"
        "COPY company-ca.crt /usr/local/share/ca-certificates/\n"
        "RUN update-ca-certificates\n"
        "\n"
        "USER dev\n"
    )
    image = {"built": True, "languages": chosen, "packages": packages, "dockerfile": "5d0c1f7a9e42"}
    folder = "/home/you/.config/hardcode/profiles/default/image"
    image_steps = {
        "profile": "default",
        "path": f"{folder}/Dockerfile",
        "folder": folder,
        "exists": True,
        "dockerfile": steps,
        "starter": dockerfile.STARTER,
        "digest": image["dockerfile"],
        "base": (ROOT / "images" / "vscode" / "Dockerfile").read_text(encoding="utf-8"),
        "basePath": "/home/you/.cache/hardcode/stack/0.1.0/images/vscode/Dockerfile",
        "image": image,
        "rebuildNeeded": False,
        "rebuildReasons": [],
    }

    # Its devices: all off, on a computer with KVM and a GPU, whose Docker
    # has no NVIDIA runtime.
    app_devices = devices.states([], device_host(), lambda: devices.Runtimes(("io.containerd.runc.v2", "runc")))

    app_data = {
        "allowlist": allowlist.as_dicts(allowlist.effective(text, chosen)),
        "devices": app_devices,
        "langs": app_langs,
        "extensions": langs.extension_sources(chosen, ["eamodio.gitlens", "somedev.handy-snippets"]),
        "image": image,
        "packages": packages,
        "imageSteps": image_steps,
        "rebuildReasons": [],
        "rebuildNeeded": False,
        "blocked": blocked_items,
        "lan": [pin.as_dict() for pin in allowlist.pins(text)],
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
            "owner": MEMBER,
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
            "owner": "lee@example.com",
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
        "packages": ["postgresql-client"],
        "lan": {
            "enabled": effective["lan"]["enabled"],
            "pins": [{**pin, "resolves": [pin["address"]], "state": "ok"} for pin in effective["lan"]["pins"]],
        },
        "access": {
            "token": TOKEN,
            "url": f"https://team-a.code.example.com/?tkn={TOKEN}",
            "portForward": "kubectl -n sandbox port-forward svc/team-a-vscode 8000:8000",
            "host": "team-a.code.example.com",
        },
        "requests": [item for item in REQUESTS if item["state"] == "pending"],
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
        "users": PEOPLE,
        "requests": REQUESTS,
        "member": {"email": MEMBER, "rights": MEMBER_RIGHTS},
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
