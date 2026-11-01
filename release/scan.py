#!/usr/bin/env python3
"""
Known vulnerabilities in what a release is built from and in the images
that are its boundary, as OSV-Scanner finds them against OSV's databases.
The scanner itself runs sealed (release/scan.sh: no network, no token, a
read-only container); these are the steps around it.

    release/scan.py databases DIR
        download OSV's databases for the ecosystems HardCode uses into DIR,
        laid out as the scanner reads them offline
    release/scan.py images --prefix PREFIX --tag TAG
        the images to scan, a line each ("name reference"): the proxy, the
        gateway and the admin as this run published them, and CoreDNS and
        the Docker sidecar as compose.yaml pins them
    release/scan.py summary DIR --out FILE
        from the scanner's reports in DIR (NAME.json each), the description's
        "Known vulnerabilities" section (FILE) and every finding
        (vulnerabilities.json, beside FILE)

The findings are reported, not acted on: a base image always carries a few
nobody has fixed yet, and a release that waited for none would never ship.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent

# OSV's databases, one zip per ecosystem: the app's crates, the Python pins,
# the Go binaries in CoreDNS's and Docker's images, Docker's Alpine base,
# CoreDNS's Debian one, and Ubuntu, which the rest are built on.
DATABASES = "https://osv-vulnerabilities.storage.googleapis.com/{ecosystem}/all.zip"
ECOSYSTEMS = ["crates.io", "PyPI", "Go", "Alpine", "Debian", "Ubuntu"]

# Our images worth scanning: the boundary. The VS Code image is what the
# sandbox contains, not what contains it.
OWN_IMAGES = ["squid", "gateway", "admin"]

# What each report is about, by its name (the report is NAME.json): in the
# table, and in the list of fixes.
LABELS = {
    "source": ("The app's crates, and the Python packages the command and the build use", "the app's crates"),
    "squid": ("The proxy and allowlist renderer (hardcode-squid)", "the proxy"),
    "gateway": ("The gateway (hardcode-gateway)", "the gateway"),
    "admin": ("The operator and admin (hardcode-admin)", "the admin"),
    "coredns": ("CoreDNS", "CoreDNS"),
    "docker": ("The Docker sidecar", "the Docker sidecar"),
}

# CVSS scores, by the word for them.
SEVERITIES = [("critical", 9.0), ("high", 7.0), ("medium", 4.0), ("low", 0.1)]


class ScanError(Exception):
    """
    A scan step can't be done; the message says why.
    """


def databases(directory: Path, fetch: Callable[[str], bytes]) -> list[Path]:
    """
    Download each ecosystem's database into *directory*, where the scanner
    looks offline (OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY): osv-scalibr/
    ECOSYSTEM/all.zip. The paths written.
    """
    written = []

    for ecosystem in ECOSYSTEMS:
        path = directory / "osv-scalibr" / ecosystem / "all.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fetch(DATABASES.format(ecosystem=ecosystem)))
        written.append(path)

    return written


def download(url: str) -> bytes:
    """
    *url*'s content.
    """
    try:
        with urllib.request.urlopen(url, timeout=600) as response:
            return response.read()
    except OSError as error:
        raise ScanError(f"{url}: {error}") from error


def images(prefix: str, tag: str, compose: str) -> list[tuple[str, str]]:
    """
    The images to scan, as (name, reference): ours at *tag* under *prefix*,
    and the ones *compose* (compose.yaml's text) pins for CoreDNS and the
    Docker sidecar.
    """
    found = [(name, f"{prefix}-{name}:{tag}") for name in OWN_IMAGES]

    for name, pattern in (("coredns", r"coredns/coredns"), ("docker", r"docker")):
        match = re.search(rf"^\s+image:\s+({pattern}:\S+)\s*$", compose, re.MULTILINE)
        if match is None:
            raise ScanError(f"compose.yaml pins no {name} image")
        found.append((name, match.group(1)))

    return found


def severity(score: str) -> str:
    """
    The word for a CVSS score ("9.8"); "unscored" without one.
    """
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "unscored"

    for word, floor in SEVERITIES:
        if value >= floor:
            return word

    return "unscored"


def _numbers(version: str) -> tuple[int, ...]:
    """
    A version's numbers, in order ("1.2.3-r4" is (1, 2, 3, 4)), for
    comparing versions of one package in any ecosystem's spelling.
    """
    return tuple(int(part) for part in re.findall(r"\d+", version))


def version_unknown(version: str) -> bool:
    """
    Whether the scanner couldn't read a package's version: a Go binary built
    without module versions says "(devel)" or a "0.0.0-" pseudo-version,
    which it matches against every version ever affected.
    """
    return not version or version == "(devel)" or version.startswith("0.0.0-")


def findings(report: dict) -> list[dict]:
    """
    Every issue in a scanner report, once (a group of aliases is one issue,
    and a package found twice, in two binaries, is one): the package, its
    version, the issue's ids, its severity, a summary, the versions that fix
    it, the smallest of those above the installed one, and whether it is
    counted (not when the version is unknown, nor when every fix is older
    than the installed version).
    """
    found = {}

    for result in report.get("results") or []:
        for package in result.get("packages") or []:
            name = package["package"]["name"]
            version = package["package"].get("version", "")
            vulnerabilities = {item["id"]: item for item in package.get("vulnerabilities") or []}

            for group in package.get("groups") or []:
                if (name, version, group["ids"][0]) in found:
                    continue

                described = [vulnerabilities[key] for key in group["ids"] if key in vulnerabilities]
                fixed = sorted(
                    {
                        event["fixed"]
                        for item in described
                        for affected in item.get("affected") or []
                        if affected.get("package", {}).get("name") == name
                        for ranges in affected.get("ranges") or []
                        for event in ranges.get("events") or []
                        if "fixed" in event
                    },
                    key=_numbers,
                )
                newer = [candidate for candidate in fixed if _numbers(candidate) > _numbers(version)]

                found[(name, version, group["ids"][0])] = {
                    "package": name,
                    "version": version,
                    "ids": group["ids"],
                    "severity": severity(group.get("max_severity", "")),
                    "summary": (described[0].get("summary") or "").strip() if described else "",
                    "fixed": fixed,
                    "fix": newer[0] if newer else "",
                    "counted": not version_unknown(version) and not (fixed and not newer),
                }

    return list(found.values())


def summary(reports: dict[str, dict], scanner: str, now: Optional[datetime] = None) -> tuple[str, list]:
    """
    The description's section for *reports* (by name), and every finding,
    by name, for vulnerabilities.json.
    """
    now = now or datetime.now(timezone.utc)
    columns = [word for word, _ in SEVERITIES] + ["unscored"]
    every = {name: findings(report) for name, report in reports.items()}

    lines = [
        "## Known vulnerabilities",
        "",
        f"What this build is made from and the images that are its boundary, scanned with {scanner} "
        f"against OSV's databases of {now:%Y-%m-%d}, offline. Reported, not blocking: "
        "`vulnerabilities.json` has each finding.",
        "",
        "| | " + " | ".join(word.capitalize() for word in columns) + " | Not counted* |",
        "|---|" + "---|" * (len(columns) + 1),
    ]
    for name, items in every.items():
        counted = [item for item in items if item["counted"]]
        cells = [str(sum(1 for item in counted if item["severity"] == word)) for word in columns]
        cells.append(str(len(items) - len(counted)))
        lines.append(f"| {LABELS.get(name, (name, name))[0]} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "\\* Found in a binary whose version the scanner can't read (it matches those against every "
        "affected version), or fixed only in versions older than the one installed.",
    ]

    # What a newer version fixes, critical and high: a line for each
    # package, with the version that fixes all of them, the most severe first.
    fixes: dict[tuple, dict] = {}
    for name, items in every.items():
        for item in items:
            if not item["counted"] or not item["fix"] or item["severity"] not in ("critical", "high"):
                continue

            entry = fixes.setdefault((item["package"], item["version"], name), {"critical": 0, "high": 0, "fix": ""})
            entry[item["severity"]] += 1
            entry["fix"] = max(entry["fix"], item["fix"], key=_numbers)

    if fixes:
        lines += ["", "Critical and high, fixed in a newer version:", ""]
        ordered = sorted(fixes.items(), key=lambda pair: (-pair[1]["critical"], -pair[1]["high"], pair[0]))
        for (package, version, name), entry in ordered:
            severities = ", ".join(f"{entry[word]} {word}" for word in ("critical", "high") if entry[word])
            where = LABELS.get(name, (name, name))[1]
            lines.append(f"- {package} {version} → {entry['fix']}, in {where}: {severities}")

    return "\n".join(lines) + "\n", [{"scanned": name, "findings": items} for name, items in every.items()]


def main(argv: list[str]) -> int:
    """
    The command line; see the module's docstring.
    """
    parser = argparse.ArgumentParser(prog="scan.py", description="Known vulnerabilities, around the scanner.")
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("databases", help="download OSV's databases into DIR")
    command.add_argument("directory", type=Path)

    command = commands.add_parser("images", help="the images to scan")
    command.add_argument("--prefix", required=True)
    command.add_argument("--tag", required=True)

    command = commands.add_parser("summary", help="the description's section, from DIR's reports")
    command.add_argument("directory", type=Path)
    command.add_argument("--scanner", required=True)
    command.add_argument("--out", type=Path, required=True)

    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "databases":
            for path in databases(arguments.directory, download):
                print(f"{path} ({path.stat().st_size // 1048576} MB)")

        elif arguments.command == "images":
            compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
            for name, reference in images(arguments.prefix, arguments.tag, compose):
                print(f"{name} {reference}")

        elif arguments.command == "summary":
            reports = {
                path.stem: json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(arguments.directory.glob("*.json"))
            }
            if not reports:
                raise ScanError(f"no reports in {arguments.directory}")

            # The lock files first, then the images in the order they're listed.
            names = ["source"] + OWN_IMAGES + ["coredns", "docker"]
            ordered = {name: reports[name] for name in names if name in reports}
            text, every = summary(ordered, arguments.scanner)

            arguments.out.write_text(text, encoding="utf-8")
            details = arguments.out.with_name("vulnerabilities.json")
            details.write_text(json.dumps(every, indent=2) + "\n", encoding="utf-8")

    except (ScanError, OSError, ValueError) as error:
        print(f"scan.py: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
