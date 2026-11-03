#!/usr/bin/env python3
"""
The Docker sidecar's runc wrapper with the real runc, on real images, with
no Docker daemon: tests/docker.py runs this in the sidecar's image, as its
rootless user, with

    /images/NAME.tar   root filesystems (docker export): alpine (node:alpine),
                       debian, ubi, distroless and temurin
    /proxy-ca/ca.pem   the suite's test CA, where the wrapper reads the proxy's
    /tls/              the origin's certificate and key from it (origin.pem,
                       origin.key), and a "company" CA (company.crt)
    /fixtures/         this folder, with the clients the containers run

It serves HTTPS on 127.0.0.1:8443 with the origin's certificate, as squid
would with one it made. Then it runs each case through "runc" from PATH
(the wrapper): as a build step (runc run) and as a container (runc create,
start, exec, delete), and prints what it saw, one JSON object per line,
for the suite to check.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

# Docker's runc, in the sidecar's image; "runc" on PATH is the wrapper.
REAL_RUNC = "/usr/local/bin/runc"

IMAGES = Path("/images")
PROXY_CA = Path("/proxy-ca")
TLS = Path("/tls")
FIXTURES = Path("/fixtures")
WORK = Path("/tmp/hardcode-cases")
STATE = WORK / "runc-state"
URL = "https://127.0.0.1:8443/ok"

# The marker the wrapper puts around the CA it adds to a bundle.
MARKER = b"# HardCode proxy CA begin"

# A file outside every image, that a symlink in one names.
SECRET = Path("/tmp/hardcode-secret.pem")

# The system bundle the Debian images keep (and update-ca-certificates writes).
DEBIAN_BUNDLE = "etc/ssl/certs/ca-certificates.crt"

PATH = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# How each image's containers fetch the origin.
CLIENTS = {
    "alpine": ["node", "/hardcode-test/fetch.js", URL],
    "debian": ["python3", "/hardcode-test/fetch.py", URL],
    "ubi": [
        "curl",
        "--silent",
        "--show-error",
        "--max-time",
        "10",
        "-o",
        "/dev/null",
        "-w",
        "status %{http_code}\n",
        URL,
    ],
    "distroless": ["/usr/bin/python3", "/hardcode-test/fetch.py", URL],
    "temurin": ["/opt/java/openjdk/bin/java", "-XX:-UsePerfData", "/hardcode-test/Fetch.java", URL],
}


def report(case: str, **facts: object) -> None:
    """
    Print one case's facts as a JSON line.
    """
    print(json.dumps({"case": case, **facts}), flush=True)


def run(command: list, **options) -> subprocess.CompletedProcess:
    """
    Run a command, its output captured as text; it must succeed.
    """
    return subprocess.run([str(part) for part in command], check=True, capture_output=True, text=True, **options)


def serve() -> None:
    """
    HTTPS on 127.0.0.1:8443, answering "ok" to everything, in a thread.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        """
        200 "ok" for any GET.
        """

        def do_GET(self) -> None:
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *arguments) -> None:
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(TLS / "origin.pem", TLS / "origin.key")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 8443), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def certificate_body(path: Path) -> bytes:
    """
    The first base64 line of a PEM certificate: enough to find it in a file.
    """
    lines = path.read_bytes().splitlines()
    return lines[1]


def extract(name: str) -> Path:
    """
    A fresh root filesystem from /images/NAME.tar, with the folder the
    fixtures are mounted on.

    Its files are this user's (as an unprivileged user, tarfile sets no
    other owner), and writable by it: in the daemon, the wrapper is root in
    the daemon's user namespace and writes an image's read-only files
    (UBI's bundle is 0444); here it runs without those capabilities. Device
    files and what is under dev/ are left out, as runc makes /dev itself.
    """
    rootfs = WORK / name / "rootfs"
    if rootfs.exists():
        shutil.rmtree(rootfs)
    rootfs.mkdir(parents=True)

    with tarfile.open(IMAGES / f"{name}.tar") as archive:
        members = [
            member
            for member in archive.getmembers()
            if not member.isdev() and not member.name.removeprefix("./").startswith("dev/")
        ]
        archive.extractall(rootfs, members=members, filter="fully_trusted")

    # Top down: a folder is made writable before it is walked into.
    for folder, names, files in os.walk(rootfs):
        for path in [folder, *(os.path.join(folder, entry) for entry in names + files)]:
            status = os.lstat(path)
            if not stat.S_ISLNK(status.st_mode):
                os.chmod(path, stat.S_IMODE(status.st_mode) | stat.S_IWUSR | stat.S_IRUSR)

    (rootfs / "hardcode-test").mkdir(exist_ok=True)
    return rootfs


