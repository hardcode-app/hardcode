#!/usr/bin/env python3
"""
The Docker sidecar (images/docker): Docker's own rootless image, with
HardCode's runc wrapper on top, which gives every container and every
build step the profile's proxy CA.

    tests/docker.py                                    build the image and test it
    DOCKER_SIDECAR_IMAGE=name tests/docker.py          test an image you built yourself

1. The wrapper's unit tests (images/docker/test_hardcode_runc.py), on the
   build image's Python and on the sidecar's own (Alpine's, musl).
2. The image: Docker's, as it is (the same entrypoint, user, daemon, runc
   and rootlesskit as its base), with the wrapper first on PATH and
   Docker's runc where the wrapper runs it; compose.yaml running it with
   the settings below.
3. The wrapper with the real runc, on real images, without a daemon
   (tests/fixtures/docker/runc_cases.py, in the image): Alpine, Debian,
   UBI, distroless and Temurin, as build steps (runc run) and as
   containers (runc create); an image with no CA bundle, and one whose
   bundle paths are hostile symlinks, a FIFO and a huge file; read-only
   containers, the container's own mounts, exec, opting out,
   update-ca-certificates in a build, and Java's proxy settings.
4. The daemon: the sidecar started as compose.yaml starts it, the same
   images loaded into it, and docker run, exec and build there. This
   needs a Docker daemon that can run rootless Docker in a container. A
   rootless daemon can't (the kernel refuses the nested user namespace's
   id map, for Docker's own image as for this one): there the section says
   so and is skipped.

The images are pinned by digest, and pulled when they are missing.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import tarfile
from pathlib import Path

import harness
from harness import FIXTURES, ROOT, Docker, Suite

SIDECAR_IMAGE = os.environ.get("DOCKER_SIDECAR_IMAGE", "hardcode-docker:test")
CONTEXT = ROOT / "images" / "docker"
CASES = FIXTURES / "docker"

# The images the cases run, by the names the fixture knows them by.
IMAGES = {
    "alpine": "node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914",
    "debian": "python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1",
    "ubi": "redhat/ubi9-minimal:9.8@sha256:dfa0eb04abc59221544e74862432496add1f491f915c9b531880f91affc0e12c",
    "distroless": "gcr.io/distroless/python3-debian12@sha256:2fdb05402a2cf21cf78fdb3ba4c5db167241e9e498140f5bf689d7efb773731f",
    "temurin": "eclipse-temurin:21-jdk-noble@sha256:114cabb73033ca4ce7ceec3deb62396bc1b716282bae051ed3c1d82e38fcab2d",
}

# The sidecar's settings, as compose.yaml's docker service has them (section
# 2 checks that it still does). DOCKER_APPARMOR_PROFILE, as in a profile's
# .env, for hosts without Ubuntu's rootlesskit profile.
APPARMOR = os.environ.get("DOCKER_APPARMOR_PROFILE", "rootlesskit")
SECURITY = [
    "--security-opt",
    f"apparmor={APPARMOR}",
    "--security-opt",
    "seccomp=unconfined",
    "--security-opt",
    "systempaths=unconfined",
    "--device",
    "/dev/net/tun",
]
COMPOSE_SETTINGS = [
    "build: ./images/docker",
    "apparmor=${DOCKER_APPARMOR_PROFILE:-rootlesskit}",
    "seccomp=unconfined",
    "systempaths=unconfined",
    "/dev/net/tun",
    "docker-data:/home/rootless/.local/share/docker",
    "proxy-ca-pub:/proxy-ca:ro",
]

# The daemon's socket inside the sidecar, for its own docker CLI.
SOCKET = "DOCKER_HOST=unix:///run/user/1000/docker.sock"

# What each image's containers fetch the origin with, in section 4.
ORIGIN = "https://origin/ok"
CLIENTS = {
    "alpine": ["node", "/hardcode-test/fetch.js", ORIGIN],
    "debian": ["python3", "/hardcode-test/fetch.py", ORIGIN],
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
        ORIGIN,
    ],
    "distroless": ["/usr/bin/python3", "/hardcode-test/fetch.py", ORIGIN],
    "temurin": ["/opt/java/openjdk/bin/java", "-XX:-UsePerfData", "/hardcode-test/Fetch.java", ORIGIN],
}

# A docker create'd container's files that aren't the image's: its init
# layer's mount points, made afresh for each container.
MOUNT_POINTS = {
    ".dockerenv",
    "etc/hostname",
    "etc/hosts",
    "etc/resolv.conf",
    "etc/mtab",
    "dev/console",
    "dev/pts",
    "dev/shm",
}


def wrapper_constant(name: str) -> str:
    """
    A string constant of the wrapper's, as its source sets it.
    """
    text = (CONTEXT / "hardcode_runc.py").read_text(encoding="utf-8")
    match = re.search(rf'^{name} = "([^"]*)"$', text, re.MULTILINE)
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# 1 and 2: the unit tests and the image
# ---------------------------------------------------------------------------


def base_image() -> str:
    """
    The image images/docker/Dockerfile starts from: Docker's.
    """
    dockerfile = (CONTEXT / "Dockerfile").read_text(encoding="utf-8")
    return re.search(r"^FROM (\S+)", dockerfile, re.MULTILINE).group(1)


def image_config(reference: str) -> dict:
    """
    An image's configuration, as the daemon reports it.
    """
    inspected = harness.run(["docker", "image", "inspect", "--format", "{{json .Config}}", reference])
    return json.loads(inspected.text) if inspected.ok else {}


def check_image(suite: Suite, docker: Docker) -> None:
    """
    Build the image (unless DOCKER_SIDECAR_IMAGE names one already built)
    and check how it is put together: Docker's image, with the wrapper
    added and nothing else of it changed.
    """
    suite.section(f"the image ({SIDECAR_IMAGE})")

    if "DOCKER_SIDECAR_IMAGE" not in os.environ:
        built = docker.build(SIDECAR_IMAGE, CONTEXT)
        suite.require("it builds from images/docker", built.ok, built.tail(30))

    base = base_image()
    if not harness.run(["docker", "image", "inspect", base]).ok:
        pulled = harness.run(["docker", "pull", "--quiet", base], timeout=1200)
        suite.require("its base, Docker's image, is pulled", pulled.ok, pulled.tail(10))

    ours, theirs = image_config(SIDECAR_IMAGE), image_config(base)
    kept = ("Entrypoint", "Cmd", "User", "Env", "Volumes", "ExposedPorts", "WorkingDir")
    changed = [key for key in kept if ours.get(key) != theirs.get(key)]
    suite.check(
        "its entrypoint, user, environment and volumes are Docker's image's, as they are",
        bool(ours) and not changed,
        "\n".join(f"{key}: {theirs.get(key)} -> {ours.get(key)}" for key in changed),
    )

    # Docker's own files: the same bytes in both images. (Its containerd is
    # inside dockerd; the shim that runs runc for it is a file of its own.)
    files = " ".join(
        f"/usr/local/bin/{name}"
        for name in (
            "dockerd",
            "containerd-shim-runc-v2",
            "runc",
            "docker-init",
            "rootlesskit",
            "dockerd-entrypoint.sh",
        )
    )
    sums = [
        docker.run(image, "-c", f"sha256sum {files}", memory="128m", entrypoint="sh") for image in (SIDECAR_IMAGE, base)
    ]
    suite.check(
        "its daemon, containerd's shim, runc, rootlesskit and entrypoint are Docker's, byte for byte",
        sums[0].ok and sums[0].text == sums[1].text,
        sums[0].text + sums[1].text,
    )

    real = wrapper_constant("REAL_RUNC")
    script = f"command -v runc; {real} --version | head -1; runc --version | head -1; id -un; grep -c '^rootless:' /etc/subuid"
    inside = docker.run(SIDECAR_IMAGE, "-c", script, memory="128m", entrypoint="sh")
    lines = inside.text.splitlines() + [""] * 5
    suite.check(
        f"runc on PATH is the wrapper (/usr/local/sbin/runc), and Docker's runc is at {real}, where it runs it",
        inside.ok
        and lines[0] == "/usr/local/sbin/runc"
        and lines[1].startswith("runc version")
        and lines[2] == lines[1],
        inside.text,
    )
    suite.check(
        "the daemon runs as the rootless user, with subordinate ids", lines[3:5] == ["rootless", "1"], inside.text
    )

    # BuildKit inside dockerd runs RUN steps with "runc" from PATH itself; its
    # override for that (a testing variable) lives in the same lines of code.
    # The real check is section 4's build; this one runs everywhere.
    lookup = docker.run(
        SIDECAR_IMAGE,
        "-c",
        "grep -c DOCKER_BUILDKIT_RUNC_COMMAND /usr/local/bin/dockerd",
        memory="256m",
        entrypoint="sh",
    )
    suite.check(
        "Docker's dockerd still has BuildKit's own runc lookup (DOCKER_BUILDKIT_RUNC_COMMAND)",
        lookup.ok and lookup.text.strip() != "0",
        lookup.text,
    )

    # The wrapper's openat2 and prctl go through ctypes to the C library:
    # musl here, glibc where section 1 ran them.
    musl = harness.run(
        [
            "docker",
            "run",
            "--rm",
            "--memory",
            "256m",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--volume",
            f"{CONTEXT}:/src:ro",
            "--entrypoint",
            "python3",
            SIDECAR_IMAGE,
            "-m",
            "unittest",
            "discover",
            "-s",
            "/src",
            "-t",
            "/src",
        ]
    )
    ran = re.search(r"^Ran (\d+) tests?", musl.text, re.MULTILINE)
    suite.check(
        f"the wrapper's unit tests on the image's own Python (Alpine, musl): {ran.group(1) if ran else '?'} tests",
        musl.ok,
        musl.tail(30),
    )

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    service = compose[compose.index("\n  docker:\n") :]
    service = service[: service.index("\n  # One image per profile")]
    missing = [setting for setting in COMPOSE_SETTINGS if setting not in service]
    suite.check(
        "compose.yaml's docker service builds it and runs it as this suite does", not missing, "\n".join(missing)
    )


# ---------------------------------------------------------------------------
# 3: the wrapper with the real runc
# ---------------------------------------------------------------------------


def ensure_images(suite: Suite) -> None:
    """
    Pull the images the cases run, where they are missing.
    """
    for name, reference in IMAGES.items():
        if harness.run(["docker", "image", "inspect", reference]).ok:
            continue

        pulled = harness.run(["docker", "pull", "--quiet", reference], timeout=1200)
        suite.require(f"{name}'s image ({reference.split('@')[0]}) is pulled", pulled.ok, pulled.tail(10))


def make_certificates(suite: Suite, docker: Docker, scratch: Path) -> tuple[Path, Path]:
    """
    The test CA and the origin's certificate (tests/fixtures/docker/
    make_certificates.sh), and a /proxy-ca folder with only the CA in it,
    as the pki service publishes it.
    """
    tls = scratch / "tls"
    tls.mkdir()

    made = docker.run(
        harness.BUILD_IMAGE,
        "/fixtures/make_certificates.sh",
        "/tls",
        memory="128m",
        user=harness.current_user(),
        volumes=[f"{CASES}:/fixtures:ro", f"{tls}:/tls"],
        entrypoint="bash",
    )
    suite.require("the test certificates are made", made.ok, made.tail(20))

    proxy_ca = scratch / "proxy-ca"
    proxy_ca.mkdir()
    (proxy_ca / "ca.pem").write_bytes((tls / "ca.pem").read_bytes())
    (proxy_ca / "ca.pem").chmod(0o644)

    return tls, proxy_ca


def export_images(suite: Suite, scratch: Path) -> Path:
    """
    Each image's root filesystem as a tar (docker export), readable by the
    sidecar's user.
    """
    images = scratch / "images"
    images.mkdir()

    for name, reference in IMAGES.items():
        created = harness.run(["docker", "create", reference, "true"])
        suite.require(f"{name}'s image gives a container to export", created.ok, created.text)

        container = created.text.strip().splitlines()[-1]
        exported = harness.run(["docker", "export", "--output", images / f"{name}.tar", container], timeout=600)
        harness.run(["docker", "rm", container])
        suite.require(f"{name}'s root filesystem is exported", exported.ok, exported.text)
        (images / f"{name}.tar").chmod(0o644)

    return images


def check_runc_cases(suite: Suite, docker: Docker, scratch: Path, tls: Path, proxy_ca: Path) -> None:
    """
    Run tests/fixtures/docker/runc_cases.py in the image and check what it
    saw, case by case.
    """
    images = export_images(suite, scratch)
    ran = harness.run(
        [
            "docker",
            "run",
            "--rm",
            "--memory",
            "2g",
            "--memory-swap",
            "2g",
            "--pids-limit",
            "2048",
            *SECURITY[:6],
            "--volume",
            f"{images}:/images:ro",
            "--volume",
            f"{proxy_ca}:/proxy-ca:ro",
            "--volume",
            f"{tls}:/tls:ro",
            "--volume",
            f"{CASES}:/fixtures:ro",
            "--entrypoint",
            "python3",
            SIDECAR_IMAGE,
            "/fixtures/runc_cases.py",
        ],
        timeout=1800,
    )

    cases = {}
    for line in ran.text.splitlines():
        if line.startswith("{"):
            facts = json.loads(line)
            cases[facts.pop("case")] = facts

    suite.require(
        "the cases ran, on every image",
        ran.ok and cases.get("done", {}).get("images") == list(IMAGES),
        ran.tail(40),
    )

    def case(name: str) -> dict:
        return cases.get(name, {})

    def detail(name: str) -> str:
        return json.dumps(case(name), indent=2)

    for name in IMAGES:
        step, container = case(f"{name}: step"), case(f"{name}: container")
        bundles = [copy for copy in container.get("copies", []) if copy.startswith("hardcode-ca/bundle-")]

        suite.check(
            f"{name}: a build step trusts the proxy's CA",
            step.get("exit") == 0 and "status 200" in step.get("output", ""),
            detail(f"{name}: step"),
        )
        suite.check(
            f"{name}: the step leaves the image exactly as it was",
            step.get("changed") == [] and step.get("leftovers") == [],
            detail(f"{name}: step"),
        )
        suite.check(
            f"{name}: a container trusts it, through one merged copy in its bundle folder",
            container.get("created") == 0 and "status 200" in container.get("output", "") and len(bundles) == 1,
            detail(f"{name}: container"),
        )
        suite.check(
            f"{name}: the container leaves the image as it was",
            container.get("changed") == [] and container.get("leftovers") == [],
            detail(f"{name}: container"),
        )

    suite.check(
        "temurin: Java's trust store is covered by one made from the merged bundle",
        "hardcode-ca/java-cacerts.p12" in case("temurin: container").get("copies", []),
        detail("temurin: container"),
    )

    suite.check(
        "a read-only container",
        "status 200" in case("debian: read-only container").get("output", ""),
        detail("debian: read-only container"),
    )
    suite.check(
        "SSL_CERT_FILE names the bundle when the container doesn't set it",
        "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt\nstatus 200"
        in case("debian: no mounts of its own (control)").get("output", ""),
        detail("debian: no mounts of its own (control)"),
    )
    own = case("debian: its own bundle mounted")
    suite.check(
        "a container's own mount over the bundle is left alone",
        own.get("output", "").startswith("SSL_CERT_FILE=\nfailed:")
        and "hardcode-ca/bundle-0.pem" not in own.get("copies", []),
        detail("debian: its own bundle mounted"),
    )
    folder = case("debian: its own folder mounted")
    suite.check(
        "a container's own mount over the bundle's folder is left alone, with nothing created in it",
        folder.get("output") == "listed\n" and folder.get("changed") == [],
        detail("debian: its own folder mounted"),
    )
    executed = case("debian: exec")
    suite.check(
        "exec gets the variables, and trusts the CA",
        executed.get("exec_exit") == 0
        and "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt"
        in executed.get("exec_output", "")
        and "status 200" in executed.get("exec_output", ""),
        detail("debian: exec"),
    )
    opted_out = case("debian: opted out")
    suite.check(
        "HARDCODE_PROXY_CA=off leaves a container alone",
        opted_out.get("copies") == [] and "failed:" in opted_out.get("output", ""),
        detail("debian: opted out"),
    )

    updated = case("debian: update-ca-certificates in a step")
    suite.check(
        "update-ca-certificates in a step works, and its company CA stays in the image without the proxy's",
        updated.get("exit") == 0
        and updated.get("company") is True
        and updated.get("proxy") is False
        and updated.get("leftovers") == [],
        detail("debian: update-ca-certificates in a step"),
    )
    after = case("debian: the next step")
    suite.check(
        "the next step trusts the proxy's CA again, and leaves it out of the image",
        after.get("exit") == 0 and "status 200" in after.get("output", "") and after.get("proxy") is False,
        detail("debian: the next step"),
    )

    for kind in ("step", "container"):
        symlinks = case(f"symlinks: {kind}")
        suite.check(
            f"hostile bundle paths (symlinks out of the image, a FIFO, a huge file): a {kind} gets nothing, "
            "and nothing outside is read or written",
            symlinks.get("timed_out") is False
            and symlinks.get("output") == "0\nSSL_CERT_FILE=\n"
            and symlinks.get("secret_unchanged") is True
            and symlinks.get("changed") == [],
            detail(f"symlinks: {kind}"),
        )

        scratch_case = case(f"scratch: {kind}")
        suite.check(
            f"an image with no CA bundle: a {kind} gets nothing, and nothing is created in it",
            scratch_case.get("output", "").startswith("SSL_CERT_FILE=\nbin\n")
            and "etc" not in scratch_case.get("output", "").split()
            and scratch_case.get("changed") == [],
            detail(f"scratch: {kind}"),
        )

    off, on = case("temurin: Java proxy, default"), case("temurin: Java proxy, switched on")
    suite.check(
        "Java's proxy settings are off by default",
        off.get("output", "").startswith("JAVA_TOOL_OPTIONS=\nopenjdk version"),
        detail("temurin: Java proxy, default"),
    )
    suite.check(
        "with HARDCODE_JAVA_PROXY=true, JAVA_TOOL_OPTIONS follows the container's proxy",
        "JAVA_TOOL_OPTIONS=-Dhttps.proxyHost=proxy.test -Dhttps.proxyPort=3128" in on.get("output", "")
        and "-Dhttp.nonProxyHosts=localhost|docker" in on.get("output", "")
        and "openjdk version" in on.get("output", ""),
        detail("temurin: Java proxy, switched on"),
    )


# ---------------------------------------------------------------------------
# 4: the daemon
# ---------------------------------------------------------------------------


class Sidecar:
    """
    The sidecar, started as compose.yaml starts it, on the suite's network,
    and its daemon, driven with its own docker CLI.
    """

    def __init__(self, name: str, volume: str, mounts: list[str]):
        self.name = name
        self.volume = volume
        self.mounts = mounts

    def start(self, suite: Suite, network: str, environment: tuple[str, ...] = ()) -> None:
        """
        Start it (again: its images stay in the volume) and wait for its daemon.
        """
        harness.run(["docker", "rm", "--force", self.name])

        arguments = ["docker", "run", "--detach", "--name", self.name, "--network", network, "--hostname", "docker"]
        arguments += ["--memory", "3g", "--memory-swap", "3g", "--pids-limit", "2048", *SECURITY]
        arguments += ["--env", "DOCKER_TLS_CERTDIR=/certs", "--env", "DOCKER_TLS_SAN=DNS:docker"]
        for variable in environment:
            arguments += ["--env", variable]
        arguments += ["--volume", f"{self.volume}:/home/rootless/.local/share/docker"]
        for mount in self.mounts:
            arguments += ["--volume", mount]

        started = harness.run([*arguments, SIDECAR_IMAGE])
        suite.require("the sidecar starts", started.ok, started.text)

        ready = harness.wait_until(lambda: self.docker("info").ok, timeout=180, interval=2)
        logs = harness.run(["docker", "logs", "--tail", "30", self.name]).text
        suite.require("its daemon answers", ready, logs)

    def docker(self, *arguments: object, input: str = None, timeout: float = 600) -> harness.Output:
        """
        The sidecar's own docker CLI, on its daemon's socket; *input*, when
        given, on its stdin.
        """
        interactive = ["--interactive"] if input is not None else []
        command = ["docker", "exec", *interactive, self.name, "env", SOCKET, "docker", *arguments]
        return harness.run(command, input=input, timeout=timeout)

    def inside(self, script: str) -> harness.Output:
        """
        A shell script in the daemon's own namespaces (rootlesskit's), where
        its bundle folders are.
        """
        dockerd = (
            'pid=$(for p in /proc/[0-9]*; do [ "$(cat $p/comm 2>/dev/null)" = dockerd ] && basename $p; done | head -1); '
            'exec nsenter -U --preserve-credentials -m -t "$pid" sh -c "$0"'
        )
        return harness.run(["docker", "exec", self.name, "sh", "-c", dockerd, script])

    def load(self, reference: str, tag: str) -> bool:
        """
        Copy an image from the host's daemon into this one, as *tag*.
        """
        harness.run(["docker", "tag", reference, tag])
        save = subprocess.Popen(["docker", "save", tag], stdout=subprocess.PIPE)
        loaded = subprocess.run(
            ["docker", "exec", "--interactive", self.name, "env", SOCKET, "docker", "load", "--quiet"],
            stdin=save.stdout,
            capture_output=True,
        )
        save.stdout.close()
        saved = save.wait()
        harness.run(["docker", "rmi", tag])

        return saved == 0 and loaded.returncode == 0

    def files(self, image: str) -> dict[str, tuple]:
        """
        Every file of *image*'s filesystem (docker create, then export: no
        runtime is involved), with its kind, size, mode, modification time
        and, for regular files up to 10 MiB, its content.
        """
        created = self.docker("create", image, "true")
        container = created.text.strip().splitlines()[-1]

        exported = subprocess.Popen(
            ["docker", "exec", self.name, "env", SOCKET, "docker", "export", container],
            stdout=subprocess.PIPE,
        )
        found = {}
        with tarfile.open(fileobj=exported.stdout, mode="r|") as archive:
            for member in archive:
                name = member.name.removeprefix("./").lstrip("/")
                content = None
                if member.isfile() and member.size <= 10 << 20:
                    content = archive.extractfile(member).read()
                found[name] = (member.type, member.size, member.mode, int(member.mtime), content)
        exported.wait()

        self.docker("rm", container)
        return found


def certificate_line(path: Path) -> bytes:
    """
    A PEM certificate's first base64 line: enough to find it in a file.
    """
    return path.read_bytes().splitlines()[1]


def differences(base: dict, built: dict) -> list[str]:
    """
    What a build changed in its base image's files, a line each, leaving
    out the mount points docker create adds to both.

    A folder is compared by its type and mode alone: its time changes
    whenever something in it does (the mount points, made at each docker
    create, change dev's and etc's), and what is in it is compared anyway.
    """
    changed = []

    for name in sorted(set(base) | set(built)):
        if name in MOUNT_POINTS:
            continue
        if name not in built:
            changed.append(f"removed {name}")
            continue
        if name not in base:
            changed.append(f"added {name}")
            continue

        before = base[name]
        after = built[name]
        if before[0] == tarfile.DIRTYPE and after[0] == tarfile.DIRTYPE:
            before = (before[0], before[2])
            after = (after[0], after[2])
        if before != after:
            changed.append(f"changed {name}")

    return changed


def holding(files: dict, needle: bytes) -> list[str]:
    """
    The files whose content holds *needle*.
    """
    return [name for name, entry in files.items() if entry[4] is not None and needle in entry[4]]


def check_daemon(suite: Suite, docker: Docker, scratch: Path, tls: Path, proxy_ca: Path) -> None:
    """
    The sidecar's daemon for real: containers, exec and builds, on the same
    images, against an origin whose certificate is from the test CA.
    """
    suite.section("the daemon: docker run, exec and build in the sidecar")

    options = harness.run(["docker", "info", "--format", "{{json .SecurityOptions}}"]).text
    if "name=rootless" in options:
        suite.note(
            "skipped: this Docker daemon is rootless, and a rootless daemon can't run another in a\n"
            "container (newuidmap is refused). Run this suite on a rootful daemon, as CI does."
        )
        return

    network = f"hc-docker-{secrets.token_hex(3)}"
    created = docker.create_network(network, "--internal")
    suite.require("the suite's network is created", created.ok, created.text)

    docker.run(
        harness.BUILD_IMAGE,
        "python3",
        "/origin.py",
        memory="128m",
        name=f"{network}-origin",
        detach=True,
        network=network,
        aliases=["origin"],
        volumes=[f"{FIXTURES / 'proxy_origin.py'}:/origin.py:ro", f"{tls}:/tls:ro"],
    )
    address = harness.run(
        [
            "docker",
            "inspect",
            "--format",
            f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
            f"{network}-origin",
        ]
    ).text.strip()
    suite.require("the origin has an address", bool(re.fullmatch(r"[0-9.]+", address)), address)

    # The builds' context: the clients and the company CA.
    context = scratch / "context"
    context.mkdir()
    for client in ("fetch.py", "Fetch.java", "fetch.js"):
        (context / client).write_bytes((CASES / client).read_bytes())
    (context / "company.crt").write_bytes((tls / "company.crt").read_bytes())
    for path in context.iterdir():
        path.chmod(0o644)

    volume = f"{network}-data"
    sidecar = Sidecar(f"{network}-docker", volume, [f"{proxy_ca}:/proxy-ca:ro", f"{context}:/context:ro"])
    host = ["--add-host", f"origin:{address}"]

    try:
        sidecar.start(suite, network)

        for name, reference in IMAGES.items():
            suite.require(f"{name}'s image is loaded into it", sidecar.load(reference, f"hc-docker-test/{name}:latest"))

        proxy = certificate_line(proxy_ca / "ca.pem")
        for name in IMAGES:
            check_image_in_daemon(suite, sidecar, name, host, proxy)

        check_containers(suite, sidecar, host)
        check_update_ca_certificates(suite, sidecar, host, proxy, certificate_line(tls / "company.crt"))
        check_bundles_go(suite, sidecar)
        check_java_proxy(suite, sidecar, network)
    finally:
        harness.run(["docker", "rm", "--force", sidecar.name])
        harness.run(["docker", "volume", "rm", "--force", volume])


def check_image_in_daemon(suite: Suite, sidecar: Sidecar, name: str, host: list[str], proxy: bytes) -> None:
    """
    One image: docker run trusts the CA, a RUN step does too (moby runs it
    with runc from PATH, the wrapper), and the image it builds has nothing
    of the proxy's in it and nothing else changed.
    """
    image = f"hc-docker-test/{name}:latest"
    client = CLIENTS[name]

    # Without the image's entrypoint, as a RUN step runs it: distroless's is
    # python3, which would take the client's python3 for its script.
    ran = sidecar.docker(
        "run",
        "--rm",
        *host,
        "--volume",
        "/context:/hardcode-test:ro",
        "--entrypoint",
        "",
        image,
        *client,
    )
    suite.check(f"{name}: docker run trusts the proxy's CA", "status 200" in ran.text, ran.text)

    dockerfile = (
        f"FROM {image}\n"
        "ENV PYTHONDONTWRITEBYTECODE=1\n"
        f"RUN --mount=type=bind,source=.,target=/hardcode-test {json.dumps(client)}\n"
    )
    built_image = f"hc-docker-test/{name}-built:latest"
    built = sidecar.docker(
        "build",
        "--no-cache",
        "--progress",
        "plain",
        *host,
        "--tag",
        built_image,
        "--file",
        "-",
        "/context",
        input=dockerfile,
    )
    suite.check(
        f"{name}: a RUN step trusts it: the daemon's BuildKit ran the wrapper, runc from PATH",
        built.ok and "status 200" in built.text,
        built.tail(30),
    )
    if not built.ok:
        return

    base, result = sidecar.files(image), sidecar.files(built_image)
    leaked = holding(result, proxy) + holding(result, b"# HardCode proxy CA begin")
    suite.check(f"{name}: the built image holds nothing of the proxy's CA", not leaked, "\n".join(leaked))
    changed = differences(base, result)
    suite.check(f"{name}: and nothing else of the base image changed", not changed, "\n".join(changed[:40]))


def check_containers(suite: Suite, sidecar: Sidecar, host: list[str]) -> None:
    """
    A read-only container, the container's own mounts, exec, opting out.
    """
    image = "hc-docker-test/debian:latest"
    fetch = ["--volume", "/context:/hardcode-test:ro"]

    ran = sidecar.docker("run", "--rm", "--read-only", *host, *fetch, image, *CLIENTS["debian"])
    suite.check("a --read-only container trusts it", "status 200" in ran.text, ran.text)

    own = sidecar.docker(
        "run",
        "--rm",
        *host,
        *fetch,
        "--volume",
        "/etc/ssl/certs/ca-certificates.crt:/etc/ssl/certs/ca-certificates.crt:ro",
        image,
        "sh",
        "-c",
        f"echo SSL_CERT_FILE=$SSL_CERT_FILE; {' '.join(CLIENTS['debian'])}",
    )
    suite.check(
        "a container's own mount over the bundle is left alone",
        own.text.startswith("SSL_CERT_FILE=\nfailed:"),
        own.text,
    )

    volume = sidecar.docker(
        "run",
        "--rm",
        "--volume",
        "hc-certs:/etc/ssl/certs",
        image,
        "sh",
        "-c",
        "ls -A /etc/ssl/certs | grep -c hardcode || true",
    )
    sidecar.docker("volume", "rm", "hc-certs")
    suite.check(
        "a volume over the bundle's folder is left alone, with nothing added to it",
        volume.text.strip() == "0",
        volume.text,
    )

    opted_out = sidecar.docker(
        "run", "--rm", "--env", "HARDCODE_PROXY_CA=off", *host, *fetch, image, *CLIENTS["debian"]
    )
    suite.check("HARDCODE_PROXY_CA=off leaves a container alone", "failed:" in opted_out.text, opted_out.text)

    started = sidecar.docker("run", "--detach", "--name", "hc-exec", *host, *fetch, image, "sleep", "300")
    executed = sidecar.docker(
        "exec",
        "hc-exec",
        "sh",
        "-c",
        f"echo SSL_CERT_FILE=$SSL_CERT_FILE NODE_EXTRA_CA_CERTS=$NODE_EXTRA_CA_CERTS; {' '.join(CLIENTS['debian'])}",
    )
    suite.check(
        "docker exec gets the variables, and trusts the CA",
        started.ok
        and "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt"
        in executed.text
        and "status 200" in executed.text,
        started.text + executed.text,
    )

    copies = sidecar.inside("find /run/user/1000/docker -type d -name hardcode-ca")
    suite.check("a running container's copies are in its bundle folder", "/hardcode-ca" in copies.text, copies.text)
    sidecar.docker("rm", "--force", "hc-exec")


def check_update_ca_certificates(suite: Suite, sidecar: Sidecar, host: list[str], proxy: bytes, company: bytes) -> None:
    """
    A Dockerfile that adds a company CA with update-ca-certificates, then
    fetches in the next step.
    """
    dockerfile = (
        "FROM hc-docker-test/debian:latest\n"
        "ENV PYTHONDONTWRITEBYTECODE=1\n"
        "COPY company.crt /usr/local/share/ca-certificates/company.crt\n"
        "RUN update-ca-certificates\n"
        f"RUN --mount=type=bind,source=.,target=/hardcode-test {json.dumps(CLIENTS['debian'])}\n"
    )
    built = sidecar.docker(
        "build",
        "--no-cache",
        "--progress",
        "plain",
        *host,
        "--tag",
        "hc-docker-test/company:latest",
        "--file",
        "-",
        "/context",
        input=dockerfile,
    )
    suite.check(
        "update-ca-certificates in a build works, and the next step trusts the proxy's CA",
        built.ok and "status 200" in built.text,
        built.tail(30),
    )
    if not built.ok:
        return

    files = sidecar.files("hc-docker-test/company:latest")
    bundle = files.get("etc/ssl/certs/ca-certificates.crt", (None,) * 5)[4] or b""
    suite.check(
        "the company CA is in the image's bundle, and the proxy's CA nowhere in the image",
        company in bundle and not holding(files, proxy),
        "\n".join(holding(files, proxy)),
    )


def check_bundles_go(suite: Suite, sidecar: Sidecar) -> None:
    """
    With every container gone and the builds done, no copy is left: the
    bundle folders went, and the copies with them.
    """
    left = sidecar.inside(
        "find /run/user/1000/docker /home/rootless/.local/share/docker/buildkit -name hardcode-ca 2>/dev/null; true"
    )
    suite.check("the bundle folders, and the copies in them, are gone", left.ok and not left.text.strip(), left.text)


def check_java_proxy(suite: Suite, sidecar: Sidecar, network: str) -> None:
    """
    Java's proxy settings: off by default; on, in containers and steps,
    once the daemon runs with HARDCODE_JAVA_PROXY=true.
    """
    image = "hc-docker-test/temurin:latest"
    proxy = ["--env", "HTTPS_PROXY=http://proxy.test:3128"]
    show = ["sh", "-c", "echo JAVA_TOOL_OPTIONS=$JAVA_TOOL_OPTIONS"]

    off = sidecar.docker("run", "--rm", *proxy, image, *show)
    suite.check("Java's proxy settings are off by default", off.text.strip() == "JAVA_TOOL_OPTIONS=", off.text)

    sidecar.start(suite, network, ("HARDCODE_JAVA_PROXY=true",))

    on = sidecar.docker("run", "--rm", *proxy, image, *show)
    suite.check(
        "with HARDCODE_JAVA_PROXY=true, a container's JAVA_TOOL_OPTIONS follows its proxy",
        "-Dhttps.proxyHost=proxy.test -Dhttps.proxyPort=3128" in on.text,
        on.text,
    )

    dockerfile = f"FROM {image}\nRUN echo JAVA_TOOL_OPTIONS=$JAVA_TOOL_OPTIONS\n"
    built = sidecar.docker(
        "build",
        "--no-cache",
        "--progress",
        "plain",
        "--build-arg",
        "HTTPS_PROXY=http://proxy.test:3128",
        "--file",
        "-",
        "/context",
        input=dockerfile,
    )
    suite.check("and a build step's", "-Dhttps.proxyHost=proxy.test" in built.text, built.tail(20))


def body(suite: Suite) -> None:
    suite.section("the wrapper's unit tests")
    harness.run_unittests(suite, "images/docker/test_hardcode_runc.py", CONTEXT, CONTEXT)

    with Docker() as docker:
        check_image(suite, docker)

        suite.section("the wrapper with the real runc, on real images (no daemon)")
        ensure_images(suite)

        with harness.scratch_directory("hc-docker-") as scratch:
            tls, proxy_ca = make_certificates(suite, docker, scratch)
            check_runc_cases(suite, docker, scratch, tls, proxy_ca)
            check_daemon(suite, docker, scratch, tls, proxy_ca)


if __name__ == "__main__":
    harness.main("docker", body)
