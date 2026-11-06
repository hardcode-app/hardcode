"""
The admin API and its page (Flask).

Sign-in is a password and then a TOTP code. Signed in, an admin manages the
profiles: the languages and tools each is built around (which open the
allowlist entries HardCode provides for them and bring their VS Code
extensions), its own extensions and how far its VS Code container has got
with installing them, its own Ubuntu packages, its allowlist (its own
entries and the provided ones it switches off, one at a time or across
every profile), what its proxy refused, and who owns it; manages the
accounts, admins' and members'; and approves or denies members' requests.

A member sees only the profiles they own (``spec.owner``), and another's is
404 to them, as if it weren't there. On their own they do the day-to-day
(suspend and resume, languages, extensions, packages, VS Code's address,
blocked traffic, delete, and create up to their limit), and what their
rights say with the allowlist: nothing, ask (each change that would let the
sandbox reach more waits for an admin), or edit. The image, the Docker
sidecar, the Ingress host, the volumes and the rest of the settings, the
bulk allowlist and the accounts are an admin's.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_for
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request, send_file

from hardcode_cli import allowlist, blocked, lan, langs, packages

from . import manifests, totp
from .approvals import APPROVED, DENIED, PENDING, ApprovalError, Approvals, NoSuchRequest
from .auth import (
    MEMBER,
    AuthError,
    Sessions,
    StoreConflict,
    Throttle,
    Users,
    canonical,
    check_password,
    hash_password,
    is_admin,
    rights_of,
)
from .k8s import ApiError, K8s
from .mail import Mailer
from .operator import profiles_path

WEB = Path(__file__).resolve().parent / "web"
REPO = Path(__file__).resolve().parent.parent.parent

# A profile's name: the CRD's rule. It starts with a letter because the
# operator names Services after it (<name>-dns, <name>-squid, ...), and a
# Service name is a DNS-1035 label. The CLI's local profiles may still start
# with a digit: Compose has no such limit.
NAME_OK = re.compile(r"^[a-z]([a-z0-9-]{0,30}[a-z0-9])?$")
NAME_RULE = "a-z, 0-9 and -, up to 32 characters, starting with a letter and not ending in -"

# What an Ingress host may be: dot-separated DNS labels, no scheme, path or port.
HOST_OK = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

OPS = ("add", "remove", "enable", "disable")
EXTENSION_OPS = ("add", "remove")
PACKAGE_OPS = ("add", "remove")
LAN_OPS = ("add", "remove")
SINCE = {"10m": 600, "30m": 1800, "2h": 7200, "24h": 86400}
WRITES = ("POST", "PUT", "PATCH", "DELETE")

# A profile's own VS Code extensions, at most: the CRD's maxItems on
# spec.extensions. Checked here first, so the answer is a 400 that says so
# rather than the API server's refusal.
MAX_EXTENSIONS = 100

# A profile's own Ubuntu packages, at most: the CRD's maxItems on
# spec.packages, checked here first for the same reason.
MAX_PACKAGES = 200

# The header the page's background refresh sends: such a request isn't
# activity, so it doesn't hold off the session's idle timeout.
POLL_HEADER = "X-HardCode-Poll"

# How much of the end of the VS Code container's log is read for how far its
# extensions have got: enough for a whole pass of the sync, between whatever
# VS Code itself logs, while staying a small read on every refresh.
EXTENSION_LOG_LINES = 1000

# The lines the VS Code container's extension sync logs
# (images/vscode/sync-extensions.sh). A pass that has something to install
# starts with "[vscode] extensions: 3 of 12 installed, installing 9", logs a
# line for each extension it installs or can't, and every pass ends with
# "[vscode] extensions: 11 of 12 installed", followed by ", 1 refused" and
# ", 1 to try again" when there are any.
EXTENSION_PROGRESS = re.compile(r"^\[vscode\] extensions: (\d+) of (\d+) installed(.*)$")
EXTENSION_INSTALLING = re.compile(r"^, installing \d+$")
EXTENSION_REFUSED_COUNT = re.compile(r", (\d+) refused")
EXTENSION_FAILED_COUNT = re.compile(r", (\d+) to try again")
EXTENSION_INSTALLED_LINE = re.compile(r"^\[vscode\] installed VS Code extension \S+$")
EXTENSION_REFUSED_LINE = re.compile(r"^\[vscode\] WARNING: VS Code extension \S+ refused:")
EXTENSION_FAILED_LINE = re.compile(r"^\[vscode\] WARNING: VS Code extension \S+ couldn't be installed or checked")

# The volumes whose size an edit may set: the operator compares their sizes,
# so each must be a Kubernetes quantity.
SIZED_VOLUMES = ("home", "shared", "docker")

# Wrong second-factor codes before the challenge is thrown away.
MAX_CODE_ATTEMPTS = 5

# Wrong codes for one account across challenges, per throttle window: signing
# in with the password again doesn't buy more guesses.
ACCOUNT_CODE_ATTEMPTS = 15

# Wrong passwords for one account, from anywhere, per throttle window.
ACCOUNT_ATTEMPTS = 40

# Read-and-reapply attempts when a profile changed under an edit.
PATCH_RETRIES = 4

# How long the On your network card's Check waits for its names' lookups,
# all of them together. The pod resolves names with the cluster's DNS,
# which often can't see them, and a lookup that gets no answer waits out
# the resolver's timeout: the request answers when this is up, with the
# names that didn't resolve in time unresolved.
LAN_CHECK_SECONDS = 2.0

# The lookups that run at once, for every Check together. One that hangs
# holds one of these threads until the resolver gives up, never a request
# thread, and gunicorn's request threads are few.
LAN_CHECK_LOOKUPS = 4

# What PATCH /api/profiles/<name> may change as given. It may change
# `languages` too, which is checked apart and brings the allowlist's language
# blocks with it.
EDITABLE_FIELDS = (
    "image",
    "suspended",
    "home",
    "shared",
    "docker",
    "resources",
    "env",
    "vscodeHost",
    "networkPolicy",
    "upstreamDNS",
    "extraVolumes",
    "extraVolumeMounts",
    "allowlist",
)

# What a member may change with PATCH /api/profiles/<name> on a profile of
# theirs; everything else in EDITABLE_FIELDS, and the owner, is an admin's.
MEMBER_EDITABLE = ("suspended", "languages")

# What a member's copy of one of their profiles takes from it: what a member
# may change themselves. The rest (the image, the Docker sidecar, volumes,
# mounts, environment, ...) is the chart's defaults, as for a profile from
# the template: an admin's settings for one profile stay that profile's.
MEMBER_COPIED = ("allowlist", "languages", "extensions", "packages")

# What a member may not give a new profile, in the words of the refusal.
ADMIN_ONLY_ON_CREATE = {"image": "the image", "vscodeHost": "the VS Code host", "owner": "the owner"}

# The Secret holding the members' requests (approvals.py), beside the
# accounts' (``USERS_SECRET``).
REQUESTS_SECRET = "hardcode-admin-requests"

# Why a member's allowlist edit is refused, by what it needed.
NO_ALLOWLIST_RIGHTS = "an admin decides what this profile may reach: ask one to change it"
NOT_ALLOWED_TO_OPEN = {
    "rw": "your account can't allow read-write entries: ask an admin",
    "ssh": "your account can't allow ssh tunnels: ask an admin",
    "lan": "your account can't let names reach your network: ask an admin",
}

# A password hash for addresses that have no account: checking against it
# costs the same as a real check, so the response time doesn't tell which
# addresses exist.
DUMMY_USER = hash_password("the same work for an unknown address")

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
)


class HttpError(Exception):
    """
    A request to refuse, with the status and message to send back.
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def qr_data_url(text: str) -> Optional[str]:
    """
    An SVG QR code of *text* as a data URL, when the qrcode package is installed.
    """
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None

    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
    return "data:image/svg+xml;base64," + base64.b64encode(image.to_string()).decode()