def busybox_rootfs(name: str, alpine: Path) -> Path:
    """
    An image with nothing but Alpine's busybox (and its loader): no CA
    bundle at all, as FROM scratch images have none.
    """
    rootfs = WORK / name / "rootfs"
    if rootfs.exists():
        shutil.rmtree(rootfs)

    for folder in ("bin", "lib", "proc", "sys", "dev", "hardcode-test"):
        (rootfs / folder).mkdir(parents=True)

    shutil.copy2(alpine / "bin" / "busybox", rootfs / "bin" / "busybox")
    for loader in (alpine / "lib").glob("ld-musl-*.so.1"):
        shutil.copy2(loader, rootfs / "lib" / loader.name)

    return rootfs


def listing(rootfs: Path) -> dict[str, list]:
    """
    Every path in *rootfs*, with its kind, size, mode and modification time.
    """
    found = {}

    for folder, names, files in os.walk(rootfs):
        for name in names + files:
            path = Path(folder) / name
            status = path.lstat()
            kind = "link" if path.is_symlink() else ("dir" if path.is_dir() else "file")
            found[str(path.relative_to(rootfs))] = [kind, status.st_size, status.st_mode, status.st_mtime_ns]

    return found


def differences(before: dict, after: dict) -> list[str]:
    """
    What changed between two listings, a line each.
    """
    changed = []

    for path in sorted(set(before) | set(after)):
        if path not in after:
            changed.append(f"removed {path}")
        elif path not in before:
            changed.append(f"added {path}")
        elif before[path] != after[path]:
            changed.append(f"changed {path}: {before[path]} -> {after[path]}")

    return changed


def leftovers(rootfs: Path) -> list[str]:
    """
    Files in *rootfs* that hold the proxy's CA, or the wrapper's marker.
    """
    body = certificate_body(PROXY_CA / "ca.pem")
    found = []

    for folder, _names, files in os.walk(rootfs):
        for name in files:
            path = Path(folder) / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 10 << 20:
                continue
            try:
                content = path.read_bytes()
            except OSError:
                continue
            if body in content or MARKER in content:
                found.append(str(path.relative_to(rootfs)))

    return found


def fingerprint_in(path: Path, certificate: Path) -> bool:
    """
    Whether the PEM file *path* holds *certificate*.
    """
    return path.exists() and certificate_body(certificate) in path.read_bytes()


def bundle_for(rootfs: Path, case: str, args: list, env: list, mounts: list = (), readonly: bool = False) -> Path:
    """
    A bundle folder for one case: runc's own rootless config.json, sharing
    this container's network (for the origin on 127.0.0.1), with the
    fixtures mounted read-only at /hardcode-test.
    """
    bundle = rootfs.parent / "bundles" / case
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    run([REAL_RUNC, "spec", "--rootless", "--bundle", bundle])
    config = bundle / "config.json"
    spec = json.loads(config.read_text(encoding="utf-8"))

    spec["root"] = {"path": str(rootfs), "readonly": readonly}
    spec["process"]["terminal"] = False
    spec["process"]["args"] = [str(arg) for arg in args]
    # The clients write nothing into the image: no bytecode, no JVM perf data.
    spec["process"]["env"] = [PATH, "PYTHONDONTWRITEBYTECODE=1", *env]
    spec["linux"]["namespaces"] = [entry for entry in spec["linux"]["namespaces"] if entry["type"] != "network"]
    spec["mounts"].append(
        {"destination": "/hardcode-test", "type": "bind", "source": str(FIXTURES), "options": ["rbind", "ro"]}
    )
    spec["mounts"].extend(mounts)

    config.write_text(json.dumps(spec), encoding="utf-8")
    return bundle


def runc(*arguments: object, environment: dict = None, **options) -> subprocess.CompletedProcess:
    """
    "runc" from PATH (the wrapper), with this suite's state folder; its
    output captured unless *options* say where it goes.
    """
    if "stdout" not in options:
        options["capture_output"] = True

    return subprocess.run(
        ["runc", "--root", str(STATE), *[str(argument) for argument in arguments]],
        text=True,
        env=environment,
        **options,
    )


def step(case: str, rootfs: Path, args: list, env: list = (), environment: dict = None) -> dict:
    """
    A build step: ``runc run`` in the foreground, as BuildKit runs one.
    """
    bundle = bundle_for(rootfs, case, args, list(env))

    try:
        done = runc("run", "--bundle", bundle, case, environment=environment, timeout=300)
    except subprocess.TimeoutExpired:
        runc("delete", "--force", case)
        return {"exit": None, "output": "timed out", "timed_out": True}

    return {"exit": done.returncode, "output": (done.stdout + done.stderr)[-3000:], "timed_out": False}


