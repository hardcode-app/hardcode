"""
The HTTP API end to end, with Flask's test client: sign-in with TOTP
enrolment, revocation, the second-factor and password limits, the idle
timeout, cross-site refusals, profiles (names, deletion), languages and
extensions (and how far a VS Code container has got with installing them),
allowlist edits (single, bulk, and racing each other), settings, admins, and
signing out.
"""

import base64
import unittest
from pathlib import Path
from unittest import mock

from hardcode_admin import manifests, totp, web
from hardcode_admin.auth import Sessions, StoreConflict, Users
from hardcode_admin.web import (
    ACCOUNT_CODE_ATTEMPTS,
    MAX_CODE_ATTEMPTS,
    MAX_EXTENSIONS,
    SecretStore,
    create_app,
    extension_status_from_log,
    qr_data_url,
)
from hardcode_cli import langs

from .helpers import FakeK8s, MemoryStore

try:
    import yaml
except ImportError:  # pragma: no cover - the build image has PyYAML
    yaml = None

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE = str(REPO / "template" / "allowlist.txt")
CRD = REPO / "helm" / "hardcode" / "crds" / "profiles.hardcode-app.github.io.yaml"
JSON = {"Content-Type": "application/json"}
POLL = {"X-HardCode-Poll": "1"}
PROFILE_PATH = "/apis/hardcode-app.github.io/v1alpha1/namespaces/test/profiles"

ROOT = "root@example.com"
ROOT_PASSWORD = "bootstrap-password"
SECOND = "second@example.com"
SECOND_PASSWORD = "a-long-enough-password"


