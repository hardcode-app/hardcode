# hardcode-admin

The Kubernetes operator and admin web UI: one Python process (Flask behind
gunicorn, the operator in a thread), sharing the allowlist, blocked-traffic
and language code with the `hardcode` command (`hardcode_cli`). Packaged by
[images/admin/Dockerfile](../images/admin/Dockerfile): Python from the Ubuntu
26.04 archive, and Flask, gunicorn and qrcode from PyPI as
[images/admin/uv.lock](../images/admin/uv.lock) locks them; deployed by the
[Helm chart](../helm/README.md).

```
hardcode_admin/web.py        the API and page: sign-in (password, TOTP), profiles, their languages and extensions, allowlists, people, requests
hardcode_admin/operator.py   watches Profile objects, reconciles their resources with server-side apply
hardcode_admin/manifests.py  the resources one Profile turns into (mirrors the Compose stack)
hardcode_admin/k8s.py        API client on urllib: in-cluster service account, or K8S_API for development
hardcode_admin/auth.py       scrypt passwords, accounts (admins and members, their rights) in a Secret, sessions, login throttling
hardcode_admin/approvals.py  members' requests for allowlist entries, in a Secret of their own
hardcode_admin/mail.py       the admins' emails about requests, through SMTP (smtplib)
hardcode_admin/totp.py       RFC 6238 TOTP, otpauth URLs
hardcode_admin/wsgi.py       builds everything from the environment; `app` for gunicorn
hardcode_admin/web/          the page (base.css and the icon are copied from app/ at build)
tests/                       unit + API tests against an in-memory Kubernetes API (Flask test client)
```

Environment: `K8S_NAMESPACE` (defaults to the service account's), `ADMIN_EMAIL`
/ `ADMIN_PASSWORD` (bootstrap admin, used only while no accounts exist),
`PROFILE_DEFAULTS_JSON`, `USERS_SECRET` (default `hardcode-admin-users`),
`COOKIE_SECURE` (`auto` | `always` | `never`), `ADMIN_URL` and `SMTP_*` (the
admins' emails about requests), `OPERATOR=0` / `ADMIN=0` to run only one
half, `PORT` (8080).

Run against a cluster from a checkout: `kubectl proxy &` then

```sh
kubectl apply -f helm/hardcode/crds/
K8S_API=http://127.0.0.1:8001 K8S_NAMESPACE=sandbox ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='a long password' python3 -m hardcode_admin   # from admin/
```

Tests: `tests/admin.py`, which runs them in the build image, or from
`admin/`: `python3 -m unittest discover -s tests -t .`.

The full documentation is in [docs/admin.md](../docs/admin.md) (UI with screenshots, API, configuration).
