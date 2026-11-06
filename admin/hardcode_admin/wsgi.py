"""
The process: the operator in a thread, and the Flask app for gunicorn.

Configured through the environment:

    K8S_NAMESPACE           the namespace to manage (default: the service account's)
    ADMIN_EMAIL             the first admin, created only while there are no
    ADMIN_PASSWORD          accounts at all
    PROFILE_DEFAULTS_JSON   the defaults every Profile inherits
    USERS_SECRET            the Secret holding the accounts (default hardcode-admin-users)
    COOKIE_SECURE           auto | always | never
    TRUSTED_PROXIES         addresses or CIDRs whose X-Forwarded-For is believed
    ADMIN_URL               the portal's address, for the links in the admins' emails
    SMTP_HOST, SMTP_PORT,   where members' requests are emailed to the admins
    SMTP_TLS, SMTP_FROM,    from (mail.settings_from); without SMTP_HOST, they
    SMTP_USERNAME,          are only shown in the portal
    SMTP_PASSWORD
    OPERATOR=0, ADMIN=0     run only one half
"""

from __future__ import annotations

import json
import logging
import os

from flask import Flask

from .auth import ADMIN, Users
from .k8s import K8s
from .mail import Mailer, settings_from
from .operator import Operator
from .web import SecretStore, create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("hardcode.admin")


def portal_url(environment: dict) -> str:
    """
    The portal's address from ``ADMIN_URL``, for the links in the admins'
    emails: ``http://`` or ``https://`` and a host, without a final slash;
    ``""`` when it isn't set.
    """
    url = environment.get("ADMIN_URL", "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise ValueError(f"ADMIN_URL is the portal's address, starting with https://, not {url!r}")

    return url


def build(env: dict | None = None) -> Flask:
    """
    Start the operator (unless switched off) and return the web app.

    With the admin switched off, the app is a bare health check, for the
    probes of an operator-only deployment.
    """
    environment = dict(os.environ if env is None else env)
    k8s = K8s(env=environment)

    defaults_json = environment.get("PROFILE_DEFAULTS_JSON")
    defaults = json.loads(defaults_json) if defaults_json else {}

    users = Users(SecretStore(k8s, environment.get("USERS_SECRET", "hardcode-admin-users")))

    email, password = environment.get("ADMIN_EMAIL"), environment.get("ADMIN_PASSWORD")
    if not users.list() and email and password:
        users.add(email, password, ADMIN)
        log.info("created the first admin %s (TOTP enrolment at first sign-in)", email)

    if environment.get("OPERATOR", "1") != "0":
        Operator(k8s, defaults).start()

    if environment.get("ADMIN", "1") != "0":
        return create_app(
            k8s,
            users,
            defaults,
            environment.get("COOKIE_SECURE", "auto"),
            trusted_proxies=environment.get("TRUSTED_PROXIES", "").split(","),
            mailer=Mailer(settings_from(environment)),
            portal_url=portal_url(environment),
        )

    health_only = Flask("hardcode_admin_operator_only")
    health_only.add_url_rule("/healthz", "healthz", lambda: {"ok": True})
    return health_only


app = build()
