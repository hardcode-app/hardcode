# Getting started

## What you get

One `hardcode up` builds and starts a **profile**: an Ubuntu 26.04 container
with VS Code served to your browser, Claude Code, toolchains for a dozen
languages, a rootless Docker daemon beside it, and a proxy + DNS pair that
let traffic out only to the URLs on the profile's allowlist. The home
directory lives in a volume, so installs and settings survive restarts and
rebuilds.

## Requirements

- **Docker Engine 28 or newer with Compose v2** (`docker compose`). The
  sandbox network uses the isolated gateway mode that arrived in 28 (no path
  to services on the host); older engines also forward DNS out of internal
  networks (CVE-2024-29018), which would defeat the DNS filter.
- **Linux, macOS or Windows** as the host, amd64 or arm64. On Ubuntu 23.10+
  everything works as-is; on other hosts set `DOCKER_APPARMOR_PROFILE=unconfined`
  in the profile's `.env` (see [Troubleshooting](troubleshooting.md#docker-keeps-restarting)).
- **Python 3.9+** for the `hardcode` command. No packages: YAML is read and
  written through mikefarah's `yq` if you have it, else PyYAML if it is
  installed, else `yq`'s container image through Docker.
- About **12 GB** of disk for the full VS Code image, and 15–20 minutes for its
  first build. Each language can be switched off to shrink it.

## Install the command

Three equivalent ways; pick one:

```sh
hardcode-linux-x64 --version       # a standalone binary from the Releases page (macOS and Windows too)
pipx install ./cli                 # or: pip install ./cli, from a checkout → `hardcode` on your PATH
cli/hardcode --version             # straight from a checkout
```

Wherever you run it from, the command keeps your profiles in its **home**:
`~/.config/hardcode` on Linux (`$XDG_CONFIG_HOME/hardcode`),
`~/Library/Application Support/HardCode` on macOS, `%LOCALAPPDATA%\HardCode`
on Windows, or the folder `--config DIR` or `$HARDCODE_CONFIG` names. It needs
no checkout: the Compose file, the images' build files and the template come
with it, and it writes them out to its cache folder the first time it runs
(run from a checkout, it uses the checkout's own). It doesn't need root, and
refuses to run under sudo, which would give it root's home instead of yours.

## First run

```sh
hardcode up             # asks what your first profile is built around, then builds the images and starts everything
hardcode open           # prints VS Code's URL (with its token) and opens it in your browser
```

The first `up` asks which languages and tools the `default` profile is for
(`python,docker`, say; the tools alone if you just press Enter): they decide
what its image installs, which registries its allowlist opens and which VS
Code extensions it gets, so pick only what you use, and the first build is
quicker. Without a terminal to ask on (a script), make it first:
`hardcode profile add default --lang python,docker`. The desktop app asks
the same in a dialog.

The URL looks like `http://localhost:11111/?tkn=…`: each profile gets a
port of its own from 11111 up, clear of the ports dev servers use. If
another program already has it, the start fails with Docker's "port is
already allocated", and `hardcode port <number>` moves the profile. The token is generated on
first start and kept in the home volume; `hardcode open --no-browser` prints
the URL again. It never appears in the logs (VS Code's own mention of it is
hidden there), and `hardcode token rotate` replaces it. VS Code is published
on `127.0.0.1` only.

What just happened:

1. `profiles/default/` was created in the home: a `.env` with only what
   differs from the defaults (the port, the subnet, the languages you
   chose) and a commented list of every other setting, and the template's
   `allowlist.txt` and `compose.override.yaml`. The folder is yours; every
   later change goes there.
2. `hardcode` wrote `proxy-allowlist.txt`: the entries HardCode provides to
   the profile's languages and tools, with its own from `allowlist.txt`
   (none yet). The `allowlist` one-shot container rendered that into Squid
   and CoreDNS rule files, and `pki` generated this profile's proxy CA.
3. `coredns`, `squid`, `docker`, `vscode` and `gateway` started on the profile's
   private network. The VS Code container trusts the CA and routes HTTP(S) through Squid.

Inside VS Code, open a terminal: `claude` is on the PATH, `docker ps` talks to
the rootless daemon, and `curl https://pypi.org` works while
`curl https://example.com` is refused (and shows up under *Blocked traffic*).

## Day to day

```sh
hardcode stop / start / restart        # keep containers and volumes
hardcode down                          # remove the containers; volumes (home, /shared) stay
hardcode down -v                       # ... and the volumes: a fresh home directory next time
hardcode status                        # every profile, its port and state
hardcode allow github.com/yourname     # let the sandbox clone your repositories (read-only)
hardcode mount ~/Git git               # see your host folder at ~/git in the VS Code container
hardcode docker off                      # no Docker daemon for this profile: nothing privileged (hardcode docker on to undo)
hardcode shell                         # a shell in the VS Code container, as user dev
```

The [desktop app](app.md) does the same from a window; the
[`hardcode` reference](cli.md) lists every command.

## Where your files live

| In the VS Code container | Storage | Notes |
|---|---|---|
| `/home/dev` | the profile's `home` volume | VS Code server and extensions (`~/.vscode-server`), Claude Code, caches, settings. Seeded from the image on first start. |
| `/shared` | the profile's `shared` volume | Also mounted at `/shared` in the Docker daemon's container, so `docker run -v /shared/...` works. `~/shared` links here. |
| host folders | your disk | Added with `hardcode mount`; mounted in both vscode and `docker`. |
| everything else | the container | Reset whenever the container is recreated (`sudo apt install` included; put it in the image instead, see [the VS Code image](vscode-image.md)). |

Language-level installs land in the home volume and persist: `pip --user`
and venvs, `npm i -g` (`~/.local`), `cargo install` (`~/.cargo`),
`go install` (`~/go`), `gem install` (`~/.gem`), `cpanm` (`~/perl5`).

## Next steps

- [The allowlist](allowlist.md): what the defaults allow and how to add your
  git namespace, a registry or an API, read-only or `rw`.
- [Profiles](profiles.md): a second environment with other languages or a
  stricter allowlist.
- [Security model](security.md): what the sandbox does and doesn't protect
  against, before you mount anything writable.
- [Kubernetes](kubernetes.md): the same thing for a team, on a cluster.
