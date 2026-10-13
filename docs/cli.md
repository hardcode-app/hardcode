# The `hardcode` command

```
hardcode [-p NAME | --all] [--json] [--config DIR] <command> [args]
```

Python 3.9+, standard library only. Use a release binary, install it from a
checkout with `pipx install ./cli`, or run it there as `cli/hardcode`. It
keeps the profiles in its home ([where](profiles.md#where-profiles-are)),
and carries what it runs them with (`compose.yaml`, the images' build
files, the template), written out to its cache folder the first time; run
from a checkout, it uses the checkout's. It refuses to run under sudo: it
never needs root, and as root it would use root's home.

## Shared options

| Option | Meaning |
|---|---|
| `-p NAME`, `--profile NAME` | The profile to act on, before or after the command. Default: `$HARDCODE_PROFILE`, else `default`. |
| `--all` | Act on every profile. Accepted by `allow`, `deny`, `allowlist`, `reload`, `mount`, `unmount`, `enable`, `disable`, `rebuild`, `docker`, `extension`. |
| `--json` | Machine-readable output (what the desktop app uses). |
| `--config DIR` | The folder the profiles are kept in (the home). Default: `$HARDCODE_CONFIG`, else `~/.config/hardcode` on Linux (`$XDG_CONFIG_HOME/hardcode`), `~/Library/Application Support/HardCode` on macOS, `%LOCALAPPDATA%\HardCode` on Windows. |
| `--version` | |

Exit codes: `0` success, `1` a reported error (`error: …` on stderr; a
damaged `compose.override.yaml` is reported with its path, never as a
traceback), `2` usage. With `--all`, `1` only when every profile failed;
profiles that couldn't take the change are reported as `skipped`.

One change to a profile at a time: every command that changes a profile
(its files or its containers: everything but `show`, `ps`, `profiles`,
`languages`, `extensions`, `blocked`, `logs`, `open`, `shell` and
`compose`) takes the profile's lock, `.lock` in its folder. A second one,
from another terminal or the app, says `waiting for another change to
profile 'NAME' to finish...` on stderr and goes when the first is done, so
two quick switches in the app are applied one after the other instead of
colliding. The lock goes with the process that holds it, however it ends.

## Lifecycle

| Command | Does |
|---|---|
| `up [--build] [service…]` | `docker compose up -d`, building images that don't exist yet (`--build`: rebuild). With no such profile yet, on a terminal, it asks which languages and tools to build it around (the tools if the answer is empty) and makes it; without a terminal it says how (`hardcode profile add default --lang …`). Gives the proxy its allowlist as this HardCode provides it first, as `reload` does, so entries a newer HardCode added to a block arrive (and ones it dropped go), printing what changed (`allowlist: Rust: + newrustsite.com`); a proxy that was already running is reloaded when something did. |
| `down [-v]` | Remove the containers; with `-v` the volumes too (home directory included). |
| `start`, `stop`, `restart` | The containers, keeping everything. `start` and `restart` give the proxy its allowlist as this HardCode provides it first, as `up` does. |
| `build [service…]` | Build the profile's images (`hardcode build vscode` after changing languages by hand). |
| `logs [-f] [--tail N] [service…]` | Container logs: followed with `-f`, the last `N` lines of each with `--tail`. With `--json`, the last lines (200 unless `--tail` says otherwise) as one string. |
| `ps` | Containers and their state. |
| `shell` | `bash -l` in the VS Code container as `dev`. |
| `open [--no-browser]` | Print VS Code's URL with its token and open it. The address is where the running gateway really publishes VS Code (`docker compose port`), so a `.env` edited since the start or a `VSCODE_BIND` address can't give a URL that doesn't connect. Says which is missing: the profile isn't running, VS Code is still starting (no token yet), or the gateway isn't running. The token never goes to the logs: this is the way to get it. |
| `token rotate` | A new VS Code connection token, so that every copy of the old one (a screenshot, a shared screen, a browser's history) stops working: the token file in the home volume is deleted, and a running VS Code container restarted to write a new one; a stopped profile gets it at its next start. A token fixed in `.env` (`VSCODE_CONNECTION_TOKEN`) is changed there instead. |
| `compose <args…>` | Any `docker compose` command with the profile's project, env file and override, over the stack (the only way to run Compose for a profile: `compose.yaml` needs what the command sets). |
| `port <number>` | Move VS Code to another port on this machine: `VSCODE_PORT` in `.env`, and a running profile's gateway recreated on it (VS Code itself carries on). Refused for a port another profile publishes or one of this profile's forwards; if another program holds it, Docker can't publish it, and the old port stays. |

## Profiles

| Command | Does |
|---|---|
| `profiles`, `status` | Every profile with its port, subnet and state; with `--json`, also the home they are in. |
| `profile add NAME --lang LIST`, `profile add NAME --from P [--lang LIST]` | Create the profile from the template, or as a copy of profile `P` ([what a copy keeps](profiles.md#copying-a-profile): not its fixed VS Code token), with a port (the lowest from 11111 no other profile uses) and a subnet no other profile or Docker network uses. `--lang python,go,docker` builds it around exactly those languages and tools (`--lang ""` for none); from the template it is required, and a copy keeps its source's without it. Names: `a-z`, `0-9` and `-`, up to 32 characters, not ending in `-` (the admin and the CRD also want a leading letter, which Kubernetes Service names need). |
| `profile remove NAME [-y]` | `down -v`, remove the profile's VS Code image, delete its folder. Asks first unless `-y`/`--yes`. Any profile can go, `default` and the last one included: the next `up` asks for a new one. The folder goes only once the containers and volumes are gone: if the teardown fails the files stay, and the error says so; an image that couldn't be removed is reported. |

## Languages and extensions

A profile is built around its languages (and four tools: build essentials,
the Docker CLI, the Claude Code CLI, the Codex CLI). Each one decides three things: what
the image installs, which [allowlist entries](allowlist.md#what-hardcode-provides)
are open, and which VS Code extensions the VS Code container gets.

| Command | Does |
|---|---|
| `languages` | Every language and tool: on or off, the allowlist entries it opens, the extensions it brings, and whether the image has it. Says when the image was built with other languages. For a profile that doesn't exist yet, the catalogue, every entry off (what the app's first-profile dialog offers). |
| `enable <lang>... [--no-build]`, `disable <lang>... [--no-build]` | Switch languages on or off: `INSTALL_<LANG>` in `.env`, the allowlist entries they provide (the proxy reloads at once if running; `allowlist.txt` isn't touched, and entries switched off in it stay off), the extension list (a running VS Code container installs or removes the extensions within seconds), then the image is rebuilt with the toolchains and the VS Code container recreated. `--no-build` leaves the image for a later `rebuild`; everything else still applies at once. Languages: `python node java dotnet go rust cpp ruby perl`, tools `build_essentials docker claude_cli codex_cli`, with aliases such as `c++`, `c#`, `js`, `ts`, `golang`, `claude`, `codex`. |
| `rebuild` | Rebuild the VS Code image with the profile's languages, and recreate the container if running. Gives the proxy its allowlist as this HardCode provides it too, as `up` does. |
| `extensions` | Every VS Code extension the VS Code container gets and where it comes from: every VS Code container, a language or tool, or the profile's own. While the container runs, which are still installing, refused (publishers without a verified domain) or to be tried again. |
| `extension add\|remove <publisher.name>` | One of the profile's own extensions (`VSCODE_EXTENSIONS` in `.env`). A running VS Code container installs or removes it within seconds, without a restart. One a language or tool already brings isn't added twice; a language's own goes when the language is switched off, and one every container has can't be removed. Only publishers with a verified domain are installed. |

## Allowlist

| Command | Does |
|---|---|
| `allowlist` | Every entry of the profile's allowlist, as the app and the admin list them: those HardCode provides, by block (every profile's, then its languages' and tools'), then its own from `allowlist.txt`, each on or off, with its access and note. One profile at a time. |
| `allow <entry> [rw\|ssh]` | Add the entry (or uncomment and re-flag an existing line), validate it, reload the proxy if running. A provided entry that allows as much already isn't added; switched off, it is switched back on. |
| `deny <entry> [ssh]` | Comment the entry out, wherever it is in the file; a provided entry is switched off (an `off <entry>` line). Fails if it isn't active. `ssh` addresses the host's SSH entry; otherwise its HTTPS one. |
| `allowlist enable\|disable\|remove <entry> [ssh]` | Comment in, comment out, or delete the entry's line(s). A provided entry is switched on or off (its `off` line removed or added) and can't be removed: it would come back with its block. |
| `reload` | Give the running proxy the allowlist again, after hand edits, with the entries this HardCode provides; says what changed. If the proxy doesn't come back healthy, that is an error (exit 1, the edit kept): the policy in force is then unknown until `hardcode logs allowlist squid` says why. The same applies to every allowlist edit on a running profile. |
| `blocked [--since 10m\|30m\|2h\|24h]` | What Squid and CoreDNS refused, aggregated by target. |

See [The allowlist](allowlist.md) for the entry format.

## Forwards and mounts

| Command | Does |
|---|---|
| `forward <port> [vscode\|docker] [target]` | Publish `127.0.0.1:<port>` to `<target>` (default: the same port) in the VS Code container or in the Docker daemon's container (for `docker run -p`). Refuses the VS Code port, 8000, ports another profile uses (as VS Code port or forward; every profile holding it is named), and `docker` on a profile running without the daemon. Another profile's damaged `compose.override.yaml` is a warning on stderr (its forwards can't be counted), not a stop. Recreates the gateway if running; if it can't start with the forward (the port is taken on this machine), the forward is undone and the gateway started again as it was, and the error gives Compose's reason. |
| `unforward <port>` | Remove it. |
| `mount <local> <remote> [--ro]` | Mount a host folder in the VS Code container and in docker. `<remote>` is absolute or relative to the home directory (`git` → `/home/dev/git`). `~` and `$HOME` prefixes are written as `${HOME}`. Recreates vscode and docker if running, undoing the mount if they can't start with it. |
| `unmount <remote>` | Remove it from both. A mount written by hand as `/home/${DEV_USER:-dev}/git` is the one `unmount git` names. |

## The Docker daemon, and everything at once

| Command | Does |
|---|---|
| `docker on\|off` | The rootless Docker daemon beside the VS Code container. `off` leaves it out of the profile: nothing privileged runs for it and there is no `docker` inside (the harder sandbox). Takes effect at once on a running profile: the daemon's container is stopped and removed (its data volume stays for `on`), the VS Code container is recreated, and nothing recreates the daemon while it is off. `down` always covers it. |
| `show` | The profile's languages, extensions, allowlist, forwards and mounts at a glance, and whether the image needs a rebuild. |

## Environment variables

| Variable | Effect |
|---|---|
| `HARDCODE_PROFILE` | Default for `-p`. |
| `HARDCODE_CONFIG` | Default for `--config`: the folder the profiles are kept in. |

## `--json` output

Operations print `{"results": [...]}`, one result per profile touched:

```json
{
  "results": [
    {"profile": "default", "message": "allowed github.com/ourcompany (rw)", "changed": true, "applied": true, "data": {}}
  ]
}
```

`applied` is `true` when the running containers were updated, `false` when
the profile isn't running (the change applies at the next start) and `null`
when there was nothing to apply. `profile add` puts the allocated port and
subnet in `data`; `open` puts the URL there.

Queries print their own objects:

| Command | Shape |
|---|---|
| `profiles` / `status` | `{"home": "<folder>", "profiles": [{"name", "port", "subnet", "state", "running"}]}` |
| `ps` | `{"services": [{"service", "state", "status", "health"}], "docker": true, "error": "", "extensions": {"state", "installed", "total"}}` (`docker: false` when the Docker CLI isn't found; `error` says why Compose couldn't list the containers, such as a daemon that isn't running, and is empty when it could; `extensions` is how far the running VS Code container's extensions have got, `state` `installing` or `done`, and `null` when it isn't running) |
| `show` | `{"profile", "env": {…}, "vscodePort", "allowlist": [entries], "forwards": [{port, host, target}], "mounts": [{local, remote, mode}], "langs": [languages], "extensions": [extensions], "image": {"built", "languages"}, "rebuildNeeded", "docker": bool, "profiles": [{name, port}]}` (`image.built` is `false` with no image and `null` when Docker can't say; `image.languages` is `[]` with no image, the image's languages and tools when it has the label, `null` when nobody can tell; `rebuildNeeded` is `null` when nobody can tell) |
| `languages` | `{"languages": [languages], "rebuildNeeded": bool}` |
| `extensions` | `{"extensions": [extensions], "refused": [ids], "status": {"state", "total", "installed", "pending": [ids], "refused": [ids], "failed": [ids], "updated"}}` (`status` is what the running VS Code container's extension sync last wrote; `null` when it isn't running) |
| `blocked` | `{"blocked": [{"kind": "http"\|"dns", "method", "host", "url", "target", "client", "count"}]}` |
| `logs` | `{"services": [names], "logs": "…"}`: without colours, and without the service-name prefix when one service is named |

An allowlist entry is `{"id", "entry", "flag", "comment", "enabled",
"invalid", "provided", "source", "section", "language", "line", "off_line",
"kind"}`: `id` its place in the list; `kind` one of `host`, `subdomains`,
`path`, `ssh`; `provided` whether HardCode provides it, `source` the block's
key (`vscode`, `os_packages`, `registries`, `github_raw`, or a language's)
or `custom` for the profile's own, `section` the block's name (for the
profile's own, the section of `allowlist.txt` it is in), `language` the
language or tool it comes with (`""` for the others), `line` its line in
`allowlist.txt` (`-1` for a provided one). `hardcode --json allowlist` is
`{"allowlist": [entries]}`, the same list `show` has.

A language is `{"key", "name", "desc", "kind": "language"|"tool", "domains":
[allowlist entries it opens], "extensions": [ids it brings], "enabled",
"inImage"}` (`inImage`: whether the image was built with it; `null` when
Docker can't say). An extension is `{"id", "source": "core"|<language
key>|"custom", "enabled", "duplicate"}`: every language's extensions are
listed, `enabled` only for languages that are on; `duplicate` marks one of
the profile's own that a language or every VS Code container brings anyway.

## Examples

```sh
hardcode up --build                                    # first start
hardcode allow github.com/ourcompany rw                # clone and push within the org
hardcode allow ssh.github.com:443 ssh                  # … or over SSH, if you must
hardcode deny .gcr.io                                  # no Google registry for this profile
hardcode forward 3000                                  # a dev server in the VS Code container → http://localhost:3000
hardcode forward 8080 docker 80                          # a container's published port 80 → http://localhost:8080
hardcode mount ~/Git git --ro                          # read your repos at ~/git, no writes back
hardcode profile add api --lang python,docker          # a profile for a Python service, nothing else open
hardcode disable dotnet java                           # a smaller image, those registries closed (one rebuild)
hardcode extension add eamodio.gitlens                 # an extension of the profile's own
hardcode profile add client-x --from default
hardcode -p client-x allow api.client-x.example rw
hardcode -p client-x docker off                          # no Docker daemon for this one: nothing privileged
hardcode --all allow docs.ourcompany.example
hardcode --json blocked --since 24h | jq '.blocked[] | select(.count > 10)'
hardcode compose exec vscode id                        # anything docker compose can do
```
