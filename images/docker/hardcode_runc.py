#!/usr/bin/python3 -IS
"""
HardCode's runc: the proxy's CA for every container and build step that
the Docker daemon runs.

The daemon beside the VS Code container reaches the internet only through
squid, which inspects HTTPS with the profile's own CA. The containers it
runs, and the RUN steps of the images it builds, don't trust that CA by
themselves. This program is installed as ``runc`` ahead of the real one on
PATH, so that both reach it (containerd's shim for containers, the daemon's
BuildKit for build steps), and gives them the CA on the way through:

``runc create`` (``docker run``, Compose, Testcontainers)
    Over each CA bundle the image has, a copy of it with the proxy's CA
    added is mounted. The copies are kept in the container's bundle
    folder, which goes when the container does.

``runc run`` (a build's RUN step)
    The proxy's CA is added to the image's bundle in place for the step,
    and taken out again, by its fingerprint, before the layer is made. What
    the step does to the bundle itself (update-ca-certificates with a
    company CA) stays.

Both
    SSL_CERT_FILE and NODE_EXTRA_CA_CERTS name the bundle when the
    container doesn't set them. Every Java trust store (cacerts) is covered
    by one made from the same certificates. With HARDCODE_JAVA_PROXY=true
    in the daemon's environment, JAVA_TOOL_OPTIONS points Java at the
    container's HTTPS_PROXY too.

``runc exec`` (``docker exec``)
    The variables the container was given.

A container or step with HARDCODE_PROXY_CA=off in its environment is left
alone. Every other command, and anything this can't make sense of, goes to
the real runc untouched; a container this fails to help still starts.

The image's files are untrusted: they are only opened inside its root
filesystem (openat2's RESOLVE_IN_ROOT, so that a symlink can't lead out of
it), regular files only, of at most 10 MiB.

Standard library only.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import errno
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Optional

# The real runc: Docker's, in its image. This program is /usr/local/sbin/runc,
# ahead of it on PATH.
REAL_RUNC = "/usr/local/bin/runc"

# The proxy's CA, as the pki service publishes it.
PROXY_CA = "/proxy-ca/ca.pem"

# This image's own bundle: what a Java trust store is made from when the
# container's image has no bundle of its own.
OWN_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

# Where TLS stacks look for the system's CA bundle: Debian and Ubuntu,
# Alpine; Fedora, RHEL and UBI; openSUSE. Several are symlinks to one file,
# which is then covered once.
BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/pki/tls/cacert.pem",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/cert.pem",
)

# Java trust stores at fixed places (Debian's and Fedora's, made from the
# system's bundle), and the folders JDKs are installed in, with the names
# of the folders in them that are JDKs (none: every one).
JAVA_STORES = (
    "/etc/ssl/certs/java/cacerts",
    "/etc/pki/java/cacerts",
    "/etc/pki/ca-trust/extracted/java/cacerts",
)
JAVA_FOLDERS = (
    ("/usr/lib/jvm", ()),
    ("/opt/java", ()),
    ("/usr/local", ("openjdk", "jdk", "java")),
    ("/opt", ("openjdk", "jdk", "java", "graalvm", "zulu", "corretto")),
)
JAVA_STORE_IN_HOME = ("lib/security/cacerts", "jre/lib/security/cacerts")

# At most this many entries of each of those folders are looked into.
MAX_FOLDER_ENTRIES = 64

# A bundle or trust store larger than this is left alone.
MAX_BYTES = 10 << 20

# In a container's bundle folder: the copies, and the variables added (for
# ``runc exec``).
COPIES = "hardcode-ca"
ADDED_ENV = "env.json"

# The variable that leaves a container or step alone, and the daemon's
# variable that switches the Java proxy settings on.
OPT_OUT = "HARDCODE_PROXY_CA"
JAVA_PROXY = "HARDCODE_JAVA_PROXY"

# What marks the CA added to a bundle in place, to find it again.
BEGIN = b"# HardCode proxy CA begin\n"
END = b"# HardCode proxy CA end\n"

# A PEM certificate: its base64 body is the first group.
PEM_CERTIFICATE = re.compile(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.DOTALL)

# runc's options before the command: those that take a value, and the switches.
GLOBAL_VALUES = {"root", "log", "log-format", "criu", "rootless"}
GLOBAL_SWITCHES = {"debug", "systemd-cgroup"}

# The options of create and run, and of exec, with their one-letter names.
RUN_VALUES = {"bundle", "console-socket", "pid-file", "preserve-fds", "pidfd-socket"}
RUN_SWITCHES = {"detach", "keep", "no-pivot", "no-new-keyring", "no-subreaper"}
EXEC_VALUES = {
    "console",
    "console-socket",
    "pidfd-socket",
    "cwd",
    "env",
    "user",
    "additional-gids",
    "process",
    "pid-file",
    "process-label",
    "apparmor",
    "cap",
    "preserve-fds",
    "cgroup",
}
EXEC_SWITCHES = {"tty", "detach", "no-new-privs", "ignore-paused"}
SHORT_NAMES = {
    "b": "bundle",
    "d": "detach",
    "p": "process",
    "e": "env",
    "u": "user",
    "g": "additional-gids",
    "c": "cap",
}
SHORT_NAMES_EXEC = {**SHORT_NAMES, "t": "tty"}

# The signals passed on to the real runc while a build step runs.
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2)

# What can go wrong while preparing a container, short of a bug.
PREPARE_ERRORS = (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)

# openat2(2): its number (the same on every architecture) and flags; and
# prctl(2)'s option for a signal on the parent's death.
SYS_OPENAT2 = 437
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_IN_ROOT = 0x10
PR_SET_PDEATHSIG = 1


class Refused(Exception):
    """
    Something in the image this won't touch; the container goes without.
    """


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


@dataclass
class Command:
    """
    A runc command line, as far as this needs to read it.

    ``options`` are the command's own, by their long names (a switch's
    value is ""); ``positional`` its other arguments: the container's id,
    and for exec what to run.
    """

    global_options: list[str]
    name: str
    options: dict[str, str] = field(default_factory=dict)
    positional: list[str] = field(default_factory=list)


def _option(
    arguments: list[str],
    index: int,
    values: set[str],
    switches: set[str],
    short: dict[str, str],
) -> Optional[tuple[str, str, int]]:
    """
    The option at *index*: its long name, its value and how many arguments
    it took. ``None`` for one this doesn't know.

    runc takes ``--name value``, ``--name=value``, ``-n value`` and the
    same with one dash for long names (Go's flag package).
    """
    text = arguments[index].lstrip("-")
    name, equals, value = text.partition("=")
    name = short.get(name, name)

    if name in switches:
        return name, value, 1

    if name in values:
        if equals:
            return name, value, 1
        if index + 1 < len(arguments):
            return name, arguments[index + 1], 2

    return None


def parse(arguments: list[str]) -> Optional[Command]:
    """
    Read a runc command line. ``None`` when it has an option this doesn't
    know before the command, or no command: such a line is passed on as it
    is.

    Only create, run and exec have their options read; for any other
    command, only its name.
    """
    index = 0
    global_options: list[str] = []

    while index < len(arguments) and arguments[index].startswith("-"):
        option = _option(arguments, index, GLOBAL_VALUES, GLOBAL_SWITCHES, {})
        if option is None:
            return None

        used = option[2]
        global_options += arguments[index : index + used]
        index += used

    if index >= len(arguments):
        return None

    command = Command(global_options, arguments[index])
    index += 1

    if command.name in ("create", "run"):
        values, switches, short = RUN_VALUES, RUN_SWITCHES, SHORT_NAMES
    elif command.name == "exec":
        values, switches, short = EXEC_VALUES, EXEC_SWITCHES, SHORT_NAMES_EXEC
    else:
        return command

    # Options come before the container's id; for exec, everything after
    # the id is the command to run.
    while index < len(arguments):
        argument = arguments[index]

        if argument == "--":
            command.positional += arguments[index + 1 :]
            break

        if argument.startswith("-") and argument != "-":
            option = _option(arguments, index, values, switches, short)
            if option is None:
                return None

            name, value, used = option
            command.options[name] = value
            index += used
            continue

        command.positional += arguments[index:]
        break

    return command


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Authority:
    """
    The proxy's CA: its PEM text, and its certificates' SHA-256 fingerprints.
    """

    pem: bytes
    fingerprints: frozenset[bytes]


def certificates(content: bytes) -> list[bytes]:
    """
    The DER bytes of every PEM certificate in *content*; ones that aren't
    valid base64 are skipped.
    """
    found = []

    for match in PEM_CERTIFICATE.finditer(content):
        body = b"".join(match.group(1).split())
        try:
            found.append(base64.b64decode(body, validate=True))
        except (binascii.Error, ValueError):
            continue

    return found


def fingerprints(content: bytes) -> set[bytes]:
    """
    The SHA-256 fingerprints of the certificates in *content*.
    """
    return {hashlib.sha256(der).digest() for der in certificates(content)}


def read_authority(path: str = PROXY_CA) -> Optional[Authority]:
    """
    The proxy's CA, or ``None`` when there is none to give (no file, or no
    certificate in it).
    """
    try:
        with open(path, "rb") as file:
            pem = file.read(MAX_BYTES)
    except FileNotFoundError:
        return None

    found = fingerprints(pem)
    if not found:
        return None

    if not pem.endswith(b"\n"):
        pem += b"\n"

    return Authority(pem, frozenset(found))


def has_authority(content: bytes, authority: Authority) -> bool:
    """
    Whether *content* already has every certificate of the proxy's CA.
    """
    return authority.fingerprints <= fingerprints(content)


def block(content: bytes, authority: Authority) -> bytes:
    """
    What is appended to *content* to add the proxy's CA: the CA between
    markers, on a line of its own.
    """
    separator = b"\n" if content and not content.endswith(b"\n") else b""
    return separator + BEGIN + authority.pem + END


def merged(content: bytes, authority: Authority) -> bytes:
    """
    *content*, a CA bundle, with the proxy's CA added unless it has it.
    """
    if has_authority(content, authority):
        return content

    return content + block(content, authority)


def strip(content: bytes, authority: Authority) -> bytes:
    """
    *content* without the proxy's CA: the marked block when it is there
    and holds the CA, else every PEM certificate that is the CA's (a step
    may have rewritten the file and kept the certificate without the
    markers).
    """
    begin = content.find(BEGIN)
    if begin >= 0:
        end = content.find(END, begin + len(BEGIN))
        if end >= 0:
            end += len(END)
            if fingerprints(content[begin:end]) & authority.fingerprints:
                return content[:begin] + content[end:]

    kept = []
    position = 0
    for match in PEM_CERTIFICATE.finditer(content):
        if fingerprints(match.group(0)) & authority.fingerprints:
            kept.append(content[position : match.start()])
            position = match.end()
            if content[position : position + 1] == b"\n":
                position += 1

    kept.append(content[position:])
    return b"".join(kept)


def make_trust_store(certificates_pem: bytes, folder: str) -> str:
    """
    A Java trust store (PKCS#12) of the certificates in *certificates_pem*,
    written into *folder*; its path.

    OpenSSL marks each certificate trusted the way Java's keytool does
    (-jdktrust), and neither encrypts nor protects the file (no password,
    no MAC): Java opens its default trust store without a password, and a
    protected one would look empty to it.
    """
    source = os.path.join(folder, "java-certificates.pem")
    store = os.path.join(folder, "java-cacerts.p12")

    with open(source, "wb") as file:
        file.write(certificates_pem)

    made = subprocess.run(
        [
            "openssl",
            "pkcs12",
            "-export",
            "-nokeys",
            "-in",
            source,
            "-jdktrust",
            "anyExtendedKeyUsage",
            "-certpbe",
            "NONE",
            "-nomac",
            "-passout",
            "pass:",
            "-out",
            store,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    os.unlink(source)

    if made.returncode != 0:
        raise ValueError(f"openssl couldn't make a Java trust store: {made.stderr.decode(errors='replace').strip()}")

    os.chmod(store, 0o644)
    return store


# ---------------------------------------------------------------------------
# The image's root filesystem
# ---------------------------------------------------------------------------


class _OpenHow(ctypes.Structure):
    """
    openat2's ``struct open_how``.
    """

    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]


_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


@dataclass
class Found:
    """
    A regular file in an image: its path in the container with every symlink
    followed, what it is (device and inode), its status and its content.
    """

    path: str
    identity: tuple[int, int]
    status: os.stat_result
    content: bytes


class Root:
    """
    An image's root filesystem, opened so that nothing in it leads outside:
    every path is resolved as if it were the root (openat2's
    RESOLVE_IN_ROOT), symlinks included.
    """

    def __init__(self, path: str):
        self.fd = os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        self.real = os.readlink(f"/proc/self/fd/{self.fd}")

    def close(self) -> None:
        """
        Let go of the root filesystem.
        """
        os.close(self.fd)

    def open(self, path: str, flags: int) -> int:
        """
        A descriptor for *path* (absolute, in the container), resolved
        inside the root. Raises ``OSError`` as open(2) would.
        """
        how = _OpenHow(flags | os.O_CLOEXEC, 0, RESOLVE_IN_ROOT | RESOLVE_NO_MAGICLINKS)
        relative = path.lstrip("/").encode() or b"."

        fd = _libc.syscall(
            ctypes.c_long(SYS_OPENAT2),
            ctypes.c_int(self.fd),
            ctypes.c_char_p(relative),
            ctypes.byref(how),
            ctypes.c_size_t(ctypes.sizeof(how)),
        )
        if fd < 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number), path)

        return fd

    def path_of(self, fd: int) -> str:
        """
        Where a descriptor opened here is, in the container: the path with
        every symlink followed.
        """
        target = os.readlink(f"/proc/self/fd/{fd}")
        relative = os.path.relpath(target, self.real)
        if relative == ".." or relative.startswith("../"):
            raise Refused(f"{target} is outside the image")

        return "/" if relative == "." else "/" + relative

    def find(self, path: str) -> Optional[tuple[int, str, os.stat_result]]:
        """
        *path*, when it is a regular file no larger than MAX_BYTES: a
        descriptor that can only name it (O_PATH, so that opening it does
        nothing, as opening a FIFO or a device would), where it is, and its
        status. ``None`` for anything else.
        """
        try:
            fd = self.open(path, os.O_PATH)
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EACCES, errno.EXDEV):
                return None
            raise

        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_BYTES:
            os.close(fd)
            return None

        return fd, self.path_of(fd), status

    def oversized(self, path: str) -> bool:
        """
        Whether *path* is a regular file larger than MAX_BYTES.
        """
        try:
            fd = self.open(path, os.O_PATH)
        except OSError:
            return False

        try:
            status = os.fstat(fd)
        finally:
            os.close(fd)

        return stat.S_ISREG(status.st_mode) and status.st_size > MAX_BYTES

    def read(self, path: str) -> Optional[Found]:
        """
        *path*'s content, when it is a regular file no larger than MAX_BYTES.
        """
        located = self.find(path)
        if located is None:
            return None

        fd, where, status = located
        try:
            content = _read_through(fd)
        finally:
            os.close(fd)

        if content is None:
            return None

        return Found(where, (status.st_dev, status.st_ino), status, content)

    def write(self, path: str, content: bytes, append: bool, times: Optional[tuple[int, int]] = None) -> bool:
        """
        Append *content* to *path*, or replace what it holds with it, in
        place (the file keeps its inode, mode and owner); then set its
        access and modification times to *times* (nanoseconds), if given.
        False when *path* isn't a regular file any more.
        """
        located = self.find(path)
        if located is None:
            return False

        fd = located[0]
        flags = os.O_WRONLY | os.O_CLOEXEC | (os.O_APPEND if append else os.O_TRUNC)
        try:
            writable = os.open(f"/proc/self/fd/{fd}", flags)
        finally:
            os.close(fd)

        try:
            _write_all(writable, content)
            if times is not None:
                os.utime(writable, ns=times)
        finally:
            os.close(writable)

        return True

    def entries(self, path: str) -> list[str]:
        """
        The names in the folder *path*, at most MAX_FOLDER_ENTRIES of them,
        sorted; none when it isn't a folder.
        """
        try:
            fd = self.open(path, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return []

        try:
            return sorted(os.listdir(fd))[:MAX_FOLDER_ENTRIES]
        finally:
            os.close(fd)


def _read_through(fd: int) -> Optional[bytes]:
    """
    The content of the regular file an O_PATH descriptor names, at most
    MAX_BYTES; ``None`` when there is more.
    """
    readable = os.open(f"/proc/self/fd/{fd}", os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)

    try:
        chunks = []
        size = 0
        while size <= MAX_BYTES:
            chunk = os.read(readable, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(readable)

    if size > MAX_BYTES:
        return None

    return b"".join(chunks)


def _write_all(fd: int, content: bytes) -> None:
    """
    Write all of *content* to *fd*.
    """
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _write_file(path: str, content: bytes, mode: int) -> None:
    """
    A new file of ours at *path* (in a bundle folder), with *mode*.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        _write_all(fd, content)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def covered(path: str, destinations: list[str]) -> bool:
    """
    Whether one of the container's own mounts is at *path* or above it:
    then the image's file isn't what the container sees, and this leaves
    it to the mount.
    """
    return any(path == destination or path.startswith(destination.rstrip("/") + "/") for destination in destinations)


def find_files(root: Root, paths: list[str], destinations: list[str]) -> list[Found]:
    """
    Each regular file among *paths* in the image, once (symlinks to one
    file are one), leaving out those a mount of the container's covers.
    """
    found: list[Found] = []
    seen: set[tuple[int, int]] = set()

    for path in paths:
        if covered(path, destinations):
            continue

        file = root.read(path)
        if file is None or file.identity in seen or covered(file.path, destinations):
            continue

        seen.add(file.identity)
        found.append(file)

    return found


def java_store_paths(root: Root) -> list[str]:
    """
    Where the image may have Java trust stores: the fixed places, and
    cacerts in each JDK under the folders JDKs are installed in.
    """
    paths = list(JAVA_STORES)

    for folder, prefixes in JAVA_FOLDERS:
        for entry in root.entries(folder):
            if prefixes and not entry.startswith(prefixes):
                continue
            for store in JAVA_STORE_IN_HOME:
                paths.append(f"{folder}/{entry}/{store}")

    return paths


# ---------------------------------------------------------------------------
# Containers and build steps
# ---------------------------------------------------------------------------


def env_value(env: list[str], name: str) -> Optional[str]:
    """
    The value of *name* in an OCI environment list, ``None`` when unset.
    """
    prefix = name + "="
    for entry in env:
        if entry.startswith(prefix):
            return entry[len(prefix) :]

    return None


def java_non_proxy_hosts(no_proxy: str) -> str:
    """
    NO_PROXY ("localhost,::1,.example.com") as Java's http.nonProxyHosts
    ("localhost|[::1]|*.example.com"). Address ranges (10.0.0.0/8) have no
    equivalent there and are left out.
    """
    hosts = []

    for entry in no_proxy.split(","):
        entry = entry.strip()
        if not entry or "/" in entry:
            continue
        if entry.startswith("."):
            entry = "*" + entry
        elif ":" in entry and not entry.startswith("["):
            entry = f"[{entry}]"
        hosts.append(entry)

    return "|".join(hosts)


def java_options(env: list[str]) -> Optional[str]:
    """
    JAVA_TOOL_OPTIONS that send Java through the container's proxy
    (HTTPS_PROXY, or HTTP_PROXY) and around it for NO_PROXY's hosts;
    ``None`` without a proxy.
    """
    proxy = None
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        proxy = env_value(env, name)
        if proxy:
            break

    if not proxy:
        return None

    parts = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    if not parts.hostname:
        return None

    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    options = [
        f"-Dhttps.proxyHost={host}",
        f"-Dhttps.proxyPort={port}",
        f"-Dhttp.proxyHost={host}",
        f"-Dhttp.proxyPort={port}",
    ]

    no_proxy = env_value(env, "NO_PROXY") or env_value(env, "no_proxy") or ""
    hosts = java_non_proxy_hosts(no_proxy)
    if hosts:
        options.append(f"-Dhttp.nonProxyHosts={hosts}")

    return " ".join(options)


@dataclass
class Appended:
    """
    A bundle the proxy's CA was appended to, in place: what it held before,
    what was appended, and its access and modification times then.
    """

    path: str
    original: bytes
    added: bytes
    times: tuple[int, int]


@dataclass
class Changes:
    """
    What preparing a container did: the variables it added, the bundles it
    appended to (a build step's, undone afterwards), and the Java trust
    store a step was given with its content (a step mustn't change it).
    """

    env: list[str] = field(default_factory=list)
    appended: list[Appended] = field(default_factory=list)
    store: Optional[tuple[str, bytes]] = None
    root: Optional[Root] = None
    authority: Optional[Authority] = None


def _mount(spec: dict, destination: str, source: str) -> None:
    """
    Add a bind mount of the file *source* over *destination* to *spec*.
    """
    spec.setdefault("mounts", []).append(
        {
            "destination": destination,
            "type": "bind",
            "source": source,
            "options": ["bind", "nosuid", "nodev", "noexec"],
        }
    )


def prepare(
    spec: dict,
    bundle: str,
    in_place: bool,
    changes: Changes,
    authority: Optional[Authority],
    environment: dict[str, str],
    trust_store: Callable[[bytes, str], str] = make_trust_store,
    own_bundle: str = OWN_BUNDLE,
) -> None:
    """
    Give the container whose OCI *spec* (config.json) is in *bundle* the
    proxy's CA, recording what was done in *changes* as it goes.

    *in_place*: append the CA to the image's bundles, as for a build step,
    rather than mount copies over them. *environment* is this program's,
    where HARDCODE_JAVA_PROXY is read.
    """
    process = spec.setdefault("process", {})
    env = process.get("env") or []

    if authority is None or env_value(env, OPT_OUT) == "off":
        return

    rootfs = spec["root"]["path"]
    if not os.path.isabs(rootfs):
        rootfs = os.path.join(bundle, rootfs)

    # A read-only root filesystem is never written to, not even for a step.
    in_place = in_place and not spec["root"].get("readonly", False)

    destinations = [os.path.normpath(mount["destination"]) for mount in spec.get("mounts") or []]
    copies = os.path.join(bundle, COPIES)
    os.makedirs(copies, mode=0o700, exist_ok=True)

    root = Root(rootfs)
    changes.root = root
    changes.authority = authority

    bundles = find_files(root, list(BUNDLES), destinations)
    for number, found in enumerate(bundles):
        if not in_place:
            copy = os.path.join(copies, f"bundle-{number}.pem")
            _write_file(copy, merged(found.content, authority), stat.S_IMODE(found.status.st_mode) or 0o644)
            _mount(spec, found.path, copy)
            continue

        if has_authority(found.content, authority):
            continue

        added = block(found.content, authority)
        times = (found.status.st_atime_ns, found.status.st_mtime_ns)
        if root.write(found.path, added, append=True):
            changes.appended.append(Appended(found.path, found.content, added, times))

    if bundles:
        for name in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"):
            if env_value(env, name) is None:
                changes.env.append(f"{name}={bundles[0].path}")

    stores = find_files(root, java_store_paths(root), destinations)
    if stores:
        if bundles:
            base = bundles[0].content
        else:
            with open(own_bundle, "rb") as file:
                base = file.read(MAX_BYTES)

        store = trust_store(merged(base, authority), copies)
        for found in stores:
            _mount(spec, found.path, store)

        if in_place:
            with open(store, "rb") as file:
                changes.store = (store, file.read())

    if environment.get(JAVA_PROXY) == "true" and env_value(env, "JAVA_TOOL_OPTIONS") is None:
        options = java_options(env)
        if options:
            changes.env.append(f"JAVA_TOOL_OPTIONS={options}")

    process["env"] = env + changes.env
    with open(os.path.join(copies, ADDED_ENV), "w", encoding="utf-8") as file:
        json.dump(changes.env, file)


def finish(changes: Changes) -> list[str]:
    """
    Undo what preparing a build step did to its image, after the step:
    take the proxy's CA out of the bundles again, and check that the Java
    trust store it was given is as it was. What went wrong, as messages
    (the step then fails: its layer would be wrong).
    """
    problems = []
    root, authority = changes.root, changes.authority

    for appended in changes.appended:
        try:
            now = root.read(appended.path)
            if now is None and root.oversized(appended.path):
                problems.append(f"{appended.path} grew past {MAX_BYTES} bytes: the proxy's CA can't be taken out of it")
                continue
            if now is None:
                # The step replaced or removed the file: what is there isn't ours.
                continue

            if now.content == appended.original + appended.added:
                content = appended.original
            else:
                content = strip(now.content, authority)

            if content == now.content:
                continue

            times = appended.times if content == appended.original else None
            root.write(appended.path, content, append=False, times=times)
        except (OSError, Refused) as error:
            problems.append(f"couldn't take the proxy's CA out of {appended.path} again: {error}")

    if changes.store is not None:
        path, content = changes.store
        with open(path, "rb") as file:
            if file.read() != content:
                problems.append(
                    "this step changed a Java trust store that HardCode covered with the proxy's for the step, "
                    "and the change can't be kept; to change the image's own, run the step with "
                    f"{OPT_OUT}=off in its environment (ARG {OPT_OUT}=off before it)"
                )

    if root is not None:
        root.close()
        changes.root = None

    return problems


# ---------------------------------------------------------------------------
# Running the real runc
# ---------------------------------------------------------------------------


def warn(message: str) -> None:
    """
    Say something on runc's stderr (a build's output, a container's log).
    """
    print(f"hardcode-runc: {message}", file=sys.stderr, flush=True)


def pass_through(arguments: list[str]) -> None:
    """
    Become the real runc, with the same arguments. Doesn't return.
    """
    os.execv(REAL_RUNC, [REAL_RUNC, *arguments])


def run_real(arguments: list[str]) -> int:
    """
    Run the real runc as a child, passing it the signals that stop it, and
    return its exit code (128 + the signal's number when one ended it).

    BuildKit has runc killed when the daemon's thread that started it goes
    (its parent-death signal, which reaches this program); the child is
    given the same, so that it doesn't outlive this one.
    """

    def die_with_parent() -> None:
        _libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)

    # close_fds=False: runc may be handed more descriptors (--preserve-fds).
    child = subprocess.Popen([REAL_RUNC, *arguments], close_fds=False, preexec_fn=die_with_parent)

    def forward(number: int, frame: object) -> None:
        child.send_signal(number)

    previous = {number: signal.signal(number, forward) for number in FORWARDED_SIGNALS}
    try:
        code = child.wait()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)

    return code if code >= 0 else 128 - code


