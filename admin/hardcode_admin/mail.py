"""
Emails to the admins when a member asks for an allowlist entry.

They are sent only when the chart gives an SMTP server (``SMTP_HOST``),
with Python's own ``smtplib``, in the background: a slow or unreachable
server delays no one's request, and a failure is logged. The email links to
the request in the admin portal; approving or denying it takes a signed-in
admin there, never the link alone, which a mail scanner may open first.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Callable, Optional

log = logging.getLogger("hardcode.admin.mail")

# How the connection to the SMTP server is protected: STARTTLS after
# connecting (port 587), TLS from the start (port 465), or nothing (a relay
# inside the cluster).
SECURITY = ("starttls", "tls", "none")

# How long the server may take to answer, each step.
TIMEOUT_SECONDS = 20

# An entry's access, in the email's words: in full, and in its subject.
ACCESS_WORDS = {"": "read-only", "rw": "read-write (uploads, push)", "ssh": "ssh tunnel"}
SHORT_ACCESS_WORDS = {"rw": "read-write", "ssh": "ssh tunnel"}


@dataclass(frozen=True)
class SmtpSettings:
    """
    Where the emails go out: the server, how the connection is protected,
    the sender's address, and the login when the server wants one.
    """

    host: str
    port: int
    security: str
    sender: str
    username: str = ""
    password: str = ""


def settings_from(environment: dict) -> Optional[SmtpSettings]:
    """
    The SMTP settings from the environment (``SMTP_HOST``, ``SMTP_PORT``,
    ``SMTP_TLS``, ``SMTP_FROM``, ``SMTP_USERNAME``, ``SMTP_PASSWORD``);
    ``None`` without a host, when no emails are sent.
    """
    host = environment.get("SMTP_HOST", "").strip()
    if not host:
        return None

    security = environment.get("SMTP_TLS", "starttls").strip() or "starttls"
    if security not in SECURITY:
        raise ValueError(f"SMTP_TLS is starttls, tls or none, not {security!r}")

    default_port = "465" if security == "tls" else "587"
    port = environment.get("SMTP_PORT", "").strip() or default_port
    if not port.isdigit():
        raise ValueError(f"SMTP_PORT is a port number, not {port!r}")

    sender = environment.get("SMTP_FROM", "").strip()
    if not sender:
        raise ValueError("SMTP_FROM is needed with SMTP_HOST: the address the emails come from")

    return SmtpSettings(
        host=host,
        port=int(port),
        security=security,
        sender=sender,
        username=environment.get("SMTP_USERNAME", ""),
        password=environment.get("SMTP_PASSWORD", ""),
    )


def describe(request: dict) -> str:
    """
    What a request asks for, in a few words: ``example.com``,
    ``github.com/team (read-write)``, ``git.lan to reach 192.168.1.20``.
    """
    if request["kind"] == "lan":
        return f"{request['entry']} to reach {request['address']}"

    if request["access"]:
        return f"{request['entry']} ({SHORT_ACCESS_WORDS[request['access']]})"

    return request["entry"]


def request_message(request: dict, sender: str, to: str, portal_url: str) -> EmailMessage:
    """
    The email telling the admin *to* about *request*. Its link opens the
    request in the portal; the reason is quoted, as the member wrote it.
    """
    what = describe(request)

    lines = [
        f"{request['by']} asks to allow {what} in the profile {request['profile']}.",
        "",
    ]
    if request["kind"] == "entry":
        lines.append(f"Access: {ACCESS_WORDS[request['access']]}")
    else:
        lines.append("A name on your network: the proxy lets it reach that address, and nothing else there.")

    if request["reason"]:
        lines += ["", "Their reason:"]
        lines += [f"> {line}" for line in request["reason"].splitlines()]

    lines.append("")
    if portal_url:
        lines += [
            "Open the request, sign in, and approve or deny it there:",
            f"{portal_url}/#request/{request['id']}",
        ]
    else:
        lines.append("Sign in to the admin portal and open Requests to approve or deny it.")

    lines += ["", "Nothing is allowed until an admin approves it in the portal."]

    message = EmailMessage()
    message["Subject"] = f"HardCode: {request['by']} asks to allow {what} on {request['profile']}"
    message["From"] = sender
    message["To"] = to
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    message.set_content("\n".join(lines) + "\n")

    return message


class Mailer:
    """
    Sends the admins' emails, when there are *settings*.

    *send* delivers a list of messages (``send_smtp``, by default), and *run*
    runs a job: in a background thread of its own by default, so that no
    request waits for the server. Tests give both.
    """

    def __init__(
        self,
        settings: Optional[SmtpSettings],
        send: Optional[Callable[[list[EmailMessage]], None]] = None,
        run: Optional[Callable[[Callable[[], None]], object]] = None,
    ):
        self.settings = settings
        self.send = send or self.send_smtp
        self._executor = None if run else ThreadPoolExecutor(max_workers=1, thread_name_prefix="mail")
        self.run = run or self._executor.submit

    @property
    def enabled(self) -> bool:
        return self.settings is not None

    def request_made(self, request: dict, admins: list[str], portal_url: str) -> None:
        """
        Tell every admin in *admins* about a new *request*: one email each,
        sent in the background. Nothing without settings, or admins.
        """
        if not self.enabled or not admins:
            return

        messages = [request_message(request, self.settings.sender, admin, portal_url) for admin in admins]

        def deliver() -> None:
            # Whatever goes wrong is logged: in a thread of its own, an error
            # would otherwise end in a result nobody reads. Most are the
            # server's (smtplib's and ssl's errors are OSErrors), but an
            # address the server's encoding can't carry fails otherwise.
            try:
                self.send(messages)
            except Exception as error:  # noqa: BLE001 - logged, as the operator's loops do
                log.error("couldn't email the admins about request %s: %s", request["id"], error)

        self.run(deliver)

    def send_smtp(self, messages: list[EmailMessage]) -> None:
        """
        Deliver *messages* through the SMTP server, over one connection,
        checking its certificate whenever the connection is protected.
        """
        settings = self.settings
        context = ssl.create_default_context()

        if settings.security == "tls":
            client = smtplib.SMTP_SSL(settings.host, settings.port, timeout=TIMEOUT_SECONDS, context=context)
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=TIMEOUT_SECONDS)

        with client:
            if settings.security == "starttls":
                client.starttls(context=context)
            if settings.username:
                client.login(settings.username, settings.password)

            for message in messages:
                client.send_message(message)
