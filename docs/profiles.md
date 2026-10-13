# Profiles

A profile is a complete, separate environment: its own languages,
allowlist, extensions, published port, mounts, containers, image and
volumes. The first, `default`, is made when you first run `hardcode up` (or
the app), which asks what it is built around; add more for a client with a
stricter allowlist, a project in another language, or an experiment you
want to throw away afterwards.

A profile starts with its **languages**: they decide what its image
installs, which [allowlist entries](allowlist.md#what-hardcode-provides) are open,
and which VS Code extensions its VS Code container gets. A Python service's profile
needs neither the npm registry nor the Java toolchain.

```sh
hardcode profiles                              # or: hardcode status
hardcode profile add api --lang python,docker  # built around Python (and the Docker CLI), nothing else open
hardcode profile add old-node --from default   # copies its languages, allowlist, settings and mounts
hardcode -p old-node disable dotnet java       # then make it different
hardcode -p old-node up --build                # its own containers, volumes, port, image
hardcode profile remove old-node               # down -v, delete the image, delete its folder
```

A profile made from the template needs its languages and tools (`--lang
python,docker`, or `--lang ""` for none); a copy keeps its source's unless
given others.

Every command takes `-p NAME` / `--profile NAME` (before or after the
command) or reads `HARDCODE_PROFILE`; without either it acts on `default`.

## Where profiles are

Every profile is a folder in HardCode's **home**: `~/.config/hardcode` on
Linux (`$XDG_CONFIG_HOME/hardcode`), `~/Library/Application Support/HardCode`
on macOS, `%LOCALAPPDATA%\HardCode` on Windows (not the roaming profile: a
profile names this machine's folders and ports). `--config DIR` or
`$HARDCODE_CONFIG` uses another folder instead, `/Apps/hardcode` say; the
app takes `--config DIR` too, and passes it on. `hardcode profiles` says which
home it is using.

A home other than the default one gets an id of its own (a short hash of its
path) in its profiles' Compose project and image names
(`hardcode-<id>-<profile>`), so its `default` and the default home's are
separate containers.

```
~/.config/hardcode/
  profiles/
    default/
      .env                    what differs from the defaults: port, subnet, languages, your own settings
      allowlist.txt           its own allowlist entries, and the provided ones it switches off
      compose.override.yaml   mounts and extra port forwards, and anything else you change in Compose
      proxy-allowlist.txt     what the proxy is given: allowlist.txt with HardCode's entries; written by hardcode, don't edit
      proxy-allowlist.applied what the running proxy last loaded (a digest); written by hardcode
      vscode-extensions.txt   the VS Code extensions it wants; written by hardcode, don't edit
      .lock                   held while a command changes the profile
```

The first three files are plain text and meant to be edited by hand as well
as by the command and the app; the next three are written from them and by
`hardcode`. A
missing `.env` or `allowlist.txt` is restored on the next command; a
restored `.env` gets a port and subnet of its own, as a new profile does
(and no languages).

What every profile runs with (`compose.yaml`, the images' build files, the
template a new profile's files start from) belongs to the HardCode you run,
not to the profile: it comes with the command and is written out to its
cache folder (`~/.cache/hardcode/stack/<hash>` on Linux), a new folder for
each new version, so an update reaches every profile at its next start.
Don't edit it there: a change of your own goes in the profile, as a
setting in `.env` or as Compose additions in `compose.override.yaml`,
which Compose merges over `compose.yaml`.

### Only what differs

A profile's `.env` holds only what differs from the defaults, which are
`compose.yaml`'s: the port and subnet HardCode picked, the languages and
tools switched on, and what you set yourself. Below them, a commented list
shows every setting with its default; write one without the `#` (above
that list) to change it, and delete the line to go back to the default. A
newer HardCode's defaults reach a profile that doesn't set them; a value
the profile sets stays.

The CLI reads `.env` as Compose does: a `# comment` after a value (with a
space before the `#`) and an `export` in front of a key are ignored, and when
a key appears twice the last line counts (`hardcode` edits change every line
for the key).

### `.env`

| Key | Default | Meaning |
|---|---|---|
| `INSTALL_<LANG>` | off | The profile's languages and tools ([list](vscode-image.md#languages)): what the image includes, which allowlist entries HardCode provides, which extensions the VS Code container gets. Exactly `true` is on; unset, or anything else (`True`, `yes`), is off, as the Dockerfile and the VS Code container read it, so a language a newer HardCode adds stays off in the profiles you have. `hardcode enable`/`disable` edit these and apply them at once; a hand edit here reaches the proxy at the next start or `reload`. |
| `VSCODE_EXTENSIONS` | empty | VS Code extensions of the profile's own, beside its languages' (`publisher.name`, comma-separated). Installed when the VS Code container starts; only from publishers whose domain the Marketplace has verified. `hardcode extension add\|remove` edit it. |
| `VSCODE_BIND` | `127.0.0.1` | Address VS Code is published on. Keep it local unless you put TLS and sign-in in front. |
| `VSCODE_PORT` | `11111` | Host port for VS Code. Must differ between profiles; `profile add` picks the lowest from 11111 that no other profile uses; if another program holds it, Docker says so when the profile starts, and `hardcode port <number>` (the app's *change* beside it) moves it. |
| `VSCODE_CONNECTION_TOKEN` | empty | Fixed token; empty means generated on first start and kept in the home volume. |
| `VSCODE_DEFAULT_FOLDER` | empty (home) | Folder VS Code opens when the URL doesn't name one. |
| `DEV_USER`, `DEV_UID`, `DEV_GID` | `dev`, `1000`, `1000` | The VS Code container user. Match your host uid/gid if you mount host folders; rootless docker always runs as 1000. |
| `ENABLE_DOCKER` | `true` | The rootless Docker daemon beside the VS Code container; on only for exactly `true` (or unset), as the VS Code container's entrypoint reads it. `false` leaves it out entirely (nothing privileged runs for the profile, no `docker` inside): the harder sandbox. `hardcode docker on\|off` and the switch on the docker row of the app's Overview edit this. |
| `VSCODE_MEMORY`, `DOCKER_MEMORY` | `8g`, `4g` | Hard memory limits on the VS Code container and on the Docker daemon (its containers count against it); no swap on top. Squid (512m), CoreDNS (128m) and the gateway (64m) have fixed caps. |
| `VSCODE_PIDS`, `DOCKER_PIDS` | `4096`, `2048` | How many processes each may run: a fork bomb stops there. |
| `EGRESS_INTERNAL_RANGES` | empty (loopback, link-local, private, shared) | Address ranges an allowlisted name must never resolve to; squid refuses the request. Narrow it for a registry on your LAN. |
| `SQUID_CLIENT_RANGES` | empty (private, shared, local IPv6) | Who may use the proxy. |
| `VSCODE_VERSION` | `latest` | The VS Code .deb to start from; the server self-updates at runtime regardless. |
| `GRADLE_VERSION` | `latest` | Gradle release from gradle.org, or `none`. |
| `CODEX_VERSION` | `latest` | Codex CLI release (e.g. `0.162.1`); a rebuild updates it, since its own update check is off. |
| `DEVNET_SUBNET`, `DNS_IP` | `172.31.253.0/24`, `172.31.253.53` | The profile's internal network and CoreDNS's fixed address on it. Change both together. |
| `UPSTREAM_DNS` | `/etc/resolv.conf` | Where allowlisted names resolve: a resolv.conf path or IPs (`1.1.1.1 9.9.9.9`). |
| `DOCKER_APPARMOR_PROFILE` | `rootlesskit` | AppArmor profile for rootless docker; `unconfined` on hosts without Ubuntu's profile. |

A key left out means the default; the command treats unset `VSCODE_PORT`,
`DEVNET_SUBNET` and `DNS_IP` as their defaults when allocating for new
profiles.

### `compose.override.yaml`

Merged into `compose.yaml` by Compose. `hardcode mount`/`unmount` and
`forward`/`unforward` maintain it, keeping any comment header you write at
the top; the template shows the layout:

```yaml
services:
  vscode:
    volumes:
      - ${HOME}/Git:/home/${DEV_USER:-dev}/git        # a host folder (append :ro for read-only)
  docker:
    volumes:
      - ${HOME}/Git:/home/${DEV_USER:-dev}/git        # the same path, so `docker run -v` works
  gateway:
    environment:
      GATEWAY_FORWARDS: 8000:vscode:8000 3000:vscode:3000 8080:docker:8080
    ports:
      - ${VSCODE_BIND:-127.0.0.1}:3000:3000
      - ${VSCODE_BIND:-127.0.0.1}:8080:8080
```

Host paths under your home directory are written as `${HOME}/…` so the file
works for another user. After every edit the command runs
`docker compose config` and restores the previous file if Compose rejects
the result.

## Copying a profile

`hardcode profile add NAME --from OTHER` (or *copy of OTHER* in the app)
copies `OTHER`'s `allowlist.txt`, `.env` and `compose.override.yaml`, then:

- with `--lang LIST` (the app's dialog always passes the languages ticked),
  builds the copy around those languages instead of `OTHER`'s; the copy's
  allowlist keeps `OTHER`'s own entries and its switched-off provided ones,
  and gets the provided entries of its own languages;

- allocates a **free port** (the lowest from 11111 that no other profile
  publishes, as its VS Code port or as a forward) and a **free subnet** (`172.31.N.0/24` counting down
  from 253, skipping any that overlaps a Docker network) and writes them,
  with the matching `DNS_IP`, into the new `.env`;
- keeps the **mounts** but drops the **port forwards**: a host port belongs
  to one profile.

The new profile builds its own `hardcode-vscode:NAME` image from the copied `.env`,
and starts with empty volumes: a fresh home directory, no inner Docker
images. The source profile is untouched.

## Changing all profiles at once

`--all` runs a change on every profile and reports per profile:

```sh
hardcode --all allow github.com/ourcompany rw   # [default] allowed … [old-node] allowed …
hardcode --all deny telemetry.example.net
hardcode --all allowlist disable .gcr.io
hardcode --all mount ~/Git git --ro
hardcode --all unmount git
hardcode --all enable go --no-build
hardcode --all rebuild
hardcode --all reload
hardcode --all docker off
```

A profile that can't take the change (an entry it doesn't have, a mount that
already exists) is reported as `skipped` and the others proceed; the exit code
is 1 only when every profile failed. `forward` is per profile by design. The
app has the same thing as an *all N profiles* scope on the Languages / Tools,
Extensions, Allowlist and Mounts views; the admin UI as *All allowlists*.

## What is shared between profiles

At runtime, nothing but two images: separate Compose projects, networks,
volumes, CAs and tokens, and each its own `hardcode-vscode:<name>` image. The squid
and gateway images (`hardcode-squid:<hash>`, `hardcode-gateway:<hash>`, tagged
with a hash of their build files) are built once and used by every profile,
and built again when a newer HardCode changes them. What profiles share on
disk is the stack: `compose.yaml`, the image sources and the template. Rebuilding one profile's VS Code image doesn't touch the
others'; the BuildKit cache makes the second build of the same layers fast.
