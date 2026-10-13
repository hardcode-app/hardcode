# HardCode documentation

HardCode is a sandboxed development environment: VS Code in the browser with
Claude Code, a rootless Docker daemon of its own, and network egress limited to
an allowlist of URLs enforced by an inspecting proxy and a filtering DNS.
Everything is organised in **profiles**, each a separate environment.

| Read this | When you want to |
|---|---|
| [Getting started](getting-started.md) | install, start the first sandbox, open VS Code |
| [Architecture](architecture.md) | understand the containers, networks and the path a request takes |
| [Profiles](profiles.md) | run several environments side by side; copy, switch, edit all at once |
| [The allowlist](allowlist.md) | control what the sandbox can reach: entry format, read-only vs `rw`, SSH, blocked traffic |
| [The `hardcode` command](cli.md) | every command, option and `--json` output |
| [The desktop app](app.md) | the HardCode window, view by view (with screenshots) |
| [The VS Code image](vscode-image.md) | languages, VS Code extensions, what persists, Docker inside the sandbox |
| [Kubernetes](kubernetes.md) | the Helm chart, the `Profile` resource and the operator |
| [Admin UI and API](admin.md) | sign-in with TOTP, managing profiles for a team, the HTTP API (with screenshots) |
| [Security model](security.md) | what is enforced, where, and what isn't |
| [Testing, CI and releases](testing.md) | the test suites, how to run them here, what CI and releases do |
| [Development](development.md) | repository layout, conventions, how to add a language, command or route, regenerating screenshots |
| [Troubleshooting](troubleshooting.md) | symptoms and fixes |

The repository's [README](../README.md) is the short tour; [AGENTS.md](../AGENTS.md)
sets the ground rules for anyone (or any agent) changing the code.
