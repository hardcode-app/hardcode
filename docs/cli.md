# The `hardcode` command

`hardcode` does everything the [desktop app](app.md) does, from a terminal
or a script. Both use the same profiles, so you can switch between them
whenever you like. [Getting started](getting-started.md#the-hardcode-command)
says how to install it.

It never needs root, and refuses to run under `sudo` (as root it would use
root's profiles, not yours): Docker's group, Docker Desktop or rootless
Docker are all it needs.

## Everyday use

Every command acts on the `default` profile unless you name another with
`-p NAME`, before or after the command.

**Start, open, stop:**

```sh
hardcode up                  # build if needed, and start (the first time, it asks what to build the profile around)
hardcode open                # open VS Code in your browser
hardcode stop                # stop; everything is kept
hardcode status              # every profile: its port, and whether it runs
```

**What the sandbox may reach** ([the allowlist](allowlist.md)):

```sh
hardcode allowlist                         # every entry, on or off
hardcode allow github.com/your-org         # clone and pull from your organisation (read-only)
hardcode allow github.com/your-org/app rw  # ... and push to one repository
hardcode deny github.com/your-org          # switch an entry off
hardcode blocked                           # what the sandbox tried to reach in the last 30 minutes, and couldn't
```

**Languages, tools and extensions:**

```sh
hardcode languages                 # every language and tool, on or off
hardcode enable rust               # switch one on (the image is rebuilt now)
hardcode disable java --no-build   # switch one off, and rebuild later with: hardcode rebuild
hardcode extension add eamodio.gitlens
```

**Your folders and ports:**

```sh
hardcode mount ~/Git git --ro      # your ~/Git, read-only, at ~/git in the sandbox
hardcode unmount git
hardcode forward 3000              # a server in the sandbox, at http://localhost:3000
hardcode unforward 3000
```

**More profiles** ([Profiles](profiles.md)):

```sh
hardcode profile add api --lang python,docker,claude
hardcode -p api up
hardcode profile remove api
```

**When something's wrong** ([Troubleshooting](troubleshooting.md)):

```sh
hardcode ps                  # the profile's containers, and their state
hardcode logs squid          # a container's log (vscode, docker, squid, coredns, gateway, allowlist, pki)
hardcode shell               # a terminal in the sandbox, as dev
```

## Reference

```
hardcode [-p NAME | --all] [--json] [--config DIR] <command> [args]
```

`hardcode --help` shows a summary; `hardcode <command> --help` shows one
command's options.

### Shared options

| Option | Meaning |
|---|---|
| `-p NAME`, `--profile NAME` | The profile to act on, before or after the command. Default: `$HARDCODE_PROFILE`, else `default`. |
| `--all` | Act on every profile. Accepted by `allow`, `deny`, `allowlist` (`enable`, `disable`, `remove`), `reload`, `mount`, `unmount`, `enable`, `disable`, `rebuild`, `docker`, `extension` and `token`. |
| `--json` | Machine-readable output ([below](#--json-output)); what the desktop app uses. |
| `--config DIR` | The folder the profiles are kept in (the home). Default: `$HARDCODE_CONFIG`, else `~/.config/hardcode` on Linux (`$XDG_CONFIG_HOME/hardcode`), `~/Library/Application Support/HardCode` on macOS, `%LOCALAPPDATA%\HardCode` on Windows. |
| `--version` | The command's version: `hardcode 0.1.0`, or `hardcode 0.1.0 (dev, 2c419ae)` for a development build, with the commit it was built from. |

Exit codes: `0` success; `1` a reported error (`error: …` on stderr; a
damaged `compose.override.yaml` is reported with its path, never as a
traceback); `2` a usage error, or no command. With `--all`, `1` only when
every profile failed; a profile the change failed on is reported as
`skipped`.

**One change to a profile at a time.** Every command that changes a
profile's files or containers takes the profile's lock (`.lock` in its
folder). A second one, from another terminal or the app, says `waiting for
another change to profile 'NAME' to finish...` and goes ahead when the
first is done, so two quick changes are applied one after the other
instead of colliding. The lock goes with the process that holds it,
however it ends.

### Starting and stopping

| Command | Does |
|---|---|
| `up [--build] [service…]` | Start the profile (`docker compose up -d`), building the images that don't exist yet (`--build`: build them again first). With no such profile yet, on a terminal, it asks which languages and tools to build it around (Enter alone: the five tools) and makes it; without a terminal it says how to (`hardcode profile add default --lang …`). It gives the proxy the allowlist as this HardCode provides it first, as `reload` does, so entries a newer HardCode added to a block arrive (and ones it dropped go), and prints what changed (`allowlist: Rust: + newrustsite.com`); a proxy that was already running is reloaded when something did. |
| `down [-v]` | Remove the containers; with `-v` the storage too (the home folder, `/shared`, the Docker daemon's data, the proxy's certificate). |
| `start`, `stop`, `restart` | Start, stop or restart the existing containers, keeping everything. `start` and `restart` give the proxy its allowlist first, as `up` does. |
| `build [service…]` | Build the profile's images. |
| `logs [-f] [--tail N] [service…]` | Containers' logs: followed with `-f`, the last `N` lines of each with `--tail`. With `--json`, the last lines (200 unless `--tail` says otherwise) as one string. |
| `ps` | The profile's containers and their state. |
| `shell` | A login shell (`bash -l`) in the VS Code container, as `dev`. |
| `open [--no-browser]` | Print VS Code's address with its token, and open it in your browser (`--no-browser`: just print it). The address is where the running gateway really publishes VS Code. Says what is missing when there is none: the profile isn't running, VS Code is still starting (no token yet), or the gateway isn't running. The token never goes to the logs: this is the way to get it. |
| `token rotate` | A new VS Code token, so that every copy of the old one (a screenshot, a shared screen, a browser's history) stops working: a running VS Code restarts with it; a stopped profile gets it at its next start. A token fixed in `.env` (`VSCODE_CONNECTION_TOKEN`) is refused: change it there. |
| `port <number>` | Move VS Code to another port on this machine: `VSCODE_PORT` in `.env`, and a running profile's gateway recreated on it (VS Code itself carries on). Refused for a port another profile publishes or one of this profile's forwards; if another program holds it, Docker can't publish it, and the old port stays. |
| `compose <args…>` | Any `docker compose` command for the profile, with its project, settings and override (`hardcode compose build --no-cache vscode`). This is the only way to run Compose for a profile: its Compose file needs what the command sets. |

### Profiles

| Command | Does |
|---|---|
| `profiles`, `status` | Every profile with its port, internal network and state; with `--json`, also the home they are in and the command's version. |
| `profile add NAME --lang LIST` | Create a profile from the template, built around exactly the languages and tools in `LIST` (`python,go,docker`; `--lang ""` for none), with a port (the lowest from 11111 no other profile uses) and an internal network no other profile or Docker network uses. |
| `profile add NAME --from P [--lang LIST]` | Create a profile as a copy of profile `P` ([what a copy takes](profiles.md#copy-a-profile)), with `P`'s languages and tools unless `--lang` gives others. |
| `profile remove NAME [-y]` | Remove the profile's containers and storage (`down -v`) and its VS Code image, then delete its folder. Asks first unless `-y` (`--yes`). Any profile can go, `default` and the last one included: the next `up` asks for a new one. The folder goes only once the containers and storage are gone: if that fails, the files stay, and the error says so; an image that couldn't be removed is reported. |

Profile names: `a-z`, `0-9` and `-`, up to 32 characters, starting and
ending with a letter or digit. (On Kubernetes, names must also start with
a letter.)

### Languages, tools and extensions

A profile is built around its languages and five tools (build essentials,
the Docker CLI, Kubernetes, the Claude Code CLI, the Codex CLI). Each
decides what the image installs, which [allowlist entries](allowlist.md#what-hardcode-provides)
are open, and which VS Code extensions the sandbox gets.

| Command | Does |
|---|---|
| `languages` | Every language and tool: on or off, how many allowlist entries it opens and extensions it brings, and whether the image was built with other ones. For a profile that doesn't exist yet, the catalogue, every entry off. |
| `enable <lang>… [--no-build]`, `disable <lang>… [--no-build]` | Switch languages and tools on or off: `INSTALL_<LANG>` in `.env`; the allowlist entries they bring (a running proxy reloads at once; `allowlist.txt` isn't touched, and an entry switched off there stays off); the extension list (a running sandbox installs or removes the extensions within seconds); then the image is rebuilt with the new software and the VS Code container recreated. `--no-build` leaves the image for a later `rebuild`, and does everything else at once. |
| `rebuild` | Rebuild the VS Code image with the profile's languages and tools, and recreate the container if it runs. Gives the proxy its allowlist too, as `up` does. |
| `extensions` | Every VS Code extension the sandbox gets, and where it comes from: every VS Code container, a language or tool, or the profile's own. While the sandbox runs, which are still installing, refused (a publisher without a verified domain) or to be tried again. |
| `extension add\|remove <publisher.name>` | Add or remove one of the profile's own extensions (`VSCODE_EXTENSIONS` in `.env`). A running sandbox installs or removes it within seconds, without a restart. One a language or tool already brings isn't added twice; a language's own goes when the language is switched off, and the one every sandbox has can't be removed. Only publishers with a verified domain are installed. |

Languages: `python node java android dotnet go rust cpp ruby perl`; tools:
`build_essentials docker kubernetes claude_cli codex_cli`. Short names
work too: `c`, `c++`, `cxx`, `c#`, `csharp`, `net`, `.net`, `js`,
`javascript`, `ts`, `typescript`, `nodejs`, `golang`, `claude`,
`claude_code`, `codex`, `build`, `build_essential`, `essentials`, `k8s`,
`kubectl`, `helm`. A `-` counts as `_`, and case doesn't matter.

### The allowlist

| Command | Does |
|---|---|
| `allowlist` | Every entry of one profile's allowlist, as the app lists them: its own from `allowlist.txt` first, then those HardCode provides, by block (every profile's, then its languages' and tools'); each on or off, with its access and note; an invalid line is marked. |
| `allow <entry> [rw\|ssh]` | Add the entry (or uncomment and re-flag an existing line), and reload a running proxy. A provided entry that already allows as much isn't added; one that was switched off is switched back on. `github:OWNER/REPO` stands for one GitHub repository's two entries, git's address for it and GitHub's API for it (`github.com/OWNER/REPO.git`, `api.github.com/repos/OWNER/REPO`); `deny` takes it too ([GitHub from the sandbox](github.md)). |
| `deny <entry> [ssh]` | Switch an entry off: one of the profile's own is commented out (wherever it is in the file), a provided one gets an `off <entry>` line. An error if it isn't on. `ssh` means the host's SSH entry; without it, its HTTPS one. |
| `allowlist enable\|disable\|remove <entry> [ssh]` | Comment the entry's line back in, comment it out, or delete it. A provided entry is switched on or off (its `off` line removed or added) and can't be removed: it would come back with its block. |
| `reload` | Give a running proxy the allowlist again, after you edited `allowlist.txt` by hand, with the entries this HardCode provides; says what changed. |
| `blocked [--since 10m\|30m\|2h\|24h]` | What the proxy and the DNS server refused, by target, most frequent first. Default: the last 30 minutes. |

If the proxy doesn't come back healthy after a change on a running
profile, the command fails (exit 1, the change kept): which rules are in
force is then unknown until `hardcode logs allowlist squid` says why.
[The allowlist](allowlist.md#entry-format) has the entry format.

### Ports and folders

| Command | Does |
|---|---|
| `forward <port> [vscode\|docker] [target]` | Publish `localhost:<port>` (on `VSCODE_BIND`) to port `target` (the same port by default) in the VS Code container, or with `docker` in the Docker daemon's container (for a container's `docker run -p` port). Refused: the VS Code port, 8000, a port another profile uses (every profile holding it is named), and `docker` on a profile without the Docker daemon. Another profile's damaged `compose.override.yaml` is a warning (its forwards can't be counted), not a stop. Recreates the gateway if the profile runs; if the gateway can't start with the forward (the port is taken on this machine), the forward is undone, the gateway started again as it was, and the error gives Compose's reason. |
| `unforward <port>` | Remove a forward. |
| `mount <local> <remote> [--ro]` | Share a folder of this machine with the VS Code container and the Docker daemon, at the same path in both. `<local>` must exist. `<remote>` is absolute, or relative to the home folder (`git` is `/home/dev/git`). A folder under your home folder is written as `${HOME}/…`. Recreates both containers if the profile runs, undoing the mount if they can't start with it. |
| `unmount <remote>` | Remove a mount from both. A mount written by hand as `/home/${DEV_USER:-dev}/git` is the one `unmount git` names. |

### The Docker daemon, and the whole profile

| Command | Does |
|---|---|
| `docker on\|off` | The Docker daemon beside the VS Code container. `off` leaves it out of the profile: nothing privileged runs for it, and there is no `docker` inside (the harder sandbox). On a running profile it applies at once: the daemon's container is stopped and removed (its data is kept for `on`), the VS Code container is recreated, and nothing recreates the daemon while it is off. `down` always covers it. |
| `show` | The profile's languages, extensions, allowlist, forwards and mounts at a glance, and whether the image needs a rebuild. |

### Environment variables

| Variable | Effect |
|---|---|
| `HARDCODE_PROFILE` | Default for `-p`. |
| `HARDCODE_CONFIG` | Default for `--config`: the folder the profiles are kept in. |

### `--json` output

Commands that change something print `{"results": [...]}`, one result per
profile they touched:

```json
{
  "results": [
    {"profile": "default", "message": "allowed github.com/ourcompany (rw)", "changed": true, "applied": true, "data": {}}
  ]
}
```

`applied` is `true` when the running containers took the change, `false`
when the profile isn't running (the change applies at its next start), and
`null` when there was nothing to apply. `profile add` puts the new
profile's `name`, `source`, `port`, `subnet` and `languages` in `data`;
`open` puts the `url` there; `reload` its `changes`.

Queries print their own objects:

| Command | Shape |
|---|---|
| `profiles` / `status` | `{"home": "<folder>", "version": "<as --version says it>", "profiles": [{"name", "port", "subnet", "state", "running"}]}` |
| `ps` | `{"services": [{"service", "state", "status", "health"}], "docker": true, "error": "", "extensions": {"state", "installed", "total"}}` (`docker: false` when the Docker CLI isn't found; `error` says why Compose couldn't list the containers, such as a daemon that isn't running, and is empty when it could; `extensions` is how far the running VS Code container's extensions have got, `state` `installing` or `done`, and `null` when it isn't running) |
| `show` | `{"profile", "env": {…}, "vscodePort", "allowlist": [entries], "forwards": [{port, host, target}], "mounts": [{local, remote, mode}], "langs": [languages], "extensions": [extensions], "image": {"built", "languages"}, "rebuildNeeded", "docker": bool, "profiles": [{name, port}]}` (`image.built` is `false` with no image and `null` when Docker can't say; `image.languages` is `[]` with no image, the image's languages and tools when it has the label, `null` when nobody can tell; `rebuildNeeded` is `null` when nobody can tell) |
| `languages` | `{"languages": [languages], "rebuildNeeded": bool}` |
| `extensions` | `{"extensions": [extensions], "refused": [ids], "status": {"state", "total", "installed", "pending": [ids], "refused": [ids], "failed": [ids], "updated"}}` (`status` is what the running VS Code container's extension sync last wrote; `null` when it isn't running) |
| `allowlist` | `{"allowlist": [entries]}`, the same list `show` has |
| `blocked` | `{"blocked": [{"kind": "http"\|"dns", "method", "host", "url", "target", "client", "count"}]}` |
| `logs` | `{"services": [names], "logs": "…"}`: without colours, and without the service-name prefix when one service is named |

An **allowlist entry** is `{"id", "entry", "flag", "comment", "enabled",
"invalid", "provided", "source", "section", "language", "line",
"off_line", "kind"}`: `id` is its place in the list; `kind` one of `host`,
`subdomains`, `path`, `ssh`; `provided` whether HardCode provides it;
`source` the block's key (`vscode`, `os_packages`, `registries`,
`github_raw`, or a language's) or `custom` for the profile's own;
`section` the block's name (for the profile's own, the section of
`allowlist.txt` it is in); `language` the language or tool it comes with
(`""` for the others); `line` its line in `allowlist.txt` (`-1` for a
provided one).

A **language** is `{"key", "name", "desc", "kind": "language"|"tool",
"domains": [allowlist entries it opens], "rw": [those of them that are
rw], "extensions": [ids it brings], "enabled", "inImage"}` (`inImage`:
whether the image was built with it; `null` when Docker can't say).

An **extension** is `{"id", "source": "core"|<language key>|"custom",
"enabled", "duplicate"}`: every language's extensions are listed, with
`enabled` only for languages that are on; `duplicate`, on the profile's
own only, marks one that a language or every VS Code container brings
anyway.

### Examples

```sh
hardcode up --build                                    # build the images again, and start
hardcode allow github.com/ourcompany rw                # clone and push within the organisation
hardcode allow ssh.github.com:443 ssh                  # ... or over SSH, if you must
hardcode deny .gcr.io                                  # no Google registry for this profile
hardcode forward 3000                                  # a dev server in the VS Code container -> http://localhost:3000
hardcode forward 8080 docker 80                        # a container's published port 80 -> http://localhost:8080
hardcode mount ~/Git git --ro                          # read your repositories at ~/git, no writes back
hardcode profile add api --lang python,docker          # a profile for a Python service, nothing else open
hardcode disable dotnet java                           # a smaller image, those registries closed (one rebuild)
hardcode extension add eamodio.gitlens                 # an extension of the profile's own
hardcode profile add client-x --from default
hardcode -p client-x allow api.client-x.example rw
hardcode -p client-x docker off                        # no Docker daemon for this one: nothing privileged
hardcode --all allow docs.ourcompany.example
hardcode --json blocked --since 24h | jq '.blocked[] | select(.count > 10)'
hardcode compose exec vscode id                        # anything docker compose can do
```