def _spec_path(command: Command) -> tuple[str, str]:
    """
    The bundle folder of a create or run, and its config.json.
    """
    bundle = os.path.abspath(command.options.get("bundle") or os.getcwd())
    return bundle, os.path.join(bundle, "config.json")


def _save(path: str, document: dict) -> None:
    """
    Write a JSON document (an OCI spec, a process) over *path*, whole.
    """
    scratch = f"{path}.hardcode"
    with open(scratch, "w", encoding="utf-8") as file:
        json.dump(document, file)
    os.replace(scratch, path)


def create(command: Command, arguments: list[str]) -> None:
    """
    ``runc create`` (and ``run --detach``): mount the copies, then become
    the real runc. Doesn't return.
    """
    changes = Changes()

    try:
        bundle, path = _spec_path(command)
        with open(path, encoding="utf-8") as file:
            spec = json.load(file)

        prepare(spec, bundle, False, changes, read_authority(), dict(os.environ))
        _save(path, spec)
    except (*PREPARE_ERRORS, Refused) as error:
        warn(f"this container goes without the proxy's CA: {error}")
    finally:
        if changes.root is not None:
            changes.root.close()

    pass_through(arguments)


def run_step(command: Command, arguments: list[str]) -> int:
    """
    ``runc run``, a build step: add the CA to the image, run the step, take
    the CA out again. The step fails if that can't be done.
    """
    changes = Changes()

    try:
        bundle, path = _spec_path(command)
        with open(path, encoding="utf-8") as file:
            spec = json.load(file)

        prepare(spec, bundle, True, changes, read_authority(), dict(os.environ))
        _save(path, spec)
    except (*PREPARE_ERRORS, Refused) as error:
        warn(f"this step goes without the proxy's CA: {error}")
        problems = finish(changes)
        if problems:
            for problem in problems:
                warn(problem)
            return 1

        changes = Changes()

    code = run_real(arguments)

    problems = finish(changes)
    for problem in problems:
        warn(problem)

    if problems and code == 0:
        return 1

    return code


