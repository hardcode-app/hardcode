"""
Members' requests for allowlist entries, waiting for an admin to approve
or deny them.

A member whose rights say ``ask`` doesn't change their profile's allowlist:
each entry they add (or switch back on), and each name they let reach an
address on the network, becomes a request. An admin approves it, which
makes the change, or denies it. The requests are kept in a Secret of their
own (``requests.json``), versioned like the accounts, so that two admins
deciding at once can't undo each other.

A request is a dict:

    id          random, for the links in the admins' emails
    profile     the profile's name
    by          the member's email
    kind        "entry" (an allowlist entry) or "lan" (a name on the network)
    op          "add" or "enable" (an entry); "add" (a name on the network)
    entry       the entry, or the name
    flag        the edit's flag, as it was asked for: "", "rw" or "ssh"
    access      what the change would let the sandbox do, which the admin is
                shown: "" (read-only), "rw" or "ssh"; "" for a name
    address     the address a name may reach ("" for an entry)
    reason      what the member said, if anything
    createdAt   when it was made
    state       "pending", "approved" or "denied"
    decidedBy   the admin who decided, and decidedAt when
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from .auth import canonical, modify_versioned

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"

# A member's requests waiting at once, at most: each one is an email to
# every admin.
PENDING_PER_MEMBER = 20

# Everyone's requests waiting at once, at most, and the decided ones kept
# (the latest: what a member sees became of theirs). With each request at
# most REQUEST_BYTES, all of them, base64-encoded in the Secret, stay well
# under the 1 MiB Kubernetes allows one; past that, nobody's request could
# be made or decided.
PENDING_AT_MOST = 150
DECIDED_KEPT = 100
REQUEST_BYTES = 2048

# The longest reason a request may give.
REASON_LENGTH = 500


class ApprovalError(ValueError):
    """
    A request that can't be made or decided, with a message to show.
    """


class NoSuchRequest(ApprovalError):
    """
    A request that isn't there: never made, or decided long ago.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def same_change(one: dict, other: dict) -> bool:
    """
    Whether two requests are one person's for the same change of the same
    profile. Someone else's for it is another request: its reason is
    theirs, and an approval is made for whoever asked.
    """
    fields = ("profile", "kind", "op", "entry", "flag", "address")
    same_person = canonical(one.get("by")) == canonical(other.get("by"))

    return same_person and all(one.get(field) == other.get(field) for field in fields)


class Approvals:
    """
    The requests, behind a versioned store (``auth.modify_versioned``).
    *clock* gives the time as an ISO string and *new_id* a request's id, so
    that tests can fix both.
    """

    RETRIES = 4

    def __init__(
        self,
        store,
        clock: Callable[[], str] = _now,
        new_id: Callable[[], str] = lambda: secrets.token_urlsafe(9),
    ):
        self.store = store
        self.clock = clock
        self.new_id = new_id
        self._lock = threading.Lock()

    def list(self) -> list[dict]:
        """
        Every request kept: the pending ones and the latest decided.
        """
        return self.store.load()[0]

    def find(self, request_id: str) -> Optional[dict]:
        return next((request for request in self.list() if request["id"] == request_id), None)

    def _modify(self, mutate: Callable[[list[dict]], object]) -> object:
        return modify_versioned(self.store, self._lock, mutate, self.RETRIES)

    def ask(self, change: dict, reason: str = "") -> tuple[dict, bool]:
        """
        A member's request for *change* (the fields from ``profile`` to
        ``address``, and ``by``), and whether it is new: their request for
        the same change that is still pending is that one, not another.
        """
        reason = str(reason or "").strip()
        if len(reason) > REASON_LENGTH:
            raise ApprovalError(f"the reason is at most {REASON_LENGTH} characters")

        by = canonical(change["by"])
        asked = {**change, "by": by}

        def mutate(requests: list[dict]) -> tuple[dict, bool]:
            pending = [request for request in requests if request["state"] == PENDING]

            waiting = next((request for request in pending if same_change(request, asked)), None)
            if waiting:
                return waiting, False

            if sum(1 for request in pending if request["by"] == by) >= PENDING_PER_MEMBER:
                raise ApprovalError(
                    f"you have {PENDING_PER_MEMBER} requests waiting; wait until an admin has decided on some"
                )
            if len(pending) >= PENDING_AT_MOST:
                raise ApprovalError(
                    f"{PENDING_AT_MOST} requests are waiting for the admins; ask one to decide on some first"
                )

            request = {
                "id": self.new_id(),
                **change,
                "by": by,
                "reason": reason,
                "createdAt": self.clock(),
                "state": PENDING,
                "decidedBy": "",
                "decidedAt": "",
            }

            # Measured as it is stored: an entry can be long (a path), and
            # characters outside ASCII take six bytes each in the JSON.
            if len(json.dumps(request)) > REQUEST_BYTES:
                raise ApprovalError("this request is too long: shorten the entry or the reason")

            requests.append(request)
            return request, True

        return self._modify(mutate)  # type: ignore[return-value]

    def decide(self, request_id: str, state: str, by: str) -> dict:
        """
        Mark a pending request *state* (approved or denied) by the admin
        *by*. A request someone has decided meanwhile is refused, saying who.
        Only the latest ``DECIDED_KEPT`` decided requests are kept.
        """

        def mutate(requests: list[dict]) -> dict:
            request = next((request for request in requests if request["id"] == request_id), None)
            if request is None:
                raise NoSuchRequest("no such request")
            if request["state"] != PENDING:
                raise ApprovalError(f"{request['decidedBy']} has {request['state']} this request already")

            request.update(state=state, decidedBy=canonical(by), decidedAt=self.clock())

            decided = [item for item in requests if item["state"] != PENDING]
            dropped = {item["id"] for item in decided[:-DECIDED_KEPT]}
            requests[:] = [item for item in requests if item["id"] not in dropped]

            return request

        return self._modify(mutate)  # type: ignore[return-value]

    def reopen(self, request_id: str) -> None:
        """
        Make a request pending again: its approval couldn't be applied.
        """

        def mutate(requests: list[dict]) -> None:
            for request in requests:
                if request["id"] == request_id:
                    request.update(state=PENDING, decidedBy="", decidedAt="")

        self._modify(mutate)

    def drop_profile(self, name: str) -> None:
        """
        Forget the pending requests of the profile *name*, which is being
        deleted: there is nothing left to allow them in.
        """

        def mutate(requests: list[dict]) -> None:
            requests[:] = [request for request in requests if request["profile"] != name or request["state"] != PENDING]

        self._modify(mutate)
