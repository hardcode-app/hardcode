"""
Who may use the admin: accounts (email, scrypt password and TOTP) kept in a
Kubernetes Secret, in-memory sessions, and a throttle for failed sign-ins.

An account is an admin's, who can do everything, or a member's, who sees
and manages only the profiles they own, with the rights an admin gives
them (``RIGHTS``): how many profiles they may create, and what they may do
with their profiles' allowlists.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from . import totp

EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MIN_PASSWORD = 12

# An account's role: an admin can do everything; a member works on the
# profiles they own, with the rights below.
ADMIN = "admin"
MEMBER = "member"
ROLES = (ADMIN, MEMBER)

# What a member may do with their profiles' allowlists: nothing (an admin
# edits them), ask for entries (an admin approves each), or edit them.
ALLOWLIST_RIGHTS = ("none", "ask", "edit")

# A member's rights, as a new member gets them: one profile of their own,
# and an allowlist only an admin edits. Under "ask" and "edit", rw, ssh and
# lan say whether they may also have read-write entries, ssh tunnels and
# names on the network.
RIGHTS = {"profiles": 1, "allowlist": "none", "rw": False, "ssh": False, "lan": False}

# The most profiles an admin may let one member have.
MAX_PROFILES = 100

# scrypt at 2^15 iterations: about 100 ms and 32 MB per check on a server.
# maxmem has to allow for that, as the default is lower.
SCRYPT = {"n": 2**15, "r": 8, "p": 1, "dklen": 32, "maxmem": 64 * 1024 * 1024}

# How many hashes run at once; the others wait their turn. The server has 8
# threads and the admin's container 256 MiB (the chart's limit): 8 sign-ins
# at once, hashing together, were 256 MB of scrypt, and anyone who can reach
# the sign-in page could have the admin and its operator killed.
HASHES_AT_ONCE = 2
_hashing = threading.BoundedSemaphore(HASHES_AT_ONCE)


class AuthError(ValueError):
    """
    An account operation that can't be done.
    """


class StoreConflict(Exception):
    """
    The store changed since it was read; read it again and retry.
    """


def canonical(email: object) -> str:
    """
    The one spelling of an address used for lookups, throttling and sessions.
    """
    return str(email or "").strip().lower()


def is_admin(user: dict) -> bool:
    """
    Whether *user* is an admin's account. Any other is a member's.
    """
    return user.get("role") == ADMIN


def rights_of(user: dict) -> dict:
    """
    What *user* may do as a member: their rights, with ``RIGHTS`` for any
    that weren't given.
    """
    return {**RIGHTS, **(user.get("rights") or {})}


def check_role(role: object) -> str:
    """
    A role from a request: ``admin`` or ``member``.
    """
    if role not in ROLES:
        raise AuthError("role must be admin or member")

    return str(role)


def check_rights(rights: object) -> dict:
    """
    A member's rights from a request, checked: the fields of ``RIGHTS``
    that *rights* has, and ``RIGHTS``' values for the others.
    """
    if not isinstance(rights, dict):
        raise AuthError("rights: an object with profiles, allowlist, rw, ssh and lan")

    unknown = sorted(set(rights) - set(RIGHTS))
    if unknown:
        raise AuthError(f"rights: no such right {', '.join(unknown)}")

    checked = {**RIGHTS, **rights}

    # A JSON true is a bool, which Python counts as an int: it isn't a number.
    profiles = checked["profiles"]
    if isinstance(profiles, bool) or not isinstance(profiles, int) or not 0 <= profiles <= MAX_PROFILES:
        raise AuthError(f"rights: profiles is a number from 0 to {MAX_PROFILES}")

    if checked["allowlist"] not in ALLOWLIST_RIGHTS:
        raise AuthError("rights: allowlist is none, ask or edit")

    for flag in ("rw", "ssh", "lan"):
        if not isinstance(checked[flag], bool):
            raise AuthError(f"rights: {flag} is true or false")

    return checked


def hash_password(password: str, salt: Optional[bytes] = None) -> dict:
    """
    A salted scrypt hash of *password*, as the account stores it; no more
    than ``HASHES_AT_ONCE`` at a time.
    """
    salt = secrets.token_bytes(16) if salt is None else salt
    with _hashing:
        digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)

    return {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(digest).decode()}


def check_password(password: str, user: Optional[dict]) -> bool:
    """
    Whether *password* is *user*'s. Without a user (or a hash), never.
    """
    if not user or not user.get("salt") or not user.get("hash"):
        return False

    computed = hash_password(password, base64.b64decode(user["salt"]))["hash"]

    # Constant-time, so the comparison doesn't leak how close a guess was.
    return hmac.compare_digest(base64.b64decode(computed), base64.b64decode(user["hash"]))


def modify_versioned(
    store,
    lock: threading.Lock,
    mutate: Callable[[list[dict]], object],
    retries: int = 4,
) -> object:
    """
    Load a list from a versioned *store*, let *mutate* edit it, and save it
    under the version that was loaded; returns what *mutate* returned.

    The store has ``load() -> (items, version)`` and ``save(items, version)``,
    and ``save`` raises ``StoreConflict`` when the store moved since that
    ``load``. The version travels with the snapshot it belongs to, never
    through shared state. Writes from this process are serialised by *lock*;
    one that finds the store changed by a writer elsewhere starts over from
    a fresh load, so two admins working at once don't lose each other's edits.
    """
    with lock:
        for attempt in range(retries):
            items, version = store.load()
            result = mutate(items)

            try:
                store.save(items, version)
            except StoreConflict:
                if attempt == retries - 1:
                    raise
                continue

            return result

    return None  # pragma: no cover - the loop returns or raises


class Users:
    """
    The accounts, admins' and members', behind a versioned store
    (``modify_versioned``).

    Every account carries a ``sessionVersion``. A password change or a TOTP
    reset bumps it, and sessions created under an older version stop working.
    There is always an admin: the last one can't be removed or made a member.
    """

    RETRIES = 4

    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        return self.store.load()[0]

    def find(self, email: str) -> Optional[dict]:
        wanted = canonical(email)
        return next((user for user in self.list() if user["email"] == wanted), None)

    def admins(self) -> list[dict]:
        """
        The admins' accounts.
        """
        return [user for user in self.list() if is_admin(user)]

    def _modify(self, mutate: Callable[[list[dict]], object]) -> object:
        """
        Load the accounts, let *mutate* edit them, and save them under the
        version that was loaded (``modify_versioned``).
        """
        return modify_versioned(self.store, self._lock, mutate, self.RETRIES)

    @staticmethod
    def _one(users: list[dict], email: str) -> dict:
        """
        The account for *email* in *users*; an error if there is none.
        """
        user = next((user for user in users if user["email"] == canonical(email)), None)
        if user is None:
            raise AuthError("no such user")

        return user

    def add(
        self,
        email: str,
        password: str,
        role: str,
        rights: Optional[dict] = None,
        must_enrol: bool = True,
    ) -> dict:
        """
        Create an account with *role*, and for a member *rights* (``RIGHTS``
        for any not given). Unless *must_enrol* is false, its owner sets up
        TOTP at the first sign-in.
        """
        address = canonical(email)
        if not EMAIL.match(address):
            raise AuthError("not an email address")
        if len(str(password or "")) < MIN_PASSWORD:
            raise AuthError(f"password must be at least {MIN_PASSWORD} characters")

        role = check_role(role)
        rights = check_rights(rights or {})

        def mutate(users: list[dict]) -> dict:
            if any(user["email"] == address for user in users):
                raise AuthError("that user already exists")

            user = {
                "email": address,
                "role": role,
                "rights": rights,
                **hash_password(password),
                "totpSecret": totp.generate_secret(),
                "totpEnrolled": not must_enrol,
                "totpLastStep": -1,
                "sessionVersion": 0,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
            users.append(user)
            return user

        return self._modify(mutate)  # type: ignore[return-value]

    @staticmethod
    def _keep_an_admin(users: list[dict], address: str, doing: str) -> None:
        """
        Refuse *doing* to the account *address* when it is the last admin's:
        with no admin left, nobody could manage the accounts.
        """
        others = [user for user in users if is_admin(user) and user["email"] != address]
        if not others:
            raise AuthError(f"the last admin can't be {doing}")

    def remove(self, email: str) -> None:
        """
        Delete an account. The last admin's stays, so nobody locks everyone out.
        """
        address = canonical(email)

        def mutate(users: list[dict]) -> None:
            if is_admin(self._one(users, address)):
                self._keep_an_admin(users, address, "removed")

            users[:] = [user for user in users if user["email"] != address]

        self._modify(mutate)

    def set_role(self, email: str, role: Optional[str] = None, rights: Optional[dict] = None) -> dict:
        """
        Change an account's role, or a member's rights, or both. They apply
        at the account's next request: every request reads the account.
        The last admin can't be made a member.
        """
        address = canonical(email)
        role = check_role(role) if role is not None else None
        rights = check_rights(rights) if rights is not None else None

        def mutate(users: list[dict]) -> dict:
            user = self._one(users, address)

            if role == MEMBER and is_admin(user):
                self._keep_an_admin(users, address, "made a member")

            if role is not None:
                user["role"] = role
            if rights is not None:
                user["rights"] = rights

            return user

        return self._modify(mutate)  # type: ignore[return-value]

    def update(self, email: str, **fields) -> dict:
        """
        Change fields of an account.
        """

        def mutate(users: list[dict]) -> dict:
            user = self._one(users, email)
            user.update(fields)
            return user

        return self._modify(mutate)  # type: ignore[return-value]

    def _bump(self, email: str, **fields) -> dict:
        """
        Change fields of an account and end its existing sessions.
        """

        def mutate(users: list[dict]) -> dict:
            user = self._one(users, email)
            user.update(fields)
            user["sessionVersion"] = int(user.get("sessionVersion", 0)) + 1
            return user

        return self._modify(mutate)  # type: ignore[return-value]

    def set_password(self, email: str, password: str) -> dict:
        """
        Set a new password, signing the account out everywhere.
        """
        if len(str(password or "")) < MIN_PASSWORD:
            raise AuthError(f"password must be at least {MIN_PASSWORD} characters")

        return self._bump(email, **hash_password(password))

    def reset_totp(self, email: str) -> dict:
        """
        Make the account enrol in TOTP again with a new secret, signing it
        out everywhere.
        """
        return self._bump(email, totpSecret=totp.generate_secret(), totpEnrolled=False, totpLastStep=-1)

    def consume_totp_step(self, email: str, step: int, version: int) -> dict:
        """
        Accept a code for time step *step*, exactly once.

        The check and the record are one write against the current account,
        so two requests carrying the same code can't both get in. *version* is
        the session's, in case the credentials changed since the password step.
        """

        def mutate(users: list[dict]) -> dict:
            user = self._one(users, email)

            if int(user.get("sessionVersion", 0)) != version:
                raise AuthError("session revoked; sign in again")
            if int(user.get("totpLastStep", -1)) >= step:
                raise AuthError("code already used")

            user.update(totpLastStep=step, totpEnrolled=True)
            return user

        return self._modify(mutate)  # type: ignore[return-value]


@dataclass
class Session:
    """
    One signed-in browser.

    ``stage`` is "password" after the first factor and "full" after TOTP.
    ``version`` is the account's ``sessionVersion`` when the session began;
    ``attempts`` counts wrong second-factor codes.
    """

    email: str
    stage: str
    created: float
    seen: float
    version: int = 0
    attempts: int = 0


@dataclass
class Sessions:
    """
    Sessions by random id, with a bounded lifetime, an idle timeout, and a
    short life for the password-only stage (the second-factor challenge).

    gunicorn serves requests from several threads, hence the lock.
    """

    ttl: float = 12 * 3600
    idle: float = 2 * 3600
    challenge_ttl: float = 5 * 60
    clock: Callable[[], float] = time.time
    _map: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _expired(self, session: Session, now: float) -> bool:
        too_old = now - session.created > self.ttl
        idle_too_long = now - session.seen > self.idle
        challenge_timed_out = session.stage == "password" and now - session.created > self.challenge_ttl

        return too_old or idle_too_long or challenge_timed_out

    def create(self, email: str, stage: str, version: int = 0) -> str:
        """
        Start a session and return its id. Expired sessions are cleared out
        on the way.
        """
        now = self.clock()
        session_id = secrets.token_urlsafe(32)

        with self._lock:
            for stale in [sid for sid, session in self._map.items() if self._expired(session, now)]:
                del self._map[stale]

            self._map[session_id] = Session(email, stage, now, now, version)

        return session_id

    def get(self, session_id: Optional[str], touch: bool = True) -> Optional[Session]:
        """
        The live session with this id; ``None`` if there is none or it expired.

        With *touch* (a request someone made) the session is marked as just
        seen, which holds off the idle timeout. Without it (the page's own
        background refresh) it isn't: an open tab nobody looks at still
        times out.
        """
        now = self.clock()

        with self._lock:
            session = self._map.get(session_id or "")
            if session is None:
                return None

            if self._expired(session, now):
                del self._map[session_id]
                return None

            if touch:
                session.seen = now

            return session

    def upgrade(self, session_id: str, stage: str) -> None:
        """
        Move a session to *stage*: "full" once the second factor checks out.
        """
        with self._lock:
            if session_id in self._map:
                self._map[session_id].stage = stage

    def fail(self, session_id: Optional[str]) -> int:
        """
        Count a wrong second-factor code; returns the total so far.
        """
        with self._lock:
            session = self._map.get(session_id or "")
            if session is None:
                return 0

            session.attempts += 1
            return session.attempts

    def destroy(self, session_id: Optional[str]) -> None:
        """
        End one session.
        """
        with self._lock:
            self._map.pop(session_id or "", None)

    def revoke(self, email: str) -> int:
        """
        End every session of an account; returns how many there were.
        """
        with self._lock:
            doomed = [sid for sid, session in self._map.items() if canonical(session.email) == canonical(email)]
            for sid in doomed:
                del self._map[sid]

        return len(doomed)


@dataclass
class Throttle:
    """
    Blocks a key (an address, an account) after *limit* failures within
    *window* seconds of the first.
    """

    limit: int = 8
    window: float = 15 * 60
    clock: Callable[[], float] = time.time
    _hits: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def blocked(self, key: str) -> bool:
        """
        Whether *key* has used up its failures for the current window.
        """
        with self._lock:
            hit = self._hits.get(key)
            if not hit:
                return False

            return hit["count"] >= self.limit and self.clock() - hit["first"] < self.window

    def fail(self, key: str) -> None:
        """
        Count a failure for *key*.
        """
        now = self.clock()

        with self._lock:
            # Windows that have passed are forgotten, so the map doesn't grow
            # with every address that ever tried.
            for stale in [key for key, hit in self._hits.items() if now - hit["first"] > self.window]:
                del self._hits[stale]

            hit = self._hits.get(key)
            if hit is None:
                self._hits[key] = {"first": now, "count": 1}
            else:
                hit["count"] += 1

    def clear(self, key: str) -> None:
        """
        Forget *key*'s failures, after a success.
        """
        with self._lock:
            self._hits.pop(key, None)