class WebTests(unittest.TestCase):
    def setUp(self):
        self.k8s = FakeK8s()
        self.users = Users(MemoryStore())
        self.users.add(ROOT, ROOT_PASSWORD)
        self.now = [1_700_000_000.0]

        self.app = create_app(
            self.k8s,
            self.users,
            {"image": "img:1"},
            cookie_secure="never",
            template_allowlist=TEMPLATE,
            clock=lambda: self.now[0],
            trusted_proxies=["10.0.0.0/8"],
        )
        self.app.testing = True
        self.client = self.app.test_client()

    # -- helpers ---------------------------------------------------------------

    def code(self, email=ROOT):
        """
        The account's current TOTP code, as its authenticator app would show it.
        """
        return totp.totp(self.users.find(email)["totpSecret"], self.now[0])

    def login(self, email=ROOT, password=ROOT_PASSWORD, client=None, **options):
        """
        The password step of signing in.
        """
        return (client or self.client).post("/api/login", json={"email": email, "password": password}, **options)

    def sign_in(self, client=None, email=ROOT, password=ROOT_PASSWORD):
        """
        Both steps of signing in, in a fresh time step (a code is good once).
        """
        client = client or self.client
        self.now[0] += 30

        self.assertEqual(self.login(email, password, client).status_code, 200)
        self.assertEqual(client.post("/api/totp", json={"code": self.code(email)}).status_code, 200)

    def add_second_admin(self):
        """
        A second admin, already enrolled in TOTP, signed in on a client of its own.
        """
        self.users.add(SECOND, SECOND_PASSWORD, must_enrol=False)
        other = self.app.test_client()
        self.sign_in(other, SECOND, SECOND_PASSWORD)
        return other

    def stored_spec(self, name):
        return self.k8s.get(f"{PROFILE_PATH}/{name}")["spec"]

    def allowlist_of(self, name, client=None):
        return (client or self.client).get(f"/api/profiles/{name}").json["allowlist"]

    def entry(self, profile, name, flag=None):
        """
        One entry of a profile's allowlist, as the API shows it.
        """
        for entry in self.allowlist_of(profile):
            if entry["entry"] == name and (flag is None or entry["flag"] == flag):
                return entry
        return None

    def provided_languages(self, name, client=None):
        """
        The languages whose entries a profile's allowlist has, as the API
        (*client*'s, or the test's own) shows it.
        """
        entries = self.allowlist_of(name, client)
        return sorted({entry["language"] for entry in entries if entry["provided"] and entry["language"]})

    def extension(self, name, op, extension_id):
        """
        One edit of a profile's own extensions.
        """
        return self.client.post(f"/api/profiles/{name}/extensions", json={"op": op, "id": extension_id})

    # -- signing in ------------------------------------------------------------

    def test_login_and_enrolment(self):
        self.assertEqual(self.client.get("/api/profiles").status_code, 401)
        self.assertEqual(self.login(password="nope").status_code, 401)
        self.assertEqual(self.client.post("/api/login", data="x", content_type="text/plain").status_code, 415)

        response = self.login()
        self.assertEqual(response.json["next"], "enrol")
        self.assertTrue(response.json["otpauth"].startswith("otpauth://totp/HardCode:root%40example.com"))
        self.assertIn("hc_session=", response.headers.get("Set-Cookie", ""))
        self.assertEqual(self.client.get("/api/profiles").status_code, 401, "password alone is not enough")

        self.assertEqual(self.client.post("/api/totp", json={"code": "000000"}).status_code, 401)
        self.assertEqual(self.client.post("/api/totp", json={"code": self.code()}).status_code, 200)
        self.assertTrue(self.users.find(ROOT)["totpEnrolled"])
        self.assertEqual(self.client.get("/api/me").json, {"email": ROOT, "namespace": "test"})

        self.assertEqual(self.login().json, {"next": "totp"}, "enrolled users just enter a code")

        self.assertEqual(self.client.post("/api/logout", json={}).status_code, 200)
        self.assertIsNone(self.client.get("/api/me").json)
        self.assertEqual(self.client.get("/api/profiles").status_code, 401)

    def test_a_code_is_accepted_once(self):
        self.sign_in()
        code = self.code()
        self.client.post("/api/logout", json={})

        self.login()
        replay = self.client.post("/api/totp", json={"code": code})
        self.assertEqual(replay.status_code, 401, "replay of the code that just signed in")

        self.now[0] += 30
        self.assertEqual(self.client.post("/api/totp", json={"code": self.code()}).status_code, 200)

    def test_second_factor_attempts_are_limited(self):
        self.login()

        for _ in range(MAX_CODE_ATTEMPTS - 1):
            self.assertEqual(self.client.post("/api/totp", json={"code": "000000"}).json["error"], "wrong code")
        self.assertIn("too many", self.client.post("/api/totp", json={"code": "000000"}).json["error"])

        self.assertEqual(
            self.client.post("/api/totp", json={"code": self.code()}).json["error"],
            "sign in first",
            "the challenge is gone",
        )
        self.assertEqual(self.login().status_code, 200, "the password stage isn't locked by code guesses")

    def test_the_code_budget_spans_challenges(self):
        self.users.add("enrolled@example.com", SECOND_PASSWORD, must_enrol=False)

        rounds = ACCOUNT_CODE_ATTEMPTS // MAX_CODE_ATTEMPTS
        for _ in range(rounds):
            self.assertEqual(self.login("enrolled@example.com", SECOND_PASSWORD).status_code, 200)
            for _ in range(MAX_CODE_ATTEMPTS):
                self.assertEqual(self.client.post("/api/totp", json={"code": "000000"}).status_code, 401)

        self.assertEqual(
            self.login("enrolled@example.com", SECOND_PASSWORD).status_code,
            200,
            "the password step is not what is limited",
        )

        self.now[0] += 30
        response = self.client.post("/api/totp", json={"code": self.code("enrolled@example.com")})
        self.assertEqual(
            response.status_code,
            429,
            "a new challenge doesn't buy more guesses: the budget is the account's",
        )
        self.assertEqual(self.client.get("/api/me").json, None, "and that session is gone")

        other = self.app.test_client()
        self.assertEqual(self.login("enrolled@example.com", SECOND_PASSWORD, other).status_code, 200)
        self.assertEqual(
            other.post("/api/totp", json={"code": self.code("enrolled@example.com")}).status_code,
            429,
            "from another client too",
        )

    def test_login_throttle_keys_on_the_canonical_address(self):
        for _ in range(8):
            self.login(password="nope")

        self.assertEqual(self.login().status_code, 429)
        self.assertEqual(
            self.login("  Root@Example.com ").status_code,
            429,
            "whitespace and case don't buy a new bucket",
        )
        self.assertEqual(
            self.login(headers={"X-Forwarded-For": "203.0.113.9"}).status_code,
            429,
            "the test client's 127.0.0.1 isn't a trusted proxy, so the header is ignored",
        )
        self.assertEqual(
            self.login(
                environ_base={"REMOTE_ADDR": "10.1.2.3"},
                headers={"X-Forwarded-For": "203.0.113.9"},
            ).status_code,
            200,
            "a trusted proxy's header is believed",
        )

    def test_a_proxy_that_appends_doesnt_let_the_client_pick_its_address(self):
        # The proxy adds the address it saw to whatever X-Forwarded-For the
        # client sent: the left of the header is the client's to write.
        def from_client(claimed):
            return self.login(
                password="nope",
                environ_base={"REMOTE_ADDR": "10.0.0.2"},
                headers={"X-Forwarded-For": f"{claimed}, 203.0.113.10"},
            )

        for number in range(8):
            from_client(f"198.51.100.{number + 1}")

        self.assertEqual(from_client("198.51.100.99").status_code, 429, "the same client, whatever it claims")

    def test_the_client_is_the_first_address_no_trusted_proxy_added(self):
        # Two proxies of ours: the load balancer (10.0.0.5) added the client,
        # the Ingress controller (10.0.0.2) added the load balancer.
        for _ in range(8):
            self.login(
                password="nope",
                environ_base={"REMOTE_ADDR": "10.0.0.2"},
                headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.10, 10.0.0.5"},
            )

        blocked = self.login(
            environ_base={"REMOTE_ADDR": "10.0.0.2"},
            headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.5"},
        )
        other = self.login(
            environ_base={"REMOTE_ADDR": "10.0.0.2"},
            headers={"X-Forwarded-For": "203.0.113.11, 10.0.0.5"},
        )
        self.assertEqual((blocked.status_code, other.status_code), (429, 200))

    def test_unknown_and_known_addresses_cost_the_same_password_check(self):
        with mock.patch.object(web, "check_password", wraps=web.check_password) as checks:
            self.login("nobody@example.com", "whatever-this-is")
            self.assertEqual(checks.call_count, 1, "an unknown address still runs scrypt")

            self.login(ROOT, "whatever-this-is")
            self.assertEqual(checks.call_count, 2)

    def test_account_level_throttle(self):
        # Forty wrong passwords, each from a different address behind a
        # different trusted proxy.
        for number in range(40):
            self.login(
                password="nope",
                environ_base={"REMOTE_ADDR": f"10.9.{number // 250}.{number % 250 + 1}"},
                headers={"X-Forwarded-For": f"198.51.100.{number + 1}"},
            )

        self.assertEqual(
            self.login(environ_base={"REMOTE_ADDR": "10.8.8.8"}).status_code,
            429,
            "forty failures from forty addresses still lock the account for the window",
        )

    def test_the_pages_refresh_does_not_hold_off_the_idle_timeout(self):
        # An app whose sessions run on the test's clock.
        app = create_app(
            self.k8s,
            self.users,
            cookie_secure="never",
            template_allowlist=TEMPLATE,
            sessions=Sessions(clock=lambda: self.now[0]),
            clock=lambda: self.now[0],
        )
        client = app.test_client()
        idle = Sessions().idle

        # Nothing but the page's refresh since signing in.
        self.sign_in(client)
        self.now[0] += idle - 600
        self.assertEqual(client.get("/api/profiles", headers=POLL).status_code, 200)
        self.now[0] += 601
        self.assertEqual(
            client.get("/api/profiles", headers=POLL).status_code,
            401,
            "an open tab nobody uses times out",
        )

        # Someone using the page in between keeps the session.
        self.sign_in(client)
        self.now[0] += idle - 600
        self.assertEqual(client.get("/api/profiles").status_code, 200)
        self.now[0] += 601
        self.assertEqual(client.get("/api/profiles", headers=POLL).status_code, 200)

    # -- revocation ------------------------------------------------------------

    def test_removed_admins_lose_their_sessions(self):
        other = self.add_second_admin()
        self.assertEqual(other.get("/api/profiles").status_code, 200)

        self.sign_in()
        self.assertEqual(self.client.delete("/api/users/second%40example.com", headers=JSON).status_code, 200)

        self.assertEqual(other.get("/api/profiles").status_code, 401)
        self.assertEqual(other.post("/api/profiles", json={"name": "sneaky"}).status_code, 401)
        self.assertIsNone(other.get("/api/me").json)

    def test_password_change_and_totp_reset_revoke_other_sessions(self):
        other = self.add_second_admin()
        self.sign_in()

        changed = self.client.post(
            "/api/users/second%40example.com/password", json={"password": "another-long-password"}
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(other.get("/api/profiles").status_code, 401, "the old session is gone")

        self.sign_in(other, SECOND, "another-long-password")
        self.assertEqual(other.get("/api/profiles").status_code, 200)

        self.assertEqual(self.client.post("/api/users/second%40example.com/reset-totp", json={}).status_code, 200)
        self.assertEqual(other.get("/api/profiles").status_code, 401)

        changed = self.client.post("/api/users/root%40example.com/password", json={"password": "my-own-new-password"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(
            self.client.get("/api/profiles").status_code,
            200,
            "changing your own password keeps your session",
        )

        self.assertEqual(self.client.post("/api/users/root%40example.com/reset-totp", json={}).status_code, 200)
        self.assertEqual(
            self.client.get("/api/profiles").status_code,
            401,
            "resetting your own 2FA signs you out to enrol again",
        )

    # -- cross-site requests ---------------------------------------------------

    def test_writes_need_json_and_the_same_origin(self):
        self.sign_in()

        def create(headers):
            return self.client.post("/api/profiles", json={"name": "x"}, headers=headers).status_code

        self.assertEqual(
            self.client.post(
                "/api/users/root%40example.com/reset-totp", data="", content_type="text/plain"
            ).status_code,
            415,
        )
        self.assertEqual(
            self.client.delete("/api/profiles/nope").status_code,
            415,
            "even a bodiless write names the content type",
        )

        self.assertEqual(create({"Origin": "http://evil.example"}), 403)
        self.assertEqual(create({"Origin": "http://sandbox.localhost"}), 403, "a sibling origin is still foreign")
        self.assertEqual(create({"Sec-Fetch-Site": "same-site"}), 403)
        self.assertEqual(create({"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}), 201)

        self.assertEqual(
            self.client.get("/api/profiles", headers={"Origin": "http://evil.example"}).status_code,
            200,
            "reads aren't the concern",
        )

    # -- profiles --------------------------------------------------------------

    def test_profiles_and_allowlists(self):
        self.sign_in()

        # Creating, and copying.
        self.assertEqual(self.client.get("/api/profiles").json, [])
        self.assertEqual(self.client.post("/api/profiles", json={"name": "Bad Name"}).status_code, 400)

        response = self.client.post("/api/profiles", json={"name": "team-a", "vscodeHost": "a.example.com"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["name"], "team-a")
        self.assertGreater(response.json["entries"], 50, "the template allowlist")
        self.assertEqual(self.client.post("/api/profiles", json={"name": "team-a"}).status_code, 409)

        response = self.client.post("/api/profiles", json={"name": "team-b", "from": "team-a", "image": "img:old"})
        self.assertEqual((response.status_code, response.json["image"]), (201, "img:old"))
        self.assertEqual(response.json["vscodeHost"], "", "a copy doesn't take the source's Ingress host")
        self.assertEqual(
            response.json["entries"],
            self.client.get("/api/profiles/team-a").json["entries"],
            "but does take its allowlist",
        )
        self.assertEqual(len(self.client.get("/api/profiles").json), 2)
        self.assertEqual(self.client.get("/api/profiles/nope").status_code, 404)

        # One profile's allowlist.
        def edit(body):
            return self.client.post("/api/profiles/team-a/allowlist", json=body)

        self.assertEqual(edit({"op": "add", "entry": "github.com/ourcompany", "flag": "rw"}).json, {"changed": True})

        detail = self.client.get("/api/profiles/team-a").json
        entry = self.entry("team-a", "github.com/ourcompany")
        self.assertEqual((entry["flag"], entry["enabled"], entry["kind"]), ("rw", True, "path"))
        self.assertEqual(detail["access"]["url"], f"https://a.example.com/?tkn={detail['access']['token']}")

        self.assertEqual(edit({"op": "add", "entry": "com"}).status_code, 400)
        self.assertEqual(edit({"op": "bogus", "entry": "x.org"}).status_code, 400)
        self.assertEqual(edit({"op": "add"}).status_code, 400)
        self.assertEqual(edit(["not", "an", "object"]).status_code, 400)

        edit({"op": "disable", "entry": "github.com/ourcompany"})
        self.assertFalse(self.entry("team-a", "github.com/ourcompany")["enabled"])
        edit({"op": "remove", "entry": "github.com/ourcompany"})
        self.assertIsNone(self.entry("team-a", "github.com/ourcompany"))

        # Every profile's allowlist at once.
        def bulk(body):
            return self.client.post("/api/allowlist/bulk", json=body)

        both_changed = {"team-a": "changed", "team-b": "changed"}
        self.assertEqual(bulk({"op": "add", "entry": "github.com/everyone"}).json, both_changed)
        self.assertEqual(
            bulk({"op": "add", "entry": "github.com/everyone"}).json,
            {"team-a": "unchanged", "team-b": "unchanged"},
        )
        self.assertEqual(bulk({"op": "remove", "entry": "github.com/everyone"}).json, both_changed)
        self.assertEqual(bulk({"op": "add", "entry": "com"}).status_code, 400, "checked once, not per profile")
        self.assertEqual(bulk({"op": "bogus", "entry": "x.org"}).status_code, 400)

        # Settings, and the whole allowlist as text.
        response = self.client.patch(
            "/api/profiles/team-b",
            json={"suspended": True, "image": "img:new", "home": {"size": "50Gi"}, "ignored": 1},
        )
        self.assertEqual((response.json["suspended"], response.json["image"]), (True, "img:new"))
        self.assertIs(
            self.client.patch("/api/profiles/team-b", json={"suspended": 0}).json["suspended"],
            False,
            "coerced to a boolean",
        )

        self.assertEqual(self.client.patch("/api/profiles/team-b", json={"allowlist": "com\n"}).status_code, 400)
        self.assertEqual(
            self.client.patch("/api/profiles/team-b", json={"allowlist": "pypi.org\nexample.com"}).status_code,
            200,
        )
        self.assertEqual(
            self.stored_spec("team-b")["allowlist"],
            "pypi.org\nexample.com\n",
            "text edits end with a newline",
        )

        # Deleting.
        self.assertEqual(self.client.delete("/api/profiles/team-b", headers=JSON).status_code, 200)
        self.assertEqual(self.client.get("/api/profiles/team-b").status_code, 404)

        self.assertEqual(self.client.get("/api/template").json["allowlist"], Path(TEMPLATE).read_text(encoding="utf-8"))

    def test_empty_image_means_inherit(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a", "image": "img:mine"})

        response = self.client.patch("/api/profiles/team-a", json={"image": "", "vscodeHost": " "})

        self.assertEqual(response.json["image"], "img:1", "the operator's default again")
        self.assertNotIn("image", self.stored_spec("team-a"))
        self.assertNotIn("vscodeHost", self.stored_spec("team-a"))

    def test_racing_allowlist_edits_both_survive(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})
        self.client.post("/api/profiles/team-a/allowlist", json={"op": "add", "entry": "old.example"})

        def someone_else_adds_first(patched_path, patch):
            # Another admin's edit lands between our read and our write.
            self.k8s.before_patch = None
            current = self.stored_spec("team-a")["allowlist"]
            self.k8s.merge_patch(f"{PROFILE_PATH}/team-a", {"spec": {"allowlist": current + "new.example\n"}})

        self.k8s.before_patch = someone_else_adds_first
        response = self.client.post("/api/profiles/team-a/allowlist", json={"op": "disable", "entry": "old.example"})
        self.assertEqual(response.json, {"changed": True})

        enabled = {entry["entry"]: entry["enabled"] for entry in self.allowlist_of("team-a")}
        self.assertEqual(
            (enabled["old.example"], enabled["new.example"]),
            (False, True),
            "the revocation stuck and the other edit wasn't lost",
        )

    def test_an_editor_opened_before_a_change_cannot_overwrite_it(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})
        self.client.post("/api/profiles/team-a/allowlist", json={"op": "add", "entry": "github.com", "flag": "ssh"})

        # A opens the text editor; then B revokes the SSH entry.
        opened = self.client.get("/api/profiles/team-a").json
        self.assertTrue(opened["version"])
        self.client.post("/api/profiles/team-a/allowlist", json={"op": "disable", "entry": "github.com", "flag": "ssh"})

        response = self.client.patch(
            "/api/profiles/team-a",
            json={"allowlist": opened["spec"]["allowlist"] + "new.example\n", "version": opened["version"]},
        )
        self.assertEqual(response.status_code, 409, "A's save is refused: the text it edited is stale")
        self.assertIn("since you opened it", response.json["error"])
        self.assertFalse(self.entry("team-a", "github.com", flag="ssh")["enabled"], "the revocation stands")

        fresh = self.client.get("/api/profiles/team-a").json
        response = self.client.patch("/api/profiles/team-a", json={"suspended": True, "version": fresh["version"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.patch("/api/profiles/team-a", json={"suspended": False}).status_code,
            200,
            "without a version, a single-field change still goes through",
        )

    def test_host_names_are_validated(self):
        self.sign_in()

        bad = self.client.post("/api/profiles", json={"name": "team-a", "vscodeHost": "https://bad.example"})
        self.assertEqual(bad.status_code, 400)

        good = self.client.post("/api/profiles", json={"name": "team-a", "vscodeHost": "Team-A.Code.Example.com"})
        self.assertEqual(good.json["vscodeHost"], "team-a.code.example.com")

        self.assertEqual(
            self.client.patch("/api/profiles/team-a", json={"vscodeHost": "no host/with/path"}).status_code,
            400,
        )
        self.assertEqual(self.client.patch("/api/profiles/team-a", json={"vscodeHost": ""}).json["vscodeHost"], "")

    def test_stale_settings_are_refused(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})

        def someone_else_changes_the_image(patched_path, patch):
            self.k8s.before_patch = None
            self.k8s.merge_patch(f"{PROFILE_PATH}/team-a", {"spec": {"image": "img:other"}})

        self.k8s.before_patch = someone_else_changes_the_image
        response = self.client.patch("/api/profiles/team-a", json={"suspended": True})

        self.assertEqual(response.status_code, 409)
        self.assertIn("changed meanwhile", response.json["error"])

    def test_a_status_write_does_not_make_an_open_editor_stale(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})
        opened = self.client.get("/api/profiles/team-a").json

        # The operator reports on the profile meanwhile, as it does all the time.
        self.k8s.merge_patch(f"{PROFILE_PATH}/team-a/status", {"status": {"phase": "Ready"}})

        response = self.client.patch(
            "/api/profiles/team-a",
            json={"allowlist": "pypi.org\n", "version": opened["version"]},
        )
        self.assertEqual(response.status_code, 200, "a status write isn't a change to what was being edited")

        response = self.client.patch("/api/profiles/team-a", json={"suspended": True, "version": opened["version"]})
        self.assertEqual(response.status_code, 409, "a spec change (the save above) is")
        self.assertIn("since you opened it", response.json["error"])

    def test_volume_sizes_are_validated(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})

        def settings(body):
            return self.client.patch("/api/profiles/team-a", json=body)

        response = settings({"home": {"size": "50GB"}})
        self.assertEqual(response.status_code, 400, "the operator couldn't compare it, and the profile would fail")
        self.assertEqual(response.json["error"], "home size: a size such as 20Gi or 500Mi")
        self.assertNotIn("home", self.stored_spec("team-a"))

        self.assertEqual(settings({"docker": {"enabled": True, "size": "lots"}}).status_code, 400)
        self.assertEqual(settings({"shared": {"size": "5 Gi"}}).status_code, 400)

        self.assertEqual(settings({"home": {"size": " 50Gi "}}).status_code, 200)
        self.assertEqual(self.stored_spec("team-a")["home"], {"size": "50Gi"})
        self.assertEqual(settings({"home": {"size": ""}}).status_code, 200, "empty means inherit")

    def test_names_start_with_a_letter(self):
        self.sign_in()

        response = self.client.post("/api/profiles", json={"name": "1team"})
        self.assertEqual(response.status_code, 400, "its Services (1team-dns, ...) couldn't be created")
        self.assertIn("starting with a letter", response.json["error"])
        self.assertEqual(self.client.post("/api/profiles", json={"name": "team-1"}).status_code, 201)

        self.assertIn(
            f"matches('{web.NAME_OK.pattern}')",
            CRD.read_text(encoding="utf-8"),
            "the CRD refuses the names the admin refuses",
        )

    def test_a_profile_being_deleted_says_so(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})
        self.k8s.merge_patch(f"{PROFILE_PATH}/team-a/status", {"status": {"phase": "Ready"}})

        # Kubernetes marks it, and keeps it until everything it owned is gone.
        self.k8s.store[f"{PROFILE_PATH}/team-a"]["metadata"]["deletionTimestamp"] = "2026-10-10T00:00:00Z"

        self.assertEqual(self.client.get("/api/profiles").json[0]["phase"], "Deleting", "not the Ready it last had")
        self.assertEqual(self.client.get("/api/profiles/team-a").json["phase"], "Deleting")

        response = self.client.post("/api/profiles", json={"name": "team-a"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["error"], "profile 'team-a' is still being deleted; try again in a moment")

    def test_blocked_traffic(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})
        self.assertEqual(self.client.get("/api/profiles/team-a/blocked").json, [])

        squid_pod = {
            "metadata": {
                "name": "team-a-squid-1",
                "labels": {"hardcode-app.github.io/profile": "team-a", "hardcode-app.github.io/component": "squid"},
            },
            "status": {},
        }
        self.k8s.pods = [squid_pod]
        self.k8s.logs_by_pod["team-a-squid-1/squid"] = (
            "1 0 10.0.0.1 TCP_DENIED/403 1 GET https://example.com/x - HIER_NONE/- -\n"
        )

        items = self.client.get("/api/profiles/team-a/blocked?since=2h").json

        self.assertEqual((items[0]["host"], items[0]["count"]), ("example.com", 1))

    # -- languages and extensions ----------------------------------------------

    def test_the_catalogue(self):
        self.sign_in()
        catalogue = self.client.get("/api/languages").json

        self.assertEqual([entry["key"] for entry in catalogue], list(langs.LANGS))
        python = catalogue[0]
        self.assertEqual((python["name"], python["kind"]), ("Python", "language"))
        self.assertIn("pypi.org", python["domains"])
        self.assertIn("ms-python.python", python["extensions"])
        self.assertNotIn("enabled", python, "whether it is on is each profile's own")

    def test_a_profile_shows_its_languages_and_extensions(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})
        self.assertNotIn("languages", self.stored_spec("team-a"), "none of its own: every one")

        detail = self.client.get("/api/profiles/team-a").json

        self.assertEqual([lang["key"] for lang in detail["languages"]], list(langs.LANGS))
        self.assertTrue(all(lang["enabled"] for lang in detail["languages"]))
        self.assertEqual(detail["extensions"], langs.extension_sources(list(langs.LANGS), []))
        self.assertEqual(self.entry("team-a", "pypi.org")["language"], "python")
        self.assertEqual(
            self.entry("team-a", "api.anthropic.com")["language"], "claude_cli", "the coding agent's block"
        )
        self.assertEqual(
            self.entry("team-a", "update.code.visualstudio.com")["language"], "", "not in a language block"
        )

        self.assertEqual(self.client.get("/api/profiles").json[0]["languages"], list(langs.LANGS))

    def test_new_languages_bring_their_entries(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})
        self.client.post("/api/profiles/team-a/allowlist", json={"op": "add", "entry": "github.com/ourcompany"})
        self.client.post("/api/profiles/team-a/allowlist", json={"op": "disable", "entry": "crates.io"})
        own = self.stored_spec("team-a")["allowlist"]

        patches = len(self.k8s.patched)
        response = self.client.patch("/api/profiles/team-a", json={"languages": ["js", "python", "docker"]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["languages"], ["python", "node", "docker"], "normalised, in catalogue order")
        self.assertEqual(len(self.k8s.patched) - patches, 1, "one write")

        spec = self.stored_spec("team-a")
        self.assertEqual(spec["languages"], ["python", "node", "docker"])
        self.assertEqual(spec["allowlist"], own, "the profile's own allowlist isn't touched")
        self.assertEqual(self.provided_languages("team-a"), ["node", "python"])
        self.assertIsNotNone(self.entry("team-a", "github.com/ourcompany"), "entries of your own stay")

        # Back on: its entries come back, one switched off still off.
        self.client.patch("/api/profiles/team-a", json={"languages": ["python", "node", "rust"]})
        self.assertEqual(self.provided_languages("team-a"), ["node", "python", "rust"])
        self.assertTrue(self.entry("team-a", "static.crates.io")["enabled"])
        self.assertFalse(self.entry("team-a", "crates.io")["enabled"])

        # None at all.
        self.client.patch("/api/profiles/team-a", json={"languages": []})
        self.assertEqual((self.stored_spec("team-a")["languages"], self.provided_languages("team-a")), ([], []))

    def test_provided_entries_are_switched_not_deleted(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a", "languages": ["rust"]})
        self.client.post("/api/profiles", json={"name": "team-b", "languages": ["rust"]})

        vscode = self.entry("team-a", "update.code.visualstudio.com")
        self.assertEqual((vscode["provided"], vscode["source"], vscode["section"]), (True, "vscode", "VS Code"))

        response = self.client.post("/api/profiles/team-a/allowlist", json={"op": "disable", "entry": "crates.io"})
        self.assertEqual((response.status_code, response.json), (200, {"changed": True}))
        self.assertIn("\noff crates.io\n", self.stored_spec("team-a")["allowlist"])
        self.assertFalse(self.entry("team-a", "crates.io")["enabled"])

        response = self.client.post("/api/profiles/team-a/allowlist", json={"op": "remove", "entry": "crates.io"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("comes with Rust and can't be deleted", response.json["error"])

        added = self.client.post("/api/profiles/team-a/allowlist", json={"op": "add", "entry": "crates.io"})
        self.assertEqual(added.json, {"changed": True}, "adding it switches it back on")
        self.assertTrue(self.entry("team-a", "crates.io")["enabled"])

        results = self.client.post("/api/allowlist/bulk", json={"op": "disable", "entry": "static.crates.io"}).json
        self.assertEqual(results, {"team-a": "changed", "team-b": "changed"})
        self.assertFalse(self.entry("team-b", "static.crates.io")["enabled"])

    def test_new_languages_survive_a_concurrent_allowlist_edit(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})

        def someone_else_adds_first(patched_path, patch):
            # Another admin's allowlist edit lands between our read and our write.
            self.k8s.before_patch = None
            current = self.stored_spec("team-a")["allowlist"]
            self.k8s.merge_patch(f"{PROFILE_PATH}/team-a", {"spec": {"allowlist": current + "new.example\n"}})

        self.k8s.before_patch = someone_else_adds_first
        response = self.client.patch("/api/profiles/team-a", json={"languages": ["go"]})

        self.assertEqual(response.status_code, 200, "made again on top of the other edit")
        self.assertEqual(self.provided_languages("team-a"), ["go"])
        self.assertIsNotNone(self.entry("team-a", "new.example"), "and the other edit wasn't lost")

    def test_languages_are_checked(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})

        response = self.client.patch("/api/profiles/team-a", json={"languages": ["python", "cobol"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown language 'cobol'. One of: python,", response.json["error"])

        response = self.client.patch("/api/profiles/team-a", json={"languages": "python"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("a list of language keys", response.json["error"])
        self.assertNotIn("languages", self.stored_spec("team-a"), "nothing was written")

        opened = self.client.get("/api/profiles/team-a").json
        self.client.post("/api/profiles/team-a/allowlist", json={"op": "add", "entry": "new.example"})
        response = self.client.patch("/api/profiles/team-a", json={"languages": ["go"], "version": opened["version"]})
        self.assertEqual(response.status_code, 409, "a version names what the change was made on")

    def test_creating_with_languages(self):
        self.sign_in()
        template = Path(TEMPLATE).read_text(encoding="utf-8")

        response = self.client.post("/api/profiles", json={"name": "team-a", "languages": ["go", "docker"]})
        self.assertEqual((response.status_code, response.json["languages"]), (201, ["go", "docker"]))
        self.assertEqual(self.provided_languages("team-a"), ["go"])
        self.assertEqual(self.stored_spec("team-a")["allowlist"], template, "the provided entries aren't stored")

        # A copy keeps its source's languages and allowlist, unless it is
        # given languages of its own.
        self.client.post("/api/profiles/team-a/allowlist", json={"op": "add", "entry": "github.com/ourcompany"})
        self.client.post("/api/profiles", json={"name": "team-b", "from": "team-a"})
        self.assertEqual(self.stored_spec("team-b")["languages"], ["go", "docker"])
        self.assertEqual(self.stored_spec("team-b")["allowlist"], self.stored_spec("team-a")["allowlist"])

        self.client.post("/api/profiles", json={"name": "team-c", "from": "team-a", "languages": ["python"]})
        self.assertEqual(self.stored_spec("team-c")["languages"], ["python"])
        self.assertEqual(self.provided_languages("team-c"), ["python"])
        self.assertIsNotNone(self.entry("team-c", "github.com/ourcompany"), "the copied entries stay")

        # From the template without languages: none of its own, every block.
        self.client.post("/api/profiles", json={"name": "team-d"})
        self.assertNotIn("languages", self.stored_spec("team-d"))
        self.assertEqual(self.stored_spec("team-d")["allowlist"], template)

        self.assertEqual(
            self.client.post("/api/profiles", json={"name": "team-e", "languages": ["x"]}).status_code, 400
        )
        self.assertIsNone(self.k8s.get(f"{PROFILE_PATH}/team-e"))

    def test_a_template_profile_follows_the_charts_default_languages(self):
        app = create_app(
            self.k8s,
            self.users,
            {"languages": ["rust"]},
            cookie_secure="never",
            template_allowlist=TEMPLATE,
            clock=lambda: self.now[0],
        )
        client = app.test_client()
        self.sign_in(client)

        client.post("/api/profiles", json={"name": "team-a"})

        self.assertNotIn("languages", self.stored_spec("team-a"), "it keeps inheriting the default")
        self.assertEqual(self.provided_languages("team-a", client), ["rust"], "and its allowlist opens what that needs")
        self.assertEqual(client.get("/api/profiles").json[0]["languages"], ["rust"])

    def test_extensions(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a", "languages": ["python"]})

        response = self.extension("team-a", "add", "EAmodio.GitLens")
        self.assertEqual(response.json, {"changed": True, "message": "added extension eamodio.gitlens"})
        self.assertEqual(self.stored_spec("team-a")["extensions"], ["eamodio.gitlens"])
        self.assertEqual(self.extension("team-a", "add", "eamodio.gitlens").json["changed"], False)

        # What the VS Code container gets anyway.
        response = self.extension("team-a", "add", "ms-python.python")
        self.assertEqual(response.json, {"changed": False, "message": "ms-python.python already comes with Python"})
        response = self.extension("team-a", "add", "redhat.vscode-yaml")
        self.assertEqual(response.json["message"], "redhat.vscode-yaml already comes with every VS Code container")

        response = self.extension("team-a", "remove", "ms-python.python")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json["error"],
            "ms-python.python comes with Python; switch that off in Languages / Tools to remove it",
        )
        self.assertEqual(self.extension("team-a", "remove", "nobody.here").status_code, 400)

        # A language that is off brings nothing: its extension is the profile's own to add.
        self.assertEqual(self.extension("team-a", "add", "golang.go").json["changed"], True)

        detail = self.client.get("/api/profiles/team-a").json
        custom = [row["id"] for row in detail["extensions"] if row["source"] == "custom"]
        self.assertEqual(custom, ["eamodio.gitlens", "golang.go"])

        self.assertEqual(self.extension("team-a", "remove", "eamodio.gitlens").json["changed"], True)
        self.assertEqual(self.extension("team-a", "remove", "golang.go").json["changed"], True)
        self.assertNotIn("extensions", self.stored_spec("team-a"), "none of its own left")

    def test_a_new_vscode_token(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})
        secret_path = "/api/v1/namespaces/test/secrets/team-a-vscode-token"

        # The operator hasn't made the token yet.
        self.assertEqual(self.client.post("/api/profiles/team-a/token", json={}).status_code, 409)

        old = base64.b64encode(b"old-token").decode()
        self.k8s.create(
            "/api/v1/namespaces/test/secrets", {"metadata": {"name": "team-a-vscode-token"}, "data": {"token": old}}
        )

        response = self.client.post("/api/profiles/team-a/token", json={})
        self.assertEqual(response.status_code, 200, response.json)
        new = self.k8s.get(secret_path)["data"]["token"]
        self.assertNotEqual(new, old)
        self.assertGreaterEqual(len(base64.b64decode(new)), 24)

        # A token the spec fixes is changed there, not here.
        self.client.patch("/api/profiles/team-a", json={})
        self.k8s.merge_patch(f"{PROFILE_PATH}/team-a", {"spec": {"vscodeToken": "fixed-one"}})
        refused = self.client.post("/api/profiles/team-a/token", json={})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("fixed in its spec", refused.json["error"])

    def test_extensions_are_checked(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})

        response = self.extension("team-a", "add", "not an id")
        self.assertEqual(response.status_code, 400)
        self.assertIn("is not an extension id (publisher.name", response.json["error"])

        self.assertEqual(self.extension("team-a", "bogus", "eamodio.gitlens").status_code, 400)
        self.assertEqual(self.extension("nobody", "add", "eamodio.gitlens").status_code, 404)

        many = [f"publisher.extension-{number}" for number in range(MAX_EXTENSIONS)]
        self.k8s.merge_patch(f"{PROFILE_PATH}/team-a", {"spec": {"extensions": many}})
        response = self.extension("team-a", "add", "eamodio.gitlens")
        self.assertEqual(response.status_code, 400)
        self.assertIn(f"at most {MAX_EXTENSIONS} extensions", response.json["error"])

    def test_racing_extension_edits_both_survive(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})

        def someone_else_adds_first(patched_path, patch):
            self.k8s.before_patch = None
            self.k8s.merge_patch(f"{PROFILE_PATH}/team-a", {"spec": {"extensions": ["theirs.one"]}})

        self.k8s.before_patch = someone_else_adds_first
        self.assertEqual(self.extension("team-a", "add", "mine.one").json["changed"], True)

        self.assertEqual(self.stored_spec("team-a")["extensions"], ["theirs.one", "mine.one"])

    def test_a_profile_shows_how_far_its_extensions_have_got(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})

        self.assertIsNone(
            self.client.get("/api/profiles/team-a").json["extensionStatus"],
            "no pod, or nothing logged yet",
        )

        self.k8s.logs_by_pod["team-a-vscode-0/vscode"] = (
            "[vscode] VS Code: http://localhost:8000/?tkn=x\n"
            "[vscode] extensions: 3 of 12 installed, installing 9\n"
            "[vscode] installed VS Code extension ms-python.python\n"
            "[vscode] installed VS Code extension golang.go\n"
        )
        status = self.client.get("/api/profiles/team-a").json["extensionStatus"]

        self.assertEqual(status, {"state": "installing", "installed": 5, "total": 12, "refused": 0, "failed": 0})
        self.assertEqual(
            self.k8s.log_reads[-1],
            {"pod": "team-a-vscode-0", "container": "vscode", "since": None, "tail": web.EXTENSION_LOG_LINES},
            "the StatefulSet's pod, by name, and only the end of its log",
        )

    def test_only_the_end_of_the_log_is_read_for_the_extensions(self):
        self.sign_in()
        self.client.post("/api/profiles", json={"name": "team-a"})

        chatter = "[2026-10-11 09:00:01] info something VS Code said\n" * web.EXTENSION_LOG_LINES
        self.k8s.logs_by_pod["team-a-vscode-0/vscode"] = "[vscode] extensions: 12 of 12 installed\n" + chatter

        self.assertIsNone(self.client.get("/api/profiles/team-a").json["extensionStatus"])

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_the_crd_knows_the_languages_and_extensions(self):
        crd = yaml.safe_load(CRD.read_text(encoding="utf-8"))
        spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]

        self.assertEqual(spec["languages"]["items"]["enum"], list(langs.LANGS), "the catalogue's keys, in its order")
        self.assertEqual(spec["extensions"]["items"]["pattern"], langs.EXTENSION_ID.pattern)
        self.assertEqual(spec["extensions"]["maxItems"], MAX_EXTENSIONS)

    @unittest.skipUnless(yaml, "needs PyYAML")
    def test_the_crd_is_the_group_the_operator_uses(self):
        crd = yaml.safe_load(CRD.read_text(encoding="utf-8"))

        self.assertEqual(crd["spec"]["group"], manifests.GROUP)
        self.assertEqual(crd["metadata"]["name"], f"profiles.{manifests.GROUP}")
        self.assertEqual(CRD.name, f"profiles.{manifests.GROUP}.yaml")
        self.assertEqual([version["name"] for version in crd["spec"]["versions"]], [manifests.VERSION])

    # -- admins ----------------------------------------------------------------

    def test_users(self):
        self.sign_in()

        self.assertEqual(self.client.post("/api/users", json={"email": SECOND, "password": "short"}).status_code, 400)
        self.assertEqual(
            self.client.post("/api/users", json={"email": SECOND, "password": SECOND_PASSWORD}).status_code,
            201,
        )
        self.assertEqual(len(self.client.get("/api/users").json), 2)

        self.assertEqual(self.client.post("/api/users/second%40example.com/reset-totp", json={}).status_code, 200)
        changed = self.client.post(
            "/api/users/second%40example.com/password", json={"password": "another-long-password"}
        )
        self.assertEqual(changed.status_code, 200)

        self.assertEqual(self.client.delete("/api/users/second%40example.com", headers=JSON).status_code, 200)
        self.assertEqual(
            self.client.delete("/api/users/root%40example.com", headers=JSON).status_code,
            400,
            "the last admin stays",
        )

    # -- the page, and the store -----------------------------------------------

    def test_static_and_health(self):
        self.assertEqual(self.client.get("/healthz").json, {"ok": True})

        with self.client.get("/") as page:
            self.assertEqual(page.status_code, 200)
            self.assertIn("frame-ancestors 'none'", page.headers["Content-Security-Policy"])
            self.assertEqual(page.headers["X-Frame-Options"], "DENY")

        for path in ("/app.js", "/admin.css", "/base.css", "/icon.png"):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200, path)

    def test_qr_and_secret_store(self):
        url = qr_data_url("otpauth://totp/x")
        self.assertTrue(url is None or url.startswith("data:image/svg+xml;base64,"))

        store = SecretStore(self.k8s, "users")
        users, version = store.load()
        self.assertEqual((users, version), ([], None))

        store.save([{"email": "a@b.co"}], version)
        users, version = store.load()
        self.assertEqual(users, [{"email": "a@b.co"}])
        self.assertIn("/api/v1/namespaces/test/secrets/users", self.k8s.store)

        # The version belongs to the snapshot: a later read through the same
        # store doesn't rescue a stale write.
        _, newer_version = store.load()
        store.save([{"email": "a@b.co"}, {"email": "c@d.co"}], newer_version)

        with self.assertRaises(StoreConflict):
            store.save([{"email": "a@b.co"}, {"email": "x@y.co"}], version)
        self.assertEqual([user["email"] for user in store.load()[0]], ["a@b.co", "c@d.co"], "the loser reloads")


class LanWebTests(unittest.TestCase):
    """
    A profile's On your network card: names let reach an address on the
    network, given with their address, as the admin's pod can't look them
    up there.
    """

    def setUp(self):
        self.k8s = FakeK8s()
        self.users = Users(MemoryStore())
        self.users.add(ROOT, ROOT_PASSWORD, must_enrol=False)
        self.now = [1_700_000_000.0]
        self.looked_up = {"gitlab.example.com": ["192.168.71.200"], "moved.example.com": ["10.0.0.9"]}

        self.client = self.signed_in({"image": "img:1", "lan": {"enabled": True, "clusterRanges": ["10.244.0.0/16"]}})
        self.assertEqual(self.client.post("/api/profiles", json={"name": "team-a"}).status_code, 201)

    def signed_in(self, defaults):
        """
        A client of an admin with the chart's *defaults*, signed in.
        """
        app = create_app(
            self.k8s,
            self.users,
            defaults,
            cookie_secure="never",
            template_allowlist=TEMPLATE,
            clock=lambda: self.now[0],
            resolve=lambda name: list(self.looked_up.get(name, [])),
        )
        app.testing = True
        client = app.test_client()

        self.now[0] += 30
        client.post("/api/login", json={"email": ROOT, "password": ROOT_PASSWORD})
        code = totp.totp(self.users.find(ROOT)["totpSecret"], self.now[0])
        self.assertEqual(client.post("/api/totp", json={"code": code}).status_code, 200)

        return client

    def lan(self, **body):
        return self.client.post("/api/profiles/team-a/lan", json=body)

    def card(self):
        return self.client.get("/api/profiles/team-a").json["lan"]

    def test_add_list_move_and_remove(self):
        added = self.lan(op="add", name="GitLab.example.com", address="192.168.71.200")
        self.assertEqual(
            added.json, {"changed": True, "message": "gitlab.example.com may reach 192.168.71.200 on your network"}
        )

        self.lan(op="add", name="moved.example.com", address="10.0.0.8")
        card = self.card()
        self.assertTrue(card["enabled"])
        self.assertEqual(
            [(pin["name"], pin["address"], pin["state"], pin["ignored"]) for pin in card["pins"]],
            [("gitlab.example.com", "192.168.71.200", "ok", ""), ("moved.example.com", "10.0.0.8", "moved", "")],
        )

        moved = self.lan(op="add", name="moved.example.com", address="10.0.0.9")
        self.assertEqual(moved.json["message"], "moved moved.example.com on your network: 10.0.0.8 -> 10.0.0.9")

        removed = self.lan(op="remove", name="gitlab.example.com")
        self.assertEqual(removed.json["message"], "gitlab.example.com no longer reaches your network")
        self.assertEqual([pin["name"] for pin in self.card()["pins"]], ["moved.example.com"])

        spec = self.k8s.get(f"{PROFILE_PATH}/team-a")["spec"]
        self.assertIn("lan moved.example.com 10.0.0.9", spec["allowlist"])

    def test_refused_like_the_proxy_and_the_operator_would(self):
        for body, why in (
            ({"op": "add", "name": "metadata.example.com", "address": "169.254.169.254"}, "can't be pinned"),
            ({"op": "add", "name": "public.example.com", "address": "8.8.8.8"}, "isn't on your network"),
            ({"op": "add", "name": ".example.com", "address": "10.0.0.1"}, "only an exact name"),
            ({"op": "add", "name": "pods.example.com", "address": "10.244.0.7"}, "pod or service ranges"),
            ({"op": "pin", "name": "x.example.com", "address": "10.0.0.1"}, "op must be add or remove"),
            ({"op": "add", "address": "10.0.0.1"}, "name is required"),
        ):
            response = self.lan(**body)
            self.assertEqual(response.status_code, 400, body)
            self.assertIn(why, response.json["error"], body)

        self.assertEqual(self.card()["pins"], [])

    def test_the_card_says_when_the_cluster_ignores_pins(self):
        self.lan(op="add", name="gitlab.example.com", address="192.168.71.200")

        card = self.signed_in({"image": "img:1"}).get("/api/profiles/team-a").json["lan"]

        self.assertFalse(card["enabled"])
        self.assertEqual(card["pins"][0]["ignored"], manifests.LAN_OFF)


class ExtensionStatusTests(unittest.TestCase):
    """
    How far a VS Code container has got with its extensions, read from the
    lines images/vscode/sync-extensions.sh logs.
    """

    def status(self, *lines):
        """
        The status read from a log of these lines.
        """
        return extension_status_from_log("".join(f"{line}\n" for line in lines))

    def test_nothing_logged_yet(self):
        self.assertIsNone(extension_status_from_log(""))
        self.assertIsNone(self.status("[vscode] VS Code: http://localhost:8000/?tkn=x", "Web UI available at …"))

    def test_a_pass_that_is_installing_counts_as_it_goes(self):
        status = self.status(
            "[vscode] extensions: 3 of 12 installed, installing 9",
            "[vscode] installed VS Code extension ms-python.python",
            "[2026-10-11 09:00:01] info something VS Code said",
            "[vscode] installed VS Code extension golang.go",
            "[vscode] WARNING: VS Code extension some.one refused: its publisher, or one it pulls in, "
            "has no verified domain",
            "[vscode] WARNING: VS Code extension other.one couldn't be installed or checked; will try again",
            "[vscode] installed VS Code extension eamodio.gitlens",
        )

        self.assertEqual(status, {"state": "installing", "installed": 6, "total": 12, "refused": 1, "failed": 1})

    def test_a_finished_pass_says_what_it_left(self):
        self.assertEqual(
            self.status("[vscode] extensions: 12 of 12 installed"),
            {"state": "done", "installed": 12, "total": 12, "refused": 0, "failed": 0},
        )
        self.assertEqual(
            self.status("[vscode] extensions: 10 of 11 installed, 2 refused, 1 to try again"),
            {"state": "done", "installed": 10, "total": 11, "refused": 2, "failed": 1},
        )
        self.assertEqual(
            self.status("[vscode] extensions: 11 of 11 installed, 1 refused"),
            {"state": "done", "installed": 11, "total": 11, "refused": 1, "failed": 0},
        )

    def test_the_latest_pass_counts(self):
        status = self.status(
            "[vscode] extensions: 0 of 10 installed, installing 10",
            "[vscode] installed VS Code extension ms-python.python",
            "[vscode] extensions: 9 of 10 installed, 1 to try again",
            "[vscode] extensions: 9 of 11 installed, installing 2",
            "[vscode] removing VS Code extension golang.go (not wanted by the profile any more)",
            "[vscode] installed VS Code extension eamodio.gitlens",
        )
        self.assertEqual(status, {"state": "installing", "installed": 10, "total": 11, "refused": 0, "failed": 0})

        status = self.status(
            "[vscode] extensions: 9 of 11 installed, installing 2",
            "[vscode] installed VS Code extension eamodio.gitlens",
            "[vscode] extensions: 10 of 11 installed, 1 to try again",
        )
        self.assertEqual(status, {"state": "done", "installed": 10, "total": 11, "refused": 0, "failed": 1})


if __name__ == "__main__":
    unittest.main()
