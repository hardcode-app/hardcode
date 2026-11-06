"""
The admins' emails about members' requests (hardcode_admin.mail), and the
requests themselves (hardcode_admin.approvals), with the SMTP server and
the store faked.
"""

import smtplib
import unittest
from unittest import mock

from hardcode_admin import approvals, mail
from hardcode_admin.approvals import ApprovalError, Approvals, NoSuchRequest

from .helpers import MemoryStore

REQUEST = {
    "id": "abc123",
    "profile": "team-a",
    "by": "member@example.com",
    "kind": "entry",
    "op": "add",
    "entry": "github.com/acme",
    "flag": "rw",
    "access": "rw",
    "address": "",
    "reason": "we push there\nevery day",
}


def change(entry="example.com", by="member@example.com", profile="team-a"):
    """
    A change a member asks for: one read-only entry.
    """
    return {
        "profile": profile,
        "by": by,
        "kind": "entry",
        "op": "add",
        "entry": entry,
        "flag": "",
        "access": "",
        "address": "",
    }


class SettingsTests(unittest.TestCase):
    """
    The SMTP settings, from the environment the chart gives.
    """

    def test_no_host_no_emails(self):
        self.assertIsNone(mail.settings_from({}))
        self.assertIsNone(mail.settings_from({"SMTP_HOST": " ", "SMTP_FROM": "a@b.co"}))

    def test_the_port_follows_the_protection(self):
        base = {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "hardcode@example.com"}

        self.assertEqual(mail.settings_from(base).port, 587)
        self.assertEqual(mail.settings_from({**base, "SMTP_TLS": "tls"}).port, 465)
        self.assertEqual(mail.settings_from({**base, "SMTP_PORT": "25", "SMTP_TLS": "none"}).port, 25)

        settings = mail.settings_from({**base, "SMTP_USERNAME": "user", "SMTP_PASSWORD": "secret"})
        self.assertEqual((settings.username, settings.password, settings.security), ("user", "secret", "starttls"))

    def test_mistakes_are_said_at_start(self):
        base = {"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "hardcode@example.com"}

        for broken, words in [
            ({**base, "SMTP_TLS": "ssl"}, "starttls, tls or none"),
            ({**base, "SMTP_PORT": "twenty-five"}, "a port number"),
            ({"SMTP_HOST": "smtp.example.com"}, "SMTP_FROM is needed"),
        ]:
            with self.assertRaises(ValueError) as raised:
                mail.settings_from(broken)
            self.assertIn(words, str(raised.exception))


class MessageTests(unittest.TestCase):
    """
    What an admin's email says.
    """

    def test_an_entry_with_its_access_and_reason(self):
        message = mail.request_message(
            REQUEST, "hardcode@example.com", "admin@example.com", "https://admin.example.com"
        )

        self.assertEqual(
            message["Subject"],
            "HardCode: member@example.com asks to allow github.com/acme (read-write) on team-a",
        )
        self.assertEqual((message["From"], message["To"]), ("hardcode@example.com", "admin@example.com"))

        body = message.get_content()
        self.assertIn("Access: read-write (uploads, push)", body)
        self.assertIn("> we push there\n> every day", body, "the reason quoted, as they wrote it")
        self.assertIn("https://admin.example.com/#request/abc123", body)
        self.assertIn("sign in, and approve or deny it there", body)

    def test_a_name_on_the_network_without_the_portals_address(self):
        request = {**REQUEST, "kind": "lan", "entry": "git.lan", "flag": "", "access": "", "address": "192.168.1.20"}
        message = mail.request_message(request, "hardcode@example.com", "admin@example.com", "")

        self.assertIn("git.lan to reach 192.168.1.20", message["Subject"])
        body = message.get_content()
        self.assertIn("open Requests", body)
        self.assertNotIn("#request/", body)

    def test_a_header_cant_be_given_another_line(self):
        sneaky = {**REQUEST, "profile": "team-a\nBcc: everyone@example.com"}

        with self.assertRaises(ValueError):
            mail.request_message(sneaky, "hardcode@example.com", "admin@example.com", "")


class MailerTests(unittest.TestCase):
    """
    Sending: one email per admin, in the background, and nothing without
    settings.
    """

    SETTINGS = mail.SmtpSettings("smtp.example.com", 587, "starttls", "hardcode@example.com", "user", "secret")

    def test_one_email_per_admin(self):
        sent, jobs = [], []
        mailer = mail.Mailer(self.SETTINGS, send=sent.extend, run=jobs.append)

        mailer.request_made(REQUEST, ["one@example.com", "two@example.com"], "https://admin.example.com")
        self.assertEqual(sent, [], "sent in the background, not by the request")

        jobs.pop()()
        self.assertEqual([message["To"] for message in sent], ["one@example.com", "two@example.com"])

    def test_nothing_without_settings_or_admins(self):
        sent = []

        mail.Mailer(None, send=sent.extend, run=lambda job: job()).request_made(REQUEST, ["a@b.co"], "")
        mail.Mailer(self.SETTINGS, send=sent.extend, run=lambda job: job()).request_made(REQUEST, [], "")

        self.assertEqual(sent, [])
        self.assertFalse(mail.Mailer(None).enabled)

    def test_a_server_that_fails_is_logged(self):
        def refuse(messages):
            raise smtplib.SMTPRecipientsRefused({"one@example.com": (550, b"no")})

        mailer = mail.Mailer(self.SETTINGS, send=refuse, run=lambda job: job())

        with self.assertLogs("hardcode.admin.mail", level="ERROR") as logged:
            mailer.request_made(REQUEST, ["one@example.com"], "")
        self.assertIn("couldn't email the admins about request abc123", logged.output[0])

    def test_starttls_then_the_login(self):
        mailer = mail.Mailer(self.SETTINGS)
        message = mail.request_message(REQUEST, "hardcode@example.com", "one@example.com", "")

        with mock.patch.object(smtplib, "SMTP") as smtp:
            mailer.send_smtp([message])

        smtp.assert_called_once_with("smtp.example.com", 587, timeout=mail.TIMEOUT_SECONDS)
        client = smtp.return_value
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("user", "secret")
        client.send_message.assert_called_once_with(message)

    def test_tls_from_the_start_and_no_login(self):
        settings = mail.SmtpSettings("smtp.example.com", 465, "tls", "hardcode@example.com")
        mailer = mail.Mailer(settings)

        with mock.patch.object(smtplib, "SMTP_SSL") as smtp_ssl, mock.patch.object(smtplib, "SMTP") as smtp:
            mailer.send_smtp([])

        smtp.assert_not_called()
        self.assertEqual(smtp_ssl.call_args.args, ("smtp.example.com", 465))
        smtp_ssl.return_value.login.assert_not_called()


class ApprovalsTests(unittest.TestCase):
    """
    The requests' store: asking, deciding, and what is kept.
    """

    def setUp(self):
        self.ids = iter(f"r{number}" for number in range(1000))
        self.approvals = Approvals(
            MemoryStore(), clock=lambda: "2026-11-06T00:00:00+00:00", new_id=lambda: next(self.ids)
        )

    def test_the_same_change_is_one_request(self):
        first, new = self.approvals.ask(change(by="Member@Example.com"), "because")
        self.assertEqual(
            (first["id"], new, first["by"], first["reason"]), ("r0", True, "member@example.com", "because")
        )

        again, new = self.approvals.ask(change())
        self.assertEqual((again["id"], new), ("r0", False))

        other, new = self.approvals.ask(change(profile="team-b"))
        self.assertEqual((other["id"], new), ("r1", True), "another profile's is another request")

    def test_reasons_are_short(self):
        with self.assertRaises(ApprovalError):
            self.approvals.ask(change(), "x" * (approvals.REASON_LENGTH + 1))

    def test_deciding_once(self):
        request, _ = self.approvals.ask(change())

        decided = self.approvals.decide(request["id"], approvals.APPROVED, "Admin@Example.com")
        self.assertEqual((decided["state"], decided["decidedBy"]), ("approved", "admin@example.com"))

        with self.assertRaises(ApprovalError) as raised:
            self.approvals.decide(request["id"], approvals.DENIED, "other@example.com")
        self.assertIn("admin@example.com has approved this request already", str(raised.exception))

        with self.assertRaises(NoSuchRequest):
            self.approvals.decide("nothing", approvals.DENIED, "admin@example.com")

        self.approvals.reopen(request["id"])
        self.assertEqual(self.approvals.find(request["id"])["state"], "pending")

    def test_only_the_latest_decided_are_kept(self):
        with mock.patch.object(approvals, "DECIDED_KEPT", 2):
            for number in range(4):
                request, _ = self.approvals.ask(change(entry=f"site{number}.example.com"))
                self.approvals.decide(request["id"], approvals.DENIED, "admin@example.com")

        self.assertEqual([request["id"] for request in self.approvals.list()], ["r2", "r3"])

    def test_a_deleted_profiles_waiting_requests_go(self):
        waiting, _ = self.approvals.ask(change(profile="gone"))
        decided, _ = self.approvals.ask(change(profile="gone", entry="example.org"))
        self.approvals.decide(decided["id"], approvals.DENIED, "admin@example.com")
        kept, _ = self.approvals.ask(change(profile="kept"))

        self.approvals.drop_profile("gone")

        self.assertEqual([request["id"] for request in self.approvals.list()], [decided["id"], kept["id"]])


if __name__ == "__main__":
    unittest.main()
