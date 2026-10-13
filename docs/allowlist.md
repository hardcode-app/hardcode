# The allowlist

A profile's allowlist is its egress policy. It has two parts: the entries
**HardCode provides**, in blocks (those every profile has, and those of the
languages and tools the profile has on), and the profile's **own**, in its
`allowlist.txt`, which also records the provided entries it switches off.
`hardcode allowlist` lists both, as the app's and the admin's *Allowlist*
do.

The provided entries aren't copied into the profile: the `hardcode` command
(on Kubernetes, the operator) adds them whenever it gives the proxy its
rules, into `proxy-allowlist.txt` beside `allowlist.txt` (on Kubernetes, the
profile's allowlist ConfigMap). The `allowlist` one-shot container (the same
`allowlist-render` script the operator runs as an init container) turns that
into Squid rule files and a CoreDNS Corefile before the proxy and the DNS
start. Because Squid decrypts HTTPS with the profile's own CA, rules apply
to **full URLs and HTTP methods**, not just host names.

## Entry format

One entry per line; `#` starts a comment; blank lines and column alignment
are free. Case doesn't matter.

```
example.com                 # that host, any URL
.example.com                # that host and every subdomain
github.com/myuser           # only URLs under that path: /myuser and /myuser/…
github.com/myuser   rw      # … and uploads (POST, PUT, PATCH, DELETE; git push) are allowed too
github.com          ssh     # an SSH tunnel to github.com:22
ssh.github.com:443  ssh     # … or to another port
```

| Form | Matches | Notes |
|---|---|---|
| `host` | `http(s)://host[:port]/…` | a name with at least two labels: a bare TLD (`com`) is refused, and so is an address (`127.0.0.1`): the proxy matches names |
| `.host` | `host` and every subdomain | can't carry a path |
| `host/prefix` | URLs whose path is `/prefix` or starts with `/prefix/` (or `/prefix?`) | `?` and `#` aren't allowed in the prefix |
| `… rw` | the same URLs, every method | the only other flag is `ssh`; read-only is the default, not a flag |
| `host[:port] ssh` | a CONNECT tunnel to that host and port (22 by default) | no wildcard, no path; a colon needs a port |

Anything else is a validation error: the command and both UIs refuse it, and
the renderer fails loudly rather than start a proxy with a partial list. The
two implementations (`cli/hardcode_cli/allowlist.py` and
`images/squid/allowlist-render.sh`) are run over the same corpus,
`tests/allowlist-corpus.txt`, so they can't drift apart. A file whose last
line has no newline is read whole.

**An empty list allows nothing.** Disable or remove the last entry and the
renderer produces a deny-all policy (no host matches, every name is
NXDOMAIN) rather than refusing to run and leaving the previous rules in
place.

## Read-only unless `rw`

Without `rw` an entry allows `GET`, `HEAD`, `OPTIONS`, and the one `POST` git
uses to clone, fetch and pull (`…/git-upload-pack`). So:

- package indexes and container registries can be downloaded from but not
  published or pushed to;
- a git namespace can be cloned but not pushed to (`git-receive-pack` is a
  different URL and a `POST`);
- an API that takes `POST` needs `rw`: the Claude API, the VS Code
  Marketplace query, sign-in flows.

`rw` entries are where data can leave. Keep them to the few hosts that need
it, and prefer a path (`github.com/ourcompany rw`) over a host.