def container(
    case: str,
    rootfs: Path,
    args: list,
    env: list = (),
    mounts: list = (),
    readonly: bool = False,
    environment: dict = None,
    exec_args: list = None,
) -> dict:
    """
    A container: ``runc create`` and ``start``, as containerd's shim runs
    one; with *exec_args*, ``runc exec`` into it while it runs. What it
    printed (no exit code: runc's state doesn't keep it), and the
    wrapper's copies in its bundle folder.
    """
    bundle = bundle_for(rootfs, case, args, list(env), list(mounts), readonly)
    output = bundle / "output.txt"
    facts = {"timed_out": False}

    with open(output, "w", encoding="utf-8") as stream:
        try:
            created = runc(
                "create", "--bundle", bundle, case, environment=environment, stdout=stream, stderr=stream, timeout=120
            )
        except subprocess.TimeoutExpired:
            runc("delete", "--force", case)
            return {"output": "timed out", "timed_out": True, "copies": []}

    facts["created"] = created.returncode
    facts["copies"] = sorted(str(path.relative_to(bundle)) for path in (bundle / "hardcode-ca").glob("*"))

    if created.returncode == 0:
        runc("start", case)

        if exec_args is not None:
            process = {"args": exec_args, "env": [PATH], "cwd": "/", "user": {"uid": 0, "gid": 0}, "terminal": False}
            process_file = bundle / "exec.json"
            process_file.write_text(json.dumps(process), encoding="utf-8")
            executed = runc("exec", "--process", process_file, case, timeout=120)
            facts["exec_exit"] = executed.returncode
            facts["exec_output"] = (executed.stdout + executed.stderr)[-3000:]
            runc("kill", case, "KILL")

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = runc("state", case)
            if status.returncode != 0 or json.loads(status.stdout)["status"] == "stopped":
                break
            time.sleep(0.2)
        else:
            facts["timed_out"] = True
            runc("kill", case, "KILL")

    runc("delete", "--force", case)

    printed = output.read_text(encoding="utf-8", errors="replace")
    facts["output"] = printed[-3000:]
    return facts


def image_cases(name: str, rootfs: Path) -> None:
    """
    An image's client fetching the origin, as a build step and as a
    container, and what each left in the image.
    """
    before = listing(rootfs)

    facts = step(f"{name}-step", rootfs, CLIENTS[name])
    report(f"{name}: step", **facts, changed=differences(before, listing(rootfs)), leftovers=leftovers(rootfs))

    facts = container(f"{name}-container", rootfs, CLIENTS[name])
    report(f"{name}: container", **facts, changed=differences(before, listing(rootfs)), leftovers=leftovers(rootfs))


def debian_cases(rootfs: Path) -> None:
    """
    The Debian image's cases: a read-only root, the container's own mounts,
    exec, opting out, and update-ca-certificates in a build.
    """
    fetch = CLIENTS["debian"]
    before = listing(rootfs)

    facts = container("debian-read-only", rootfs, fetch, readonly=True)
    report("debian: read-only container", **facts)

    sidecar_bundle = {
        "destination": "/etc/ssl/certs/ca-certificates.crt",
        "type": "bind",
        "source": "/etc/ssl/certs/ca-certificates.crt",
        "options": ["rbind", "ro"],
    }
    facts = container("debian-control", rootfs, ["sh", "-c", f"echo SSL_CERT_FILE=$SSL_CERT_FILE; {' '.join(fetch)}"])
    report("debian: no mounts of its own (control)", **facts)
    facts = container(
        "debian-own-file",
        rootfs,
        ["sh", "-c", f"echo SSL_CERT_FILE=$SSL_CERT_FILE; {' '.join(fetch)}"],
        mounts=[sidecar_bundle],
    )
    report("debian: its own bundle mounted", **facts)

    folder = {"destination": "/etc/ssl/certs", "type": "tmpfs", "source": "tmpfs", "options": ["nosuid", "nodev"]}
    facts = container("debian-own-folder", rootfs, ["sh", "-c", "ls -A /etc/ssl/certs; echo listed"], mounts=[folder])
    report("debian: its own folder mounted", **facts, changed=differences(before, listing(rootfs)))

    facts = container(
        "debian-exec",
        rootfs,
        ["sleep", "60"],
        exec_args=[
            "sh",
            "-c",
            f"echo SSL_CERT_FILE=$SSL_CERT_FILE NODE_EXTRA_CA_CERTS=$NODE_EXTRA_CA_CERTS; {' '.join(fetch)}",
        ],
    )
    report("debian: exec", **facts)

    facts = container("debian-opt-out", rootfs, fetch, env=["HARDCODE_PROXY_CA=off"])
    report("debian: opted out", **facts)

    # A Dockerfile's COPY of a company CA, then RUN update-ca-certificates.
    shutil.copy(TLS / "company.crt", rootfs / "usr/local/share/ca-certificates/company.crt")
    facts = step("debian-update-ca", rootfs, ["update-ca-certificates"])
    bundle = rootfs / DEBIAN_BUNDLE
    report(
        "debian: update-ca-certificates in a step",
        **facts,
        company=fingerprint_in(bundle, TLS / "company.crt"),
        proxy=fingerprint_in(bundle, PROXY_CA / "ca.pem"),
        leftovers=leftovers(rootfs),
    )

    facts = step("debian-after-update", rootfs, fetch)
    report("debian: the next step", **facts, proxy=fingerprint_in(bundle, PROXY_CA / "ca.pem"))


