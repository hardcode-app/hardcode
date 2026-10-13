# Ground rules for working on HardCode

This file is for anyone changing this repository, people and coding agents
alike. HardCode is a security boundary that people run untrusted code and AI
agents inside; sloppy changes here have consequences. Read it before you
change anything, and keep it current.

## 1. Understand before you act

- **Discussion first, then execution.** When the owner is thinking out loud
  or asking for options, answer the question. Don't change files, start
  containers or touch the running stack until told to go. Once told, move
  fast and finish.
- **Read the code you're changing**, and the tests and docs that describe
  it. [docs/architecture.md](docs/architecture.md) and
  [docs/development.md](docs/development.md) are the map.
- **Don't add what wasn't asked for**: no extra tools, packages, extensions,
  features or "while I'm here" refactors. Propose them instead.

## 2. Every change comes with its tests and its docs

A change is complete when all of these are true:

- **Tests** cover the new behaviour and the old tests still pass. Where
  they live: `cli/tests/` (every CLI function; `docker compose` is faked
  with `FakeRunner`), `admin/tests/` (`FakeK8s`, Flask test client),
  `app/src-tauri/src/lib.rs` (`mod tests`), the suites in `tests/*.py`,
  and `tests/proxy.py` for anything that changes what the proxy enforces
  (it runs the real squid; the grammar corpus is `tests/allowlist-corpus.txt`).
  Run the suite for what you touched (`tests/run_all.py` runs everything)
  and **report the real result**, including failures and skips.
- **Docs** in `docs/` say what the code now does: the page for the part you
  changed, the CLI/API reference tables, the README if a headline fact
  changed. A UI change regenerates its **screenshots**
  (`docs/tools/screenshots.sh`) and you look at them.
- **Comments and help text** (`USAGE` in `cli.py`, the template files'
  comments, `--help` strings) match.
- Nothing is left half-done: a renamed module is renamed everywhere, a
  removed tool is removed from CI, Dependabot, `.gitignore`, the README's
  layout and this file.

## 3. Keep the security model intact

