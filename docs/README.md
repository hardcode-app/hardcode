# HardCode documentation

HardCode gives you, and the AI coding agents you work with, a development
environment in a box: VS Code in your browser, Claude Code and Codex, a
Docker daemon of its own, and an internet connection limited to an
**allowlist** that you control. Each box, with its settings, is a
**profile**.

## Start here

| Read | To |
|---|---|
| [Getting started](getting-started.md) | go from nothing (not even Docker) to Claude Code running in a sandbox, step by step |
| [Staying safe](staying-safe.md) | know what HardCode protects you from, what it doesn't, and how to use it so you stay protected |

## Using HardCode

| Read | To |
|---|---|
| [Profiles](profiles.md) | run several sandboxes side by side; copy, reset and delete them; change their settings |
| [The allowlist](allowlist.md) | let a sandbox reach what it needs, and nothing more; read what was blocked |
| [Inside the sandbox](vscode-image.md) | know what's installed, what is kept and what isn't, how Claude Code and Codex are set up, and how to use Docker inside |
| [The desktop app](app.md) | find your way around the app, page by page, with pictures |
| [The `hardcode` command](cli.md) | use the command line: everyday commands, then every command, option and `--json` output |
| [Troubleshooting](troubleshooting.md) | fix what isn't working |
| [Security model](security.md) | read the complete account of what is enforced, where, and what isn't |

## For teams

| Read | To |
|---|---|
| [Kubernetes](kubernetes.md) | run everyone's sandboxes on a cluster: the Helm chart, the `Profile` resource, the operator |
| [The admin UI and API](admin.md) | manage a team's profiles, allowlists and admins; script it with the API |

## For contributors

| Read | To |
|---|---|
| [Architecture](architecture.md) | understand the containers, networks and the path a request takes |
| [Development](development.md) | find your way around the repository and its conventions; add a language, a command or a route; regenerate the screenshots |
| [Testing, CI and releases](testing.md) | run the test suites, and see what CI and a release check |
| [Releasing](releasing.md) | make a release, and set up the app's updater |

The repository's [README](../README.md) is the short tour;
[AGENTS.md](../AGENTS.md) sets the ground rules for anyone, person or
agent, changing the code.
