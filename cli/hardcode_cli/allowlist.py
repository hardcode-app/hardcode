"""
A profile's allowlist: the entries HardCode provides, and the profile's own.

The provided entries come in blocks (``langs.blocks``): every profile's (VS
Code's hosts, the OS package mirrors, the container registries, GitHub's
raw files) and those of the languages and tools the profile has on. They
aren't written into the profile. They are added whenever the proxy's rules
are made (``render``), so an entry a newer HardCode adds to a block reaches
every profile switched on, and one it drops leaves.

The profile's ``allowlist.txt`` holds what was decided for it: its own
entries (under Custom; one commented out is switched off), an ``off
<entry>`` line for each provided entry it switches off, which stays off
through updates and language switches, and a ``lan <name> <address>`` line
for each name it lets reach an address on your network (under On your
network; ``hardcode_cli.lan`` has the rules for the address). ``effective``
lists the entries, as the CLI, the app and the admin show them, and ``pins``
the names on your network.

An entry's rules are the same as in ``images/squid/allowlist-render.sh``,
which turns the rendered file into Squid and CoreDNS rules;
``tests/allowlist-corpus.txt`` is run through both to keep them in step.
The file stays hand-editable: comments and section headers are preserved,
and every edit touches single lines.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Iterable, Optional

from . import lan, langs

CUSTOM_HEADER = "# --- Custom ----------------------------------------------------------------"
CUSTOM_NOTE = "# Entries added by `hardcode allow` or the HardCode app land here."
OFF_HEADER = "# --- Switched off ----------------------------------------------------------"
OFF_NOTE = '# Entries HardCode provides that this profile doesn\'t allow, one "off <entry>" each.'
LAN_HEADER = "# --- On your network -------------------------------------------------------"
LAN_NOTE = '# Names that may reach an address on your network, one "lan <name> <address>" each.'
FLAGS = ("", "rw", "ssh")

# The note under each section header, written when the section is added.
NOTES = {CUSTOM_HEADER: CUSTOM_NOTE, OFF_HEADER: OFF_NOTE, LAN_HEADER: LAN_NOTE}

# The word that starts a line switching a provided entry off.
OFF = "off"

# The word that starts a line pinning a name to an address on your network.
LAN = "lan"

# One DNS label: letters, digits and inner hyphens. A host is two or more.
_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
_HOST = re.compile(rf"^{_LABEL}(\.{_LABEL})+$")

# A section header: "# --- Custom -----------".
_SECTION = re.compile(r"^#\s*---\s*(.+?)\s*-{3,}\s*$")

# How wide the section headers are, dashes included.
HEADER_WIDTH = len(CUSTOM_HEADER)

# Where an entry comes from, in a full list: one of the blocks' keys, or this.
CUSTOM = "custom"

# "github:OWNER/REPO", which allow and deny take for one GitHub repository:
# an owner (letters, digits, inner hyphens) and a repository name (letters,
# digits, ".", "-", "_"), as GitHub allows them. It is never written as it
# is: it stands for the repository's two entries (github_entries).
_GITHUB_REPOSITORY = re.compile(r"^github:([a-z0-9](?:[a-z0-9-]{0,38}))/([a-z0-9._-]{1,100})$", re.IGNORECASE)


class AllowlistError(ValueError):
    """
    An entry or an edit that can't be applied.
    """


@dataclass(frozen=True)
class Entry:
    """
    One allowlist entry: a line of ``allowlist.txt``, or one HardCode provides.

    For a line, ``line`` is its index in the file and ``section`` the header
    above it (both filled in by ``parse``); ``invalid`` marks an active line
    the proxy would refuse to start with, and ``off_line`` an ``off <entry>``
    line, which switches a provided entry off rather than allowing anything.
    ``lan_line`` marks a ``lan <name> <address>`` line, a pin: ``entry`` is
    the name and ``address`` the address, and it allows nothing by itself.

    In a full list (``effective``), ``source`` says where the entry comes
    from (a block's key, or ``custom``), ``provided`` whether HardCode
    provides it, ``section`` names its block, and ``language`` is the
    language or tool it comes with (``""`` for the others).
    """

    entry: str
    flag: str = ""
    comment: str = ""
    enabled: bool = True
    invalid: bool = False
    line: int = -1
    section: str = ""
    language: str = ""
    off_line: bool = False
    source: str = ""
    provided: bool = False
    lan_line: bool = False
    address: str = ""

    @property
    def kind(self) -> str:
        """
        ``ssh``, ``subdomains``, ``path`` or ``host``.
        """
        return kind_of(self.entry, self.flag)


def canonical(entry: str, flag: str = "") -> str:
    """
    The form an entry is written in.

    Lower case, no trailing dots on the host, and no trailing slash on the path
    (``host/`` is just the host). Typed input is normalised this way, while a
    line already in the file is held to the strict grammar.
    """
    entry = entry.strip().lower()

    # SSH entries (host:port) are taken as they are.
    if flag == "ssh" or ":" in entry:
        return entry

    host, _, path = entry.partition("/")
    host = host.rstrip(".")
    path = path.rstrip("/")

    return f"{host}/{path}" if path else host


def validate(entry: str, flag: str = "") -> Optional[str]:
    """
    Why *entry* with *flag* can't be allowlisted, or ``None`` when it can.
    """
    entry = entry.strip().lower()

    if not entry:
        return "enter a host, host/path or host:port"
    if flag not in FLAGS:
        return f"unknown flag '{flag}' (rw or ssh; read-only is the default)"
    if re.search(r"[\s#]", entry):
        return "entries can't contain spaces or #"

    if flag == "ssh":
        return _validate_ssh(entry)

    if ":" in entry:
        return "only ssh entries take a port"

    host, slash, path = entry.partition("/")

    # ".example.com" means example.com and every name under it.
    wildcard = host.startswith(".")
    if wildcard:
        host = host[1:]

    # One trailing dot is the fully qualified spelling of the same name; the
    # renderer accepts it too.
    if host.endswith("."):
        host = host[:-1]

    if not _HOST.match(host):
        return f"'{host}' is not a valid hostname"
    if _numeric_tld(host):
        return f"'{host}' is not a valid hostname (IP addresses can't be allowlisted; the proxy matches names)"
    if wildcard and slash:
        return "a .wildcard can't have a path"

    # The proxy matches a path entry as a URL prefix. A query, a fragment or a
    # dot segment in the prefix itself could never match a normalised URL, and
    # dot segments are exactly what the proxy refuses in requests.
    if re.search(r"[?#]", path):
        return "paths can't contain ? or #"
    if re.search(r"(^|/)\.\.?(/|$)", path):
        return "paths can't contain . or .. segments"

    return None


def _validate_ssh(entry: str) -> Optional[str]:
    """
    The rules for ``host[:port] ssh`` entries.
    """
    host, colon, port = entry.partition(":")

    if ":" in port or host.startswith(".") or "/" in host:
        return "ssh entries are host[:port], no wildcard or path"
    if not _HOST.match(host) or _numeric_tld(host):
        return f"'{host}' is not a valid hostname"
    if colon and not (port.isdigit() and 1 <= int(port) <= 65535):
        return "bad port"

    return None


def _numeric_tld(host: str) -> bool:
    """
    Whether the last label is all digits, as in ``127.0.0.1``: that is an
    address, not a name.
    """
    return host.rsplit(".", 1)[-1].isdigit()


def pin_problem(name: str, address: str, ranges: Optional[Iterable[str]] = None) -> Optional[str]:
    """
    Why *name* can't be pinned to *address* (a ``lan`` line) in a profile
    with the internal *ranges* (the default ones when ``None``); ``None``
    when it can.

    A pin is an exact name: no wildcard, path or port, and never an address.
    """
    name = name.strip().lower()

    if not name:
        return "name a host to let reach your network"
    if name.startswith(".") or "/" in name or ":" in name:
        return f"'{name}': only an exact name can reach your network, without a wildcard, path or port"
    if not _HOST.match(name) or _numeric_tld(name):
        return f"'{name}' is not a valid hostname"
    if not address.strip():
        return f"give its address too: {LAN} {name} <address>"

    return lan.address_problem(address, ranges)


def pin_name(entry: str, flag: str = "") -> Optional[str]:
    """
    The name an allowlist entry would be pinned by: its host, without a path
    or a port. ``None`` for a ``.wildcard`` (a pin is one exact name), for
    ``github:OWNER/REPO``, and for an entry that isn't valid.
    """
    entry = entry.strip().lower()
    if entry.startswith("github:") or entry.startswith("."):
        return None

    host = entry.partition(":")[0] if flag == "ssh" else canonical(entry).partition("/")[0]
    if not _HOST.match(host) or _numeric_tld(host):
        return None

    return host


def kind_of(entry: str, flag: str = "") -> str:
    """
    Classify an entry for display: ``ssh``, ``subdomains``, ``path`` or ``host``.
    """
    if flag == "ssh":
        return "ssh"
    if entry.startswith("."):
        return "subdomains"
    if "/" in entry and entry.partition("/")[2]:
        return "path"

    return "host"


def parse_line(line: str) -> Optional[Entry]:
    """
    The entry on one line, or ``None`` for blank lines and plain comments.

    A commented-out line counts as a disabled entry only if what follows the
    ``#`` is a valid entry; anything else is just a comment. ``off <entry>
    [ssh]`` is an ``off_line``, and ``lan <name> <address>`` a ``lan_line``,
    checked against the default internal ranges (``pins`` checks them
    against a profile's own).
    """
    body = line.lstrip()

    enabled = True
    if body.startswith("#"):
        enabled = False
        body = body[1:].lstrip()

    comment = ""
    if "#" in body:
        body, _, comment = body.partition("#")
        comment = comment.strip()

    parts = body.split()
    if not parts:
        return None

    # "off <entry> [ssh]": a provided entry switched off. Commented out, it
    # is just a comment, like any other line that isn't an entry.
    if parts[0] == OFF and enabled and len(parts) > 1:
        entry = parts[1].lower()
        flag = parts[2] if len(parts) > 2 else ""
        invalid = len(parts) > 3 or flag not in ("", "ssh") or validate(entry, flag) is not None

        return Entry(entry=entry, flag=flag, comment=comment, invalid=invalid, off_line=True)

    # "lan <name> <address>": a name let reach an address on your network.
    # Commented out, it is just a comment too.
    if parts[0] == LAN and enabled and len(parts) > 1:
        name = parts[1].lower()
        address = parts[2].lower() if len(parts) > 2 else ""
        invalid = len(parts) != 3 or pin_problem(name, address) is not None

        return Entry(entry=name, address=address, comment=comment, invalid=invalid, lan_line=True)

    entry = parts[0].lower()
    flag = parts[1] if len(parts) > 1 else ""
    invalid = len(parts) > 2 or validate(entry, flag) is not None

    # "# Allow the registry below" is prose, not a switched-off entry.
    if invalid and not enabled:
        return None

    return Entry(entry=entry, flag=flag, comment=comment, enabled=enabled, invalid=invalid)


def _lines(text: str) -> list[str]:
    """
    The file's lines, without the empty string after the final newline.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    return lines


def _join(lines: list[str]) -> str:
    """
    Lines back into file text, ending with a newline.
    """
    return "\n".join(lines) + "\n"


def parse(text: str) -> list[Entry]:
    """
    Every entry in *text*, ``off`` lines included, with its line number and
    its section.
    """
    entries: list[Entry] = []
    section = ""

    for number, line in enumerate(_lines(text)):
        heading = _SECTION.match(line)
        if heading:
            section = heading.group(1)
            continue

        parsed = parse_line(line)
        if parsed:
            entries.append(replace(parsed, line=number, section=section))

    return entries


def _matches(item: Entry, entry: str, ssh: Optional[bool]) -> bool:
    """
    Whether *item* is *entry*: the same canonical form, and with *ssh* the
    same kind. An SSH entry and an HTTPS entry for the same host are
    different things; ``None`` matches both.
    """
    if canonical(item.entry, item.flag) != canonical(entry):
        return False

    return ssh is None or (item.flag == "ssh") == ssh


def find(text: str, entry: str, ssh: Optional[bool] = None) -> list[Entry]:
    """
    The profile's own lines, enabled or not, that hold *entry* (its ``off``
    and ``lan`` lines aren't among them); *ssh* picks the SSH or the HTTPS
    kind, and ``None`` finds both.
    """
    return [item for item in parse(text) if not (item.off_line or item.lan_line) and _matches(item, entry, ssh)]


def off_lines(text: str, entry: str, ssh: bool = False) -> list[Entry]:
    """
    The ``off`` lines that switch the provided *entry* off.
    """
    return [item for item in parse(text) if item.off_line and not item.invalid and _matches(item, entry, ssh)]


def format_entry(entry: str, flag: str = "", comment: str = "") -> str:
    """
    One line in the file's column layout: the entry, the flag in column 29,
    and a comment in column 36.
    """
    line = entry
    if flag:
        line = f"{line:<27} {flag}"
    if comment:
        line = f"{line:<34} # {comment}"

    return line.rstrip()


def _section_end(lines: list[str], header: str) -> int:
    """
    Where a line goes at the end of the section *header* starts: before the
    next section header (and the blank lines above it), or at the end of the
    file. The section is added at the end of the file when it is missing.
    """
    if header not in lines:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend([header, NOTES[header]])
        return len(lines)

    start = lines.index(header)
    end = next((number for number in range(start + 1, len(lines)) if _SECTION.match(lines[number])), len(lines))

    while end > start + 1 and not lines[end - 1].strip():
        end -= 1

    return end


def add(text: str, entry: str, flag: str = "") -> str:
    """
    Make *entry* one of the profile's own, returning the new text.

    An existing line for it (enabled or not) is re-enabled and given the new
    flag in place. Otherwise the entry goes at the end of the Custom section,
    which is created at the end of the file when it is missing.
    """
    entry = canonical(entry, flag)

    problem = validate(entry, flag)
    if problem:
        raise AllowlistError(problem)

    lines = _lines(text)

    existing = find(text, entry, ssh=(flag == "ssh"))
    if existing:
        first = existing[0]
        lines[first.line] = format_entry(entry, flag, first.comment)
        return _join(lines)

    at = _section_end(lines, CUSTOM_HEADER)
    lines.insert(at, format_entry(entry, flag))

    return _join(lines)


def set_enabled(text: str, line: int, enabled: bool) -> str:
    """
    Comment line number *line* out (``enabled=False``) or back in.
    """
    lines = _lines(text)
    if not 0 <= line < len(lines):
        raise AllowlistError("no such line")

    parsed = parse_line(lines[line])
    if parsed is None:
        raise AllowlistError("not an entry")
    if parsed.enabled == enabled:
        return _join(lines)

    # Disabling prefixes one "#"; enabling removes exactly that, so a toggle
    # round-trips the line unchanged, indentation included.
    current = lines[line]
    if enabled:
        indent = current[: len(current) - len(current.lstrip())]
        lines[line] = indent + current.lstrip()[1:]
    else:
        lines[line] = "#" + current

    return _join(lines)


def remove(text: str, line: int) -> str:
    """
    Delete line number *line*.
    """
    lines = _lines(text)
    if not 0 <= line < len(lines):
        raise AllowlistError("no such line")

    del lines[line]
    return _join(lines)


def set_entry_enabled(text: str, entry: str, enabled: bool, ssh: bool = False) -> str:
    """
    Comment out, or back in, every one of the profile's own lines holding
    *entry*: its HTTPS form, or with *ssh* its SSH form. The text is
    unchanged when there is none.
    """
    for item in find(text, entry, ssh=ssh):
        text = set_enabled(text, item.line, enabled)

    return text


def remove_entry(text: str, entry: str, ssh: bool = False) -> str:
    """
    Delete every one of the profile's own lines holding *entry*: its HTTPS
    form, or with *ssh* its SSH form. The text is unchanged when there is
    none.
    """
    # From the bottom up, so the line numbers still to come stay valid.
    for item in sorted(find(text, entry, ssh=ssh), key=lambda item: item.line, reverse=True):
        text = remove(text, item.line)

    return text


# -- provided entries and the full list ----------------------------------------


def _key(item: Entry) -> tuple[str, bool]:
    """
    What an ``off`` line and a provided entry are matched on: the entry's
    canonical form, and whether it is the SSH kind.
    """
    return canonical(item.entry, item.flag), item.flag == "ssh"


def provided(enabled: Iterable[str]) -> list[Entry]:
    """
    Every entry HardCode provides to a profile with the *enabled* languages
    and tools, block by block, each switched on.
    """
    entries = []

    for block in langs.blocks(enabled):
        language = "" if block.every_profile else block.key

        for domain in block.domains:
            entry = Entry(
                entry=domain,
                flag="rw" if domain in block.rw else "",
                comment=langs.NOTES.get(domain, ""),
                section=block.name,
                language=language,
                source=block.key,
                provided=True,
            )
            entries.append(entry)

    return entries


def providers(enabled: Iterable[str], entry: str, ssh: bool = False) -> list[Entry]:
    """
    The provided entries that are *entry* (its HTTPS form, or with *ssh* its
    SSH form), among those a profile with the *enabled* languages has.
    """
    return [item for item in provided(enabled) if _matches(item, entry, ssh)]


def effective(text: str, enabled: Iterable[str]) -> list[Entry]:
    """
    Everything a profile's allowlist has: the profile's own, as *text* has
    them (``source`` custom), then the provided entries block by block, each
    switched on unless an ``off`` line in *text* switches it off.

    The profile's own come first, so that the app, the admin and ``hardcode
    allowlist`` show one just added at the top rather than below every block.
    An ``off`` line the proxy couldn't make sense of is listed with them, as
    invalid. The ``lan`` lines aren't entries: ``pins`` lists them.
    """
    lines = parse(text)
    switched_off = {_key(item) for item in lines if item.off_line and not item.invalid}

    entries = []

    for item in lines:
        if (item.off_line and not item.invalid) or item.lan_line:
            continue
        entries.append(replace(item, source=CUSTOM))

    for item in provided(enabled):
        entries.append(replace(item, enabled=_key(item) not in switched_off))

    return entries


def as_dicts(entries: list[Entry]) -> list[dict]:
    """
    A full list as ``--json``, the app and the admin show it: each entry with
    its ``id`` (its place in the list) and its kind.
    """
    return [{**asdict(item), "kind": item.kind, "id": number} for number, item in enumerate(entries)]


# -- what the proxy is given ---------------------------------------------------

RENDERED_HEADER = (
    "# What this profile's proxy allows, written by HardCode from allowlist.txt",
    "# and the profile's languages and tools whenever they change. Don't edit it:",
    "# edits go in allowlist.txt. `hardcode allowlist` lists the same.",
)


def heading(title: str) -> str:
    """
    A section header line, as wide as the others.
    """
    start = f"# --- {title} "
    return start + "-" * max(3, HEADER_WIDTH - len(start))


def render(text: str, enabled: Iterable[str]) -> str:
    """
    The file the proxy is given: the provided entries block by block, those
    switched off commented out, then the profile's own lines from *text*,
    under Custom, and its ``lan`` lines under On your network.

    An entry with the same access already written above isn't written
    again. A line of the profile's the proxy can't make sense of is passed
    on as it is, so that the proxy refuses to start rather than allow other
    than what the file says.
    """
    lines = list(RENDERED_HEADER)
    written: set[tuple[str, str]] = set()
    section = None

    for item in effective(text, enabled):
        if not item.provided:
            continue

        if item.section != section:
            section = item.section
            lines += ["", heading(section)]

        if not item.enabled:
            lines.append("#" + format_entry(item.entry, item.flag, "switched off in allowlist.txt"))
            continue

        if (canonical(item.entry, item.flag), item.flag) in written:
            continue

        written.add((canonical(item.entry, item.flag), item.flag))
        lines.append(format_entry(item.entry, item.flag, item.comment))

    lines += ["", heading("Custom")]
    source = _lines(text)

    for item in parse(text):
        if (item.off_line and not item.invalid) or item.lan_line:
            continue

        active = item.enabled and not item.invalid
        if active and (canonical(item.entry, item.flag), item.flag) in written:
            continue
        if active:
            written.add((canonical(item.entry, item.flag), item.flag))

        lines.append(source[item.line])

    pinned = [source[item.line] for item in parse(text) if item.lan_line]
    if pinned:
        lines += ["", heading("On your network"), *pinned]

    return _join(lines)


def _allowed(rendered: str) -> list[tuple[str, str]]:
    """
    Every entry a rendered file allows, and every name it lets reach your
    network, with its section: ``(section, entry or pin as written)``, in
    order.
    """
    allowed = []

    for item in parse(rendered):
        if not item.enabled or item.invalid or item.off_line:
            continue

        shown = f"{LAN} {item.entry} {item.address}" if item.lan_line else format_entry(item.entry, item.flag)
        allowed.append((item.section, shown))

    return allowed


def changes(before: str, after: str) -> list[str]:
    """
    What the rendered file *after* allows that *before* didn't, and the
    other way round, one line each: ``Rust: + newrustsite.com``, ``Rust: -
    oldsite.com``.
    """
    old = _allowed(before)
    new = _allowed(after)

    added = [f"{section}: + {entry}" for section, entry in new if (section, entry) not in old]
    removed = [f"{section}: - {entry}" for section, entry in old if (section, entry) not in new]

    return added + removed


# -- edits that know about provided entries ------------------------------------


def _shown(entry: str, flag: str = "") -> str:
    """
    An entry as the messages name it: ``github.com/me (rw)``.
    """
    return canonical(entry, flag) + (f" ({flag})" if flag else "")


def switch_off(text: str, entry: str, ssh: bool = False) -> str:
    """
    Switch the provided *entry* off: an ``off`` line for it under Switched
    off, which is added at the end of the file when it is missing. The text
    is unchanged when there is one already.
    """
    if off_lines(text, entry, ssh):
        return text

    lines = _lines(text)
    at = _section_end(lines, OFF_HEADER)
    lines.insert(at, f"{OFF} {canonical(entry, 'ssh' if ssh else '')}" + (" ssh" if ssh else ""))

    return _join(lines)


def switch_on(text: str, entry: str, ssh: bool = False) -> str:
    """
    Switch the provided *entry* back on: its ``off`` lines go.
    """
    # From the bottom up, so the line numbers still to come stay valid.
    for item in sorted(off_lines(text, entry, ssh), key=lambda item: item.line, reverse=True):
        text = remove(text, item.line)

    return text


def github_entries(entry: str) -> Optional[list[str]]:
    """
    The entries ``github:OWNER/REPO`` stands for: git's address for the
    repository (which ends in ``.git``), and GitHub's API for it. ``None``
    when *entry* isn't one.
    """
    if not entry.strip().lower().startswith("github:"):
        return None

    match = _GITHUB_REPOSITORY.match(entry.strip())
    if not match or match.group(2).strip(".") == "":
        raise AllowlistError("github: takes one repository, as OWNER/REPO in its address on GitHub")

    owner, repository = match.groups()
    if repository.lower().endswith(".git"):
        repository = repository[: -len(".git")]
    if not repository:
        raise AllowlistError("github: takes one repository, as OWNER/REPO in its address on GitHub")

    return [f"github.com/{owner}/{repository}.git", f"api.github.com/repos/{owner}/{repository}"]


def _for_each_github_entry(text: str, entry: str, flag: str, edit) -> Optional[tuple[str, str]]:
    """
    For ``github:OWNER/REPO``, *edit* applied to each of its entries in
    turn, each starting from the text the one before left: the last text,
    and their messages together. ``None`` when *entry* isn't one.

    *edit* takes the text and one entry, and returns the new text and a
    message, as ``allow`` and ``switch`` do.
    """
    entries = github_entries(entry)
    if entries is None:
        return None

    if flag == "ssh":
        raise AllowlistError("github: is git and GitHub's API over HTTPS: read-only, or rw to push")

    messages = []
    for one in entries:
        text, message = edit(text, one)
        messages.append(message)

    return text, "; ".join(messages)


def allow(text: str, entry: str, flag: str, enabled: Iterable[str]) -> tuple[str, str]:
    """
    Allow *entry* with *flag* in a profile with the *enabled* languages:
    the new text, and what to say about it.

    A provided entry that allows as much already (the same access, or rw
    where read-only is asked for) is switched back on if it was off, and
    otherwise there is nothing to do. For more than it allows (rw of a
    read-only one) the profile gets an entry of its own.

    ``github:OWNER/REPO`` allows that repository's two entries
    (``github_entries``), with *flag*.
    """
    repository = _for_each_github_entry(
        text,
        entry,
        flag,
        lambda now, one: allow(now, one, flag, enabled),
    )
    if repository is not None:
        return repository

    shown = _shown(entry, flag)
    ssh = flag == "ssh"

    covering = [item for item in providers(enabled, entry, ssh) if item.flag in (flag, "rw")]
    if covering:
        name = covering[0].section
        if off_lines(text, entry, ssh):
            return switch_on(text, entry, ssh), f"switched {shown} back on (it comes with {name})"

        return text, f"{shown} is already allowed (it comes with {name})"

    after = add(text, entry, flag)
    if after == text:
        return text, f"{shown} is already allowed"

    return after, f"allowed {shown}"


def switch(text: str, entry: str, on: bool, ssh: bool, enabled: Iterable[str]) -> tuple[str, str]:
    """
    Switch *entry* on or off in a profile with the *enabled* languages: the
    profile's own lines for it are commented back in or out, and a provided
    one loses or gets its ``off`` line. The new text, and what to say about it.

    ``github:OWNER/REPO`` switches that repository's two entries.
    """
    repository = _for_each_github_entry(
        text,
        entry,
        "ssh" if ssh else "",
        lambda now, one: switch(now, one, on, ssh, enabled),
    )
    if repository is not None:
        return repository

    shown = canonical(entry) + (" (ssh)" if ssh else "")
    offered = providers(enabled, entry, ssh)

    if not (find(text, entry, ssh=ssh) or offered or off_lines(text, entry, ssh)):
        return text, f"'{shown}' is not in the allowlist"

    after = set_entry_enabled(text, entry, on, ssh=ssh)
    if on:
        after = switch_on(after, entry, ssh)
    elif offered:
        after = switch_off(after, entry, ssh)

    state = "enabled" if on else "disabled"
    if after == text:
        return text, f"{shown} is already {state}"

    return after, f"{state} {shown}"


def delete(text: str, entry: str, ssh: bool, enabled: Iterable[str]) -> tuple[str, str]:
    """
    Delete the profile's own lines for *entry*, in a profile with the
    *enabled* languages: the new text, and what to say about it.

    A provided entry can't be deleted, since it comes with its block; it can
    be switched off, which the refusal says.
    """
    shown = canonical(entry) + (" (ssh)" if ssh else "")
    offered = providers(enabled, entry, ssh)

    after = remove_entry(text, entry, ssh=ssh)
    if after == text and offered:
        raise AllowlistError(f"{shown} comes with {offered[0].section} and can't be deleted: switch it off instead")
    if after == text:
        return text, f"'{shown}' is not in the allowlist"

    still = f"; it still comes with {offered[0].section}" if offered else ""
    return after, f"removed {shown}{still}"


# -- names on your network -----------------------------------------------------


@dataclass(frozen=True)
class Pin:
    """
    A name the profile lets reach an address on your network: a ``lan
    <name> <address>`` line of its allowlist, at index ``line``.

    ``problem`` says why the proxy would refuse to start with it (``""``
    when it wouldn't). A pin allows nothing by itself: the name's allowlist
    entries still say what it may do.
    """

    name: str
    address: str
    line: int
    comment: str = ""
    problem: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def pins(text: str, ranges: Optional[Iterable[str]] = None) -> list[Pin]:
    """
    Every ``lan`` line of *text*, checked against the internal *ranges* of
    the profile (the default ones when ``None``). A name pinned a second
    time is a problem, as it is for the proxy.
    """
    ranges = None if ranges is None else list(ranges)
    found: list[Pin] = []
    seen: set[str] = set()

    for item in parse(text):
        if not item.lan_line:
            continue

        problem = pin_problem(item.entry, item.address, ranges) or ""
        if item.invalid and not problem:
            problem = f"write it as: {LAN} <name> <address>"
        if not problem and item.entry in seen:
            problem = f"{item.entry} is already on your network, on another line"

        seen.add(item.entry)
        found.append(Pin(item.entry, item.address, item.line, item.comment, problem))

    return found


def add_pin(text: str, name: str, address: str, ranges: Optional[Iterable[str]] = None) -> tuple[str, str]:
    """
    Let *name* reach *address* on your network, in a profile with the
    internal *ranges*: the new text, and what to say about it.

    A name already pinned elsewhere is moved, in place, since a name is
    pinned to one address; the section is added at the end of the file
    when it is missing.
    """
    name = name.strip().lower()

    problem = pin_problem(name, address, ranges)
    if problem:
        raise AllowlistError(problem)

    address = lan.canonical_address(address)
    line = f"{LAN} {name} {address}"
    lines = _lines(text)

    existing = [pin for pin in pins(text, ranges) if pin.name == name]
    if existing and existing[0].address == address and len(existing) == 1:
        return text, f"{name} already reaches {address} on your network"

    if existing:
        # The first line takes the new address; any other goes.
        lines[existing[0].line] = line
        for pin in reversed(existing[1:]):
            del lines[pin.line]
        return _join(lines), f"moved {name} on your network: {existing[0].address} -> {address}"

    at = _section_end(lines, LAN_HEADER)
    lines.insert(at, line)

    return _join(lines), f"{name} may reach {address} on your network"


def remove_pin(text: str, name: str) -> tuple[str, str]:
    """
    Stop letting *name* reach your network: its ``lan`` lines go. The new
    text, and what to say about it.
    """
    name = name.strip().lower()
    found = [item for item in parse(text) if item.lan_line and item.entry == name]
    if not found:
        return text, f"{name} is not on your network"

    # From the bottom up, so the line numbers still to come stay valid.
    for item in sorted(found, key=lambda item: item.line, reverse=True):
        text = remove(text, item.line)

    return text, f"{name} no longer reaches your network"


def ignore_pins(text: str, reasons: dict[str, str]) -> str:
    """
    *text* with the ``lan`` lines of the names in *reasons* commented out,
    each saying why, so that the proxy doesn't get them (on a cluster that
    doesn't take pins, or for a pin it refuses).
    """
    lines = _lines(text)

    for item in parse(text):
        if item.lan_line and item.entry in reasons:
            lines[item.line] = f"#{lines[item.line]}  # ignored: {reasons[item.entry]}"

    return _join(lines)


def entry_ports(entries: Iterable[Entry], name: str) -> list[int]:
    """
    The ports the enabled, valid *entries* let *name* use, in order: an SSH
    entry's own port, and 80 and 443 for a host or path entry for it or a
    ``.wildcard`` above it. None when no entry lets it through, in which
    case a pin of it allows nothing.
    """
    ports: set[int] = set()

    for item in entries:
        if not item.enabled or item.invalid or item.off_line or item.lan_line:
            continue

        if item.flag == "ssh":
            host, _, port = item.entry.partition(":")
            if host == name:
                ports.add(int(port or 22))
            continue

        host = item.entry.partition("/")[0]
        if host == name or (host.startswith(".") and (name == host[1:] or name.endswith(host))):
            ports.update((80, 443))

    return sorted(ports)