- The allowlist is the policy. Defaults stay narrow: **never allow a whole
  git host**, registries stay read-only, `rw` only where `POST` is required.
  Changes to the entries HardCode provides (`EVERY_PROFILE` and the
  languages' `domains` in `cli/hardcode_cli/langs.py`), or to
  `template/allowlist.txt` or `helm/hardcode/files/allowlist.txt`,
  need a reason in the commit message: they reach every existing profile at
  its next start.
- Four places define what an entry means and must agree:
  `images/squid/allowlist-render.sh`, `cli/hardcode_cli/allowlist.py`, the
  template's comments, `docs/allowlist.md`; a grammar change adds lines to
  `tests/allowlist-corpus.txt`, which both implementations are run over.
- Languages are first class: the catalogue in `cli/hardcode_cli/langs.py`
  says what each one installs, opens in the allowlist and brings as
  extensions. The Dockerfile's build args and languages label,
  `compose.yaml`'s `x-languages`, the template `.env` and the CRD's
  `spec.languages` enum repeat it, and the tests check that they agree.
- The allowlist entries HardCode provides are only in the catalogue:
  every profile's blocks and each language's. A profile stores its own
  entries and the provided ones it switches off (`off <entry>`), never a
  copy of the provided ones, which the CLI and the operator add whenever
  they give the proxy its rules. The template's and the chart's
  `allowlist.txt` are a profile's own part (the two files stay identical),
  with no entry allowed. A language's registries belong in its block,
  nowhere else.
- Extensions are never baked into the image: the running VS Code container
  installs the profile's list.
- A profile stores only what differs. Its `.env` overrides `compose.yaml`'s
  defaults (every setting is a `${NAME:-default}` there and a commented
  line in `template/.env`, which the tests keep in step), and Compose
  changes go in its `compose.override.yaml`. The stack (`compose.yaml`,
  `images/`, `template/`) ships with the command and is written out per
  version; never copy it into a profile.
- Don't weaken the containers' settings (capabilities, `internal` networks,
  `ip_forward`, read-only filesystems, `no-new-privileges`, resource limits,
  `max_filedescriptors`) without saying so explicitly and updating
  [docs/security.md](docs/security.md).
- The app's updater trusts one key, `release/updater.pub`: changing it is
  a security change, and its commit says why. Its private key is in the
  repository's secrets and the owner's safekeeping, never in a checkout.
  Only the release workflow sets the updater up (`release/release.py
  updater-config`); no configuration in the checkout does, and a test
  checks.
- No secrets in the repository. `profiles/*` is gitignored for a reason;
  don't commit anything from it.
- VS Code extensions: domain-verified publishers only. A reviewed exception
  is marked `unverified-ok` with a comment saying why.

## 4. Package and dependency policy

- Ubuntu 26.04 archive packages first; vendor sources only where the
  archive has nothing usable, with the reason next to the line.
- The CLI stays standard-library Python 3.9+; the admin stays Flask plus
  what the archive ships; the app's page stays plain JS; Rust stays the thin
  shell it is. A new runtime dependency anywhere is a discussion, not a
  commit.
- Pin what you add (image tags, chart versions, actions) and let Dependabot
  move it.
- **Everything builds and tests in Ubuntu 26.04 containers. Always.** The
  build image (`images/build/Dockerfile`) is the environment; `tests/*.py`
  and `app/build.sh` run themselves in it through `images/build/run.sh`.
  Don't `apt install`, `pip install` or `cargo install` on the machine to
  get a build or a test out, and don't run a suite on the host "just this
  once"; add what's missing to the Dockerfile, pinned, with the reason.
  The archive's package first; a vendor binary only when the archive has
  nothing, checksum-pinned.

## 5. Don't hurt the machine you're on

- **Every container, cluster node and heavy process gets a hard memory cap**
  (`docker run --memory`, `docker update --memory` on kind nodes,
  `systemd-run --user --scope -p MemoryMax=…`). A proxy once took 58 GB of a
  developer's RAM from inside a test cluster. Never again.
- Tests use a throwaway home (`--config` or `HARDCODE_CONFIG` naming a
  temporary folder) and temporary directories; **never touch the owner's own
  profiles** (the default home, `~/.config/hardcode`) or their running stack.
  A command run without `--config` or `HARDCODE_CONFIG` *is* on the owner's
  profiles. Other containers and programs on the host may hold ports (on the
  owner's machine, 8000 belongs to something else).
- **Outside the default home, a profile's Compose project and VS Code image
  carry the home's id** (`hardcode-<id>-<profile>`), so a test's `default`
  can't be the owner's stack. A test that reaches the real Docker daemon
  still prefers random profile names, or runs with no daemon at all
  (`DOCKER_HOST` pointing nowhere), as `tests/cli.py` and `tests/app.py` do.
- Clean up what you start: clusters, containers, port-forwards, temp dirs.
- Build the big VS Code image only when the change needs it; CI builds the
  minimal variant on purpose.

## 6. Code style

Code here is written for the next person who has to read it. Clever and
compact loses to plain and spaced out.

- **Everywhere**: match the surrounding code. One statement per line.
  Functions read as paragraphs: a blank line between the steps, and a
  comment above a step when what it does, or what it guards against, isn't
  obvious from the code. Comments say why; for hard-to-read code (a regex,
  a pipeline, a security rule) they also say what. Names are spelled out
  (`profile`, not `p`). A long expression gets named intermediate values
  rather than one more level of nesting.
- **Python**: docstrings in full form on every module, class and function,
  test methods excepted where the name says it all:

  ```python
  def allocate(root: Path) -> tuple[int, str]:
      """
      A VS Code port and an internal subnet that no other profile uses.

      The details, when there are any.
      """
  ```

  Small modules, dataclasses, injected `runner`/`clock`/API for
  testability, exceptions with messages a user can act on, no bare `except`
  (the operator's loops are the documented exception, and they log). A call
  that doesn't fit on one line gets one argument per line, with a trailing
  comma. Every text file is read and written with `encoding="utf-8"`:
  Windows' default isn't UTF-8 (`tests/lint.py` checks). Formatted by
  `tests/lint.py --fix` (ruff, then isort, as `pyproject.toml` says, at 120
  columns); `tests/lint.py` must pass.
- **JavaScript**: plain JS, no build step. A `/** */` comment on every
  function, braces around every `if` and loop body, `esc()` on every value
  put into HTML. In HTML templates, whitespace between inline elements
  renders: break lines only between block elements, and check the
  screenshots haven't moved (`docs/tools/screenshots.sh`).
- **Rust**: `///` docs on every item; formatted by `cargo fmt` (checked by
  `tests/lint.py`).
- **Shell**: a header comment saying what the script does and how it is
  run; `set -euo pipefail`; a comment above each function; long commands
  split with one option (or option and value) per line; quote everything;
  `mktemp` + `trap` cleanup; literal paths rather than `rm -rf $VAR/*`.
- **Tests**: the suites in `tests/` are Python scripts on
  `tests/harness.py`. A helper program a suite runs in a container is a
  real file in `tests/fixtures/`, not a string inside the suite.
- Keep platform reach: the CLI runs on Linux, macOS and Windows (paths,
  `~`, drive letters, no `cd` in subprocesses).
- Keep the three front ends consistent: the same words for the same things
  (profile, allowlist, entry, read-only/`rw`/`ssh`, forward, mount,
  suspended).

## 7. Git

- **Commit only when asked.** Stage what you changed, nothing from
  `profiles/`, no build output. Don't rewrite history.
- Commit messages say what and why, with the test evidence; mention policy
  changes (allowlist defaults, security settings) explicitly.
- **Conventional Commits.** The subject is `type(scope): summary`: types
  `feat`, `fix`, `docs`, `test`, `ci`, `build`, `refactor`, `perf`,
  `chore`, `revert`, `style`; the scope, when there is one, the part
  changed (`cli`, `app`, `admin`, `chart`, `images`, `proxy`, `release`,
  `ci`, `deps`). A breaking change gets `!` after the type and a
  `BREAKING CHANGE:` footer saying how to move over. A commit that bundles
  several changes lists them in its body, a line each in the same form,
  so that changelog tools can split it. `tests/commits.py` checks every
  commit, in CI too.
- **No attribution.** Agents never add `Co-Authored-By`, "Generated with"
  or any other attribution line to a commit message or a pull request's
  description; the commits are the owner's.
- `main` is the default branch; the release workflow runs on `v*` tags
  ([docs/releasing.md](docs/releasing.md)).

## 8. Before you say you're done

```
[ ] the relevant suites ran, and the report says exactly what passed, failed or was skipped
[ ] the app or the CLI it bundles changed: app/build.sh bundle rebuilt the release,
    run on the host so it also refreshes the launcher (the owner starts
    app/src-tauri/target/release/hardcode-app, with its bundled CLI, from it)
[ ] docs/ updated; screenshots regenerated if a page changed; README still true
[ ] no stray files (scratch output, caches, screenshots in the wrong place)
[ ] nothing unrequested was added; nothing requested was quietly dropped
[ ] this file still describes how things are done
```