def temurin_cases(rootfs: Path) -> None:
    """
    Java's proxy settings: off by default, on with HARDCODE_JAVA_PROXY=true
    in the wrapper's (the daemon's) environment.
    """
    show = ["sh", "-c", "echo JAVA_TOOL_OPTIONS=$JAVA_TOOL_OPTIONS; /opt/java/openjdk/bin/java -version 2>&1 | head -2"]
    proxied = ["HTTPS_PROXY=http://proxy.test:3128", "NO_PROXY=localhost,docker"]

    facts = container("temurin-proxy-off", rootfs, show, env=proxied)
    report("temurin: Java proxy, default", **facts)

    switched_on = {**os.environ, "HARDCODE_JAVA_PROXY": "true"}
    facts = container("temurin-proxy-on", rootfs, show, env=proxied, environment=switched_on)
    report("temurin: Java proxy, switched on", **facts)


def symlink_cases(alpine: Path) -> None:
    """
    An image whose bundle paths are a symlink to a file outside it (by an
    absolute path and by "../"), a FIFO and a file too large: the wrapper
    must neither read nor write outside the image, nor hang.
    """
    rootfs = busybox_rootfs("symlinks", alpine)
    certificates = rootfs / "etc/ssl/certs"
    certificates.mkdir(parents=True)
    (certificates / "ca-certificates.crt").symlink_to(SECRET)
    (rootfs / "etc/ssl/cert.pem").symlink_to("../../../../../../../.." + str(SECRET))
    (rootfs / "etc/pki/tls/certs").mkdir(parents=True)
    os.mkfifo(rootfs / "etc/pki/tls/certs/ca-bundle.crt")
    with open(rootfs / "etc/ssl/ca-bundle.pem", "wb") as large:
        large.truncate((10 << 20) + 1)

    SECRET.write_bytes((PROXY_CA / "ca.pem").read_bytes().replace(b"CERTIFICATE", b"SECRET"))
    secret = hashlib.sha256(SECRET.read_bytes()).hexdigest()
    show = [
        "busybox",
        "sh",
        "-c",
        "busybox grep -c -e /etc/ssl -e /etc/pki /proc/self/mountinfo; echo SSL_CERT_FILE=$SSL_CERT_FILE",
    ]

    before = listing(rootfs)
    for kind, run_case in (("step", step), ("container", container)):
        facts = run_case(f"symlinks-{kind}", rootfs, show)
        report(
            f"symlinks: {kind}",
            **facts,
            secret_unchanged=hashlib.sha256(SECRET.read_bytes()).hexdigest() == secret,
            changed=differences(before, listing(rootfs)),
        )


def scratch_cases(alpine: Path) -> None:
    """
    An image with no CA bundle at all: nothing added, nothing created.
    """
    rootfs = busybox_rootfs("scratch", alpine)
    show = ["busybox", "sh", "-c", "echo SSL_CERT_FILE=$SSL_CERT_FILE; busybox ls /"]

    before = listing(rootfs)
    for kind, run_case in (("step", step), ("container", container)):
        facts = run_case(f"scratch-{kind}", rootfs, show)
        report(f"scratch: {kind}", **facts, changed=differences(before, listing(rootfs)))


def main() -> int:
    serve()
    STATE.mkdir(parents=True, exist_ok=True)

    names = [name for name in CLIENTS if (IMAGES / f"{name}.tar").exists()]
    for name in names:
        rootfs = extract(name)
        image_cases(name, rootfs)

        if name == "debian":
            debian_cases(rootfs)
        if name == "temurin":
            temurin_cases(rootfs)
        if name == "alpine":
            symlink_cases(rootfs)
            scratch_cases(rootfs)

        if name != "alpine":
            shutil.rmtree(rootfs.parent)

    report("done", images=names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