class SecretStore:
    """
    A list kept as JSON in one Secret: the accounts (``users.json``), or the
    members' requests (``requests.json``).

    ``load`` returns the list with the resourceVersion it came from, and
    ``save`` names that version, so a write based on an older snapshot is
    refused (``StoreConflict``) rather than overwriting a newer one. Nothing is
    kept between calls: the version belongs to the snapshot, not to the store
    the threads share.
    """

    def __init__(self, k8s: K8s, name: str = "hardcode-admin-users", key: str = "users.json"):
        self.k8s = k8s
        self.name = name
        self.key = key
        self.path = f"/api/v1/namespaces/{k8s.namespace}/secrets/{name}"

    def load(self) -> tuple[list[dict], Optional[str]]:
        secret = self.k8s.get(self.path) or {}
        version = secret.get("metadata", {}).get("resourceVersion")
        data = secret.get("data", {}).get(self.key)

        items = json.loads(base64.b64decode(data)) if data else []
        return items, version

    def save(self, items: list[dict], version: Optional[str]) -> None:
        data = {self.key: base64.b64encode(json.dumps(items).encode()).decode()}

        try:
            if version is None:
                self.k8s.create(f"/api/v1/namespaces/{self.k8s.namespace}/secrets", self._new_secret(data))
            else:
                # Conditional on the version: refused with 409 if the Secret moved.
                self.k8s.merge_patch(self.path, {"metadata": {"resourceVersion": version}, "data": data})

        except ApiError as error:
            if error.status == 409:
                raise StoreConflict(str(error)) from error
            raise

    def _new_secret(self, data: dict) -> dict:
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": self.name,
                "namespace": self.k8s.namespace,
                "labels": {"app.kubernetes.io/name": "hardcode-admin"},
            },
            "data": data,
        }


def _template_allowlist(path: Optional[str]) -> str:
    """
    The template allowlist new profiles start from: the given file, the one
    ``$TEMPLATE_ALLOWLIST`` names, the image's copy, or the checkout's.
    """
    candidates = [
        path,
        os.environ.get("TEMPLATE_ALLOWLIST"),
        "/app/template-allowlist.txt",
        str(REPO / "template" / "allowlist.txt"),
    ]

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).read_text(encoding="utf-8")

    raise HttpError(500, "no template allowlist found")


