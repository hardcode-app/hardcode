"""
Members, end to end through the API with Flask's test client: the profiles
they own and nobody else's, what they may change, how many they may make,
their allowlist rights (none, ask, edit, and rw, ssh and the network), the
requests they make, the admins' emails about them and the admins' approvals,
and the People page's accounts.
"""

import unittest
from pathlib import Path
from unittest import mock

from hardcode_admin import approvals, totp
from hardcode_admin.auth import ADMIN, MEMBER, Users
from hardcode_admin.mail import Mailer, SmtpSettings
from hardcode_admin.web import REQUESTS_SECRET, SecretStore, create_app

from .helpers import FakeK8s, MemoryStore

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE = str(REPO / "template" / "allowlist.txt")
JSON = {"Content-Type": "application/json"}
PROFILE_PATH = "/apis/hardcode-app.github.io/v1alpha1/namespaces/test/profiles"
PORTAL = "https://admin.example.com"

ROOT = "root@example.com"
OTHER_ADMIN = "other.admin@example.com"
MEMBER_EMAIL = "member@example.com"
PASSWORD = "a-long-enough-password"


class MemberTestCase(unittest.TestCase):
    """
    An admin signed in on ``self.admin``, a second admin who only gets
    emails, and a member (created by ``member()``) on a client of their own.
    Emails are kept in ``self.sent`` as they would be sent.
    """

    def setUp(self):
        self.k8s = FakeK8s()
        self.users = Users(MemoryStore())
        self.users.add(ROOT, PASSWORD, ADMIN, must_enrol=False)
        self.users.add(OTHER_ADMIN, PASSWORD, ADMIN, must_enrol=False)
        self.now = [1_700_000_000.0]
        self.sent = []

        settings = SmtpSettings("smtp.example.com", 587, "starttls", "hardcode@example.com")
        mailer = Mailer(settings, send=self.sent.extend, run=lambda job: job())

        self.app = create_app(
            self.k8s,
            self.users,
            {"image": "img:1", "lan": {"enabled": True, "clusterRanges": ["10.244.0.0/16"]}},
            cookie_secure="never",
            template_allowlist=TEMPLATE,
            clock=lambda: self.now[0],
            mailer=mailer,
            portal_url=PORTAL + "/",
        )
        self.app.testing = True
        self.admin = self.signed_in(ROOT)

    def signed_in(self, email):
        """
        A client of *email*'s, signed in with both steps, in a fresh time step.
        """
        client = self.app.test_client()
        self.now[0] += 30

        login = client.post("/api/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(login.status_code, 200)

        code = totp.totp(self.users.find(email)["totpSecret"], self.now[0])
        self.assertEqual(client.post("/api/totp", json={"code": code}).status_code, 200)

        return client

    def member(self, **rights):
        """
        The member's account, with *rights*, signed in on a client of its own.
        """
        self.users.add(MEMBER_EMAIL, PASSWORD, MEMBER, rights, must_enrol=False)
        return self.signed_in(MEMBER_EMAIL)

    def profile(self, name, owner="", **fields):
        """
        A profile the admin makes, for *owner* when given.
        """
        response = self.admin.post("/api/profiles", json={"name": name, "owner": owner, **fields})
        self.assertEqual(response.status_code, 201, response.json)

    def stored_spec(self, name):
        return self.k8s.get(f"{PROFILE_PATH}/{name}")["spec"]

    def own_lines(self, name):
        return self.stored_spec(name)["allowlist"].splitlines()

    def entry(self, client, profile, name):
        """
        One entry of a profile's allowlist, as *client* is shown it.
        """
        entries = client.get(f"/api/profiles/{profile}").json["allowlist"]
        return next((entry for entry in entries if entry["entry"] == name), None)

    def allowlist(self, client, profile, **body):
        return client.post(f"/api/profiles/{profile}/allowlist", json=body)


class OwnProfilesTests(MemberTestCase):
    """
    A member's profiles: theirs alone, to see and to work on.
    """

    def test_a_member_sees_only_the_profiles_they_own(self):
        member = self.member()
        self.profile("mine", owner=MEMBER_EMAIL)
        self.profile("theirs")

        self.assertEqual([profile["name"] for profile in member.get("/api/profiles").json], ["mine"])
        self.assertEqual(member.get("/api/profiles/mine").json["owner"], MEMBER_EMAIL)
        self.assertEqual(len(self.admin.get("/api/profiles").json), 2, "an admin sees every one")

        # Another's profile isn't there, whatever is asked of it.
        attempts = [
            member.get("/api/profiles/theirs"),
            member.patch("/api/profiles/theirs", json={"suspended": True}),
            member.delete("/api/profiles/theirs", headers=JSON),
            member.post("/api/profiles/theirs/allowlist", json={"op": "disable", "entry": "pypi.org"}),
            member.post("/api/profiles/theirs/extensions", json={"op": "add", "id": "eamodio.gitlens"}),
            member.post("/api/profiles/theirs/packages", json={"op": "add", "names": ["jq"]}),
            member.post("/api/profiles/theirs/token", json={}),
            member.post("/api/profiles/theirs/lan", json={"op": "remove", "name": "git.lan"}),
            member.post("/api/profiles/theirs/lan/check", json={}),
            member.get("/api/profiles/theirs/blocked"),
            member.post("/api/profiles", json={"name": "copy", "from": "theirs"}),
        ]
        self.assertEqual([attempt.status_code for attempt in attempts], [404] * len(attempts))
        self.assertIn("theirs", [profile["name"] for profile in self.admin.get("/api/profiles").json])

    def test_a_member_does_the_day_to_day_on_their_own(self):
        member = self.member()
        self.profile("mine", owner=MEMBER_EMAIL)

        self.assertIs(member.patch("/api/profiles/mine", json={"suspended": True}).json["suspended"], True)
        self.assertEqual(member.patch("/api/profiles/mine", json={"languages": ["rust"]}).status_code, 200)
        self.assertEqual(self.stored_spec("mine")["languages"], ["rust"])

        extension = member.post("/api/profiles/mine/extensions", json={"op": "add", "id": "eamodio.gitlens"})
        self.assertEqual(extension.json["changed"], True)
        package = member.post("/api/profiles/mine/packages", json={"op": "add", "names": ["jq"]})
        self.assertEqual(package.json["changed"], True)

        self.assertIn("token", member.get("/api/profiles/mine").json["access"], "their VS Code address")
        self.assertEqual(member.get("/api/profiles/mine/blocked").status_code, 200)

        self.assertEqual(member.delete("/api/profiles/mine", headers=JSON).status_code, 200)
        self.assertEqual(self.admin.get("/api/profiles/mine").status_code, 404)

    def test_the_settings_are_an_admins(self):
        member = self.member()
        self.profile("mine", owner=MEMBER_EMAIL)

        for field, value in [
            ("image", "img:2"),
            ("vscodeHost", "mine.example.com"),
            ("home", {"size": "100Gi"}),
            ("docker", {"enabled": True}),
            ("resources", {"limits": {"memory": "64Gi"}}),
            ("env", {"X": "1"}),
            ("extraVolumes", []),
            ("allowlist", "example.com\n"),
            ("owner", ""),
        ]:
            response = member.patch("/api/profiles/mine", json={field: value})
            self.assertEqual(response.status_code, 403, field)
            self.assertIn(f"only an admin can change {field}", response.json["error"])

        self.assertNotIn("image", self.stored_spec("mine"))
        self.assertEqual(self.stored_spec("mine")["owner"], MEMBER_EMAIL)

    def test_a_member_makes_as_many_profiles_as_their_rights_allow(self):
        member = self.member(profiles=2)

        first = member.post("/api/profiles", json={"name": "one", "languages": ["python"]})
        self.assertEqual((first.status_code, first.json["owner"]), (201, MEMBER_EMAIL))

        self.admin.patch("/api/profiles/one", json={"vscodeHost": "one.example.com"})
        copy = member.post("/api/profiles", json={"name": "two", "from": "one"})
        self.assertEqual((copy.status_code, copy.json["owner"], copy.json["vscodeHost"]), (201, MEMBER_EMAIL, ""))

        third = member.post("/api/profiles", json={"name": "three"})
        self.assertEqual(third.status_code, 403)
        self.assertIn("may have 2 profiles", third.json["error"])

        # Deleting one makes room for another.
        member.delete("/api/profiles/two", headers=JSON)
        self.assertEqual(member.post("/api/profiles", json={"name": "three"}).status_code, 201)

        for field in ("image", "vscodeHost", "owner"):
            refused = member.post("/api/profiles", json={"name": "four", field: "x.example.com"})
            self.assertEqual(refused.status_code, 403, field)

    def test_a_members_copy_takes_only_what_they_may_set(self):
        member = self.member(profiles=2)
        self.profile("mine", owner=MEMBER_EMAIL, languages=["go"], image="img:special")
        self.admin.patch(
            "/api/profiles/mine",
            json={"env": {"TOKEN": "x"}, "home": {"size": "200Gi"}, "docker": {"enabled": False}},
        )
        member.post("/api/profiles/mine/extensions", json={"op": "add", "id": "eamodio.gitlens"})

        member.post("/api/profiles", json={"name": "copy", "from": "mine"})

        copied = self.stored_spec("copy")
        self.assertEqual(sorted(copied), ["allowlist", "extensions", "languages", "owner"])
        self.assertEqual((copied["languages"], copied["extensions"]), (["go"], ["eamodio.gitlens"]))
        self.assertEqual(copied["allowlist"], self.stored_spec("mine")["allowlist"])

    def test_a_member_without_profiles_to_make_is_told_so(self):
        member = self.member(profiles=0)

        refused = member.post("/api/profiles", json={"name": "one"})
        self.assertEqual(refused.status_code, 403)
        self.assertIn("can't create profiles", refused.json["error"])

        self.profile("given", owner=MEMBER_EMAIL)
        self.assertEqual(len(member.get("/api/profiles").json), 1, "an admin can still give them one")

    def test_an_admin_gives_profiles_to_people(self):
        member = self.member()
        self.profile("team", owner=MEMBER_EMAIL)
        self.assertEqual(self.stored_spec("team")["owner"], MEMBER_EMAIL)

        copy = self.admin.post("/api/profiles", json={"name": "copy", "from": "team"})
        self.assertEqual(copy.json["owner"], "", "a copy has no owner unless it is given one")

        self.assertEqual(self.admin.patch("/api/profiles/copy", json={"owner": "nobody@example.com"}).status_code, 400)
        self.assertEqual(
            self.admin.patch("/api/profiles/copy", json={"owner": " Member@Example.com "}).status_code, 200
        )
        self.assertEqual(len(member.get("/api/profiles").json), 2)

        self.admin.patch("/api/profiles/team", json={"owner": ""})
        self.assertNotIn("owner", self.stored_spec("team"))
        self.assertEqual(member.get("/api/profiles/team").status_code, 404)

        refused = self.admin.post("/api/profiles", json={"name": "lost", "owner": "nobody@example.com"})
        self.assertEqual(refused.status_code, 400)

    def test_an_admins_routes_are_refused_to_a_member(self):
        member = self.member()

        attempts = [
            member.post("/api/allowlist/bulk", json={"op": "add", "entry": "example.com"}),
            member.get("/api/users"),
            member.post("/api/users", json={"email": "x@example.com", "password": PASSWORD}),
            member.patch("/api/users/member%40example.com", json={"role": ADMIN}),
            member.delete("/api/users/root%40example.com", headers=JSON),
            member.post("/api/users/root%40example.com/reset-totp", json={}),
            member.post("/api/users/root%40example.com/password", json={"password": "a-new-long-password"}),
            member.post("/api/requests/anything/approve", json={}),
            member.post("/api/requests/anything/deny", json={}),
        ]
        self.assertEqual([attempt.status_code for attempt in attempts], [403] * len(attempts))
        self.assertEqual(self.users.find(MEMBER_EMAIL)["role"], MEMBER)

        changed = member.post("/api/users/member%40example.com/password", json={"password": "a-new-long-password"})
        self.assertEqual(changed.status_code, 200, "their own password is theirs")
        self.assertEqual(member.get("/api/profiles").status_code, 200, "and this session goes on")

    def test_a_member_is_told_who_they_are(self):
        member = self.member(allowlist="ask", rw=True)

        me = member.get("/api/me").json
        self.assertEqual((me["role"], me["mail"]), (MEMBER, True))
        self.assertEqual(me["rights"], {"profiles": 1, "allowlist": "ask", "rw": True, "ssh": False, "lan": False})

    def test_new_rights_apply_at_the_next_request(self):
        member = self.member()
        self.profile("mine", owner=MEMBER_EMAIL)

        self.assertEqual(self.allowlist(member, "mine", op="add", entry="example.com").status_code, 403)

        rights = {"profiles": 1, "allowlist": "edit", "rw": False, "ssh": False, "lan": False}
        self.assertEqual(self.admin.patch("/api/users/member%40example.com", json={"rights": rights}).status_code, 200)
        self.assertEqual(self.allowlist(member, "mine", op="add", entry="example.com").json, {"changed": True})

        self.admin.patch("/api/users/member%40example.com", json={"role": ADMIN})
        self.assertEqual(len(member.get("/api/users").json), 3, "made an admin, they are one at once")


class AllowlistRightsTests(MemberTestCase):
    """
    What a member may do with their profile's allowlist, by their rights.
    """

    def test_without_rights_the_allowlist_is_an_admins(self):
        member = self.member()
        self.profile("mine", owner=MEMBER_EMAIL)

        for body in [
            {"op": "add", "entry": "example.com"},
            {"op": "remove", "entry": "pypi.org"},
            {"op": "disable", "entry": "pypi.org"},
        ]:
            response = self.allowlist(member, "mine", **body)
            self.assertEqual(response.status_code, 403, body)
            self.assertIn("an admin decides", response.json["error"])

        self.assertEqual(member.post("/api/profiles/mine/lan", json={"op": "remove", "name": "x.lan"}).status_code, 403)
        self.assertEqual(self.sent, [])

    def test_edit_rights_make_the_change(self):
        member = self.member(allowlist="edit")
        self.profile("mine", owner=MEMBER_EMAIL)

        self.assertEqual(self.allowlist(member, "mine", op="add", entry="example.com").json, {"changed": True})
        self.assertIn("example.com", self.own_lines("mine"))

        self.assertEqual(self.allowlist(member, "mine", op="disable", entry="example.com").json, {"changed": True})
        self.assertEqual(self.allowlist(member, "mine", op="remove", entry="example.com").json, {"changed": True})
        self.assertEqual(self.sent, [], "nobody is asked")

    def test_read_write_ssh_and_the_network_each_need_their_right(self):
        member = self.member(allowlist="edit")
        self.profile("mine", owner=MEMBER_EMAIL)

        refusals = {
            "read-write": self.allowlist(member, "mine", op="add", entry="github.com/acme", flag="rw"),
            "ssh tunnels": self.allowlist(member, "mine", op="add", entry="git.example.com:22", flag="ssh"),
            "reach your network": member.post(
                "/api/profiles/mine/lan",
                json={"op": "add", "name": "git.lan", "address": "192.168.1.20"},
            ),
        }
        for words, response in refusals.items():
            self.assertEqual(response.status_code, 403, words)
            self.assertIn(words, response.json["error"])

        rights = {"profiles": 1, "allowlist": "edit", "rw": True, "ssh": True, "lan": True}
        self.admin.patch("/api/users/member%40example.com", json={"rights": rights})

        self.assertEqual(self.allowlist(member, "mine", op="add", entry="github.com/acme", flag="rw").status_code, 200)
        self.assertEqual(
            self.allowlist(member, "mine", op="add", entry="git.example.com:22", flag="ssh").status_code,
            200,
        )
        pinned = member.post("/api/profiles/mine/lan", json={"op": "add", "name": "git.lan", "address": "192.168.1.20"})
        self.assertEqual(pinned.json["changed"], True)

    def test_what_an_edit_opens_is_what_the_proxy_would_get(self):
        # An admin's read-write repository (git's address and GitHub's API
        # for it), switched off; the member switches it on naming no access.
        # Its entries are read-write whatever the request says, and the
        # member may not have those.
        member = self.member(allowlist="edit")
        self.profile("mine", owner=MEMBER_EMAIL)
        for entry in ("github.com/acme/tool.git", "api.github.com/repos/acme/tool"):
            self.allowlist(self.admin, "mine", op="add", entry=entry, flag="rw")
            self.allowlist(self.admin, "mine", op="disable", entry=entry)

        response = self.allowlist(member, "mine", op="enable", entry="github.com/acme/tool.git", flag="")
        self.assertEqual(response.status_code, 403)
        self.assertIn("read-write", response.json["error"])

        shorthand = self.allowlist(member, "mine", op="enable", entry="github:acme/tool")
        self.assertEqual(shorthand.status_code, 403, "github:OWNER/REPO switches on its read-write entries")
        self.assertFalse(self.entry(self.admin, "mine", "github.com/acme/tool.git")["enabled"])

        self.allowlist(self.admin, "mine", op="add", entry="github.com/acme/other", flag="rw")
        narrowed = self.allowlist(member, "mine", op="add", entry="github.com/acme/other", flag="")
        self.assertEqual(narrowed.status_code, 200, "read-only where it is read-write already opens nothing")

    def test_an_admins_switch_off_holds_whatever_the_languages(self):
        # The admin switched Claude Code's read-write API off. Its language
        # switched off and on again around it, switching the entry back on
        # still opens a read-write entry, which the member may not have.
        member = self.member(allowlist="edit")
        self.profile("mine", owner=MEMBER_EMAIL, languages=["python", "claude_cli"])
        self.allowlist(self.admin, "mine", op="disable", entry="api.anthropic.com")

        member.patch("/api/profiles/mine", json={"languages": ["python"]})
        response = self.allowlist(member, "mine", op="enable", entry="api.anthropic.com", flag="rw")
        self.assertEqual(response.status_code, 403)
        self.assertIn("read-write", response.json["error"])
        self.assertIn("off api.anthropic.com", self.own_lines("mine"))

        own = self.allowlist(member, "mine", op="add", entry="api.anthropic.com")
        self.assertEqual(own.status_code, 200, "a read-only entry of the profile's own is theirs to add")
        self.assertIn("off api.anthropic.com", self.own_lines("mine"), "and leaves the admin's switch as it is")

    def test_flags_are_checked_for_every_edit(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL, languages=["rust"])
        self.allowlist(self.admin, "mine", op="disable", entry="crates.io")

        refused = self.allowlist(member, "mine", op="enable", entry="crates.io", flag="sometimes")
        self.assertEqual(refused.status_code, 400)

        first = self.allowlist(member, "mine", op="enable", entry="crates.io", flag="rw").json["request"]
        again = self.allowlist(member, "mine", op="enable", entry="crates.io", flag="").json["request"]
        self.assertEqual((first["id"], first["flag"]), (again["id"], ""), "the same switch is one request")

    def test_a_provided_entry_switched_off_by_an_admin_needs_asking_again(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL, languages=["rust"])
        self.allowlist(self.admin, "mine", op="disable", entry="crates.io")

        response = self.allowlist(member, "mine", op="enable", entry="crates.io")
        self.assertEqual((response.status_code, response.json["requested"]), (202, True))
        self.assertFalse(self.entry(self.admin, "mine", "crates.io")["enabled"])

        self.assertEqual(self.allowlist(member, "mine", op="disable", entry="static.crates.io").json, {"changed": True})


class RequestsTests(MemberTestCase):
    """
    A member who asks: their requests, the admins' emails, and the admins'
    decisions.
    """

    def ask(self, member, entry="example.com", flag="", reason="the docs live there"):
        return self.allowlist(member, "mine", op="add", entry=entry, flag=flag, reason=reason)

    def test_an_entry_asked_for_waits_for_an_admin(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL)

        response = self.ask(member)
        self.assertEqual(response.status_code, 202)
        request = response.json["request"]
        self.assertEqual(
            {key: request[key] for key in ("profile", "by", "kind", "op", "entry", "flag", "access", "state")},
            {
                "profile": "mine",
                "by": MEMBER_EMAIL,
                "kind": "entry",
                "op": "add",
                "entry": "example.com",
                "flag": "",
                "access": "",
                "state": "pending",
            },
        )
        self.assertNotIn("example.com", self.own_lines("mine"), "nothing changes yet")
        self.assertIn(REQUESTS_SECRET, " ".join(self.k8s.store), "kept in a Secret of its own")

        # Every admin is emailed, with a link to the request in the portal.
        self.assertEqual(sorted(message["To"] for message in self.sent), [OTHER_ADMIN, ROOT])
        message = self.sent[0]
        self.assertEqual(message["Subject"], f"HardCode: {MEMBER_EMAIL} asks to allow example.com on mine")
        body = message.get_content()
        self.assertIn(f"{PORTAL}/#request/{request['id']}", body)
        self.assertIn("> the docs live there", body)
        self.assertIn("Access: read-only", body)

        # Asking again is the same request, and nobody is emailed again.
        again = self.ask(member)
        self.assertEqual(again.json["request"]["id"], request["id"])
        self.assertEqual(len(self.sent), 2)

        # The member sees it waiting, on the profile and in their requests.
        self.assertEqual([item["id"] for item in member.get("/api/profiles/mine").json["requests"]], [request["id"]])
        self.assertEqual([item["state"] for item in member.get("/api/requests").json], ["pending"])

    def test_what_takes_something_away_isnt_asked_for(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL)

        self.allowlist(self.admin, "mine", op="add", entry="example.org")

        self.assertEqual(self.allowlist(member, "mine", op="disable", entry="pypi.org").json, {"changed": True})
        self.assertEqual(self.allowlist(member, "mine", op="remove", entry="example.org").json, {"changed": True})
        self.assertEqual(self.ask(member, entry="api.anthropic.com").json, {"changed": False}, "it has it already")
        self.assertEqual(self.sent, [])

    def test_asking_needs_the_right_too(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL)

        self.assertEqual(self.ask(member, entry="github.com/acme", flag="rw").status_code, 403)

        rights = {"profiles": 1, "allowlist": "ask", "rw": True, "ssh": False, "lan": False}
        self.admin.patch("/api/users/member%40example.com", json={"rights": rights})

        request = self.ask(member, entry="github.com/acme", flag="rw").json["request"]
        self.assertEqual(request["access"], "rw")
        self.assertIn("github.com/acme (read-write)", self.sent[0]["Subject"])

    def test_an_approval_makes_the_change(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL)
        request = self.ask(member).json["request"]

        approved = self.admin.post(f"/api/requests/{request['id']}/approve", json={})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json["changed"], True)
        self.assertIn("example.com", self.own_lines("mine"))

        seen = member.get("/api/requests").json[0]
        self.assertEqual((seen["state"], seen["decidedBy"]), ("approved", ROOT))
        self.assertEqual(member.get("/api/profiles/mine").json["requests"], [], "nothing waits any more")

        other_admin = self.signed_in(OTHER_ADMIN)
        twice = other_admin.post(f"/api/requests/{request['id']}/deny", json={})
        self.assertEqual(twice.status_code, 409)
        self.assertIn(f"{ROOT} has approved this request already", twice.json["error"])

        self.assertEqual(self.admin.post("/api/requests/nothing/approve", json={}).status_code, 404)

    def test_a_denial_changes_nothing(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL)
        request = self.ask(member).json["request"]

        denied = self.admin.post(f"/api/requests/{request['id']}/deny", json={})
        self.assertEqual(denied.json["request"]["state"], "denied")
        self.assertNotIn("example.com", self.own_lines("mine"))

        again = self.ask(member)
        self.assertNotEqual(again.json["request"]["id"], request["id"], "asking again is a new request")

    def test_a_name_on_the_network_asked_for(self):
        member = self.member(allowlist="ask", lan=True)
        self.profile("mine", owner=MEMBER_EMAIL)

        body = {"op": "add", "name": "git.lan", "address": "192.168.1.20", "reason": "our git server"}
        response = member.post("/api/profiles/mine/lan", json=body)
        self.assertEqual(response.status_code, 202)
        request = response.json["request"]
        self.assertEqual((request["kind"], request["entry"], request["address"]), ("lan", "git.lan", "192.168.1.20"))
        self.assertIn("git.lan to reach 192.168.1.20", self.sent[0]["Subject"])

        refused = member.post("/api/profiles/mine/lan", json={**body, "address": "10.244.0.9"})
        self.assertEqual(refused.status_code, 400, "checked as it is asked for, not as it is approved")

        self.admin.post(f"/api/requests/{request['id']}/approve", json={})
        pins = self.admin.get("/api/profiles/mine").json["lan"]["pins"]
        self.assertEqual([(pin["name"], pin["address"]) for pin in pins], [("git.lan", "192.168.1.20")])

        removed = member.post("/api/profiles/mine/lan", json={"op": "remove", "name": "git.lan"})
        self.assertEqual(removed.json["changed"], True, "taking it away isn't asked for")

    def test_a_profile_deleted_meanwhile(self):
        member = self.member(allowlist="ask", profiles=2)
        self.profile("mine", owner=MEMBER_EMAIL)
        self.profile("gone", owner=MEMBER_EMAIL)
        request = self.ask(member).json["request"]
        other = self.allowlist(member, "gone", op="add", entry="example.org").json["request"]

        # Deleted with kubectl, the request stays: an approval can't be made,
        # and leaves it waiting, to be denied.
        self.k8s.delete(f"{PROFILE_PATH}/mine")
        self.assertEqual([item["profileGone"] for item in self.admin.get("/api/requests").json], [False, True])
        self.assertEqual(self.admin.post(f"/api/requests/{request['id']}/approve", json={}).status_code, 404)
        stored = approvals.Approvals(SecretStore(self.k8s, REQUESTS_SECRET, "requests.json"))
        self.assertEqual(stored.find(request["id"])["state"], "pending")

        # Deleted in the portal, its requests go with it.
        member.delete("/api/profiles/gone", headers=JSON)
        self.assertNotIn(other["id"], [item["id"] for item in self.admin.get("/api/requests").json])

    def test_a_profile_given_to_someone_else_meanwhile(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL)
        request = self.ask(member).json["request"]

        self.admin.patch("/api/profiles/mine", json={"owner": OTHER_ADMIN})
        self.assertIs(self.admin.get("/api/requests").json[0]["notTheirs"], True)

        refused = self.admin.post(f"/api/requests/{request['id']}/approve", json={})
        self.assertEqual(refused.status_code, 409)
        self.assertIn(f"mine no longer belongs to {MEMBER_EMAIL}", refused.json["error"])
        self.assertNotIn("example.com", self.own_lines("mine"))

        self.assertEqual(self.admin.post(f"/api/requests/{request['id']}/deny", json={}).status_code, 200)

    def test_a_request_is_only_so_long(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL)

        refused = self.ask(member, entry="example.com/" + "a" * 3000)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("too long", refused.json["error"])

    def test_everyones_requests_waiting_are_only_so_many(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL)

        with mock.patch.object(approvals, "PENDING_AT_MOST", 2):
            self.ask(member, entry="one.example.com")
            self.ask(member, entry="two.example.com")
            refused = self.ask(member, entry="three.example.com")

        self.assertEqual(refused.status_code, 400)
        self.assertIn("2 requests are waiting for the admins", refused.json["error"])

    def test_a_member_has_only_so_many_requests_waiting(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL)

        for number in range(approvals.PENDING_PER_MEMBER):
            self.assertEqual(self.ask(member, entry=f"site{number}.example.com").status_code, 202)

        refused = self.ask(member, entry="one-more.example.com")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("requests waiting", refused.json["error"])

    def test_the_admins_see_everyones_and_a_member_their_own(self):
        member = self.member(allowlist="ask")
        self.profile("mine", owner=MEMBER_EMAIL)
        self.ask(member)

        self.users.add("second.member@example.com", PASSWORD, MEMBER, {"allowlist": "ask"}, must_enrol=False)
        second = self.signed_in("second.member@example.com")
        self.profile("theirs", owner="second.member@example.com")
        self.allowlist(second, "theirs", op="add", entry="example.org")

        self.assertEqual(len(self.admin.get("/api/requests").json), 2)
        self.assertEqual([item["entry"] for item in member.get("/api/requests").json], ["example.com"])
        self.assertEqual([item["entry"] for item in second.get("/api/requests").json], ["example.org"])
        self.assertEqual(len(self.admin.get("/api/profiles/theirs").json["requests"]), 1)


