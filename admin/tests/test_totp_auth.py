"""
Tests for TOTP (hardcode_admin.totp) and for accounts, sessions and the
sign-in throttle (hardcode_admin.auth).
"""

import base64
import threading
import time
import unittest
from unittest import mock

from hardcode_admin import auth, totp
from hardcode_admin.auth import ADMIN, MEMBER, AuthError, Sessions, StoreConflict, Throttle, Users

from .helpers import MemoryStore

# The secret RFC 6238's test vectors use for SHA-1.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")

PASSWORD = "longenoughpassword"


class FakeClock:
    """
    A clock the test moves by hand.
    """

    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TotpTests(unittest.TestCase):
    def test_rfc_6238_sha1_vectors(self):
        vectors = [
            (59, "287082"),
            (1111111109, "081804"),
            (1111111111, "050471"),
            (1234567890, "005924"),
            (2000000000, "279037"),
            (20000000000, "353130"),
        ]
        for moment, code in vectors:
            self.assertEqual(totp.totp(RFC_SECRET, moment), code, moment)

        self.assertEqual(totp.hotp(RFC_SECRET, 0), "755224")

        # Secrets are accepted the way people type them.
        self.assertEqual(totp.decode_secret(RFC_SECRET), b"12345678901234567890")
        self.assertEqual(totp.decode_secret("gezd gnbv gy3t qojq gezd gnbv gy3t qojq"), b"12345678901234567890")

    def test_verify_window_and_matching_step(self):
        secret = totp.generate_secret()
        now = 1_700_000_000
        step = now // 30

        # The current step and one either side, for clock drift; not two.
        self.assertEqual(totp.matching_step(secret, totp.totp(secret, now), now), step)
        self.assertEqual(totp.matching_step(secret, totp.totp(secret, now - 30), now), step - 1)
        self.assertEqual(totp.matching_step(secret, totp.totp(secret, now + 30), now), step + 1)
        self.assertIsNone(totp.matching_step(secret, totp.totp(secret, now - 90), now))

        self.assertTrue(totp.verify(secret, totp.totp(secret, now), now))
        self.assertFalse(totp.verify(secret, "12345", now))
        self.assertFalse(totp.verify(secret, "abcdef", now))
        self.assertFalse(totp.verify(secret, None, now))

    def test_secret_and_url(self):
        secret = totp.generate_secret()
        self.assertEqual(len(secret), 32)

        url = totp.otpauth_url(secret, "a@b.c")
        self.assertTrue(url.startswith("otpauth://totp/HardCode:a%40b.c?secret="))
        self.assertIn("issuer=HardCode", url)


