# The desktop app

The HardCode app is a window over the `hardcode` command for Linux, macOS and
Windows: a [Tauri 2](https://tauri.app) shell (Rust) around a plain HTML/JS
page. Everything it does is one CLI invocation (`hardcode --json …`), streamed
to a console drawer when it takes a while, so the app has no logic of its
own and the CLI's tests cover its behaviour.

Get it from the Releases page (AppImage/deb/rpm, dmg, msi/exe; the `hardcode`
binary is bundled as a sidecar) or [build it](#building) from a checkout. It
needs nothing else: the profiles are in HardCode's home
(`~/.config/hardcode` on Linux; [where](profiles.md#where-profiles-are)),
and `hardcode-app --config DIR` (or `$HARDCODE_CONFIG`) uses another folder,
which the app passes on to the CLI. The current profile is remembered in the
app's settings file (`~/.config/io.github.hardcode-app.hardcode/settings.json` on Linux,
`~/Library/Application Support/io.github.hardcode-app.hardcode/` on macOS,
`%APPDATA%\io.github.hardcode-app.hardcode\` on Windows), and the app opens on it the next
time (without one, on `default`, else the first profile).

On first start, with no profile yet, the app asks for one: the new-profile
dialog, as **Your first profile**, named `default`, with the tools ticked
and no language. Choose what it is built around and *Create*; it can't be
dismissed, since there is nothing to show without a profile. The same
happens after deleting the last profile.

## The window

The sidebar has the app's version under its name (a development build's
with the commit it was built from: `0.1.0 (dev, 2c419ae)`), the
**profile switcher** (with the port each profile is
published on), a **＋** for new profiles, the views (Overview, Languages /
Tools, Extensions, Allowlist, Blocked traffic, Port forwards, Mounts), and
where the profiles are, with *Show folder* to open it in the file manager
(to edit a profile's files by hand). Languages / Tools comes right
after the Overview because a profile is built around its languages and
tools: each one opens the allowlist entries its package registries need,
brings its VS Code extensions, and puts its toolchain or CLI in the VS Code
image. The top bar shows the profile's state and the **Start** / **Stop** /
**Open VS Code** buttons, which are always available.
The **console drawer** at the bottom opens by itself while a long command
(start, rebuild, reset, delete) streams its output; **Stop** ends the
running command and everything it started (the `docker compose` build or
start underneath it), as does the 15-minute timeout on any CLI call.

### Overview

![Overview](images/app-overview.png)

Two cards side by side, the profile and VS Code; the languages and tools
across the whole width below them; then four counts in a row.

The containers, in order of importance: **vscode**, **docker**, then the
plumbing (squid, coredns, gateway). A dot says how each is: green, running
and healthy; amber, starting, restarting or unhealthy; red, stopped while the
rest of the sandbox runs; grey, stopped or switched off. A row only has words
when something needs a look (*starting*, *Exited (1) …*); the tooltip has the
full status, and a click opens the container's logs. The **docker** row has
the on/off switch for the rootless Docker daemon beside the VS Code
container, showing the profile's real setting (it comes from `hardcode show`,
and the headless smoke test checks that it does): off, no daemon runs for the
profile and `docker` has nothing to talk to (`hardcode docker on|off`;
applied at once, the VS Code container is recreated). Under the list:
*Restart*, *Rebuild images*, *Reset…* (`down -v`: removes containers and
volumes, keeps the profile) and *Delete profile…* (any profile, `default`
and the last one included: then the app asks for a new first one); the
port VS Code is on, with *change* beside it (a dialog: `hardcode port
<number>`, refused for a port another profile uses; a running
profile's gateway moves at once); the VS Code URL, its token hidden (*Show* reveals it; *Copy* and *Open VS
Code* use the whole URL), and *New token…*, which gives VS Code a new token
after asking (`hardcode token rotate`: every copy of the old one stops
working, and VS Code restarts); the profile's **languages and tools**, with what they open and
bring, a *Choose languages and tools…* button to the Languages / Tools
view, and the warning with *Rebuild image* when the image was built with
other ones; and
counts for the VS Code extensions (every VS Code container's, the languages', your
own), the allowlist, forwards and mounts.

The URL is read from the running VS Code container once it is up (`hardcode --json
open --no-browser`), whichever view is showing. *Open VS Code*, here and in
the header, and *Copy* are enabled once the URL is known; until then the box
and the buttons' tooltip say why there is no token yet (the VS Code container may still
be starting), and the next status refresh asks again. The URL's address
is where the gateway really publishes VS Code.

The status pill in the header says *running*, *stopped*, *not created*,
*docker not found* (no `docker` command), *docker not running* (the daemon
doesn't answer), or *status unknown* when Compose or the CLI failed; its
tooltip then has the reason, and the Overview shows it instead of *Not
created yet*. While the VS Code container installs extensions in the
background it adds how far it has got, *running (5) · extensions 3/12*
(`extensions` in `hardcode --json ps`, from the status file the
container's extension sync writes). The status is read every 15 seconds;
while extensions are installing, and for a few seconds after a change
that adds or removes some, every 3 seconds, until they are in place.

A change that fails (a language or allowlist switch, the Docker daemon
switch, a removal) shows the error, and the page reads the profile again, so
every switch shows what is really set. Switching profiles drops any answer
still on its way for the one shown before. After *Start* or *Rebuild
images* the profile is read again, so the Overview and Languages / Tools
show what the image was built with.

Click a container to see its logs:

![A container's logs](images/app-logs.png)

The dialog shows the last 300 lines (`hardcode --json logs --tail 300
<service>`) and reads them again every few seconds while it is open,
following the end unless you scroll up. *Refresh* reads them at once;
*Close*, a click outside or Escape closes it.

![The console drawer while a profile starts](images/app-console.png)

### Languages / Tools

![Languages / Tools](images/app-langs.png)

What the profile is built around: a card per language, then per tool
(build essentials, the Docker CLI, the Claude Code CLI, the Codex CLI with
its VS Code extension), each with its switch, its toolchain or CLI, the
allowlist entries it **opens** (an entry switched off in the allowlist is
struck through) and the VS Code extensions it **brings** (one too long for
the card is cut short with "…", and its tooltip has it whole; `hardcode --json
show`, the same catalogue `hardcode languages` lists).

A switch (`hardcode enable|disable --no-build <lang>`, which writes
`INSTALL_<LANG>` to the profile's `.env`) changes three things, at
different times, as the banner says. The allowlist entries open or close
at once: the proxy is given the block or loses it, and a running proxy
reloads (`allowlist.txt` isn't touched, and an entry switched off there
stays off). The extensions follow within seconds, with no
restart: the CLI rewrites the profile's extension list, and a running VS
Code container installs or removes them in the background while VS Code
runs (an open window may need a reload to show one). The toolchain or CLI
comes with the next image rebuild. The toast says what happened, e.g.
*Codex on · proxy reloaded · extensions installing*, or *applies when the
sandbox starts* when it isn't running.

When the image was built with other languages or tools than the
profile's, a badge on the card says *not in the image yet* or *still in
the image*, and a warning names the difference with a **Rebuild image**
button (`hardcode rebuild`: the image is built with the profile's
languages and tools, only the steps after the first changed one running
again, and the VS Code container recreated, streamed to the console).
Without the warning, *Rebuild image* is in the toolbar. Before the first
start there is no image yet: it is built with the chosen ones at the first
*Start*. With the *all profiles* scope, the switches and the rebuild apply
to every profile.

### Extensions

![Extensions](images/app-extensions.png)

Every VS Code extension the profile's VS Code container gets, grouped by
what brings it: **every VS Code container** (YAML), each language or tool
that is on (Claude Code's comes with the Claude Code CLI, Codex's with the
Codex CLI), and **your extensions**, the profile's own
(`VSCODE_EXTENSIONS` in its `.env`). A line names the languages and tools
that are off, whose extensions come with them from the Languages / Tools
view. Only extensions whose publisher has a verified domain are installed.

The extensions aren't in the image. A running VS Code container installs
and removes them in the background, within seconds of a change, while VS
Code runs; a stopped one does it as it starts. When the view opens, the
container is asked how far it has got (`hardcode --json extensions`), and
asked again every 3 seconds while it is installing. Each extension then
shows its state: *installing…* while it is on its way, *refused: publisher
has no verified domain*, or *couldn't install; tried again shortly*;
nothing once it is in place. While extensions are installing, a line at the
top says *Installing in the background: 3 of 12 in place*, and that VS
Code picks them up as they arrive (reload the window if one doesn't
appear); once done, the toolbar says *all 12 installed* (with how many were
refused, or are to be tried again). With the sandbox stopped there is
nothing to ask, and nothing is shown.

The form adds one of your own by its id, `publisher.name` as the
Marketplace shows it (`hardcode extension add <id>`); ✕ removes it
(`hardcode extension remove <id>`). Neither restarts anything: the toast
says *installing in the background* or *removing in the background*, or
*applies when the sandbox starts*. An id the CLI refuses is explained under
the form; one a language or every VS Code container already brings is not
added twice. One of your own that a language also brings is marked
*already comes with* that language. The scope chips apply additions and
removals to every profile.

### Allowlist

![Allowlist](images/app-allowlist.png)

Every entry of the profile's allowlist (`hardcode --json allowlist`): first
those HardCode provides, block by block (*VS Code · every profile*, …, then
*Python · comes with the language*, …), then the profile's own from
`allowlist.txt` (*Custom · this profile's own*), each with its kind (host,
subdomains, path, ssh), access (read-only, `rw`, `ssh`) and note. The toggle
switches an entry off or back on; ✕ deletes one of the profile's own; the
form adds a validated entry to the Custom section. Search and the chips
filter the table; *provided* and *your own* show just those.

A provided entry has no ✕: it would come back with its block. One a
language or tool provides carries its name as a badge; every profile's are
under their section's heading. Switched off, it stays off
(an `off <entry>` line in `allowlist.txt`) through updates and through
switching its language off and on, while an entry a newer HardCode adds to
a block arrives switched on. A language's section heading, and the button
where its entries' ✕ would be, open Languages / Tools.

![Searching the allowlist](images/app-allowlist-search.png)

Every edit is written to the file and applied to the running proxy at once
(the CLI reloads it; the toast says *proxy reloaded*, or *applies when the
sandbox starts* if it isn't running). **Reload proxy** gives the proxy the
allowlist again after you edited `allowlist.txt` by hand, with the entries
this HardCode provides, as every start does. With more than one profile, the
**scope** chips switch between this profile and *all N profiles*: adding,
toggling and deleting then changes the same entry everywhere.

![The all-profiles scope](images/app-allowlist-all.png)

### Blocked traffic

![Blocked traffic](images/app-blocked.png)

What this profile's Squid and CoreDNS refused in the last 10 minutes to 24
hours, aggregated by target: the HTTP method or DNS record type, the URL or
name, how often, and from which address inside the sandbox. **Allow host** and
**Allow URL** pre-fill the allowlist form with the host or the URL's
directory; nothing is allowed until you add and apply it. A `POST`/`PUT` to an
allowlisted host means the entry needs `rw`; a `CONNECT` means the host isn't
allowlisted.

### Port forwards

![Port forwards](images/app-forwards.png)

VS Code's own forward (from `VSCODE_PORT` in `.env`) and any others:
`localhost:<port>` → a server in the VS Code container (which must listen on `0.0.0.0`)
or a port a container published with `docker run -p`. Host ports belong to
one profile; the gateway is recreated at once if the profile is running.

### Mounts

![Mounts](images/app-mounts.png)

Host folders mounted into the VS Code container and, at the same path, into the Docker
daemon's container, so `docker run -v` works for them. *Browse…* opens a
folder picker; the remote path may be relative to the home directory;
*read-only* adds `:ro`. The callout is there for a reason: a writable mount
is a way out of the sandbox ([security model](security.md#not-covered)).
The scope chips apply a mount to every profile.

### New profile

![New profile](images/app-new-profile.png)

A name, what to start from, and the languages and tools to build it
around, the first thing a profile is about. The source is the template or
a copy of any profile (its allowlist, settings and mounts; a free port and
subnet are picked), and the boxes start ticked as the source has them: a
copy of a profile with its languages (`hardcode --json -p <profile>
languages` for one not shown), the template with the tools and no
language. The line under them adds up what the ticked ones open and bring.
*Create* runs `hardcode profile add <name> [--from <profile>]
--lang=<keys>`: the new profile has exactly those languages on, and with
them their allowlist entries. It is selected and opens on its Languages /
Tools view; its image is built with them at the first *Start*.

## Updates

![A newer release, offered in the sidebar](images/app-update.png)

A release's app asks the project's newest GitHub release whether there is a
newer one as it starts (one request, for `latest.json`). When there is, the
sidebar says so, above where the profiles are: *What's new* opens the
release's page, *Install and restart* downloads the installer of the kind
the app came from, checks that it is signed with the key the app was built
to trust, and for the version it is announced as, installs it and starts
the new app. The sandboxes keep running meanwhile. Offline, or with GitHub
down, nothing is shown and the app asks again at its next start. A
development or local build has no updater and asks nothing.
[releasing.md](releasing.md#the-apps-updater) has how releases are signed.

## How it finds the CLI

In order: `$HARDCODE_CLI` (a program, optionally with arguments, e.g.
`python3 /path/cli/hardcode`), a `hardcode` binary next to the app's
executable (releases bundle the PyInstaller build, with the stack it runs
profiles with, as a Tauri sidecar), or, for a development build,
`python3 cli/hardcode` in the checkout it was built from.

## Building

Like everything here, the app is built in the **build image**
(`images/build/Dockerfile`: Ubuntu 26.04's rustc/cargo and Tauri's
libraries from the archive, the Tauri CLI and PyInstaller pinned).
`app/build.sh` runs each step there through `images/build/run.sh`, which
builds the image on first use, mounts the checkout, caps the container
(`BUILD_MEMORY`, 6g; `BUILD_CPUS`, 4 or what the machine has) and keeps cargo's and linuxdeploy's
caches in `~/.cache/hardcode-build`, so later runs take seconds:

| Command | Does | Output |
|---|---|---|
| `app/build.sh build` | a debug build | `app/src-tauri/target/debug/hardcode-app` |
| `app/build.sh cli` | the standalone `hardcode` binary (PyInstaller) | `dist/hardcode-linux-<arch>` |
| `app/build.sh wheel` | the CLI's Python package, with its stack (the archive's setuptools; nothing is fetched) | `dist/hardcode_cli-<version>-py3-none-any.whl` |
| `app/build.sh bundle` | the CLI as the app's sidecar, then the installers, then on a Linux desktop the launcher below. The version is the files' (`release/release.py stamp X.Y.Z` writes another); `HARDCODE_UPDATER_CONFIG` adds a release's updater configuration, signed with `TAURI_SIGNING_PRIVATE_KEY` (the release workflow does) | `.deb`, `.rpm` and AppImage under `app/src-tauri/target/release/bundle/` |
| `app/build.sh desktop` | the launcher for the release build, in your app grid (Linux; run on your machine, not in the image) | `~/.local/share/applications/HardCode.desktop` |
| `app/build.sh shell` | a shell in the image with the checkout and caches mounted | |

On a Linux desktop, `app/build.sh bundle` then writes a **launcher** for the
build, as `app/build.sh desktop` does on its own: `HardCode.desktop` in
`~/.local/share/applications` (your user only; the `.deb`'s file name, so a
system install beside it doesn't make two entries), pointing at
`app/src-tauri/target/release/hardcode-app`, with the app's icon in
`~/.local/share/icons`. Start HardCode from the app grid and the dock shows
its icon: GNOME matches a window (the app's is `hardcode-app`) to an
installed `.desktop` file, and on Wayland ignores the icon a window sets
itself, so a binary started from a terminal gets a generic one. The
launcher keeps pointing at the release build, so each bundle is picked up
without installing anything again.

`tests/app.py` tests the app in the same image: `cargo test`, a build, and
the headless launch (`--smoke`) against the real CLI and a throwaway home,
given with `--config`: first with the home empty, where the first-profile
dialog must be open as `default` with the tools ticked, then with a profile,
rendering every view. It checks that the Languages / Tools view is
called that and has a card for every language and tool, that the
Extensions view renders with the extension status the CLI gives when
nothing runs (null, in `stack:status` too: no installing line, no marks),
that the new-profile dialog's picker is there (the template ticking only
the tools), and calls the language and extension channels through the CLI
without changing the profile. Last, it builds the app with a release's
updater configuration (a throwaway key), asking a `latest.json` the suite
serves, and checks that the app offers that newer version as it starts; a
Rust test keeps the updater out of the checkout's own configuration.

The app isn't run from the container: it drives Docker on the machine it
runs on. Install the `.deb` or run the AppImage. The release workflow builds
the same Linux bundles in the same image; macOS and Windows installers are
built on those platforms.

## Layout

```
app/src/index.html     the page
app/src/app.js         views and actions; talks to window.hardcode
app/src/bridge.js      window.hardcode on top of Tauri: every channel is one `hardcode --json` call
app/src/styles.css     the stylesheet (shared with the admin UI)
app/src-tauri/         Rust: finds and runs the CLI, streams output, stores settings, checks for and installs updates; Tauri config, icons, capabilities
```