class PeopleTests(MemberTestCase):
    """
    The accounts, as the People page manages them.
    """

    def test_people_with_their_roles_rights_and_profiles(self):
        self.member(allowlist="ask", profiles=3)
        self.profile("mine", owner=MEMBER_EMAIL)

        people = {person["email"]: person for person in self.admin.get("/api/users").json}
        self.assertEqual(people[ROOT]["role"], ADMIN)
        self.assertEqual(
            (
                people[MEMBER_EMAIL]["role"],
                people[MEMBER_EMAIL]["rights"]["allowlist"],
                people[MEMBER_EMAIL]["profiles"],
            ),
            (MEMBER, "ask", 1),
        )

    def test_adding_people(self):
        added = self.admin.post("/api/users", json={"email": "new@example.com", "password": PASSWORD})
        self.assertEqual(added.status_code, 201)
        self.assertEqual(self.users.find("new@example.com")["role"], MEMBER, "a member unless said otherwise")

        body = {"email": "new.admin@example.com", "password": PASSWORD, "role": ADMIN}
        self.assertEqual(self.admin.post("/api/users", json=body).status_code, 201)
        self.assertEqual(self.users.find("new.admin@example.com")["role"], ADMIN)

        rights = {"profiles": 5, "allowlist": "edit", "rw": True}
        body = {"email": "with.rights@example.com", "password": PASSWORD, "rights": rights}
        self.admin.post("/api/users", json=body)
        self.assertEqual(self.users.find("with.rights@example.com")["rights"]["profiles"], 5)

        for bad in [{"role": "owner"}, {"rights": {"profiles": -1}}, {"rights": {"allowlist": "always"}}]:
            body = {"email": "bad@example.com", "password": PASSWORD, **bad}
            self.assertEqual(self.admin.post("/api/users", json=body).status_code, 400, bad)

    def test_there_is_always_an_admin(self):
        self.admin.delete("/api/users/other.admin%40example.com", headers=JSON)

        demoted = self.admin.patch("/api/users/root%40example.com", json={"role": MEMBER})
        self.assertEqual(demoted.status_code, 400)
        self.assertIn("the last admin can't be made a member", demoted.json["error"])

        removed = self.admin.delete("/api/users/root%40example.com", headers=JSON)
        self.assertEqual(removed.status_code, 400)

    def test_a_removed_members_profiles_stay_for_an_admin(self):
        self.member()
        self.profile("mine", owner=MEMBER_EMAIL)

        self.assertEqual(self.admin.delete("/api/users/member%40example.com", headers=JSON).status_code, 200)
        self.assertEqual(self.admin.get("/api/profiles/mine").json["owner"], MEMBER_EMAIL)