class PasswordTests(unittest.TestCase):
    def test_hash_and_check(self):
        record = auth.hash_password("correct horse battery")

        self.assertTrue(auth.check_password("correct horse battery", record))
        self.assertFalse(auth.check_password("wrong", record))
        self.assertFalse(auth.check_password("x", None))
        self.assertFalse(auth.check_password("x", {"email": "a"}))

        self.assertNotEqual(auth.hash_password("same")["salt"], auth.hash_password("same")["salt"])

    def test_canonical(self):
        self.assertEqual(auth.canonical("  A@B.co "), "a@b.co")
        self.assertEqual(auth.canonical(None), "")

    def test_only_a_few_hashes_at_once(self):
        # Each scrypt takes 32 MB, and the admin's whole container 256 MiB:
        # eight sign-ins at once (the server's threads) mustn't hash at once.
        lock = threading.Lock()
        at_once = [0, 0]  # now, most

        def scrypt(*arguments, **options):
            with lock:
                at_once[0] += 1
                at_once[1] = max(at_once[1], at_once[0])
            time.sleep(0.05)
            with lock:
                at_once[0] -= 1
            return bytes(32)

        record = {"salt": "c2FsdA==", "hash": base64.b64encode(bytes(32)).decode()}
        with mock.patch.object(auth.hashlib, "scrypt", side_effect=scrypt):
            threads = [threading.Thread(target=auth.check_password, args=("guess", record)) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(at_once[1], auth.HASHES_AT_ONCE)
        self.assertLessEqual(auth.HASHES_AT_ONCE * 32, 64, "MB of scrypt at most")


class UsersTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.users = Users(self.store)

    def test_add_validation(self):
        with self.assertRaisesRegex(AuthError, "email"):
            self.users.add("not-an-email", PASSWORD, ADMIN)
        with self.assertRaisesRegex(AuthError, "12 characters"):
            self.users.add("a@b.co", "short", ADMIN)

        user = self.users.add("A@B.co", PASSWORD, ADMIN)
        self.assertEqual(user["email"], "a@b.co")
        self.assertFalse(user["totpEnrolled"])
        self.assertTrue(user["totpSecret"])
        self.assertEqual((user["sessionVersion"], user["totpLastStep"]), (0, -1))

        with self.assertRaisesRegex(AuthError, "exists"):
            self.users.add("a@b.co", PASSWORD, ADMIN)

        self.assertTrue(self.users.add("c@d.co", PASSWORD, ADMIN, must_enrol=False)["totpEnrolled"])

    def test_find_remove_update(self):
        self.users.add("a@b.co", PASSWORD, ADMIN)
        self.assertEqual(self.users.find(" A@b.co ")["email"], "a@b.co")
        self.assertIsNone(self.users.find("nope@b.co"))

        with self.assertRaisesRegex(AuthError, "last admin"):
            self.users.remove("a@b.co")

        self.users.add("c@d.co", "anotherlongpassword", ADMIN)
        with self.assertRaisesRegex(AuthError, "no such user"):
            self.users.remove("x@y.z")
        self.users.remove(" A@B.co ")
        self.assertEqual([user["email"] for user in self.users.list()], ["c@d.co"])

        with self.assertRaisesRegex(AuthError, "no such user"):
            self.users.update("x@y.z", totpEnrolled=True)
        with self.assertRaisesRegex(AuthError, "12 characters"):
            self.users.set_password("c@d.co", "short")

    def test_roles_and_rights(self):
        member = self.users.add("m@b.co", PASSWORD, MEMBER, {"allowlist": "ask", "rw": True})
        self.assertEqual(member["rights"], {**auth.RIGHTS, "allowlist": "ask", "rw": True})
        self.assertFalse(auth.is_admin(member))
        self.assertEqual(auth.rights_of({"role": MEMBER}), auth.RIGHTS, "a member's rights start as RIGHTS")

        self.assertTrue(auth.is_admin(self.users.add("a@b.co", PASSWORD, ADMIN)))
        self.assertEqual([user["email"] for user in self.users.admins()], ["a@b.co"])

        for role, rights, words in [
            ("owner", None, "role must be admin or member"),
            (MEMBER, {"profiles": True}, "profiles is a number"),
            (MEMBER, {"profiles": auth.MAX_PROFILES + 1}, "profiles is a number"),
            (MEMBER, {"allowlist": "always"}, "allowlist is none, ask or edit"),
            (MEMBER, {"rw": "yes"}, "rw is true or false"),
            (MEMBER, {"everything": True}, "no such right everything"),
            (MEMBER, ["edit"], "an object"),
        ]:
            with self.assertRaisesRegex(AuthError, words):
                self.users.add("x@y.co", PASSWORD, role, rights)

        changed = self.users.set_role("m@b.co", rights={"profiles": 3, "allowlist": "edit"})
        self.assertEqual(changed["rights"], {**auth.RIGHTS, "profiles": 3, "allowlist": "edit"}, "a whole set")
        self.assertEqual(self.users.set_role("m@b.co", role=ADMIN)["role"], ADMIN)

    def test_there_is_always_an_admin(self):
        self.users.add("a@b.co", PASSWORD, ADMIN)
        self.users.add("m@b.co", PASSWORD, MEMBER)

        with self.assertRaisesRegex(AuthError, "the last admin can't be removed"):
            self.users.remove("a@b.co")
        with self.assertRaisesRegex(AuthError, "the last admin can't be made a member"):
            self.users.set_role("a@b.co", role=MEMBER)

        self.users.add("c@d.co", PASSWORD, ADMIN)
        self.users.set_role("a@b.co", role=MEMBER)
        self.users.remove("m@b.co")
        self.users.remove("a@b.co")
        self.assertEqual([user["email"] for user in self.users.list()], ["c@d.co"])

    def test_credential_changes_bump_the_session_version(self):
        self.users.add("c@d.co", PASSWORD, ADMIN)
        before = self.users.find("c@d.co")

        self.users.consume_totp_step("c@d.co", 1234, 0)
        user = self.users.find("c@d.co")
        self.assertEqual(
            (user["totpLastStep"], user["sessionVersion"]),
            (1234, 0),
            "accepting a code isn't a credential change",
        )

        after = self.users.reset_totp("c@d.co")
        self.assertNotEqual(after["totpSecret"], before["totpSecret"])
        self.assertEqual((after["totpEnrolled"], after["totpLastStep"], after["sessionVersion"]), (False, -1, 1))

        self.users.set_password("c@d.co", "a-new-long-password")
        self.assertTrue(auth.check_password("a-new-long-password", self.users.find("c@d.co")))
        self.assertEqual(self.users.find("c@d.co")["sessionVersion"], 2)

    def test_a_code_step_is_consumed_once(self):
        self.users.add("a@b.co", PASSWORD, ADMIN)
        self.users.consume_totp_step("a@b.co", 500, 0)

        with self.assertRaisesRegex(AuthError, "already used"):
            self.users.consume_totp_step("a@b.co", 500, 0)
        with self.assertRaisesRegex(AuthError, "already used"):
            self.users.consume_totp_step("a@b.co", 499, 0)
        with self.assertRaisesRegex(AuthError, "revoked"):
            self.users.consume_totp_step("a@b.co", 501, 7)

        self.users.consume_totp_step("a@b.co", 501, 0)

    def test_concurrent_requests_cannot_both_consume_a_code(self):
        self.users.add("a@b.co", PASSWORD, ADMIN)
        outcomes: list[str] = []
        start_together = threading.Barrier(8)

        def attempt():
            start_together.wait()
            try:
                self.users.consume_totp_step("a@b.co", 900, 0)
                outcomes.append("ok")
            except AuthError:
                outcomes.append("used")

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(outcomes.count("ok"), 1, outcomes)

    def test_writes_through_one_store_are_serialised_and_versioned(self):
        self.users.add("a@b.co", PASSWORD, ADMIN)
        self.users.add("b@b.co", PASSWORD, ADMIN)
        started, release = threading.Event(), threading.Event()

        def slow(users):
            # A request that has read the accounts and is still working on them.
            started.set()
            release.wait(5)
            next(user for user in users if user["email"] == "a@b.co")["totpLastStep"] = 42

        slow_write = threading.Thread(target=self.users._modify, args=(slow,))
        slow_write.start()
        started.wait(5)

        fast_write = threading.Thread(target=self.users.set_password, args=("b@b.co", "a-brand-new-password"))
        fast_write.start()
        fast_write.join(0.3)
        self.assertTrue(fast_write.is_alive(), "the second write waits for the first instead of racing it")

        release.set()
        slow_write.join(5)
        fast_write.join(5)

        a, b = self.users.find("a@b.co"), self.users.find("b@b.co")
        self.assertEqual(a["totpLastStep"], 42)
        self.assertTrue(auth.check_password("a-brand-new-password", b), "neither write undid the other")
        self.assertEqual(b["sessionVersion"], 1)

    def test_a_writer_elsewhere_is_noticed_and_the_edit_reapplied(self):
        self.users.add("a@b.co", PASSWORD, ADMIN)
        calls = []

        def mutate(users):
            calls.append(1)

            # The first time, someone else changes the Secret after we loaded it.
            if len(calls) == 1:
                outside, _ = self.store.load()
                outside.append({"email": "x@y.co", "sessionVersion": 0})
                self.store.write_externally(outside)

            next(user for user in users if user["email"] == "a@b.co")["totpLastStep"] = 7

        self.users._modify(mutate)

        self.assertEqual(len(calls), 2, "the first save was refused, the edit was redone on the newer snapshot")
        self.assertEqual([user["email"] for user in self.users.list()], ["a@b.co", "x@y.co"])
        self.assertEqual(self.users.find("a@b.co")["totpLastStep"], 7)

    def test_updates_retry_when_the_store_moved(self):
        self.users.add("a@b.co", PASSWORD, ADMIN)

        self.store.conflicts = 2
        self.users.add("c@d.co", PASSWORD, ADMIN)
        self.assertEqual(len(self.users.list()), 2)
        self.assertEqual(self.store.saves, 4, "two refusals, then the write that counted")

        self.store.conflicts = Users.RETRIES
        with self.assertRaises(StoreConflict):
            self.users.set_password("a@b.co", "another-long-password")


class SessionTests(unittest.TestCase):
    def test_lifecycle_and_expiry(self):
        clock = FakeClock(1000.0)
        sessions = Sessions(ttl=100, idle=10, challenge_ttl=50, clock=clock)

        sid = sessions.create("a@b.co", "password", version=3)
        self.assertEqual((sessions.get(sid).stage, sessions.get(sid).version), ("password", 3))
        sessions.upgrade(sid, "full")
        self.assertEqual(sessions.get(sid).stage, "full")

        clock.advance(11)
        self.assertIsNone(sessions.get(sid), "idle timeout")

        sid = sessions.create("a@b.co", "full")
        for _ in range(11):
            clock.advance(9)
            self.assertIsNotNone(sessions.get(sid), "activity keeps it alive past the idle timeout")
        clock.advance(9)
        self.assertIsNone(sessions.get(sid), "but not past the absolute lifetime")

        self.assertIsNone(sessions.get(None))

        sid = sessions.create("a@b.co", "full")
        sessions.destroy(sid)
        self.assertIsNone(sessions.get(sid))

        # Nothing to destroy or upgrade is not an error.
        sessions.destroy(None)
        sessions.upgrade("missing", "full")

    def test_a_look_without_touching_does_not_hold_off_the_idle_timeout(self):
        clock = FakeClock(1000.0)
        sessions = Sessions(ttl=100, idle=10, clock=clock)

        sid = sessions.create("a@b.co", "full")
        clock.advance(8)
        self.assertIsNotNone(sessions.get(sid, touch=False), "a background refresh still finds the session")
        clock.advance(3)
        self.assertIsNone(sessions.get(sid, touch=False), "but doesn't count as activity")

        sid = sessions.create("a@b.co", "full")
        clock.advance(8)
        self.assertIsNotNone(sessions.get(sid), "someone using it")
        clock.advance(8)
        self.assertIsNotNone(sessions.get(sid, touch=False), "idle since that use, not since it began")

    def test_password_stage_is_short_lived(self):
        clock = FakeClock()
        sessions = Sessions(ttl=1000, idle=1000, challenge_ttl=50, clock=clock)

        sid = sessions.create("a@b.co", "password")
        for _ in range(6):
            clock.advance(8)
            self.assertIsNotNone(sessions.get(sid))
        clock.advance(8)
        self.assertIsNone(sessions.get(sid), "the second-factor challenge expired")

        sid = sessions.create("a@b.co", "password")
        sessions.upgrade(sid, "full")
        clock.advance(100)
        self.assertIsNotNone(sessions.get(sid), "a full session isn't bound by the challenge lifetime")

    def test_failures_and_revocation(self):
        sessions = Sessions()
        a_full = sessions.create("a@b.co", "full")
        a_challenge = sessions.create("A@B.co", "password")
        b_full = sessions.create("b@b.co", "full")

        self.assertEqual([sessions.fail(a_challenge) for _ in range(3)], [1, 2, 3])
        self.assertEqual(sessions.fail("missing"), 0)

        self.assertEqual(sessions.revoke(" a@b.co "), 2)
        self.assertIsNone(sessions.get(a_full))
        self.assertIsNone(sessions.get(a_challenge))
        self.assertIsNotNone(sessions.get(b_full))
        self.assertEqual(sessions.revoke("a@b.co"), 0)

    def test_expired_sessions_are_dropped_on_create(self):
        clock = FakeClock()
        sessions = Sessions(ttl=100, idle=10, clock=clock)
        for _ in range(5):
            sessions.create("a@b.co", "full")

        clock.advance(11)
        sessions.create("a@b.co", "full")

        self.assertEqual(len(sessions._map), 1)


class ThrottleTests(unittest.TestCase):
    def test_blocks_after_limit_within_window(self):
        clock = FakeClock()
        throttle = Throttle(limit=2, window=60, clock=clock)

        throttle.fail("k")
        self.assertFalse(throttle.blocked("k"))
        throttle.fail("k")
        self.assertTrue(throttle.blocked("k"))

        clock.advance(61)
        self.assertFalse(throttle.blocked("k"), "the window passed")
        throttle.fail("k")
        self.assertFalse(throttle.blocked("k"), "old failures don't count")

        throttle.fail("k")
        throttle.clear("k")
        self.assertFalse(throttle.blocked("k"))

    def test_old_windows_are_forgotten(self):
        clock = FakeClock()
        throttle = Throttle(limit=2, window=60, clock=clock)
        for number in range(50):
            throttle.fail(f"addr{number}")

        clock.advance(61)
        throttle.fail("fresh")

        self.assertEqual(list(throttle._hits), ["fresh"], "the map doesn't grow with every address ever tried")


if __name__ == "__main__":
    unittest.main()