def _with_newline(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


def extension_status_from_log(log: str) -> Optional[dict]:
    """
    How far a VS Code container has got with its extensions, from its log;
    ``None`` when the log says nothing about them.

    The latest pass of the sync counts. Its ``state`` is ``installing`` or
    ``done``; ``installed`` of ``total`` wanted extensions are in place;
    ``refused`` were turned away (a publisher without a verified domain) and
    ``failed`` couldn't be installed or checked, and will be tried again.

    A pass that is still installing said how many were in place when it
    started; each extension it has installed since adds one, and those it
    couldn't are counted as it goes.
    """
    lines = [line.strip() for line in log.splitlines()]
    starts = [index for index, line in enumerate(lines) if EXTENSION_PROGRESS.match(line)]

    if not starts:
        return None

    latest = starts[-1]
    progress = EXTENSION_PROGRESS.match(lines[latest])
    installed = int(progress.group(1))
    total = int(progress.group(2))
    rest = progress.group(3)

    if not EXTENSION_INSTALLING.match(rest):
        refused = EXTENSION_REFUSED_COUNT.search(rest)
        failed = EXTENSION_FAILED_COUNT.search(rest)

        return {
            "state": "done",
            "installed": installed,
            "total": total,
            "refused": int(refused.group(1)) if refused else 0,
            "failed": int(failed.group(1)) if failed else 0,
        }

    since = lines[latest + 1 :]

    return {
        "state": "installing",
        "installed": installed + sum(1 for line in since if EXTENSION_INSTALLED_LINE.match(line)),
        "total": total,
        "refused": sum(1 for line in since if EXTENSION_REFUSED_LINE.match(line)),
        "failed": sum(1 for line in since if EXTENSION_FAILED_LINE.match(line)),
    }


def catalogue_entry(lang: langs.Lang) -> dict:
    """
    One language or tool of the catalogue, as ``GET /api/languages`` shows
    it: what it is, what it opens and what it brings, but not whether it is
    on, which is each profile's own.
    """
    entry = langs.describe(lang, enabled=False)
    del entry["enabled"]

    return entry


class AdminApi:
    """
    The routes of the admin, and what they share: the Kubernetes client, the
    accounts, the members' requests and the emails about them, the
    sessions and the sign-in throttles.
    """

    def __init__(
        self,
        k8s: K8s,
        users: Users,
        defaults: dict,
        cookie_secure: str,
        template_allowlist: Optional[str],
        sessions: Sessions,
        clock: Optional[Callable[[], float]],
        trusted_proxies: list[str],
        approvals: Approvals,
        mailer: Mailer,
        portal_url: str,
        resolve: lan.Resolver = lan.resolve,
    ):
        self.k8s = k8s
        self.users = users
        self.approvals = approvals
        self.mailer = mailer
        self.portal_url = portal_url.rstrip("/")
        self.defaults = defaults
        self.cookie_secure = cookie_secure
        self.template_allowlist = template_allowlist
        self.sessions = sessions
        self.clock = clock
        self.resolve = resolve
        self.lookups = ThreadPoolExecutor(max_workers=LAN_CHECK_LOOKUPS, thread_name_prefix="lan-check")
        self.namespace = k8s.namespace

        # Failed sign-ins: per client address and account, per account from
        # anywhere, and wrong second-factor codes per account (which outlive
        # any one challenge).
        self.throttle = Throttle()
        self.account_throttle = Throttle(limit=ACCOUNT_ATTEMPTS)
        self.code_throttle = Throttle(limit=ACCOUNT_CODE_ATTEMPTS)

        self.trusted = [ipaddress.ip_network(network, strict=False) for network in trusted_proxies if network.strip()]

        # A member's new profile is counted and created under this lock, so
        # that two at once can't both take the last one their limit allows.
        # The admin runs one worker process: its threads share it.
        self._creating = threading.Lock()

        self.app = Flask("hardcode_admin", static_folder=None)
        self.app.config["JSON_SORT_KEYS"] = False
        self._register()

    def _register(self) -> None:
        app = self.app

        app.before_request(self.guard)
        app.after_request(self.headers)
        app.register_error_handler(HttpError, self.http_error)
        app.register_error_handler(AuthError, self.auth_error)
        app.register_error_handler(StoreConflict, self.store_conflict)
        app.register_error_handler(ApiError, self.api_error)

        routes = [
            ("GET", "/healthz", self.healthz),
            ("GET", "/", self.index),
            ("GET", "/app.js", self.app_js),
            ("GET", "/admin.css", self.admin_css),
            ("GET", "/base.css", self.base_css),
            ("GET", "/icon.png", self.icon),
            # Signing in
            ("POST", "/api/login", self.login),
            ("POST", "/api/totp", self.second_factor),
            ("POST", "/api/logout", self.logout),
            ("GET", "/api/me", self.me),
            # Profiles
            ("GET", "/api/profiles", self.list_profiles),
            ("GET", "/api/template", self.template),
            ("GET", "/api/languages", self.languages),
            ("POST", "/api/profiles", self.create_profile),
            ("GET", "/api/profiles/<name>", self.show_profile),
            ("PATCH", "/api/profiles/<name>", self.patch_profile),
            ("DELETE", "/api/profiles/<name>", self.delete_profile),
            ("POST", "/api/profiles/<name>/extensions", self.edit_extensions),
            ("POST", "/api/profiles/<name>/packages", self.edit_packages),
            ("POST", "/api/profiles/<name>/token", self.rotate_token),
            ("POST", "/api/profiles/<name>/allowlist", self.edit_allowlist),
            ("POST", "/api/profiles/<name>/lan", self.edit_lan),
            ("POST", "/api/profiles/<name>/lan/check", self.check_lan),
            ("POST", "/api/allowlist/bulk", self.bulk_allowlist),
            ("GET", "/api/profiles/<name>/blocked", self.blocked_traffic),
            # Members' requests
            ("GET", "/api/requests", self.list_requests),
            ("POST", "/api/requests/<request_id>/approve", self.approve_request),
            ("POST", "/api/requests/<request_id>/deny", self.deny_request),
            # Accounts
            ("GET", "/api/users", self.list_users),
            ("POST", "/api/users", self.add_user),
            ("PATCH", "/api/users/<path:email>", self.patch_user),
            ("DELETE", "/api/users/<path:email>", self.remove_user),
            ("POST", "/api/users/<path:email>/reset-totp", self.reset_totp),
            ("POST", "/api/users/<path:email>/password", self.set_password),
        ]
        for method, rule, view in routes:
            app.add_url_rule(rule, view.__name__, view, methods=[method])

    # -- helpers ---------------------------------------------------------------

    def profile_path(self, name: str) -> str:
        return f"{profiles_path(self.namespace)}/{name}"

    def is_trusted(self, address: str) -> bool:
        """
        Whether *address* is one of the proxies we trust.
        """
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False

        return any(parsed in network for network in self.trusted)

    def client_ip(self) -> str:
        """
        The client's address: the peer's, or, when the peer is a proxy we
        trust, the last X-Forwarded-For address that no proxy of ours added.

        Each proxy appends the address it got the request from, so the header
        is read from the right: past our own proxies, the next address is the
        one ours saw. Anything left of it is whatever the client sent, and
        the client could pick a new address for every attempt.
        """
        peer = request.remote_addr or ""
        forwarded = request.headers.get("X-Forwarded-For", "")

        if not forwarded or not self.is_trusted(peer):
            return peer

        chain = [address.strip() for address in forwarded.split(",") if address.strip()]
        for address in reversed(chain):
            if not self.is_trusted(address):
                return address

        # Proxies of ours all the way: the first of them is the client.
        return chain[0] if chain else peer

    @staticmethod
    def body() -> dict:
        """
        The request's JSON object.
        """
        if not request.is_json:
            raise HttpError(415, "JSON body required")

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise HttpError(400, "invalid JSON")

        return data

    def require(self, stage: str) -> dict:
        """
        The signed-in account, at least at *stage* ("password" or "full"), or 401.

        A session is only as good as the account it was made for: a removed
        account, or a changed credential (a new password, a TOTP reset),
        ends it.
        """
        session = g.session
        if session is None:
            raise HttpError(401, "sign in first" if stage == "password" else "sign in required")

        user = self.users.find(session.email)
        if user is None or int(user.get("sessionVersion", 0)) != session.version:
            self.sessions.destroy(g.session_id)
            g.session = None
            raise HttpError(401, "session revoked; sign in again")

        if stage == "full" and session.stage != "full":
            raise HttpError(401, "sign in required")

        return user

    def require_admin(self) -> dict:
        """
        The signed-in account, which must be an admin's: 403 for a member's.
        """
        user = self.require("full")
        if not is_admin(user):
            raise HttpError(403, "only an admin can do that")

        return user

    @staticmethod
    def owner_of(profile: dict) -> str:
        """
        The email of the account a profile belongs to; ``""`` for nobody's.
        """
        return canonical((profile.get("spec") or {}).get("owner"))

    def may_use(self, user: dict, profile: dict) -> bool:
        """
        Whether *user* may see and work on *profile*: an admin any, a member
        the ones they own.
        """
        return is_admin(user) or self.owner_of(profile) == user["email"]

    def own_profile(self, user: dict, name: str) -> dict:
        """
        A profile *user* may work on (``may_use``). Another's is 404 to a
        member, as if it weren't there.
        """
        profile = self.get_profile(name)
        if not self.may_use(user, profile):
            raise HttpError(404, f"no profile '{name}'")

        return profile

    def owner_email(self, value: object) -> Optional[str]:
        """
        A profile's owner from a request: an account's email, or ``None``
        when it is empty (nobody's).
        """
        address = canonical(value)
        if not address:
            return None

        if self.users.find(address) is None:
            raise HttpError(400, f"owner: there is no account {address}")

        return address

    def owned_profiles(self, email: str, profiles: Optional[list[dict]] = None) -> list[dict]:
        """
        The profiles the account *email* owns, those being deleted left out
        (they are going away); from *profiles* when given, or the cluster.
        """
        if profiles is None:
            profiles = self.k8s.list(profiles_path(self.namespace))

        return [
            profile
            for profile in profiles
            if self.owner_of(profile) == email and not profile["metadata"].get("deletionTimestamp")
        ]

    def check_profile_limit(self, user: dict) -> None:
        """
        Refuse (403) a new profile for a member who has as many as their
        rights allow.
        """
        limit = rights_of(user)["profiles"]
        owned = len(self.owned_profiles(user["email"]))

        if limit == 0:
            raise HttpError(403, "your account can't create profiles: ask an admin")
        if owned >= limit:
            raise HttpError(403, f"your account may have {limit} profiles, and has them: delete one, or ask an admin")

    @staticmethod
    def opened_by(before: str, after: str, languages: list[str]) -> set[str]:
        """
        What an edit of a profile's own allowlist from *before* to *after*
        would let its sandbox do that it couldn't: the access of each entry
        it would allow that wasn't allowed (``""`` read-only, ``rw``,
        ``ssh``), and ``lan`` for a name that may reach an address it
        couldn't. Empty for an edit that only takes something away.

        It compares what the proxy would be given, not what the edit asked
        for: switching on ``github:OWNER/REPO`` switches on whatever its
        entries are, which may be read-write. And it compares it under the
        profile's *languages* and under every language: a member may switch
        languages, so taking away an admin's ``off`` line for a provided
        entry opens that entry whether its language is on now or later.
        """

        def allowed(text: str, languages_on: list[str]) -> set[tuple[str, str]]:
            entries = allowlist.effective(text, languages_on)
            return {(entry.entry, entry.flag) for entry in entries if entry.enabled and not entry.invalid}

        def pinned(text: str) -> set[tuple[str, str]]:
            return {(pin.name, pin.address) for pin in allowlist.pins(text)}

        opened = set()

        for languages_on in (languages, list(langs.LANGS)):
            allowed_before = allowed(before, languages_on)

            for entry, flag in allowed(after, languages_on) - allowed_before:
                # Read-only where the same entry was read-write gives nothing new.
                if flag == "" and (entry, "rw") in allowed_before:
                    continue
                opened.add(flag)

        if pinned(after) - pinned(before):
            opened.add("lan")

        return opened

    @staticmethod
    def access_of(opened: set[str]) -> str:
        """
        The most a change that opens *opened* gives, as a request shows it:
        ``ssh``, ``rw`` or ``""``.
        """
        for access in ("ssh", "rw"):
            if access in opened:
                return access

        return ""

    @staticmethod
    def check_opening(rights: dict, opened: set[str]) -> None:
        """
        Refuse (403) an edit that would open what a member's *rights* don't
        let them: ``opened_by``'s answer.
        """
        for access, message in NOT_ALLOWED_TO_OPEN.items():
            if access in opened and not rights[access]:
                raise HttpError(403, message)

    def get_profile(self, name: str) -> dict:
        """
        A Profile by name; 400 for a bad name, 404 for a missing one.
        """
        if not NAME_OK.match(name or ""):
            raise HttpError(400, "invalid profile name")

        profile = self.k8s.get(self.profile_path(name))
        if not profile:
            raise HttpError(404, f"no profile '{name}'")

        return profile

    def summarise(self, profile: dict) -> dict:
        """
        A profile as the list of profiles shows it.
        """
        spec = manifests.effective_spec(profile, self.defaults)
        entries = allowlist.effective(spec["allowlist"], spec["languages"])
        enabled = [entry for entry in entries if entry.enabled]
        status = profile.get("status") or {}

        phase = status.get("phase", "Pending")
        message = status.get("message", "")

        # Deleting waits for everything the profile owns to go (a foreground
        # deletion), and the operator writes no status meanwhile: the last
        # one it wrote (often Ready) would say the opposite of what happens.
        if profile["metadata"].get("deletionTimestamp"):
            phase = "Deleting"
            message = "its pods, services and volumes are being removed"

        return {
            "name": profile["metadata"]["name"],
            "createdAt": profile["metadata"].get("creationTimestamp"),
            "image": spec["image"],
            "suspended": spec["suspended"],
            "phase": phase,
            "message": message,
            "languages": spec["languages"],
            "entries": len(enabled),
            "rw": sum(1 for entry in enabled if entry.flag == "rw"),
            "ssh": sum(1 for entry in enabled if entry.flag == "ssh"),
            "vscodeHost": spec["vscodeHost"],
            "owner": self.owner_of(profile),
        }

    def vscode_access(self, profile: dict) -> dict:
        """
        How to open a profile's VS Code: its token, its URL (through the
        Ingress, or a port-forward), and the port-forward command.
        """
        names = manifests.Names(profile["metadata"]["name"])
        spec = manifests.effective_spec(profile, self.defaults)

        secret = self.k8s.get(f"/api/v1/namespaces/{self.namespace}/secrets/{names.token}") or {}
        encoded = secret.get("data", {}).get("token")
        token = base64.b64decode(encoded).decode() if encoded else ""

        if spec["vscodeHost"]:
            url = f"https://{spec['vscodeHost']}/?tkn={token}"
        else:
            url = f"http://localhost:8000/?tkn={token}"

        return {
            "token": token,
            "url": url,
            "portForward": f"kubectl -n {self.namespace} port-forward svc/{names.vscode} 8000:8000",
            "host": spec["vscodeHost"],
        }

    def extension_status(self, profile: dict) -> Optional[dict]:
        """
        How far a profile's VS Code container has got with its extensions
        (``extension_status_from_log``), from the end of its log; ``None``
        while it has no pod (suspended, or not created yet) or hasn't logged
        a pass yet.

        The pod is the StatefulSet's one, by name, so this is one small
        read, which the page's refresh can afford every time.
        """
        names = manifests.Names(profile["metadata"]["name"])
        log = self.k8s.logs(names.vscode_pod, "vscode", tail_lines=EXTENSION_LOG_LINES)

        return extension_status_from_log(log)

    @staticmethod
    def check_op(data: dict) -> tuple[str, str, str]:
        """
        The operation, entry and flag of an allowlist edit, validated once up front.
        """
        op = data.get("op")
        entry = str(data.get("entry") or "").strip().lower()
        flag = str(data.get("flag") or "")

        if op not in OPS:
            raise HttpError(400, "op must be add, remove, enable or disable")
        if not entry:
            raise HttpError(400, "entry is required")
        if flag not in allowlist.FLAGS:
            raise HttpError(400, "flag must be rw, ssh or nothing")

        if op == "add":
            problem = allowlist.validate(entry, flag)
            if problem:
                raise HttpError(400, problem)

        return op, entry, flag

    def apply_op(self, text: str, data: dict, languages: list[str]) -> str:
        """
        One allowlist edit applied to a profile's own allowlist (*text*), in a
        profile with *languages*, with the CLI's rules: a provided entry is
        switched off and on with an ``off`` line, and can't be deleted.
        """
        op, entry, flag = self.check_op(data)
        ssh = flag == "ssh"

        try:
            if op == "add":
                return allowlist.allow(text, entry, flag, languages)[0]
            if op == "remove":
                return allowlist.delete(text, entry, ssh, languages)[0]
            return allowlist.switch(text, entry, op == "enable", ssh, languages)[0]

        except allowlist.AllowlistError as error:
            raise HttpError(400, str(error)) from error

    @staticmethod
    def host_name(value: object) -> str:
        """
        An Ingress host name from a form field; empty means none.
        """
        host = str(value or "").strip().lower()
        if host and not HOST_OK.match(host):
            raise HttpError(400, "vscodeHost: a host name such as team-a.code.example.com (no scheme, path or port)")

        return host

    @staticmethod
    def volume_size(volume: str, value: object) -> str:
        """
        A volume size from a form field, as a Kubernetes quantity.

        The operator compares sizes to grow volumes; one it can't read
        (``50GB``) would put the profile in Error, so it is refused here.
        """
        size = str(value).strip()
        if not manifests.is_quantity(size):
            raise HttpError(400, f"{volume} size: a size such as 20Gi or 500Mi")

        return size

    @staticmethod
    def versioned(profile: dict, patch: dict) -> dict:
        """
        *patch*, made conditional: the API server refuses it (409) if the
        profile changed since it was read.
        """
        version = profile["metadata"].get("resourceVersion")
        return {**patch, "metadata": {"resourceVersion": version}} if version else patch

    @staticmethod
    def check_version(profile: dict, data: dict) -> None:
        """
        Refuse (409) a whole-document edit made on an older spec.

        The text editor and the settings form name the spec's version (its
        generation) they were opened at. If the spec moved since, the form is
        stale and must be reloaded rather than overwrite what happened
        meanwhile. The operator's status writes don't move the generation,
        so they don't count.
        """
        opened_at = data.get("version")
        if opened_at and str(opened_at) != str(profile["metadata"].get("generation")):
            raise HttpError(409, "the profile changed since you opened it; reload and try again")

    @staticmethod
    def language_keys(value: object) -> list[str]:
        """
        The languages and tools a request names, as catalogue keys (``js`` is
        ``node``), in catalogue order and without repeats; 400 for anything
        else.
        """
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise HttpError(400, f"languages: a list of language keys, from: {', '.join(langs.LANGS)}")

        try:
            named = {langs.normalise(item) for item in value}
        except langs.LangError as error:
            raise HttpError(400, f"languages: {error}") from error

        return [key for key in langs.LANGS if key in named]

    def editable_spec(self, data: dict) -> dict:
        """
        The settings a PATCH may change, validated: the fields of
        EDITABLE_FIELDS that *data* has, with empty values meaning "inherit".
        """
        spec: dict = {key: data[key] for key in EDITABLE_FIELDS if key in data}

        if "suspended" in spec:
            spec["suspended"] = bool(spec["suspended"])

        # An empty image or host means "inherit": None removes the field.
        if "image" in spec:
            spec["image"] = str(spec["image"] or "").strip() or None
        if "vscodeHost" in spec:
            spec["vscodeHost"] = self.host_name(spec["vscodeHost"]) or None

        # An empty size means "inherit" too, and is left alone.
        for volume in SIZED_VOLUMES:
            settings = spec.get(volume)
            if isinstance(settings, dict) and settings.get("size"):
                settings["size"] = self.volume_size(volume, settings["size"])

        if "allowlist" in spec:
            spec["allowlist"] = _with_newline(str(spec["allowlist"] or ""))
            invalid = next((entry for entry in allowlist.parse(spec["allowlist"]) if entry.invalid), None)
            if invalid:
                raise HttpError(400, f"invalid entry: {invalid.entry}")

        return spec

    @staticmethod
    def extension_edit(op: str, extension_id: str, spec: dict) -> tuple[Optional[list[str]], str]:
        """
        A profile's own extensions after one edit, and what to say about it;
        ``None`` instead of the list when nothing changes.

        Adding one the VS Code container already gets (every one's, or one of
        its languages') changes nothing. Removing one of those is refused:
        switching the language off is what removes it. The words are the
        CLI's.
        """
        extras = spec["extensions"]
        provider = langs.provider(extension_id, spec["languages"])

        if op == "add":
            if provider:
                return None, f"{extension_id} already comes with {provider}"
            if extension_id in extras:
                return None, f"{extension_id} is already added"
            if len(extras) >= MAX_EXTENSIONS:
                raise HttpError(400, f"a profile has at most {MAX_EXTENSIONS} extensions of its own; remove one first")

            return [*extras, extension_id], f"added extension {extension_id}"

        if extension_id in extras:
            return [extra for extra in extras if extra != extension_id], f"removed extension {extension_id}"

        refusal = langs.removal_refusal(extension_id, spec["languages"])
        raise HttpError(400, refusal or f"{extension_id} isn't one of this profile's extensions")

    def languages_of(self, profile: dict) -> list[str]:
        """
        The languages and tools a profile has on: its own, or the defaults.
        """
        return manifests.effective_languages(profile.get("spec") or {}, self.defaults)

    def patch_allowlist(self, name: str, data: dict) -> bool:
        """
        Apply one edit to a profile's allowlist; whether anything changed.
        """
        return self.patch_allowlist_text(
            name,
            lambda current, profile: self.apply_op(current, data, self.languages_of(profile)),
        )

    def member_edit(
        self,
        user: dict,
        profile: dict,
        edit: Callable[[str, dict], str],
        change: dict,
        reason: object,
    ) -> tuple[dict, int]:
        """
        A member's edit of a profile's own allowlist (*edit*, as
        ``patch_allowlist_text`` takes it), as their rights have it: the
        answer, and its status.

        With no allowlist rights, it is refused. An edit that would let the
        sandbox do more (``opened_by``) needs the right to each thing it
        opens; with ``edit`` it is made, and with ``ask`` it becomes a
        request (*change*: what an admin's approval does), unless it would
        change nothing. One that only takes something away is made.
        """
        rights = rights_of(user)
        if rights["allowlist"] == "none":
            raise HttpError(403, NO_ALLOWLIST_RIGHTS)

        name = profile["metadata"]["name"]

        current = (profile.get("spec") or {}).get("allowlist") or ""
        updated = edit(current, profile)
        opened = self.opened_by(current, updated, self.languages_of(profile))
        self.check_opening(rights, opened)

        if updated == current:
            return {"changed": False}, 200

        if opened and rights["allowlist"] == "ask":
            return self.ask(user, {**change, "profile": name, "access": self.access_of(opened)}, reason)

        def checked(latest_text: str, latest: dict) -> str:
            # The edit is made on the text as it is by then, which another
            # edit may have moved on: checked again on that.
            made = edit(latest_text, latest)
            opens = self.opened_by(latest_text, made, self.languages_of(latest))
            self.check_opening(rights, opens)

            if opens and rights["allowlist"] == "ask":
                raise HttpError(409, "the allowlist changed meanwhile; try again")

            return made

        return {"changed": self.patch_allowlist_text(name, checked)}, 200

    def ask(self, user: dict, change: dict, reason: object) -> tuple[dict, int]:
        """
        A member's request for *change*, made (or the one waiting for the
        same change), with an email to every admin about a new one: the
        answer, and its status (202: accepted, for an admin to decide).
        """
        try:
            request, new = self.approvals.ask({**change, "by": user["email"]}, str(reason or ""))
        except ApprovalError as error:
            raise HttpError(400, str(error)) from error

        if new:
            admins = [admin["email"] for admin in self.users.admins()]
            self.mailer.request_made(request, admins, self.portal_url)

        return {"changed": False, "requested": True, "request": request}, 202

    def patch_allowlist_text(self, name: str, edit: Callable[[str, dict], str]) -> bool:
        """
        Apply *edit* (given the allowlist's text and the profile) to a
        profile's allowlist; whether anything changed.

        Read, edit, and write conditionally on the version read. If another
        edit landed in between, read again and apply ours on top of it, so
        neither is lost.
        """
        for attempt in range(PATCH_RETRIES):
            profile = self.get_profile(name)
            current = (profile.get("spec") or {}).get("allowlist") or ""

            updated = edit(current, profile)
            if updated == current:
                return False

            try:
                self.k8s.merge_patch(self.profile_path(name), self.versioned(profile, {"spec": {"allowlist": updated}}))
                return True
            except ApiError as error:
                if error.status != 409 or attempt == PATCH_RETRIES - 1:
                    raise

        return False  # pragma: no cover - the loop returns or raises

    def respond(self, payload: Any, status: int = 200, cookie: Optional[str] = None):
        """
        A JSON response; with *cookie*, the session cookie set (or, when it
        is empty, deleted).
        """
        response = jsonify(payload)
        response.status_code = status

        if cookie is not None:
            behind_https = request.headers.get("X-Forwarded-Proto") == "https"
            secure = self.cookie_secure == "always" or (self.cookie_secure == "auto" and behind_https)

            if cookie:
                response.set_cookie("hc_session", cookie, httponly=True, samesite="Strict", secure=secure, path="/")
            else:
                response.delete_cookie("hc_session", path="/")

        return response

    # -- plumbing --------------------------------------------------------------

    def guard(self) -> None:
        """
        Before every request: find the session, and refuse writes that don't
        come from the admin's own page.
        """
        # The page's background refresh doesn't count as activity: a tab left
        # open still reaches the idle timeout.
        polled = request.headers.get(POLL_HEADER) == "1"

        g.session_id = request.cookies.get("hc_session", "")
        g.session = self.sessions.get(g.session_id, touch=not polled)
        g.ip = self.client_ip()

        if request.method not in WRITES or not request.path.startswith("/api/"):
            return

        # Writes come from this page only: the same origin, and a JSON body,
        # which a plain HTML form can't send. (SameSite cookies alone don't
        # keep out a sibling subdomain, which a sandbox's Ingress may well be.)
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != request.host.lower():
            raise HttpError(403, "cross-origin request refused")

        if request.headers.get("Sec-Fetch-Site", "").lower() in ("cross-site", "same-site"):
            raise HttpError(403, "cross-site request refused")

        if not request.is_json:
            raise HttpError(415, "JSON body required")

    @staticmethod
    def headers(response):
        """
        After every request: no caching of API answers, and a page that can't
        be framed or load anything from elsewhere.
        """
        response.headers["Cache-Control"] = "no-store" if request.path.startswith("/api/") else "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"

        if response.mimetype == "text/html":
            response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
            response.headers["X-Frame-Options"] = "DENY"

        return response

    def http_error(self, error: HttpError):
        return self.respond({"error": str(error)}, error.status)

    def auth_error(self, error: AuthError):
        return self.respond({"error": str(error)}, 400)

    def store_conflict(self, error: StoreConflict):
        return self.respond({"error": "the admin list changed meanwhile; try again"}, 409)

    def api_error(self, error: ApiError):
        """
        A Kubernetes API failure: a conflict is the client's to retry, anything
        else is ours, and logged.
        """
        status = 409 if error.status == 409 else 500
        if status == 500:
            self.app.logger.error("%s %s: %s", request.method, request.path, error)

        return self.respond({"error": str(error)}, status)

    # -- the page --------------------------------------------------------------

    def healthz(self):
        return self.respond({"ok": True})

    def index(self):
        return send_file(WEB / "index.html")

    def app_js(self):
        return send_file(WEB / "app.js")

    def admin_css(self):
        return send_file(WEB / "admin.css")

    def base_css(self):
        """
        The desktop app's stylesheet, which the admin page shares: copied into
        the image, or straight from the checkout.
        """
        copied = WEB / "base.css"
        return send_file(copied if copied.exists() else REPO / "app" / "src" / "styles.css")

    def icon(self):
        copied = WEB / "icon.png"
        return send_file(copied if copied.exists() else REPO / "assets" / "hardcode-128.png")

    # -- signing in ------------------------------------------------------------

    def login(self):
        """
        The first factor: email and password. Answers with what comes next,
        a code or (the first time) TOTP enrolment.
        """
        data = self.body()
        email = canonical(data.get("email"))
        password = str(data.get("password") or "")
        key = f"{g.ip}|{email}"

        if self.throttle.blocked(key) or self.account_throttle.blocked(email):
            raise HttpError(429, "too many attempts; try again later")

        user = self.users.find(email)

        # The check runs before the existence test: an unknown address costs
        # the same scrypt as a wrong password, so the response time doesn't
        # say which addresses exist.
        matched = check_password(password, user or DUMMY_USER)
        if not user or not matched:
            self.throttle.fail(key)
            self.account_throttle.fail(email)
            raise HttpError(401, "wrong email or password")

        self.throttle.clear(key)
        self.account_throttle.clear(email)

        session_id = self.sessions.create(user["email"], "password", int(user.get("sessionVersion", 0)))

        payload: dict = {"next": "totp" if user.get("totpEnrolled") else "enrol"}
        if not user.get("totpEnrolled"):
            url = totp.otpauth_url(user["totpSecret"], user["email"])
            payload.update(otpauth=url, secret=user["totpSecret"], qr=qr_data_url(url))

        return self.respond(payload, cookie=session_id)

    def second_factor(self):
        """
        The second factor: a TOTP code, good once.
        """
        user = self.require("password")
        email = user["email"]

        if self.code_throttle.blocked(email):
            self.sessions.destroy(g.session_id)
            raise HttpError(429, "too many wrong codes for this account; try again later")

        now = self.clock() if self.clock else None
        step = totp.matching_step(user["totpSecret"], str(self.body().get("code") or ""), now)

        if step is not None:
            try:
                # Checked and recorded in one write: a code is good once
                # (RFC 6238, section 5.2), even when two requests carry it at
                # the same moment.
                self.users.consume_totp_step(email, step, g.session.version)
            except AuthError:
                step = None

        if step is None:
            self.code_throttle.fail(email)

            if self.sessions.fail(g.session_id) >= MAX_CODE_ATTEMPTS:
                self.sessions.destroy(g.session_id)
                raise HttpError(401, "too many wrong codes; sign in again")

            raise HttpError(401, "wrong code")

        self.code_throttle.clear(email)
        self.sessions.upgrade(g.session_id, "full")

        return self.respond({"ok": True, "email": email})

    def logout(self):
        self.sessions.destroy(g.session_id)
        return self.respond({"ok": True}, cookie="")

    def me(self):
        """
        Who is signed in, and as what; ``null`` when nobody is. A member's
        rights come with it, and for everyone whether new requests are
        emailed to the admins.
        """
        try:
            user = self.require("full")
        except HttpError:
            return self.respond(None)

        admin = is_admin(user)

        return self.respond(
            {
                "email": user["email"],
                "namespace": self.namespace,
                "role": "admin" if admin else MEMBER,
                "rights": None if admin else rights_of(user),
                "mail": self.mailer.enabled,
            }
        )

    # -- profiles --------------------------------------------------------------

    def list_profiles(self):
        """
        The profiles: every one for an admin, a member's own for a member.
        """
        user = self.require("full")
        profiles = self.k8s.list(profiles_path(self.namespace))

        return self.respond([self.summarise(profile) for profile in profiles if self.may_use(user, profile)])

    def template(self):
        self.require("full")
        return self.respond({"allowlist": _template_allowlist(self.template_allowlist)})

    def languages(self):
        """
        The catalogue: every language a profile can be built around, then
        the tools, each with the allowlist entries it opens and the VS Code
        extensions it brings.
        """
        self.require("full")
        return self.respond([catalogue_entry(lang) for lang in langs.CATALOGUE])

    def create_profile(self):
        """
        A new profile: from the template, or a copy of another profile.

        With ``languages``, the profile is built around those. Without, a
        copy keeps its source's, and a profile from the template has none of
        its own: it gets the chart's default, or every one. Its allowlist is
        the template's, or the copy's: the profile's own entries, and the
        provided ones it switches off; the provided entries follow its
        languages by themselves.

        An admin may give it an ``owner``, an account's email. A member's is
        their own, and only as many as their rights allow; they copy only
        their own profiles, and only what they may set themselves
        (``MEMBER_COPIED``), and can't give it an image, a host or an owner.
        """
        user = self.require("full")
        admin = is_admin(user)
        data = self.body()

        if not admin:
            for field, words in ADMIN_ONLY_ON_CREATE.items():
                if data.get(field):
                    raise HttpError(403, f"only an admin can set {words}")

        name = str(data.get("name") or "").strip().lower()
        if not NAME_OK.match(name):
            raise HttpError(400, f"name: {NAME_RULE}")

        chosen = data.get("languages")
        languages = self.language_keys(chosen) if chosen is not None else None

        owner = self.owner_email(data.get("owner")) if admin else user["email"]
        source = self.own_profile(user, str(data["from"])) if data.get("from") else None

        # Counted and created under one lock: two of a member's at once
        # can't both take the last profile their rights allow.
        with self._creating:
            if not admin:
                self.check_profile_limit(user)

            # A profile being deleted still exists until everything it owned
            # is gone; the name is free again in a moment, not taken.
            existing = self.k8s.get(self.profile_path(name))
            if existing and existing["metadata"].get("deletionTimestamp"):
                raise HttpError(409, f"profile '{name}' is still being deleted; try again in a moment")
            if existing:
                raise HttpError(409, f"profile '{name}' already exists")

            spec: dict = {"allowlist": _template_allowlist(self.template_allowlist)}
            if source and admin:
                spec = dict(source.get("spec") or {})

                # A copy gets an Ingress host, a connection token and an
                # owner of its own.
                spec.pop("vscodeHost", None)
                spec.pop("vscodeToken", None)
                spec.pop("owner", None)

            if source and not admin:
                source_spec = source.get("spec") or {}
                spec = {field: source_spec[field] for field in MEMBER_COPIED if field in source_spec}

            if data.get("image"):
                spec["image"] = str(data["image"])
            if data.get("vscodeHost"):
                spec["vscodeHost"] = self.host_name(data["vscodeHost"])
            if owner:
                spec["owner"] = owner

            if languages is not None:
                spec["languages"] = languages

            profile = {
                "apiVersion": manifests.API_VERSION,
                "kind": "Profile",
                "metadata": {"name": name, "namespace": self.namespace},
                "spec": spec,
            }
            created = self.k8s.create(profiles_path(self.namespace), profile)

        return self.respond(self.summarise(created), 201)

    def show_profile(self, name: str):
        """
        Everything about one profile, with the version of its spec it was
        read at, which a later whole-document edit names.

        The version is the generation, which only a spec change moves; the
        operator's status writes, which come and go all the time, don't make
        an open editor stale.

        Its ``languages`` are the whole catalogue, each marked on or off, its
        ``extensions`` every extension its VS Code container gets and why
        (those of the languages that are off too, marked so), and its
        ``extensionStatus`` how far the running VS Code container has got
        with installing them, and its ``packages`` its own Ubuntu packages,
        which the VS Code container installs as it starts. Its ``requests``
        are the members' requests for it still waiting for an admin: every
        one to an admin, a member's own to them.
        """
        user = self.require("full")
        profile = self.own_profile(user, name)
        spec = manifests.effective_spec(profile, self.defaults)
        enabled = spec["languages"]
        entries = allowlist.as_dicts(allowlist.effective(spec["allowlist"], enabled))

        waiting = [
            request
            for request in self.approvals.list()
            if request["profile"] == profile["metadata"]["name"]
            and request["state"] == PENDING
            and (is_admin(user) or request["by"] == user["email"])
        ]
        requests = self.request_views(waiting, [profile])

        return self.respond(
            {
                **self.summarise(profile),
                "languages": [langs.describe(lang, lang.key in enabled) for lang in langs.CATALOGUE],
                "extensions": langs.extension_sources(enabled, spec["extensions"]),
                "extensionStatus": self.extension_status(profile),
                "packages": spec["packages"],
                "spec": {**(profile.get("spec") or {}), "effective": spec},
                "status": profile.get("status") or {},
                "allowlist": entries,
                "lan": self.lan_view(profile),
                "access": self.vscode_access(profile),
                "requests": requests,
                "version": profile["metadata"].get("generation"),
            }
        )

    def patch_profile(self, name: str):
        """
        Change a profile's settings, its languages, or its whole allowlist
        (its own entries and the provided ones it switches off; the provided
        entries follow the languages by themselves).

        A patch is made on the version just read. One of ``languages`` alone
        doesn't depend on what was read, so when something else lands in
        between (the operator's status writes do, all the time) it is made
        again; any other patch that loses such a race is refused.

        An admin may also give the profile an ``owner`` (an account's email;
        empty: nobody's). A member may change only ``MEMBER_EDITABLE``.
        """
        user = self.require("full")
        self.own_profile(user, name)
        data = self.body()

        if not is_admin(user):
            refused = [field for field in (*EDITABLE_FIELDS, "owner") if field in data and field not in MEMBER_EDITABLE]
            if refused:
                raise HttpError(403, f"only an admin can change {', '.join(refused)}")

        spec = self.editable_spec(data)
        if "owner" in data:
            spec["owner"] = self.owner_email(data["owner"])

        chosen = data.get("languages")
        languages = self.language_keys(chosen) if chosen is not None else None

        # Only a patch that doesn't depend on what was read is made again.
        languages_only = languages is not None and not spec
        attempts = PATCH_RETRIES if languages_only else 1

        for attempt in range(attempts):
            profile = self.get_profile(name)
            self.check_version(profile, data)

            patch = dict(spec)
            if languages is not None:
                patch["languages"] = languages

            try:
                updated = self.k8s.merge_patch(self.profile_path(name), self.versioned(profile, {"spec": patch}))
                return self.respond(self.summarise(updated))
            except ApiError as error:
                if error.status != 409:
                    raise
                if attempt == attempts - 1:
                    raise HttpError(409, "the profile changed meanwhile; reload and try again") from error

        raise HttpError(409, "the profile changed meanwhile; reload and try again")  # pragma: no cover

    def delete_profile(self, name: str):
        """
        Delete a profile, and forget the requests still waiting for it.
        """
        user = self.require("full")
        self.own_profile(user, name)
        self.k8s.delete(self.profile_path(name))
        self.approvals.drop_profile(name)

        return self.respond({"ok": True})

    def edit_allowlist(self, name: str):
        """
        One edit of a profile's allowlist: an admin's is made, and a member's
        goes as their rights have it (``member_edit``); with ``reason``, what
        a request tells the admins.
        """
        user = self.require("full")
        profile = self.own_profile(user, name)
        data = self.body()

        if is_admin(user):
            return self.respond({"changed": self.patch_allowlist(name, data)})

        op, entry, flag = self.check_op(data)

        # Switching an entry on or off, the flag only says which of its
        # rules: its ssh one, or the other. Kept so, the same switch is the
        # same request whatever else is sent.
        if op != "add":
            flag = "ssh" if flag == "ssh" else ""

        change = {"kind": "entry", "op": op, "entry": entry, "flag": flag, "address": ""}

        def edit(current: str, latest: dict) -> str:
            return self.apply_op(current, data, self.languages_of(latest))

        payload, status = self.member_edit(user, profile, edit, change, data.get("reason"))
        return self.respond(payload, status)

    def lan_view(self, profile: dict, answers: Optional[dict[str, list[str]]] = None) -> dict:
        """
        A profile's names on the network, as its On your network card shows
        them: whether the chart switches pins on (``enabled``), why the
        operator takes none of them when it doesn't (``off``,
        ``manifests.lan_off``; ``""`` when it does), and each pin
        (``manifests.effective_pins``) with what it resolves to from here
        (``resolves``) and its ``state``, as ``hardcode lan`` gives them.

        Nothing is looked up here: the page asks for the view every ten
        seconds. Without *answers* (each name's addresses, from
        ``check_lan``) every pin is ``unchecked``. The admin's pod resolves
        names with the cluster's DNS, which often can't see the network the
        address is on: a name that doesn't resolve here is ``unresolved``,
        not wrong.
        """
        spec = manifests.effective_spec(profile, self.defaults)
        pins = []

        for pin in spec["lan"]["pins"]:
            if answers is None:
                pins.append({**pin, "resolves": [], "state": "unchecked"})
                continue

            resolves = answers.get(pin["name"]) or []

            state = "ok" if pin["address"] in resolves else "moved"
            if not resolves:
                state = "unresolved"

            pins.append({**pin, "resolves": resolves, "state": state})

        return {"enabled": spec["lan"]["enabled"], "off": manifests.lan_off(spec["lan"]), "pins": pins}

    def resolve_all(self, names: list[str]) -> dict[str, list[str]]:
        """
        What each of *names* resolves to from here, looked up side by side
        (at most ``LAN_CHECK_LOOKUPS`` at once), waiting ``LAN_CHECK_SECONDS``
        for all of them together. A name whose lookup hasn't finished by
        then has no addresses: a lookup still waiting to start never does,
        and one that hangs carries on in its own thread, not this request's.
        """
        lookups = {name: self.lookups.submit(self.resolve, name) for name in dict.fromkeys(names)}
        finished, _ = wait_for(lookups.values(), timeout=LAN_CHECK_SECONDS)

        answers = {}
        for name, lookup in lookups.items():
            if lookup in finished:
                answers[name] = lookup.result()
            else:
                lookup.cancel()
                answers[name] = []

        return answers

    def check_lan(self, name: str):
        """
        Look a profile's names on the network up from here, with a short
        deadline for all of them together (``resolve_all``): the On your
        network card, with every pin's state.
        """
        user = self.require("full")
        profile = self.own_profile(user, name)

        spec = manifests.effective_spec(profile, self.defaults)
        answers = self.resolve_all([pin["name"] for pin in spec["lan"]["pins"]])

        return self.respond(self.lan_view(profile, answers))

    def edit_lan(self, name: str):
        """
        Let one name reach an address on the network (``op`` add, with
        ``name`` and ``address``), or stop it (``op`` remove), in a profile's
        allowlist. The admin's pod can't see the network the address is on,
        so the address is given, never looked up. A name pinned already is
        moved; an address in the cluster's own ranges is refused.

        A member's edit goes as their rights have it (``member_edit``): a
        new name, or a new address for one, needs their ``lan`` right.
        """
        user = self.require("full")
        profile = self.own_profile(user, name)
        data = self.body()

        op = data.get("op")
        if op not in LAN_OPS:
            raise HttpError(400, "op must be add or remove")

        pin = str(data.get("name") or "").strip().lower()
        address = str(data.get("address") or "").strip().lower()
        if not pin:
            raise HttpError(400, "name is required")

        messages: list[str] = []
        edit = self.lan_edit(op, pin, address, messages)

        if is_admin(user):
            changed = self.patch_allowlist_text(name, edit)
            return self.respond({"changed": changed, "message": messages[-1] if messages else ""})

        change = {"kind": "lan", "op": op, "entry": pin, "flag": "", "address": address}
        payload, status = self.member_edit(user, profile, edit, change, data.get("reason"))

        return self.respond({**payload, "message": messages[-1] if messages else ""}, status)

    def lan_edit(self, op: str, pin: str, address: str, messages: list[str]) -> Callable[[str, dict], str]:
        """
        The edit, as ``patch_allowlist_text`` takes it, that lets the name
        *pin* reach *address* (``op`` add) or stops it (``op`` remove); what
        it says about it is added to *messages*.
        """

        def edit(current: str, profile: dict) -> str:
            if op == "remove":
                updated, message = allowlist.remove_pin(current, pin)
                messages.append(message)
                return updated

            spec = manifests.effective_spec(profile, self.defaults)
            if lan.in_ranges(address, spec["lan"]["clusterRanges"]):
                raise HttpError(400, f"{address} is {manifests.LAN_CLUSTER}")

            try:
                updated, message = allowlist.add_pin(
                    current,
                    pin,
                    address,
                    lan.internal_ranges(spec["internalRanges"]),
                    manifests.CLUSTER_DEVNET,
                )
            except allowlist.AllowlistError as error:
                raise HttpError(400, str(error)) from error

            messages.append(message)
            return updated

        return edit

    def rotate_token(self, name: str):
        """
        Give a profile's VS Code a new connection token, so that every copy
        of the old one stops working.

        The token is the profile's token Secret, which the operator made; a
        new one written there reaches the pod at the operator's next pass (a
        minute at most), which restarts VS Code with it. A token the spec
        fixes (``vscodeToken``) is changed there instead.
        """
        user = self.require("full")
        profile = self.own_profile(user, name)

        if (profile.get("spec") or {}).get("vscodeToken"):
            raise HttpError(400, "this profile's token is fixed in its spec (vscodeToken): change it there")

        names = manifests.Names(name)
        path = f"/api/v1/namespaces/{self.namespace}/secrets/{names.token}"
        if not self.k8s.get(path):
            raise HttpError(409, "the profile has no token yet; the operator makes one as it brings the profile up")

        token = base64.b64encode(secrets.token_urlsafe(24).encode()).decode()
        self.k8s.merge_patch(path, {"data": {"token": token}})

        return self.respond({"message": "new token; VS Code restarts with it within a minute"})

    def edit_extensions(self, name: str):
        """
        Add one of a profile's own VS Code extensions, or remove one.

        The list is the profile's ``spec.extensions``. The operator writes the
        change into the VS Code container's extension list, and the running
        container installs or removes the extension in the background, with
        no restart. Read, edit, and write conditionally on the version read;
        when another edit landed in between, read again and edit on top of it.
        """
        user = self.require("full")
        self.own_profile(user, name)
        data = self.body()

        op = data.get("op")
        if op not in EXTENSION_OPS:
            raise HttpError(400, "op must be add or remove")

        try:
            extension_id = langs.normalise_extension(str(data.get("id") or ""))
        except langs.LangError as error:
            raise HttpError(400, str(error)) from error

        for attempt in range(PATCH_RETRIES):
            profile = self.get_profile(name)
            spec = manifests.effective_spec(profile, self.defaults)

            updated, message = self.extension_edit(op, extension_id, spec)
            if updated is None:
                return self.respond({"changed": False, "message": message})

            # None removes the field: a profile with no extensions of its own.
            patch = {"spec": {"extensions": updated or None}}

            try:
                self.k8s.merge_patch(self.profile_path(name), self.versioned(profile, patch))
                return self.respond({"changed": True, "message": message})
            except ApiError as error:
                if error.status != 409 or attempt == PATCH_RETRIES - 1:
                    raise

        raise HttpError(409, "the profile changed meanwhile; try again")  # pragma: no cover

    @staticmethod
    def package_names(value: object) -> list[str]:
        """
        The Ubuntu package names a request gives, checked with Debian's
        rule, in their order and without repeats; 400 for anything else.
        """
        if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
            raise HttpError(400, "names: a list of Ubuntu package names")

        try:
            return list(dict.fromkeys(packages.check(item) for item in value))
        except packages.PackageError as error:
            raise HttpError(400, str(error)) from error

    @staticmethod
    def package_edit(op: str, names: list[str], current: list[str]) -> tuple[Optional[list[str]], str]:
        """
        A profile's own Ubuntu packages after one edit of *names*, and what
        to say about it; ``None`` instead of the list when nothing changes.
        Removing one the profile doesn't have is refused, and nothing changes.
        """
        words = "package" if len(names) == 1 else "packages"
        restart = "the VS Code container restarts, and installs what it has as it starts"

        if op == "add":
            new = [name for name in names if name not in current]
            if not new:
                return None, f"already added: {packages.join(names)}"
            if len(current) + len(new) > MAX_PACKAGES:
                raise HttpError(400, f"a profile has at most {MAX_PACKAGES} packages of its own; remove some first")

            return [*current, *new], f"added {words} {packages.join(new)}; {restart}"

        missing = [name for name in names if name not in current]
        if missing:
            raise HttpError(400, f"not one of this profile's packages: {packages.join(missing)}")

        return [name for name in current if name not in names], f"removed {words} {packages.join(names)}; {restart}"

    def edit_packages(self, name: str):
        """
        Add Ubuntu packages of a profile's own, or remove them.

        The list is the profile's ``spec.packages``. The image is shared on
        Kubernetes, so the operator hands the list to the VS Code container,
        which installs the packages as it starts: a change restarts its
        pod. Read, edit, and write conditionally on the version read; when
        another edit landed in between, read again and edit on top of it.
        """
        user = self.require("full")
        self.own_profile(user, name)
        data = self.body()

        op = data.get("op")
        if op not in PACKAGE_OPS:
            raise HttpError(400, "op must be add or remove")

        names = self.package_names(data.get("names"))

        for attempt in range(PATCH_RETRIES):
            profile = self.get_profile(name)
            current = manifests.effective_spec(profile, self.defaults)["packages"]

            updated, message = self.package_edit(op, names, current)
            if updated is None:
                return self.respond({"changed": False, "message": message})

            # None removes the field: a profile with no packages of its own.
            patch = {"spec": {"packages": updated or None}}

            try:
                self.k8s.merge_patch(self.profile_path(name), self.versioned(profile, patch))
                return self.respond({"changed": True, "message": message})
            except ApiError as error:
                if error.status != 409 or attempt == PATCH_RETRIES - 1:
                    raise

        raise HttpError(409, "the profile changed meanwhile; try again")  # pragma: no cover

    def bulk_allowlist(self):
        """
        One allowlist edit applied to every profile, reported per profile.
        """
        self.require_admin()
        data = self.body()
        self.check_op(data)

        results = {}
        for profile in self.k8s.list(profiles_path(self.namespace)):
            name = profile["metadata"]["name"]
            try:
                results[name] = "changed" if self.patch_allowlist(name, data) else "unchanged"
            except (HttpError, ApiError) as error:
                results[name] = f"error: {error}"

        return self.respond(results)

    def blocked_traffic(self, name: str):
        """
        What a profile's squid and DNS filter refused recently, from their logs.
        """
        user = self.require("full")
        profile = self.own_profile(user, name)
        since = SINCE.get(request.args.get("since", "30m"), 1800)

        selector = f"{manifests.LABEL_PROFILE}={profile['metadata']['name']}"
        pods = self.k8s.list(f"/api/v1/namespaces/{self.namespace}/pods", {"labelSelector": selector})

        def pod_of(component: str) -> Optional[str]:
            for pod in pods:
                if pod["metadata"].get("labels", {}).get(manifests.LABEL_COMPONENT) == component:
                    return pod["metadata"]["name"]
            return None

        squid, dns = pod_of("squid"), pod_of("coredns")
        squid_log = self.k8s.logs(squid, "squid", since) if squid else ""
        dns_log = self.k8s.logs(dns, "coredns", since) if dns else ""

        return self.respond([item.as_dict() for item in blocked.parse(squid_log, dns_log)])

    # -- members' requests -----------------------------------------------------

    def request_views(self, requests: list[dict], profiles: list[dict]) -> list[dict]:
        """
        *requests* as the pages show them, for *profiles* (those there are):
        ``profileGone`` marks one whose profile has been deleted since (by
        kubectl: the admin's own deletions forget them), and ``notTheirs``
        one whose profile now belongs to someone else. Either can only be
        denied.
        """
        owners = {profile["metadata"]["name"]: self.owner_of(profile) for profile in profiles}

        return [
            {
                **request,
                "profileGone": request["profile"] not in owners,
                "notTheirs": request["profile"] in owners and owners[request["profile"]] != request["by"],
            }
            for request in requests
        ]

    def list_requests(self):
        """
        The members' requests (``request_views``): every one to an admin, a
        member's own to them; those waiting first, newest first, then those
        decided, the latest decision first.
        """
        user = self.require("full")

        visible = self.request_views(
            [request for request in self.approvals.list() if is_admin(user) or request["by"] == user["email"]],
            self.k8s.list(profiles_path(self.namespace)),
        )
        waiting = [request for request in visible if request["state"] == PENDING]
        decided = [request for request in visible if request["state"] != PENDING]

        waiting.sort(key=lambda request: request["createdAt"], reverse=True)
        decided.sort(key=lambda request: request["decidedAt"], reverse=True)

        return self.respond(waiting + decided)

    def decide(self, request_id: str, state: str, admin: dict) -> dict:
        """
        Mark a request approved or denied by *admin*: 404 when there is no
        such request, 409 when someone decided it already.
        """
        try:
            return self.approvals.decide(request_id, state, admin["email"])
        except NoSuchRequest as error:
            raise HttpError(404, str(error)) from error
        except ApprovalError as error:
            raise HttpError(409, str(error)) from error

    def approve_request(self, request_id: str):
        """
        Approve a member's request: the change it asks for is made, as an
        admin would make it. It is marked approved first, so that a second
        admin can't approve or deny it meanwhile, and pending again if the
        change can't be made (its profile has gone meanwhile, say).

        A request for a profile that has gone (404), or that no longer
        belongs to the member who asked (409), is refused before anything
        is marked: the admin was told it was for that member's profile.
        """
        admin = self.require_admin()

        asked = self.approvals.find(request_id)
        if asked is None:
            raise HttpError(404, "no such request")

        name = asked["profile"]
        profile = self.k8s.get(self.profile_path(name))
        if not profile:
            raise HttpError(404, f"profile '{name}' has been deleted: deny the request")
        if self.owner_of(profile) != asked["by"]:
            raise HttpError(409, f"{name} no longer belongs to {asked['by']}: deny the request")

        request = self.decide(request_id, APPROVED, admin)

        try:
            if request["kind"] == "lan":
                messages: list[str] = []
                changed = self.patch_allowlist_text(
                    name,
                    self.lan_edit(request["op"], request["entry"], request["address"], messages),
                )
            else:
                data = {"op": request["op"], "entry": request["entry"], "flag": request["flag"]}
                changed = self.patch_allowlist(name, data)

        except (HttpError, ApiError):
            self.approvals.reopen(request_id)
            raise

        return self.respond({"request": request, "changed": changed})

    def deny_request(self, request_id: str):
        """
        Deny a member's request: nothing changes, and they see it denied.
        """
        admin = self.require_admin()
        return self.respond({"request": self.decide(request_id, DENIED, admin)})

    # -- accounts --------------------------------------------------------------

    def account_view(self, user: dict, profiles: list[dict]) -> dict:
        """
        An account as the People page shows it: its role, a member's rights,
        and how many of *profiles* it owns.
        """
        admin = is_admin(user)

        return {
            "email": user["email"],
            "role": "admin" if admin else MEMBER,
            "rights": rights_of(user),
            "profiles": len(self.owned_profiles(user["email"], profiles)),
            "totpEnrolled": bool(user.get("totpEnrolled")),
            "createdAt": user.get("createdAt"),
        }

    def list_users(self):
        self.require_admin()
        profiles = self.k8s.list(profiles_path(self.namespace))

        return self.respond([self.account_view(user, profiles) for user in self.users.list()])

    def add_user(self):
        """
        A new account: a member (with ``rights``, or ``RIGHTS``) unless
        ``role`` says admin.
        """
        self.require_admin()
        data = self.body()

        self.users.add(
            str(data.get("email") or ""),
            str(data.get("password") or ""),
            data.get("role") or MEMBER,
            data.get("rights"),
        )

        return self.respond({"ok": True}, 201)

    def patch_user(self, email: str):
        """
        Change an account's ``role``, or a member's ``rights``, or both:
        they apply at the account's next request. The last admin can't be
        made a member.
        """
        self.require_admin()
        data = self.body()

        user = self.users.set_role(email, data.get("role"), data.get("rights"))
        profiles = self.k8s.list(profiles_path(self.namespace))

        return self.respond(self.account_view(user, profiles))

    def remove_user(self, email: str):
        """
        Remove an account, ending its sessions. The profiles it owned stay,
        for an admin to give to someone else or delete.
        """
        self.require_admin()
        self.users.remove(email)
        self.sessions.revoke(email)

        return self.respond({"ok": True})

    def reset_totp(self, email: str):
        """
        Make an account enrol in TOTP again at the next sign-in (yourself included).
        """
        self.require_admin()
        self.users.reset_totp(email)
        self.sessions.revoke(email)

        return self.respond({"ok": True})

    def set_password(self, email: str):
        """
        Set an account's password, signing it out everywhere: an admin may
        set anyone's, a member only their own. Changing your own keeps this
        session.
        """
        me = self.require("full")
        if not is_admin(me) and canonical(email) != me["email"]:
            raise HttpError(403, "only an admin can do that")

        user = self.users.set_password(email, str(self.body().get("password") or ""))

        if user["email"] == me["email"]:
            g.session.version = user["sessionVersion"]
        else:
            self.sessions.revoke(email)

        return self.respond({"ok": True})


def create_app(
    k8s: K8s,
    users: Users,
    defaults: Optional[dict] = None,
    cookie_secure: str = "auto",
    template_allowlist: Optional[str] = None,
    sessions: Optional[Sessions] = None,
    clock: Optional[Callable[[], float]] = None,
    trusted_proxies: Optional[list[str]] = None,
    resolve: lan.Resolver = lan.resolve,
    approvals: Optional[Approvals] = None,
    mailer: Optional[Mailer] = None,
    portal_url: str = "",
) -> Flask:
    """
    The admin's Flask app.

    *cookie_secure* is ``auto`` (secure when the request came in over HTTPS),
    ``always`` or ``never``. *clock* stands in for the time in tests, and
    *trusted_proxies* lists the addresses or networks whose X-Forwarded-For
    is believed. *resolve* stands in for the pod's name lookups.

    The members' requests are kept in the ``REQUESTS_SECRET`` Secret unless
    *approvals* says otherwise; *mailer* emails the admins about new ones
    (none without), with links into the portal at *portal_url* (its address
    as the admins open it, ``https://admin.example.com``).
    """
    api = AdminApi(
        k8s,
        users,
        defaults=defaults or {},
        cookie_secure=cookie_secure,
        template_allowlist=template_allowlist,
        sessions=sessions or Sessions(),
        clock=clock,
        trusted_proxies=trusted_proxies or [],
        approvals=approvals or Approvals(SecretStore(k8s, REQUESTS_SECRET, "requests.json")),
        mailer=mailer or Mailer(None),
        portal_url=portal_url,
        resolve=resolve,
    )
    return api.app