class NoMailTests(unittest.TestCase):
    """
    Without an SMTP server, requests are only in the portal.
    """

    def test_requests_without_email(self):
        k8s = FakeK8s()
        users = Users(MemoryStore())
        users.add(ROOT, PASSWORD, ADMIN, must_enrol=False)
        users.add(MEMBER_EMAIL, PASSWORD, MEMBER, {"allowlist": "ask"}, must_enrol=False)
        now = [1_700_000_000.0]

        app = create_app(
            k8s, users, {"image": "img:1"}, cookie_secure="never", template_allowlist=TEMPLATE, clock=lambda: now[0]
        )
        member = app.test_client()
        member.post("/api/login", json={"email": MEMBER_EMAIL, "password": PASSWORD})
        member.post("/api/totp", json={"code": totp.totp(users.find(MEMBER_EMAIL)["totpSecret"], now[0])})

        self.assertIs(member.get("/api/me").json["mail"], False)

        k8s.create(PROFILE_PATH, {"metadata": {"name": "mine"}, "spec": {"owner": MEMBER_EMAIL, "allowlist": ""}})
        response = member.post("/api/profiles/mine/allowlist", json={"op": "add", "entry": "example.com"})
        self.assertEqual(response.status_code, 202)


if __name__ == "__main__":
    unittest.main()
