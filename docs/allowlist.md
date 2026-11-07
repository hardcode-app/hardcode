# The allowlist

A profile's **allowlist** decides what its sandbox can reach on the
internet. Anything that isn't on it is refused: web traffic by HardCode's
proxy, name lookups by its DNS server. Each line of the allowlist is an
**entry**, such as `github.com/your-org`.

Entries are **read-only** unless they say otherwise: the sandbox can
download from them (and clone, fetch and pull with git), but not upload.
An entry marked **`rw`** (read-write) also allows uploads: pushing,
publishing, calling an API with `POST`. An entry marked **`ssh`** allows an
SSH connection to a host.

Because the proxy looks inside HTTPS, an entry can be a single path on a
site: `github.com/your-org` allows your organisation's repositories, and
nothing else on GitHub.

## What a new profile allows

HardCode provides the entries a sandbox needs to work, in blocks:

- **every profile** gets VS Code's own hosts (its updates and the
  extension Marketplace), the Ubuntu, Debian and Alpine package mirrors,
  the common container registries (read-only: images can be pulled, never
  pushed), and GitHub's raw file downloads (`raw.githubusercontent.com`);
- **each language** you switch on adds its package registries, read-only:
  PyPI for Python, npm for JavaScript, and so on;
- **each coding agent** you switch on adds the hosts it talks to: the
  Claude Code CLI its API and sign-in hosts, the Codex CLI OpenAI's.

