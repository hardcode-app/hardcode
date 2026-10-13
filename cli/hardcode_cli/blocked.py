"""
What the proxy and the DNS filter refused: squid's access log and CoreDNS's
log, aggregated by target.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

# A refused request in squid's access log:
#
#   1759900000.123    0 172.31.253.10 TCP_DENIED/403 3942 GET https://example.com/x - HIER_NONE/- text/html
#
# The result code may carry a suffix (TCP_DENIED_ABORTED, TCP_DENIED_REPLY);
# the groups are the client, the method and the URL or CONNECT target.
DENIED = re.compile(r"^\S+\s+\d+\s+(\S+)\s+TCP_DENIED[A-Z_]*/\d+\s+\d+\s+(\S+)\s+(\S+)")

# A name the DNS filter refused, as its log plugin writes it:
#
#   [INFO] 172.31.253.10:41234 BLOCKED A example.com.
#
# The groups are the client, the record type and the name.
BLOCKED = re.compile(r"\[INFO\]\s+(\S+)\s+BLOCKED\s+(\S+)\s+(\S+)")

# The time windows the app offers, in seconds.
SINCE = {"10m": 600, "30m": 1800, "2h": 7200, "24h": 86400}


@dataclass
class Blocked:
    """
    One refused target, and how many times it was refused.

    ``kind`` is "http" or "dns"; ``method`` is the HTTP method, or the DNS
    record type; ``url`` is ``scheme://host/path`` for requests the proxy
    could see into, and ``""`` otherwise.
    """

    kind: str
    method: str
    host: str
    url: str
    target: str
    client: str
    count: int = 1

    def as_dict(self) -> dict:
        return asdict(self)


def parse(squid_log: str, dns_log: str) -> list[Blocked]:
    """
    The refusals in both logs, most frequent first.
    """
    seen: "OrderedDict[str, Blocked]" = OrderedDict()

    def count(key: str, item: Blocked) -> None:
        if key in seen:
            seen[key].count += 1
        else:
            seen[key] = item

    for line in squid_log.splitlines():
        match = DENIED.match(line)
        if not match:
            continue

        client, method, target = match.groups()

        if method == "CONNECT":
            # A tunnel: the target is host:port, and nothing of the URL is known.
            host = target[:-4] if target.endswith(":443") else target
            url = ""
        else:
            parts = urlsplit(target)
            if parts.netloc:
                host = parts.netloc
                url = f"{parts.scheme}://{parts.netloc}{parts.path}"
            else:
                host, url = target, ""

        count(f"{method} {target}", Blocked("http", method, host, url, target, client))

    for line in dns_log.splitlines():
        match = BLOCKED.search(line)
        if not match:
            continue

        client, record, name = match.groups()
        host = name.rstrip(".")
        count(f"DNS {host}", Blocked("dns", record, host, "", host, client))

    return sorted(seen.values(), key=lambda item: -item.count)


def since_arg(since: str) -> str:
    """
    A ``docker compose logs --since`` value for one of the app's choices.
    """
    return since if since in SINCE else "30m"
