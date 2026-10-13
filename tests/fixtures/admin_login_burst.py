"""
Eight sign-ins at the same moment, as many as the admin's server has threads
(images/admin/Dockerfile), against its real login handler: Flask's test
client, an in-memory Kubernetes API and account store, eight addresses that
have no account. tests/admin.py runs it in a container capped at the chart's
memory limit for the admin, which it must fit in.

    python3 admin_login_burst.py CHECKOUT

Prints how the eight sign-ins were answered, and exits 0 when every one was
refused as a wrong password (401).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

checkout = Path(sys.argv[1])
sys.path.insert(0, str(checkout / "admin"))

from hardcode_admin.auth import Users  # noqa: E402
from hardcode_admin.web import create_app  # noqa: E402
from tests.helpers import FakeK8s, MemoryStore  # noqa: E402

SIGN_INS = 8

users = Users(MemoryStore())
users.add("root@example.com", "bootstrap-password")

app = create_app(
    FakeK8s(),
    users,
    {"image": "img:1"},
    cookie_secure="never",
    template_allowlist=str(checkout / "template" / "allowlist.txt"),
)

# Every thread waits here, so that the eight requests arrive together.
together = threading.Barrier(SIGN_INS)
answers: list[int] = []


def sign_in(index: int) -> None:
    """
    One sign-in with an address that has no account, which costs the same
    password hash as a wrong password.
    """
    client = app.test_client()
    together.wait()

    response = client.post("/api/login", json={"email": f"nobody{index}@example.com", "password": "not this one"})
    answers.append(response.status_code)


threads = [threading.Thread(target=sign_in, args=(index,)) for index in range(SIGN_INS)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()

print(f"answers: {sorted(answers)}")
sys.exit(0 if answers == [401] * SIGN_INS else 1)