No block opens a git host: you add the places you work in. The full list
is [below](#what-hardcode-provides).

## See the allowlist

![The Allowlist page](images/app-allowlist.png)

**In the app**, open **Allowlist**. Your own entries are at the top,
under *Custom · this profile's own*. HardCode's entries follow, grouped
by where they come from (*VS Code · every profile*, *Python · comes with
the language*, and so on).
Each row shows the entry's type (host, subdomains, path, ssh) and access
(read-only, `rw`, `ssh`). Search and the filter buttons narrow the list:
*provided* and *your own* show just those.

![Searching the allowlist](images/app-allowlist-search.png)

**With the command:**

```sh
hardcode allowlist
```

## Allow something

**In the app:**

1. Open **Allowlist**.
2. Type the entry in **Entry**: `github.com/your-org`, say.
3. Choose its **Access**: *read-only (GET, clone/pull)*, *read-write
   (uploads, push)* or *ssh tunnel*.
4. Click **Add**.

**With the command:**

```sh
hardcode allow github.com/your-org              # read-only
hardcode allow github.com/your-org/app rw       # read-write
```

The change is saved at once. A running sandbox's proxy takes it five
seconds after your last change, without restarting: the app says *the
proxy takes it in 5 seconds*, and its header counts down until it has.
Many changes in a row are taken together, after the last, so a script can
allow a hundred entries without waiting for a hundred reloads. To have
the proxy take them at once, click **Apply now** on the app's Allowlist
page, or run `hardcode apply`. With the sandbox stopped, changes apply at
the next start.

Some entries you may want:

| To | Entry |
|---|---|
| clone and pull your repositories on GitHub | `github.com/your-name` |
| work on one GitHub repository ([how](github.md)) | `github:your-name/app rw` |
| use GitHub's API for your repositories | `api.github.com/repos/your-name` |
| use Claude Code's official plugin marketplace (a GitHub repository) | `github.com/anthropics/claude-code` |
| call an API that takes `POST` requests | `api.example.com rw` |
| reach a host and all its subdomains | `.example.com` |
| use SSH to GitHub, if you must | `ssh.github.com:443 ssh` |

Keep entries as narrow as you can: a path rather than a host, read-only
rather than `rw`, HTTPS rather than `ssh`.
[Staying safe](staying-safe.md#keep-the-allowlist-narrow) says why.

### A server on your own network

Some servers are on your own network: a GitLab, a package mirror or a
registry at home or at work. Their names point to an address inside your
network, and the proxy normally refuses those.

When you add such a name, HardCode asks first:

> gitlab.example.com resolves to 192.168.71.200, on your network. Let it
> reach that address?

**Yes** adds it to **On your network**, on the app's Allowlist page. From
then on, that name can reach that address, and nothing else on your
network opens. The command line asks the same question; `hardcode allow
--lan` answers yes in advance.

The name still needs its ordinary entry, such as
`gitlab.example.com:22222 ssh`. **On your network** only lets it reach
its address.

You can also manage the list with the command:

```sh
hardcode lan                                         # the list
hardcode lan add gitlab.example.com                  # with the address it has now
hardcode lan add gitlab.example.com 192.168.71.200   # or with this one
hardcode lan remove gitlab.example.com
```

If the name later points somewhere else, HardCode keeps the address you
approved. The app marks the row *now resolves to …*, with **Move** to
approve the new one, and the command prints a warning.

Only exact names can be added (no `.wildcard`), and only addresses inside
your network. Two kinds of address are never allowed, whatever the name:
your own computer, and the sandbox's own network. On Kubernetes, an
administrator switches this on first ([Kubernetes](kubernetes.md)).

## Stop allowing something

**In the app**, the switch at the start of a row turns an entry off and
on again; **✕** deletes one of the profile's own. **With the command:**

```sh
hardcode deny github.com/your-org               # off (the line stays, commented out)
hardcode allowlist enable github.com/your-org   # back on
hardcode allowlist remove github.com/your-org   # deleted
```

An entry HardCode provides can be switched off but not deleted: deleted,
it would come back with its block. Switched off, it stays off through
every update and through switching its language off and on again.

![Every profile at once](images/app-allowlist-all.png)

With more than one profile, the app's **all N profiles** button makes
adding, switching and deleting apply to every profile; the command's
`--all` does the same (`hardcode --all allow github.com/your-org`).

## When something is blocked

![Blocked traffic](images/app-blocked.png)

**Blocked traffic** in the app, or `hardcode blocked` in a terminal, lists
what the profile's proxy and DNS server refused, most frequent first:

```sh
hardcode blocked                  # the last 30 minutes
hardcode blocked --since 24h      # or 10m, 2h
```

What a row tells you:

| Row | Means |
|---|---|
| `CONNECT host:443` | The host isn't on the allowlist at all. |
| `GET` or `HEAD` with a URL | No entry allows that URL: the host is allowed, but not that path (or, for a plain `http://` address, the host isn't on the list). |
| `POST`, `PUT`, `PATCH` or `DELETE` to an allowed URL | The entry is read-only: uploading there needs `rw`. |
| `DNS A`, `DNS AAAA` | A lookup of a name that isn't allowed: it was answered "no such name" without leaving your computer. |
| *resolves to an internal address* | The name is allowed, but it points inside your network. **Allow on your network** asks whether to let it reach that address ([a server on your own network](#a-server-on-your-own-network)). |

In the app, **Allow host** and **Allow URL** fill in the allowlist form
with the row's host, or its URL's folder. Nothing is allowed until you
click **Add**. Before you do, read
[what to ask about a blocked row](staying-safe.md#read-blocked-traffic-before-you-allow-anything):
most refusals are telemetry, and better left blocked.

If something still fails after you allowed it, check the entry's kind: a
path entry (`host/path`) allows only that path.

## Edit the file by hand

The profile's own entries are in its `allowlist.txt`
([where it is](profiles.md#where-profiles-are)). It has three sections:

- **Custom**: your entries, one per line. `hardcode allow` and the app add
  here. It starts with commented-out examples for git hosting.
- **Switched off**: an `off <entry>` line (`off <host[:port]> ssh` for an
  SSH one) for each entry HardCode provides that this profile doesn't
  allow.
- **On your network**: a `lan <name> <address>` line for each name that
  may reach an address on your network.

After editing it, give the proxy the new list: **Apply now** on the app's
Allowlist page, or `hardcode apply`; until then the app's header says
*allowlist not applied*. A list with an invalid line is refused, and the
proxy keeps the rules it has: `hardcode apply` says why, the app marks the
line *invalid*, and `hardcode allowlist` flags it.

## Entry format

One entry per line; `#` starts a comment; blank lines and spacing are
free; case doesn't matter.

```
example.com                 # that host, any URL
.example.com                # that host and every subdomain
github.com/myuser           # only URLs under that path: /myuser and /myuser/…
github.com/myuser   rw      # … and uploads (POST, PUT, PATCH, DELETE; git push) too
github.com          ssh     # an SSH tunnel to github.com:22
ssh.github.com:443  ssh     # … or to another port
```

| Form | Matches | Rules |
|---|---|---|
| `host` | `http(s)://host[:port]/…` | a name with at least two labels; each label letters, digits and inner hyphens. A bare top-level name (`com`) is refused, and so is an address (`127.0.0.1`): the proxy matches names. One trailing dot is accepted. |
| `.host` | `host` and every subdomain | can't have a path |
| `host/prefix` | URLs whose path is `/prefix`, or starts with `/prefix/` or `/prefix?` | no `?`, `#` or `:` in the prefix, and no `.` or `..` segments |
| `… rw` | the same URLs, with every method | the only other flag is `ssh`; read-only is the default, not a flag |
| `host[:port] ssh` | an SSH tunnel to that host and port (22 by default) | no wildcard, no path; a colon must be followed by a port from 1 to 65535 |
| `off <entry>`, `off <host[:port]> ssh` | nothing: switches off an entry HardCode provides | |
| `lan <name> <address>` | lets that name reach that address on your network, and nothing else (it still needs its own entry) | an exact name; an address inside your network, never loopback, link-local or multicast; one address per name |

Anything else is an error: the command and the app refuse it, and the
proxy won't start with it rather than run with part of the list. Typed
entries are tidied before they are written (case, trailing dots and
slashes: `host/` is just `host`). A file whose last line has no newline is
read whole.

**An empty allowlist allows nothing.** Switch off or remove every entry
and the proxy refuses everything and the DNS server answers no name; it
doesn't keep the previous rules.

### Read-only and `rw`

Without `rw`, an entry allows `GET`, `HEAD` and `OPTIONS`, and the one
`POST` git uses to clone, fetch and pull (`…/git-upload-pack`). So:

- package registries and container registries can be downloaded from,
  but not published or pushed to;
- a git path can be cloned and pulled, but not pushed to (a push is a
  `POST` to `…/git-receive-pack`);
- an API that takes `POST` needs `rw`: the Claude API, the VS Code
  Marketplace's search, sign-in pages.

### Paths

A path entry is checked against the request as it is sent, and a server
may tidy a path it receives (`/your-org/../other` becomes `/other`). So
the proxy refuses, in every request whatever the entry, a path containing
a dot segment (`/../`, `/./`, also `%2e`-encoded or followed by an encoded
slash, or `..;`) or a backslash (plain or `%5c`). Names with dots in them,
such as `/.well-known/`, are fine, and so is an encoded slash on its own
(npm's `/@scope%2fname`).

### Addresses inside your network

The proxy refuses an allowed name that points inside your network: your
computer, your local network, or a cloud provider's metadata service.
So an allowed name can't be used to reach anything that isn't on the
internet. A name on **On your network** may reach the one address you
approved for it ([a server on your own network](#a-server-on-your-own-network)).

### SSH

SSH is passed through, not inspected, so an `ssh` entry is a **whole host,
read-write**: paths and read-only only exist over HTTPS. Inside the
sandbox, `ssh` and `git@…` addresses go through the proxy by themselves,
and the proxy refuses the tunnel unless the host has an `ssh` entry. An
`ssh` entry and an HTTPS entry for the same host are separate entries:
`hardcode deny`, `allowlist enable`, `disable` and `remove` take `ssh` at
the end to mean the SSH one (`hardcode deny github.com ssh`).

### DNS

The sandbox's only DNS server looks up exactly the names on the
allowlist, and their subdomains (a name's subdomains are served by the
same operator's name servers), including `.wildcard` entries and `ssh`
hosts. Every other name is answered "doesn't exist" by the server itself
and logged, so a lookup can't carry data out, and a name that isn't
allowed never leaves your computer.

## What HardCode provides

The entries HardCode provides are written down in one place in HardCode
itself, and added to each profile's own entries whenever the proxy is
given its rules. Entries are read-only unless marked `rw`.

**Every profile:**

| Block | Entries |
|---|---|
| VS Code | `update.code.visualstudio.com`, `vscode.download.prss.microsoft.com`, `download.visualstudio.microsoft.com`, `marketplace.visualstudio.com`, `marketplace.visualstudio.com/_apis/public/gallery` `rw` (extension search is a `POST`), `.gallery.vsassets.io`, `.gallerycdn.vsassets.io`, `.vscode-cdn.net` |
| OS package mirrors | `.archive.ubuntu.com`, `security.ubuntu.com`, `ports.ubuntu.com`, `deb.debian.org`, `security.debian.org`, `dl-cdn.alpinelinux.org` |
| Container registries | Docker Hub (`registry-1.docker.io`, `auth.docker.io`, `production.cloudfront.docker.com`, `docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com`), GitHub Container Registry (`ghcr.io`, `pkg-containers.githubusercontent.com`), Quay (`.quay.io`, `quayio-production-s3.s3.amazonaws.com`), Red Hat (`registry.access.redhat.com`, `registry.redhat.io`), Microsoft (`mcr.microsoft.com`, `.data.mcr.microsoft.com`), Google (`.gcr.io`, `.pkg.dev`), Kubernetes (`.registry.k8s.io`), AWS ECR Public (`public.ecr.aws`, `d5l0dvt14r5h8.cloudfront.net`) |
| GitHub raw files | `raw.githubusercontent.com` |

**With a language or tool switched on:**

| Language or tool | Entries |
|---|---|
| Python | `pypi.org`, `files.pythonhosted.org` |
| JavaScript / TypeScript | `registry.npmjs.org`, `registry.yarnpkg.com`, `repo.yarnpkg.com` |
| Java | `repo.maven.apache.org`, `repo1.maven.org`, `services.gradle.org`, `downloads.gradle.org`, `plugins.gradle.org`, `plugins-artifacts.gradle.org` |
| Android | `dl.google.com/android/repository` and `dl.google.com/dl/android/maven2` (Google's Android downloads, not the rest of `dl.google.com`), `maven.google.com`, the checksum list the SDK manager checks its downloads against (`f-droid.github.io/android-sdk-transparency-log`, `fdroid.gitlab.io/android-sdk-transparency-log`, `raw.githubusercontent.com/f-droid/android-sdk-transparency-log`), and the same entries as Java |
| C# / .NET | `api.nuget.org`, `globalcdn.nuget.org`, `builds.dotnet.microsoft.com` |
| Go | `proxy.golang.org`, `sum.golang.org`, `storage.googleapis.com/proxy-golang-org-prod` (where Go's downloads are stored) |
| Rust | `crates.io`, `index.crates.io`, `static.crates.io`, `static.rust-lang.org`, and for the CodeLLDB debugger's own downloads, that one project's releases on GitHub: `github.com/vadimcn/codelldb/releases/download` and `release-assets.githubusercontent.com/github-production-release-asset/49407251` |
| Ruby | `rubygems.org`, `index.rubygems.org` |
| Perl | `www.cpan.org`, `cpan.metacpan.org`, `fastapi.metacpan.org`, `cpanmetadb.plackperl.org` |
| Kubernetes | `dl.k8s.io` and `cdn.dl.k8s.io` (kubectl), `get.helm.sh` (Helm). A cluster's address is an entry of your own. |
| Claude Code CLI | `api.anthropic.com` `rw` (the API), `claude.ai` `rw` (sign-in), `platform.claude.com` `rw` (sign-in), `claude.com` (sign-in page), `downloads.claude.ai` (its list of models, and its official plugins) |
| Codex CLI | `auth.openai.com` `rw` (sign-in), `chatgpt.com/backend-api` `rw` (the model, with a ChatGPT plan), `api.openai.com` `rw` (the model, with an API key) |

C / C++, Build essentials and the Docker CLI open nothing. The coding
agents' telemetry and error-reporting hosts are never opened
([the security model](security.md#the-coding-agents) has more).

Switching a language or tool on or off (on the app's **Languages /
Tools** page, or `hardcode enable` and `disable`) brings its block or
takes it away, and a running proxy takes it a few seconds later, as it
does after any change. It doesn't touch `allowlist.txt`: an entry
you switched off stays off.

An entry of your own that a block also provides is allowed as long as
either is on. To allow more than a block does (`rw` where it is
read-only), add an entry of your own beside it: `hardcode allow pypi.org
rw`.

**Updates reach every profile.** A profile stores only its own entries and
the provided ones it switched off, so a newer HardCode's blocks apply to it
as they are: an entry added to a block arrives switched on, one dropped
from it goes, and one whose access changed follows. That happens at the
profile's next `up`, `start`, `restart`, `rebuild` or `apply`, which
print what changed (`allowlist: Rust: + newrustsite.com`). On Kubernetes
it happens at the operator's next pass after an upgrade.

## How edits are made

Edits change single lines and keep the file's comments, sections and
order. `hardcode allow` of an entry that is there but commented out
uncomments it in place (and updates its flag); a new entry goes at the end
of the Custom section. `allow` of a provided entry changes nothing, or
switches it back on if it was off; `remove` of one is refused. `allow
github.com` adds an HTTPS entry and leaves the template's commented
`#github.com ssh` example alone. `deny` of an entry that isn't on is an
error for one profile; with `--all`, each profile without it says so and
the others go ahead.

Every edit writes `allowlist.txt`, and the list the proxy is given, and
the command ends there. A running proxy watches that list: five seconds
after it last changed, the new rules are made from it and checked with
the proxy's own parser, and the proxy reloads with them in place, without
a restart and without dropping a connection. An edit made meanwhile
starts the five seconds again. The DNS server takes the new names within
two seconds of the proxy.

A list the proxy would refuse is never given to it: it keeps the rules it
has, and `hardcode --json ps` and the app's header say why (*allowlist
not applied*). `hardcode logs allowlist-watch squid` shows the proxy's
side; the next edit, or `hardcode apply`, tries again. `hardcode apply`
doesn't wait: it gives the proxy the allowlist at once, and fails with the
reason if it is refused.

An edit never waits for anything else under way, such as containers
restarting for another change: it writes only the list the proxy reads.

On Kubernetes, the operator gathers changes the same way, though there
the proxy and DNS server restart with them: it applies a profile's
changes once it has gone five seconds without one, and a change made
while they are still restarting for the last one waits for that restart
to end, then five seconds more. The profile is *Pending* meanwhile.

## How the rules are enforced

For the curious:
[`images/squid/allowlist-render.sh`](../images/squid/allowlist-render.sh)
turns the profile's list into these files before the proxy and the DNS
server start:

| File | Content |
|---|---|
| `squid/hosts.txt` | the hosts tunnels may go to and HTTPS may be opened for (`.host` for wildcards; hosts a wildcard already covers are dropped) |
| `squid/urls-ro.regex`, `squid/urls-rw.regex` | one `^https?://host(:[0-9]+)?/prefix(/|\?|$)` pattern per entry |
| `squid/ssh.conf` | `http_access allow CONNECT host port` and `on_unsupported_protocol tunnel` for each `ssh` entry |
| `Corefile` | one forwarding block listing every allowed name (none when the list is empty), and a catch-all that answers "doesn't exist" and logs |

[`squid.conf`](../images/squid/squid.conf) ties them together: tunnels
only to `hosts.txt`; HTTPS opened (`ssl_bump peek`, then `bump`) when the
server name is on the list, closed (`terminate`) otherwise; paths with dot
segments or backslashes refused first; `url_rw` for every method, `url_ro`
for the read methods and `git-upload-pack`; everything else denied, and
refused tunnels reset. `cli/hardcode_cli/allowlist.py` and the render
script are both run over `tests/allowlist-corpus.txt`, so they can't
disagree about what an entry means, and `tests/proxy.py` runs this
configuration for real.