Path entries are enforced on the request as sent, and an origin may
normalise what it receives (`/ourcompany/../other` → `/other`). So the proxy
refuses, in every request, a dot segment: one or two dots, plain or
`%2e`-encoded, between separators, plain or encoded (`/../`, `/%2e%2e/`,
`..%2f`, `..;`). A path containing one gets a 403 whatever the entry. Dotted
names such as `/.well-known/` are fine, and so is an encoded slash on its
own (npm's `/@scope%2fname`). Entries themselves can't contain `.` or `..`
segments either.

Once a request passes the name and URL checks, Squid resolves the name and
refuses it if it points at loopback, link-local, private or shared address
space (the profile's `EGRESS_INTERNAL_RANGES`), so an allowlisted name can't
be used to reach the host, the LAN or a cloud metadata service.

## SSH

SSH is tunnelled, not inspected, so an `ssh` entry is **whole-host and
read-write**; path rules only exist over HTTPS. Prefer HTTPS with a namespace
where you can. Inside the VS Code container, `ssh` and `git@…` URLs go through the proxy
automatically (`/etc/ssh/ssh_config.d/10-hardcode-proxy.conf` sets a
`ProxyCommand`); the tunnel is refused unless the host has an `ssh` entry.

## DNS

CoreDNS resolves, upstream, exactly the hosts that appear in the file (and
their subdomains, since a host's nameservers belong to the same operator)
plus `.wildcard` entries and `ssh` hosts; every other name gets NXDOMAIN
locally and is logged as `BLOCKED`. The VS Code container has no other resolver and no
route to one, so DNS can't carry data out and a name that isn't allowlisted
never even leaves the host.

## What HardCode provides

The provided entries are written down in one place, the catalogue in
`cli/hardcode_cli/langs.py`, in blocks:

- **Every profile**: *VS Code* (server self-update, the Marketplace,
  extension dependencies), *OS package mirrors* (Ubuntu, Debian, Alpine),
  *Container registries*, read-only (Docker Hub, GHCR, Quay, MCR, gcr.io /
  Artifact Registry, registry.k8s.io, ECR Public, Red Hat), and *GitHub raw
  files* (`raw.githubusercontent.com`).
- **A block per language** the profile is built around, with that
  language's package registries: PyPI for Python, npm for JavaScript /
  TypeScript, Maven Central and Gradle for Java, NuGet for .NET, the Go
  proxy, crates.io and, for Rust's debugger CodeLLDB, that one project's
  release downloads on GitHub, RubyGems, CPAN.
- **A block per coding agent**: the Claude Code CLI's (`api.anthropic.com`,
  `claude.ai` and `platform.claude.com` `rw` for the API and sign-in,
  `claude.com` and `downloads.claude.ai` read-only) and the Codex CLI's
  (`auth.openai.com`, `chatgpt.com/backend-api` and `api.openai.com`, all
  `rw`). Both are on by default; switched off, an agent's hosts close and
  its extension goes. Their telemetry and error-reporting hosts are never
  opened ([what the agents can send](security.md)).

No block opens a git host: the profile adds the namespaces it works in.

Switching a language or tool on or off (`hardcode enable python`, the app's
or the admin's *Languages / Tools*) brings its block or takes it away, and
the proxy reloads at once.

**A provided entry can be switched off**, not deleted: `hardcode allowlist
disable crates.io` (or `hardcode deny crates.io`, or its switch in the
*Allowlist*) writes `off crates.io` into the profile's `allowlist.txt`. It
stays off through updates and through switching its language off and on
again; `hardcode allowlist enable crates.io`, `hardcode allow crates.io` or
its switch takes the line away.

**Updates reach every profile.** Since a profile stores only its own
entries and what it switched off, a newer HardCode's blocks apply to it as
they are: an entry added to a block arrives switched on, one dropped from
it goes, and an entry whose access changed (read-only to `rw`) follows. On
Compose that happens at the profile's next `up`, `start`, `restart`,
`rebuild` or `reload`, which print what changed (`allowlist: Rust: +
newrustsite.com`); on Kubernetes, at the operator's next pass after an
upgrade (a minute at most), which restarts the profile's proxy and logs the
same lines.

## The profile's own

The template's `allowlist.txt` (and the chart's, which is the same file) has
two sections:

- **Custom**: where `hardcode allow` and the UIs add. It starts with
  commented examples for git hosting (`#github.com/myuser rw`,
  `#ssh.github.com:443 ssh`): add the namespaces you work in, **never the
  whole host**. `github.com` would let anything in the sandbox read, and with
  credentials write, every public repository.
- **Switched off**: an `off <entry>` line (`off <host[:port]> ssh` for an SSH
  one) for each provided entry the profile switches off.

An entry of your own that a block also provides is allowed as long as
either is on. More access than a block gives (`hardcode allow pypi.org rw`
for a read-only one) is an entry of your own beside it.

## Editing

```sh
hardcode allowlist                         # every entry: provided, by block, then your own; on or off
hardcode allow github.com/ourcompany rw    # adds (or re-enables and re-flags) and reloads the proxy
hardcode deny github.com/ourcompany        # comments the line out (a provided entry: switches it off)
hardcode allowlist enable|disable|remove <entry> [ssh]
hardcode deny github.com ssh               # the host's SSH entry; without "ssh", its HTTPS entry
hardcode reload                            # after editing the file by hand
hardcode --all allow …                     # every profile
```

Edits touch single lines and keep comments, sections and order, so the file
stays readable. `allow` of an entry that is already there, commented out,
uncomments it in place (and updates its flag); new entries go to the end of
the **Custom** section. `allow` of a provided entry changes nothing, or
switches it back on if it was off; `remove` of one is refused, since it
would come back with its block: switch it off instead. An SSH entry and an HTTPS entry for the same host
are different things: `allow github.com` leaves the commented
`#github.com ssh` example alone and adds an HTTPS line, and `deny`,
`enable`, `disable` and `remove` take `ssh` to address the SSH one. What
you type is normalised (case, trailing dots and slashes) before it is
written. The desktop app's *Allowlist* view toggles, adds and
deletes entries the same way, and *Reload proxy* reloads; the admin UI
applies within seconds through the operator.

Every edit writes `allowlist.txt`, then `proxy-allowlist.txt` from it.
When the profile is running, a reload re-renders the files and recreates
`squid` (`docker compose rm --stop --force allowlist squid`, then `up -d
allowlist squid`: any leftover of a recreate that was cut short goes too),
then waits for it to be healthy; CoreDNS watches its Corefile and reloads on its own
within 10 s. When the profile isn't running the change simply applies at the
next start.

A running proxy keeps the rules it last loaded, whatever the file says
since, so `hardcode` records what it saw the proxy load
(`proxy-allowlist.applied`: a digest, written after a healthy start or
reload, never before). A start or rebuild that finds the proxy running
with anything else reloads it: an edit whose reload didn't come back
healthy, or a start that failed after writing the file, is put right by the
next one that succeeds.

## Seeing what is blocked

```sh
hardcode blocked --since 2h     # aggregated by target, most frequent first
hardcode logs -f squid   | grep TCP_DENIED
hardcode logs -f coredns | grep BLOCKED
```

The app's *Blocked traffic* view and the admin UI's *Blocked traffic* tab show
the same data with *Allow host* / *Allow URL* shortcuts. A `403` on a `POST`
or `PUT` to an allowlisted host means the entry needs `rw`; a `CONNECT` line
means the host isn't allowlisted at all (the tunnel was reset, so a client
that isn't speaking TLS, `ssh` say, fails at once instead of waiting); a DNS
line means nothing was even attempted over HTTP.

## How the rules are rendered

For the curious, [`images/squid/allowlist-render.sh`](../images/squid/allowlist-render.sh)
writes:

| File | Content |
|---|---|
| `squid/hosts.txt` | `dstdomain` list used for `CONNECT` and for the TLS server name (`.host` for wildcards; exact hosts already covered by a wildcard are dropped) |
| `squid/urls-ro.regex`, `squid/urls-rw.regex` | one `^https?://host(:[0-9]+)?/prefix(/|\?|$)` regex per entry |
| `squid/ssh.conf` | `http_access allow CONNECT host port` + `on_unsupported_protocol tunnel` per `ssh` entry |
| `Corefile` | one forwarding zone block listing every allowlisted name (none when the list is empty), and a catch-all that answers NXDOMAIN and logs |

[`squid.conf`](../images/squid/squid.conf) ties them together: tunnels only to
`hosts.txt`, `ssl_bump peek` then `bump` when the server name is on the list,
`terminate` otherwise; the `traversal` ACL refused first; `url_rw` for every
method, `url_ro` for the read methods and `git-upload-pack`; `deny all`;
refused tunnels are reset. `tests/proxy.py` runs this configuration for real.