def state(command: Command) -> dict:
    """
    ``runc state`` for the container *command* is about.
    """
    asked = subprocess.run(
        [REAL_RUNC, *command.global_options, "state", command.positional[0]],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    if asked.returncode != 0:
        raise ValueError(asked.stderr.decode(errors="replace").strip())

    return json.loads(asked.stdout)


def add_exec_env(process: dict, added: list[str]) -> bool:
    """
    Give an exec's *process* the variables its container was given, those
    it doesn't set itself. Whether anything changed.
    """
    env = process.get("env") or []
    if env_value(env, OPT_OUT) == "off":
        return False

    missing = [entry for entry in added if env_value(env, entry.partition("=")[0]) is None]
    if not missing:
        return False

    process["env"] = env + missing
    return True


def exec_(command: Command, arguments: list[str]) -> None:
    """
    ``runc exec --process FILE ID``: the variables, then become the real
    runc. Doesn't return.
    """
    try:
        process_path = command.options.get("process")
        if process_path and command.positional:
            bundle = state(command)["bundle"]
            added_path = os.path.join(bundle, COPIES, ADDED_ENV)

            if os.path.exists(added_path):
                with open(added_path, encoding="utf-8") as file:
                    added = json.load(file)
                with open(process_path, encoding="utf-8") as file:
                    process = json.load(file)

                if add_exec_env(process, added):
                    _save(process_path, process)
    except PREPARE_ERRORS as error:
        warn(f"this exec goes without the proxy's CA variables: {error}")

    pass_through(arguments)


def main(arguments: list[str]) -> int:
    """
    Do what runc was asked, with the proxy's CA where it belongs.
    """
    command = parse(arguments)

    if command is None or command.name not in ("create", "run", "exec"):
        pass_through(arguments)

    if command.name == "exec":
        exec_(command, arguments)

    if command.name == "create" or "detach" in command.options:
        create(command, arguments)

    return run_step(command, arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
