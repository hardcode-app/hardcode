"""
The ``hardcode`` command: argument parsing, and one handler per command.

The handlers stay thin. The work is done by ``Ops`` (one profile's files and
containers) and the modules beneath it; a handler picks the profiles to act
on, calls an operation, and reports the result as text or JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from . import (
    allowlist,
    applier,
    bounded,
    devices,
    dockerfile,
    envfile,
    home,
    lan,
    langs,
    override,
    packages,
    profiles,
    stack,
    version_text,
    yamlio,
)
from .compose import Compose, ComposeError, Runner, docker_executable, docker_subnets
from .ops import Ops, Result

USAGE = """\
hardcode [-p NAME | --all] [--json] [--config DIR] <command> [args]

  up [--build]            build if needed and start the profile      down [-v]  containers (-v: volumes too)
  start · stop · restart · build · logs [-f] [--tail N] [svc|applier] · ps · shell · open · compose <docker compose args>
  profiles                list profiles                              status   the same
  profile add NAME --lang python,go,... | --from P · profile remove NAME [--yes]
  port <number>           move VS Code to another port on this machine
  languages               the languages and tools, and what each opens, installs and builds
  enable <lang>... · disable <lang>...  [--no-build]  (--all)          rebuild  (--all)  rebuild the VS Code image
  extensions              the VS Code extensions, and where each comes from
  extension add|remove <publisher.name>  (--all)
  packages                the profile's own Ubuntu packages          package add|remove <name>...  (--all)
  image [save -- <text>]  its own Dockerfile steps (FROM hardcode), and whether a rebuild is due
  token rotate  (--all)   a new VS Code token: every copy of the old one stops working
  allowlist               every entry: the profile's own, then those HardCode provides
  allow <entry> [rw|ssh]  allow a site (--all: every profile)        deny <entry> [ssh]
  allowlist enable|disable|remove <entry> [ssh]  (--all)             apply    make the changes waiting now
  lan                     names that may reach your own network      lan add|remove|check <name> [address]  (--all)
                          (allow <entry> --lan: let its name reach it, without asking)
  forward <port> [vscode|docker] [target]                              unforward <port>
  mount <local> <remote> [--ro]  (--all)                             unmount <remote>  (--all)
  docker on|off  (--all)    the Docker daemon beside the VS Code container (off: nothing privileged, no docker)
  devices                 this computer's devices the VS Code container may use (kvm, gpu, nvidia), on or off
  device on|off <name> [--yes]  switch one on (it weakens the sandbox: asks first, or --yes) or off
  blocked [--since 30m]   what the proxy refused                     show     everything about the profile (--json)
"""

# The commands that can act on every profile at once.
ALL_CAPABLE = {
    "allow",
    "deny",
    "allowlist",
    "lan",
    "apply",
    "mount",
    "unmount",
    "enable",
    "disable",
    "rebuild",
    "docker",
    "extension",
    "package",
    "token",
}

# Failures that are the user's to fix: printed as one line, without a traceback.
USER_ERRORS = (
    home.HomeError,
    stack.StackError,
    profiles.ProfileError,
    allowlist.AllowlistError,
    override.OverrideError,
    langs.LangError,
    packages.PackageError,
    dockerfile.DockerfileError,
    devices.DeviceError,
    ComposeError,
    yamlio.YamlError,
)


class CommandError(Exception):
    """
    A failure to report to the user as it is, without a traceback.
    """


def _print_error(message: str) -> None:
    print(message, file=sys.stderr)


class App:
    """
    What the command handlers share: the home (where the profiles are), the
    output mode, and the runner, printers, this computer as the devices see
    it and the start of a profile's applier (all injectable, for tests).
    """

    def __init__(
        self,
        root: Path,
        json_output: bool = False,
        runner: Runner = bounded.run,
        docker: Optional[str] = None,
        out: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        err: Callable[[str], None] = _print_error,
        ask: Optional[Callable[[str], str]] = None,
        resolve: lan.Resolver = lan.resolve,
        this_computer: lan.ThisComputer = lan.is_this_computer,
        host: Optional[devices.Host] = None,
        start_applier: Callable[[Path, profiles.Profile], None] = applier.start,
    ):
        self.root = root
        self.home_id = home.home_id(root)
        self.json_output = json_output
        self.runner = runner
        self.docker = docker
        self.out = out
        self.err = err
        self.sleep = sleep
        self.ask = ask
        self.resolve = resolve
        self.this_computer = this_computer
        self.host = host or devices.Host()
        self.start_applier = start_applier
        self.results: list[Result] = []

    def profile(self, name: str) -> profiles.Profile:
        return profiles.get(self.root, name, self.home_id)

    def ops(self, name: str) -> Ops:
        return Ops(
            self.root,
            self.profile(name),
            runner=self.runner,
            docker=self.docker,
            sleep=self.sleep,
            warn=self.err,
            resolve=self.resolve,
            this_computer=self.this_computer,
            host=self.host,
            start_applier=self.start_applier,
        )

    def compose(self, name: str) -> Compose:
        return Compose(self.profile(name), runner=self.runner, docker=self.docker, sleep=self.sleep)

    def taken_subnets(self) -> list[str]:
        """
        The subnets of Docker's networks, which a new profile's must not overlap.
        """
        try:
            docker = self.docker or docker_executable()
        except ComposeError:
            return []

        return docker_subnets(docker, self.runner)

    def create(self, name: str, source: str = "", languages: Optional[list[str]] = None) -> profiles.Created:
        """
        A new profile in the home, its subnet clear of Docker's networks.
        """
        return profiles.create(
            self.root,
            name,
            source,
            warn=self.err,
            languages=languages,
            taken_subnets=self.taken_subnets(),
        )

    def targets(self, name: str, everything: bool) -> list[str]:
        """
        The profiles a command acts on: *name*, or with ``--all`` every one.
        """
        return profiles.list_profiles(self.root) if everything else [name]

    def report(self, result: Result, prefix: str = "") -> None:
        """
        Record a result, and print it unless the output is JSON (which
        ``finish`` prints at the end, all results together).
        """
        self.results.append(result)
        if self.json_output:
            return

        line = f"{prefix}{result.message}"
        if result.restarts:
            verb = "restarts" if len(result.restarts) == 1 else "restart"
            line += f" ({', '.join(result.restarts)} {verb} in {applier.QUIET_SECONDS:g} seconds; hardcode apply: now)"
        elif result.reloading:
            line += f" (the proxy takes it in {applier.QUIET_SECONDS:g} seconds; hardcode apply: now)"
        elif result.applied is True:
            line += " (applied)"
        elif result.applied is False:
            line += f" (profile '{result.profile}' isn't running; applies when it starts)"

        self.out(line)

    def finish(self, extra: Optional[dict] = None) -> None:
        """
        Print the JSON document of every result, in JSON mode.
        """
        if not self.json_output:
            return

        payload = {"results": [asdict(result) for result in self.results]}
        if extra:
            payload.update(extra)

        self.out(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_compose(app: App, args: argparse.Namespace) -> int:
    """
    Pass any ``docker compose`` command through, for the profile.
    """
    return app.compose(args.profile).run(*args.args).returncode


# The lifecycle commands that start the profile's containers.
STARTS = ("up", "start", "restart")


def cmd_lifecycle(app: App, args: argparse.Namespace) -> int:
    """
    up, down, start, stop, restart, build and ps.
    """
    if args.command == "ps" and app.json_output:
        app.out(json.dumps(app.ops(args.profile).status(), indent=2))
        return 0

    extra = list(getattr(args, "args", []) or [])

    if args.command == "up":
        compose_args = ["up", "-d", *(["--build"] if args.build else []), *extra]
    elif args.command == "down":
        # Every service of the project goes, the Docker daemon's included
        # even while it is switched off.
        compose_args = ["--profile", "docker", "down", *(["-v"] if args.volumes else [])]
    elif args.command == "build":
        compose_args = ["build", *extra]
    elif args.command == "ps":
        compose_args = ["ps", "-a"]
    else:
        compose_args = [args.command]

    # Starting, stopping and building change the profile's containers, so
    # they wait for any other change to the profile, and make others wait.
    if args.command == "ps":
        return app.compose(args.profile).run(*compose_args).returncode

    # The first `up`, on a terminal, makes the profile: its languages are
    # the one thing it needs to be asked.
    if args.command == "up" and not profiles.exists(app.root, args.profile) and app.ask is not None:
        _first_profile(app, args.profile)

    with profiles.lock(app.profile(args.profile), waiting=app.err):
        # A start or a stop leaves nothing for the applier: the next start
        # takes every change written.
        if args.command != "build":
            applier.forget(app.profile(args.profile))

        if args.command in STARTS:
            return _start(app, args.profile, compose_args)

        return app.compose(args.profile).run(*compose_args).returncode


def _start(app: App, name: str, compose_args: list[str]) -> int:
    """
    up, start or restart, with the proxy's allowlist brought up to date
    first, so that the entries this HardCode provides (a language's new
    download host, after an update) are in force at once. What that changed
    is printed, one line per entry. Compose runs the one-shot renderer again
    as it starts the profile, so the proxy has the file, running before or
    not.

    A device of this computer the profile has on, which this computer can't
    give, stops the start before anything else, with why; the ones it can
    are written for Compose with their groups as they are now.
    """
    ops = app.ops(name)
    ops.check_devices()

    changes = ops.update_proxy_allowlist()
    for change in changes:
        app.err(f"allowlist: {change}")

    # The names on your network are looked up again; one that has moved is
    # only reported, and keeps reaching the address it was pinned to.
    for warning in ops.lan_warnings():
        app.err(f"on your network: {warning}")

    return ops.compose.run(*compose_args).returncode


def _first_profile(app: App, name: str) -> None:
    """
    Make the profile *name* on the first ``up``, asking which languages and
    tools it is built around; the tools are what an empty answer means.
    """
    languages = [lang.key for lang in langs.CATALOGUE if lang.kind == "language"]
    tools = [lang.key for lang in langs.CATALOGUE if lang.kind == "tool"]

    app.err(f"No profile '{name}' yet. Choose what it is built around: the image installs them,")
    app.err("the allowlist opens their registries, and VS Code gets their extensions.")
    app.err(f"  languages: {' '.join(languages)}")
    app.err(f"  tools:     {' '.join(tools)}")

    while True:
        answer = app.ask(f"Languages and tools, comma-separated [{','.join(tools)}]: ").strip()
        try:
            chosen = langs.parse_list(answer) if answer else tools
            break
        except langs.LangError as error:
            app.err(str(error))

    created = app.create(name, languages=chosen)
    app.err(f"profile '{name}' made (VS Code on port {created.port}); building its image")


def cmd_logs(app: App, args: argparse.Namespace) -> int:
    """
    Container logs: followed, or the last lines. With ``--json``, the last
    lines (200 unless ``--tail`` says otherwise) as one string, for the app.
    ``applier`` is the applier's log, which isn't a container's (``applier``).
    """
    if args.tail is not None and args.tail < 0:
        raise CommandError("--tail takes a number of lines, 0 or more")

    if args.services == ["applier"]:
        if args.follow:
            raise CommandError("the applier's log can't be followed: it has a line for each change")

        text = applier.log(app.profile(args.profile), 200 if args.tail is None else args.tail)
        if app.json_output:
            app.out(json.dumps({"services": args.services, "logs": text}, indent=2))
        else:
            app.out(text.rstrip("\n") or "the applier has made no changes yet")
        return 0

    if app.json_output:
        if args.follow:
            raise CommandError("logs can't be followed with --json")

        tail = 200 if args.tail is None else args.tail
        text = app.ops(args.profile).logs(args.services, tail)
        app.out(json.dumps({"services": args.services, "logs": text}, indent=2))
        return 0

    compose_args = ["logs"]
    if args.follow:
        compose_args.append("-f")
    if args.tail is not None:
        compose_args += ["--tail", str(args.tail)]

    return app.compose(args.profile).run(*compose_args, *args.services).returncode


def cmd_shell(app: App, args: argparse.Namespace) -> int:
    """
    An interactive login shell in the VS Code container.
    """
    return app.compose(args.profile).run("exec", "vscode", "bash", "-l").returncode


def cmd_open(app: App, args: argparse.Namespace) -> int:
    """
    Print the VS Code URL, with its token, and open it in the browser.
    """
    name = args.profile
    compose = app.compose(name)

    token = compose.vscode_token()
    if not token:
        vscode_running = any(service.service == "vscode" and service.state == "running" for service in compose.ps())
        if vscode_running:
            raise CommandError("VS Code is still starting: it hasn't written its token yet")

        raise CommandError(f"profile '{name}' isn't running (hardcode -p {name} up)")

    # Where the gateway really publishes VS Code, which is what the browser
    # needs: VSCODE_BIND may name another address, and a .env edited since
    # the start doesn't move a running gateway.
    address = compose.published("gateway", 8000)
    if not address:
        raise CommandError(f"VS Code isn't published: the gateway isn't running (hardcode -p {name} logs gateway)")

    url = f"http://{address}/?tkn={token}"
    app.report(Result(name, url, data={"url": url}))
    app.finish()

    if not args.no_browser and not app.json_output:
        webbrowser.open(url)

    return 0


def cmd_profiles(app: App, args: argparse.Namespace) -> int:
    """
    List the profiles with their port, subnet and state, and say where the
    home is and which HardCode this is (the app shows both).
    """
    rows = []
    for name in profiles.list_profiles(app.root):
        profile = app.profile(name)
        running = sum(1 for service in app.compose(name).ps() if service.state == "running")

        rows.append(
            {
                "name": name,
                "port": envfile.get(profile.env_file, "VSCODE_PORT"),
                "subnet": envfile.get(profile.env_file, "DEVNET_SUBNET"),
                "state": f"running ({running} containers)" if running else "stopped",
                "running": bool(running),
            }
        )

    if app.json_output:
        app.out(json.dumps({"home": str(app.root), "version": version_text(), "profiles": rows}, indent=2))
        return 0

    if not rows:
        app.out(f"no profiles yet in {app.root}: hardcode profile add default --lang python,docker")
        return 0

    app.out(f"{'PROFILE':<20} {'PORT':<8} {'SUBNET':<18} STATE")
    for row in rows:
        app.out(f"{row['name']:<20} {row['port']:<8} {row['subnet']:<18} {row['state']}")

    return 0


def cmd_profile(app: App, args: argparse.Namespace) -> int:
    """
    profile add, and profile remove.
    """
    if args.action == "add":
        languages = langs.parse_list(args.languages) if args.languages is not None else None
        created = app.create(args.name, args.source or "", languages)
        message = (
            f"profile {created.name}: created from {created.source or 'the template'} "
            f"(VS Code on port {created.port}, subnet {created.subnet}; "
            f"languages: {', '.join(created.languages) or 'none'})"
        )
        app.report(Result(created.name, message, data=asdict(created)))
        app.finish()
        return 0

    if not profiles.exists(app.root, args.name):
        raise CommandError(f"no profile '{args.name}'")

    if not args.yes and not _confirm_removal(args.name):
        app.out("cancelled")
        return 1

    with profiles.lock(app.profile(args.name), waiting=app.err):
        app.report(app.ops(args.name).destroy())
    app.finish()
    return 0


def _confirm_removal(name: str) -> bool:
    """
    Ask before deleting a profile; with no terminal to ask on, the answer is no.
    """
    question = f"Delete profile '{name}' with all its containers and volumes (home directory included)? [y/N] "

    try:
        answer = input(question)
    except EOFError:
        answer = ""

    return answer.strip().lower() in ("y", "yes")


def _for_each(
    app: App,
    args: argparse.Namespace,
    operation: Callable[[Ops], Result],
    files_only: bool = False,
) -> int:
    """
    Run *operation* on the profile, or on every profile with ``--all``.

    With ``--all``, a profile the operation fails on is reported as skipped
    and the others carry on; the command fails only if every profile did.

    An edit of the allowlist is *files_only*: it takes the lock on the
    profile's files alone, so containers being recreated (``applier``)
    don't hold it up (``profiles.files_lock``).
    """
    names = app.targets(args.profile, args.all)
    failures = 0
    hold = profiles.files_lock if files_only else profiles.lock

    for name in names:
        prefix = f"[{name}] " if args.all else ""

        try:
            # One change to a profile at a time: another one waits its turn.
            with hold(app.profile(name), waiting=app.err):
                app.report(operation(app.ops(name)), prefix)
        except USER_ERRORS as error:
            if not args.all:
                raise CommandError(str(error)) from error

            app.report(Result(name, f"skipped: {error}", changed=False), prefix)
            failures += 1

    app.finish()
    return 1 if failures == len(names) else 0


def cmd_allow(app: App, args: argparse.Namespace) -> int:
    """
    Allow an entry. When its name resolves to an address on your network,
    the proxy would refuse it there: ``--lan`` lets it reach that address,
    and on a terminal the command asks; otherwise it says how.
    """
    flag = args.flag or ""

    confirm = None
    if app.ask is not None and not args.lan:

        def confirm(name: str, address: str) -> bool:
            question = f"{name} resolves to {address}, on your network. Let it reach that address? [y/N] "
            return app.ask(question).strip().lower() in ("y", "yes")

    return _for_each(
        app,
        args,
        lambda ops: ops.allow(args.entry, flag, lan_ok=args.lan, confirm=confirm),
        files_only=True,
    )


def cmd_deny(app: App, args: argparse.Namespace) -> int:
    def deny(ops: Ops) -> Result:
        result = ops.deny(args.entry, ssh=args.flag == "ssh")

        # For one profile, denying what isn't there is an error worth an exit
        # code; with --all it is just how that profile is.
        if not result.changed and not args.all:
            where = ops.profile.allowlist_file.relative_to(app.root)
            raise CommandError(f"'{args.entry}' is not an active entry in {where}")

        return result

    return _for_each(app, args, deny, files_only=True)


def cmd_forward(app: App, args: argparse.Namespace) -> int:
    if args.all:
        raise CommandError("a host port can belong to one profile only; add forwards per profile")

    return _for_each(app, args, lambda ops: ops.forward(args.port, args.host, args.target))


def cmd_unforward(app: App, args: argparse.Namespace) -> int:
    if args.all:
        raise CommandError("forwards are per profile; use --profile")

    return _for_each(app, args, lambda ops: ops.unforward(args.port))


def cmd_mount(app: App, args: argparse.Namespace) -> int:
    return _for_each(app, args, lambda ops: ops.mount(args.local, args.remote, args.ro))


def cmd_unmount(app: App, args: argparse.Namespace) -> int:
    return _for_each(app, args, lambda ops: ops.unmount(args.remote))


def cmd_language(app: App, args: argparse.Namespace) -> int:
    """
    enable and disable: one or more languages, with everything that follows.
    """
    enabled = args.command == "enable"
    changes = {langs.normalise(lang): enabled for lang in args.lang}

    return _for_each(app, args, lambda ops: ops.set_languages(changes, build=not args.no_build))


def cmd_languages(app: App, args: argparse.Namespace) -> int:
    """
    The catalogue, with the profile's choice and what its image was built
    with; just the catalogue when the profile doesn't exist yet, for the app
    to choose a new profile's languages from.
    """
    if not profiles.exists(app.root, args.profile):
        state = {"langs": langs.enabled_in({}), "rebuildNeeded": None}
        for lang in state["langs"]:
            lang["inImage"] = None
    else:
        state = app.ops(args.profile).state()

    if app.json_output:
        app.out(json.dumps({"languages": state["langs"], "rebuildNeeded": state["rebuildNeeded"]}, indent=2))
        return 0

    for lang in state["langs"]:
        mark = "on " if lang["enabled"] else "off"
        opens = f"opens {len(lang['domains'])} allowlist entries" if lang["domains"] else "opens nothing"
        brings = f"{len(lang['extensions'])} extensions" if lang["extensions"] else "no extensions"
        app.out(f"{mark}  {lang['key']:<17} {lang['name']:<24} {opens}, {brings}")

    if state["rebuildNeeded"]:
        app.out("\n" + _image_words(state, args.profile))

    return 0


# What each reason for a rebuild says (see Ops._rebuild_reasons).
REBUILD_REASONS = {
    "languages": "the languages or tools changed",
    "packages": "the Ubuntu packages changed",
    "dockerfile": "the Dockerfile steps changed",
}


def _image_words(state: dict, profile: str) -> str:
    """
    Whether the profile's image has what the profile says, in words, with
    the command that rebuilds it when it hasn't. *state* has the profile's
    ``image``, ``rebuildNeeded`` and ``rebuildReasons``, as ``Ops.state``
    and ``Ops.image_steps`` give them.
    """
    if state["image"]["built"] is False:
        return "no image yet: the first start builds it"
    if state["rebuildNeeded"] is None:
        return f"whether the image is up to date can't be told: hardcode -p {profile} rebuild builds it again"
    if not state["rebuildNeeded"]:
        return "the image is up to date"

    why = "; ".join(REBUILD_REASONS[reason] for reason in state["rebuildReasons"])
    return f"a rebuild is due ({why}): hardcode -p {profile} rebuild"


def cmd_packages(app: App, args: argparse.Namespace) -> int:
    """
    The profile's own Ubuntu packages, and whether its image has them.
    """
    state = app.ops(args.profile).state()

    if app.json_output:
        payload = {
            "packages": state["packages"],
            "image": state["image"]["packages"],
            "rebuildNeeded": state["rebuildNeeded"],
            "rebuildReasons": state["rebuildReasons"],
        }
        app.out(json.dumps(payload, indent=2))
        return 0

    if not state["packages"]:
        app.out("no Ubuntu packages of its own: hardcode package add NAME... adds some")

    for name in state["packages"]:
        app.out(name)

    if state["rebuildNeeded"]:
        app.out("\n" + _image_words(state, args.profile))

    return 0


def cmd_package(app: App, args: argparse.Namespace) -> int:
    """
    package add and package remove: one or more names, which the image
    installs (or loses) at its next rebuild.
    """
    if args.action == "add":
        return _for_each(app, args, lambda ops: ops.add_packages(args.names))

    return _for_each(app, args, lambda ops: ops.remove_packages(args.names))


def cmd_image(app: App, args: argparse.Namespace) -> int:
    """
    The profile's own Dockerfile steps: where they are, and whether a
    rebuild is due. With ``save``, write them: the app's Image page does,
    and on the command line the file is edited as it is.
    """
    if args.action == "save":
        if args.text is None:
            raise CommandError("image save needs the Dockerfile's text: hardcode image save -- TEXT")

        return _for_each(app, args, lambda ops: ops.save_image_steps(args.text))

    steps = app.ops(args.profile).image_steps()

    if app.json_output:
        app.out(json.dumps(steps, indent=2))
        return 0

    if steps["exists"]:
        app.out(f"Dockerfile steps: {steps['path']}")
        app.out(f"built FROM hardcode (HardCode's image); the files they COPY go in {steps['folder']}")
    else:
        app.out(f"Dockerfile steps: none: write them in {steps['path']}, starting FROM hardcode")

    app.out(_image_words(steps, args.profile))
    return 0


def cmd_extensions(app: App, args: argparse.Namespace) -> int:
    """
    Every extension the VS Code container gets, where it comes from, and,
    while the container runs, how far its installing has got.
    """
    ops = app.ops(args.profile)
    rows = ops.extensions()
    status = ops.extension_status()
    refused = (status or {}).get("refused") or []

    if app.json_output:
        app.out(json.dumps({"extensions": rows, "refused": refused, "status": status}, indent=2))
        return 0

    pending = set((status or {}).get("pending") or [])
    failed = set((status or {}).get("failed") or [])

    for row in rows:
        if not row["enabled"]:
            continue

        source = langs.BY_KEY[row["source"]].name if row["source"] in langs.BY_KEY else row["source"]
        note = ""
        if row["id"] in refused:
            note = " (refused: unverified publisher)"
        elif row["id"] in pending:
            note = " (installing)"
        elif row["id"] in failed:
            note = " (failed; tried again shortly)"
        app.out(f"{row['id']:<40} {source}{note}")

    if status and status.get("state") == "installing":
        app.out(f"\ninstalling: {status.get('installed')} of {status.get('total')} in place")

    return 0


def cmd_token(app: App, args: argparse.Namespace) -> int:
    """
    token rotate: a new VS Code connection token.
    """
    return _for_each(app, args, lambda ops: ops.rotate_token())


def cmd_extension(app: App, args: argparse.Namespace) -> int:
    """
    extension add and extension remove.
    """
    if args.action == "add":
        return _for_each(app, args, lambda ops: ops.add_extension(args.id))

    return _for_each(app, args, lambda ops: ops.remove_extension(args.id))


def cmd_docker(app: App, args: argparse.Namespace) -> int:
    return _for_each(app, args, lambda ops: ops.set_docker(args.state == "on"))


def cmd_devices(app: App, args: argparse.Namespace) -> int:
    """
    The devices of this computer the profile's VS Code container may be
    given: each one on or off, and whether this computer has it, or why not.
    """
    rows = app.ops(args.profile).device_states()

    if app.json_output:
        app.out(json.dumps({"devices": rows}, indent=2))
        return 0

    for row in rows:
        mark = "on " if row["enabled"] else "off"
        where = "available here" if row["available"] else f"not available here: {row['reason']}"

        # What it gives, and under it whether this computer has it.
        app.out(f"{mark}  {row['key']:<7} {row['summary']}")
        app.out(f"{'':<13}{where}")

    on = [row["key"] for row in rows if row["enabled"]]
    if on:
        app.out(f"\non, each weakening the sandbox: {', '.join(on)} (hardcode device off NAME takes one away)")

    return 0


def cmd_device(app: App, args: argparse.Namespace) -> int:
    """
    device on and device off. On says first, on a terminal, what the device
    gives code in the sandbox, and asks; without a terminal (the app, a
    script, --json) it needs --yes, and refuses without it. Off asks nothing.
    """
    enabled = args.state == "on"
    declined: list[str] = []

    def confirm(device: devices.Device) -> bool:
        if args.yes:
            return True

        if app.ask is None:
            raise devices.DeviceError(
                f"device on {device.key} weakens the sandbox, and there is no terminal to ask on: "
                f"hardcode -p {args.profile} device on {device.key} --yes, once you have read what it gives "
                "(hardcode devices)"
            )

        app.err(f"Turning {device.key} on weakens profile '{args.profile}''s sandbox. {device.warning}")
        answer = app.ask(f"Turn {device.key} on for profile '{args.profile}'? [y/N] ")

        agreed = answer.strip().lower() in ("y", "yes")
        if not agreed:
            declined.append(device.key)
        return agreed

    code = _for_each(app, args, lambda ops: ops.set_device(args.name, enabled, confirm))

    # A no is the user's, as it is for removing a profile: not done.
    return 1 if declined else code


def cmd_allowlist(app: App, args: argparse.Namespace) -> int:
    """
    Without an action, every entry the profile's allowlist has; with one,
    enable, disable or remove an entry.
    """
    if args.action is None:
        return _list_allowlist(app, args)

    if not args.entry:
        raise CommandError(f"allowlist {args.action} needs an entry: hardcode allowlist {args.action} <entry> [ssh]")

    ssh = args.flag == "ssh"

    if args.action == "remove":
        return _for_each(app, args, lambda ops: ops.remove_entry(args.entry, ssh=ssh), files_only=True)

    enabled = args.action == "enable"
    return _for_each(app, args, lambda ops: ops.set_entry_enabled(args.entry, enabled, ssh=ssh), files_only=True)


def _list_allowlist(app: App, args: argparse.Namespace) -> int:
    """
    Every entry of one profile's allowlist, as the app and the admin show
    them: its own, then the provided ones block by block, each on or off.
    """
    if args.all:
        raise CommandError("the list is one profile's: -p NAME picks which")

    ops = app.ops(args.profile)
    entries = ops.allowlist_entries()
    pins = ops.pins()

    if app.json_output:
        payload = {"allowlist": allowlist.as_dicts(entries), "lan": [pin.as_dict() for pin in pins]}
        app.out(json.dumps(payload, indent=2))
        return 0

    title = None
    for item in entries:
        if _section_title(item) != title:
            app.out(("" if title is None else "\n") + _section_title(item))
            title = _section_title(item)

        state = "on " if item.enabled else "off"
        invalid = "   <- invalid: the proxy won't start with it" if item.invalid else ""
        app.out(f"  {state}  {allowlist.format_entry(item.entry, item.flag, item.comment)}{invalid}")

    if pins:
        app.out("\nOn your network (names that may reach an address there, in allowlist.txt)")
    for pin in pins:
        invalid = f"   <- invalid: {pin.problem}" if pin.problem else ""
        app.out(f"  {pin.name:<40} {pin.address}{invalid}")

    return 0


def _section_title(item: allowlist.Entry) -> str:
    """
    The heading an entry is listed under: its block and where that comes
    from, or the section of the profile's own allowlist.txt it is in.
    """
    if item.provided and not item.language:
        return f"{item.section} (every profile)"
    if item.provided:
        return f"{item.section} (comes with the {langs.BY_KEY[item.language].kind})"

    return f"{item.section or 'Custom'} (this profile's own, in allowlist.txt)"


def cmd_lan(app: App, args: argparse.Namespace) -> int:
    """
    The names the profile lets reach your network, each looked up again;
    with an action, add or remove one, or check what a name resolves to.
    """
    if args.action in ("add", "remove", "check") and not args.name:
        raise CommandError(f"lan {args.action} needs a name: hardcode lan {args.action} <name>")
    if args.address and args.action != "add":
        raise CommandError("only lan add takes an address: hardcode lan add <name> [address]")

    if args.action == "add":
        return _for_each(app, args, lambda ops: ops.add_pin(args.name, args.address or ""), files_only=True)
    if args.action == "remove":
        return _for_each(app, args, lambda ops: ops.remove_pin(args.name), files_only=True)

    if args.all:
        raise CommandError("the list is one profile's: -p NAME picks which")

    ops = app.ops(args.profile)

    if args.action == "check":
        found = ops.check_name(args.name)
        if app.json_output:
            app.out(json.dumps(found, indent=2))
        else:
            app.out(_checked(found))
        return 0

    states = ops.lan_states()
    if app.json_output:
        app.out(json.dumps({"lan": states}, indent=2))
        return 0

    if not states:
        app.out("nothing is on your network: hardcode lan add <name> lets one name reach its address there")

    for item in states:
        app.out(f"  {item['name']:<40} {item['address']:<20} {_lan_state(item)}")

    return 0


def _lan_state(item: dict) -> str:
    """
    A name on your network's state, in words.
    """
    if item["problem"]:
        return f"invalid: {item['problem']}"
    if item["state"] == "moved":
        return f"now resolves to {', '.join(item['resolves'])}"
    if item["state"] == "unresolved":
        return "ok (doesn't resolve from here)"
    if item["state"] == "this-computer":
        return "left out: this computer, which a sandbox never reaches"

    return "ok"


def _checked(found: dict) -> str:
    """
    What ``lan check`` found about a name, in words.
    """
    name = found["name"]
    if found["pinned"]:
        return f"{name} is on your network, at {found['pinned']}"
    if found["address"]:
        return f"{name} resolves to {found['address']}, on your network: hardcode lan add {name} lets it reach it"
    if found["refused"]:
        return found["refused"]
    if found["resolves"]:
        return f"{name} resolves to {', '.join(found['resolves'])}, not on your network"

    return f"{name} doesn't resolve from here"


def cmd_port(app: App, args: argparse.Namespace) -> int:
    """
    Move the profile's VS Code to another port on this machine.
    """
    return _for_each(app, args, lambda ops: ops.set_port(args.port))


def cmd_apply(app: App, args: argparse.Namespace) -> int:
    """
    Make the changes waiting now: the proxy's allowlist, and the containers
    waiting to be recreated.
    """
    return _for_each(app, args, lambda ops: ops.apply())


def cmd_applier(app: App, args: argparse.Namespace) -> int:
    """
    The applier of a running profile, which a change that recreates
    containers starts (``applier``); nobody runs it by hand.
    """
    ops = app.ops(args.profile)
    applier.run(ops.profile, ops.recreate_for, ops.compose.running)
    return 0


def cmd_rebuild(app: App, args: argparse.Namespace) -> int:
    return _for_each(app, args, lambda ops: ops.rebuild())


def cmd_show(app: App, args: argparse.Namespace) -> int:
    """
    The profile's state: everything, for the app (``--json``), or a summary.
    """
    state = app.ops(args.profile).state()
    state["profiles"] = [
        {"name": name, "port": envfile.get(app.profile(name).env_file, "VSCODE_PORT")}
        for name in profiles.list_profiles(app.root)
    ]

    if app.json_output:
        app.out(json.dumps(state, indent=2))
        return 0

    enabled = sum(1 for entry in state["allowlist"] if entry["enabled"])
    disabled = len(state["allowlist"]) - enabled
    forwards = [f"{forward['port']}->{forward['host']}:{forward['target']}" for forward in state["forwards"]]
    mounts = [
        f"{mount['local']} -> {mount['remote']}{' (ro)' if mount['mode'] == 'ro' else ''}" for mount in state["mounts"]
    ]
    languages = [lang["key"] for lang in state["langs"] if lang["enabled"]]
    extensions = [row["id"] for row in state["extensions"] if row["enabled"]]
    steps = state["dockerfile"]["path"] if state["dockerfile"]["exists"] else "none (HardCode's image as it is)"
    devices_on = [row["key"] for row in state["devices"] if row["enabled"]]

    app.out(f"profile {state['profile']}  (VS Code port {state['vscodePort']})")
    app.out("languages: " + (", ".join(languages) or "none"))
    app.out("packages:  " + (" ".join(state["packages"]) or "none"))
    app.out(f"dockerfile: {steps}")
    app.out(f"image:     {_image_words(state, state['profile'])}")
    app.out(f"extensions: {len(extensions)}")
    app.out(f"allowlist: {enabled} entries enabled, {disabled} disabled")
    app.out("forwards:  " + (", ".join(forwards) or "none"))
    app.out("mounts:    " + (", ".join(mounts) or "none"))
    app.out("docker:      " + ("on" if state["docker"] else "off (no Docker daemon; nothing privileged)"))
    app.out("devices:   " + (f"{', '.join(devices_on)} (each weakens the sandbox)" if devices_on else "none"))

    return 0


def cmd_blocked(app: App, args: argparse.Namespace) -> int:
    """
    What the profile's proxy and DNS filter refused recently.
    """
    items = app.ops(args.profile).blocked(args.since)

    if app.json_output:
        app.out(json.dumps({"blocked": items}, indent=2))
        return 0

    if not items:
        app.out("nothing blocked in the last " + args.since)

    for item in items:
        app.out(f"{item['count']:>4}  {item['kind']:<4} {item['method']:<8} {item['url'] or item['target']}")

    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _shared_options(parser: argparse.ArgumentParser, suppress: bool) -> None:
    """
    Add ``-p``, ``--all``, ``--json`` and ``--config``.

    They are accepted before and after the command name. On the subcommands
    they default to SUPPRESS, so a value given before the command survives
    unless it is given again after it.
    """
    if suppress:
        profile_default = all_default = json_default = config_default = argparse.SUPPRESS
    else:
        profile_default = os.environ.get("HARDCODE_PROFILE", "default")
        all_default = json_default = False
        config_default = None

    parser.add_argument(
        "-p",
        "--profile",
        default=profile_default,
        help="profile to act on (default: $HARDCODE_PROFILE or 'default')",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=all_default,
        help="act on every profile (allow, deny, allowlist, lan, apply, mount, unmount, enable, disable, rebuild, "
        "docker, extension, package, token)",
    )
    parser.add_argument("--json", action="store_true", default=json_default, help="machine-readable output")
    parser.add_argument(
        "--config",
        default=config_default,
        metavar="DIR",
        help="the folder HardCode keeps its profiles in (default: $HARDCODE_CONFIG, else ~/.config/hardcode or "
        "the platform's equivalent)",
    )


def _add_lifecycle_commands(sub) -> None:
    for name in ("start", "stop", "restart", "ps"):
        sub.add_parser(name, help=f"docker compose {name}").set_defaults(func=cmd_lifecycle)

    up = sub.add_parser("up", help="start the profile (building the images if needed)")
    up.add_argument("--build", action="store_true", help="rebuild the images first")
    up.add_argument("args", nargs="*", help="services")
    up.set_defaults(func=cmd_lifecycle)

    down = sub.add_parser("down", help="remove the profile's containers")
    down.add_argument("-v", "--volumes", action="store_true", help="and its volumes (home directory included)")
    down.set_defaults(func=cmd_lifecycle)

    build = sub.add_parser("build", help="build the profile's images")
    build.add_argument("args", nargs="*", help="services")
    build.set_defaults(func=cmd_lifecycle)

    logs = sub.add_parser("logs", help="container logs")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("--tail", type=int, metavar="N", help="only the last N lines of each")
    logs.add_argument("services", nargs="*", help="services, or applier: the applier's log")
    logs.set_defaults(func=cmd_logs)

    sub.add_parser("shell", help="a shell in the VS Code container").set_defaults(func=cmd_shell)

    open_ = sub.add_parser("open", help="print the VS Code URL and open it")
    open_.add_argument("--no-browser", action="store_true")
    open_.set_defaults(func=cmd_open)

    compose = sub.add_parser("compose", help="any docker compose command for the profile")
    compose.add_argument("args", nargs=argparse.REMAINDER)
    compose.set_defaults(func=cmd_compose)


def _add_profile_commands(sub) -> None:
    for name in ("profiles", "status"):
        sub.add_parser(name, help="list profiles").set_defaults(func=cmd_profiles)

    profile = sub.add_parser("profile", help="add or remove a profile")
    actions = profile.add_subparsers(dest="action", metavar="add|remove")

    add = actions.add_parser("add")
    add.add_argument("name")
    add.add_argument("--from", dest="source", help="copy this profile instead of the template")
    add.add_argument(
        "--lang",
        dest="languages",
        metavar="LIST",
        help='the languages and tools to build it around, e.g. python,docker ("" for none); '
        "needed from the template, and a copy keeps its source's without it",
    )

    remove = actions.add_parser("remove")
    remove.add_argument("name")
    remove.add_argument("-y", "--yes", action="store_true", help="don't ask")

    profile.set_defaults(func=cmd_profile)


def _add_allowlist_commands(sub) -> None:
    ssh_help = "the host's SSH entry rather than its HTTPS one"

    allow = sub.add_parser("allow", help="allow a site through the proxy")
    allow.add_argument(
        "entry",
        help="example.com, .example.com, example.com/path, host[:port] with ssh, or github:OWNER/REPO",
    )
    allow.add_argument("flag", nargs="?", choices=["rw", "ssh"], help="rw: uploads too; ssh: an SSH tunnel")
    allow.add_argument(
        "--lan",
        action="store_true",
        help="if its name resolves to an address on your network, let it reach that address without asking",
    )
    allow.set_defaults(func=cmd_allow)

    deny = sub.add_parser("deny", help="comment an entry out")
    deny.add_argument("entry")
    deny.add_argument("flag", nargs="?", choices=["ssh"], help=ssh_help)
    deny.set_defaults(func=cmd_deny)

    allowlist_command = sub.add_parser(
        "allowlist",
        help="every entry (provided by block, and the profile's own); or enable, disable or remove one",
    )
    allowlist_command.add_argument("action", nargs="?", choices=["enable", "disable", "remove"])
    allowlist_command.add_argument("entry", nargs="?")
    allowlist_command.add_argument("flag", nargs="?", choices=["ssh"], help=ssh_help)
    allowlist_command.set_defaults(func=cmd_allowlist)

    lan_command = sub.add_parser(
        "lan",
        help="the names that may reach your own network; or add, remove or check one",
    )
    lan_command.add_argument("action", nargs="?", choices=["add", "remove", "check"])
    lan_command.add_argument("name", nargs="?", help="an exact name: gitlab.example.com (a path or port is dropped)")
    lan_command.add_argument("address", nargs="?", help="with add: its address (default: what it resolves to here)")
    lan_command.set_defaults(func=cmd_lan)

    sub.add_parser(
        "apply",
        help="make the changes waiting now: the proxy's allowlist, and the containers waiting to be recreated "
        "(a change is made a few seconds after the last one)",
    ).set_defaults(func=cmd_apply)

    # The applier a change that recreates containers starts: not in the usage.
    sub.add_parser(applier.COMMAND).set_defaults(func=cmd_applier)

    blocked = sub.add_parser("blocked", help="what the proxy and DNS filter refused")
    blocked.add_argument("--since", default="30m", choices=["10m", "30m", "2h", "24h"])
    blocked.set_defaults(func=cmd_blocked)


def _add_network_commands(sub) -> None:
    port = sub.add_parser("port", help="move VS Code to another port on this machine")
    port.add_argument("port", type=int)
    port.set_defaults(func=cmd_port)

    forward = sub.add_parser("forward", help="publish a port to this machine")
    forward.add_argument("port", type=int)
    forward.add_argument("host", nargs="?", choices=["vscode", "docker"], default="vscode")
    forward.add_argument("target", nargs="?", type=int)
    forward.set_defaults(func=cmd_forward)

    unforward = sub.add_parser("unforward", help="remove a forward")
    unforward.add_argument("port", type=int)
    unforward.set_defaults(func=cmd_unforward)

    mount = sub.add_parser("mount", help="mount a host folder into the VS Code container (and docker)")
    mount.add_argument("local")
    mount.add_argument("remote", help="absolute, or relative to the home directory")
    mount.add_argument("--ro", action="store_true", help="read-only")
    mount.set_defaults(func=cmd_mount)

    unmount = sub.add_parser("unmount", help="remove a mount")
    unmount.add_argument("remote")
    unmount.set_defaults(func=cmd_unmount)


def _add_image_commands(sub) -> None:
    sub.add_parser("languages", help="the languages and tools, and what each brings").set_defaults(func=cmd_languages)

    for name in ("enable", "disable"):
        lang = sub.add_parser(name, help=f"{name} languages: allowlist, extensions and image")
        lang.add_argument("lang", nargs="+", help=", ".join(langs.LANGS))
        lang.add_argument("--no-build", action="store_true", help="don't rebuild the image now")
        lang.set_defaults(func=cmd_language)

    sub.add_parser("extensions", help="the VS Code extensions and their sources").set_defaults(func=cmd_extensions)

    token = sub.add_parser("token", help="VS Code's connection token: rotate gives it a new one")
    token.add_argument("action", choices=["rotate"])
    token.set_defaults(func=cmd_token)

    extension = sub.add_parser("extension", help="add or remove a VS Code extension of the profile's own")
    extension.add_argument("action", choices=["add", "remove"])
    extension.add_argument("id", help="publisher.name, as the Marketplace shows it")
    extension.set_defaults(func=cmd_extension)

    sub.add_parser("packages", help="the profile's own Ubuntu packages").set_defaults(func=cmd_packages)

    package = sub.add_parser(
        "package",
        help="add or remove Ubuntu packages of the profile's own (the image has them after its next rebuild)",
    )
    package.add_argument("action", choices=["add", "remove"])
    package.add_argument("names", nargs="+", metavar="NAME", help="a name in Ubuntu's archive: postgresql-client")
    package.set_defaults(func=cmd_package)

    image = sub.add_parser(
        "image",
        help="the profile's own Dockerfile steps (FROM hardcode) and whether a rebuild is due; save writes them",
    )
    image.add_argument("action", nargs="?", choices=["save"])
    image.add_argument("text", nargs="?", help="with save: the Dockerfile's text, after --")
    image.set_defaults(func=cmd_image)

    docker = sub.add_parser(
        "docker", help="the Docker daemon beside the VS Code container: on, or off for a harder sandbox"
    )
    docker.add_argument("state", choices=["on", "off"])
    docker.set_defaults(func=cmd_docker)

    rebuild = sub.add_parser("rebuild", help="rebuild the VS Code image and recreate the container")
    rebuild.set_defaults(func=cmd_rebuild)

    show = sub.add_parser(
        "show",
        help="the profile's allowlist, forwards, mounts, languages, packages, image and devices",
    )
    show.set_defaults(func=cmd_show)


def _add_device_commands(sub) -> None:
    """
    ``devices``, and ``device on|off NAME``: one profile's at a time, never
    ``--all``, as each device is a choice made for a profile of its own.
    """
    devices_command = sub.add_parser(
        "devices",
        help="this computer's devices the VS Code container may use: on or off, and whether this computer has them",
    )
    devices_command.set_defaults(func=cmd_devices)

    device = sub.add_parser(
        "device",
        help="give the VS Code container a device of this computer, or take it away (on weakens the sandbox)",
    )
    device.add_argument("state", choices=["on", "off"])
    device.add_argument("name", choices=list(devices.KEYS), help="kvm, gpu (AMD and Intel) or nvidia")
    device.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="on: don't ask (without a terminal, it is needed)",
    )
    device.set_defaults(func=cmd_device)


def build_parser() -> argparse.ArgumentParser:
    """
    The command line: the shared options, then one subcommand per command.
    """
    parser = argparse.ArgumentParser(
        prog="hardcode",
        usage=USAGE,
        description="HardCode: sandboxed development environments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _shared_options(parser, suppress=False)
    parser.add_argument("--version", action="version", version=f"hardcode {version_text()}")

    # Every subcommand inherits the shared options, so `hardcode allow x -p p`
    # works as well as `hardcode -p p allow x`.
    common = argparse.ArgumentParser(add_help=False)
    _shared_options(common, suppress=True)

    def subcommand_parser(**options) -> argparse.ArgumentParser:
        return argparse.ArgumentParser(parents=[common], **options)

    sub = parser.add_subparsers(dest="command", metavar="command", parser_class=subcommand_parser)

    _add_lifecycle_commands(sub)
    _add_profile_commands(sub)
    _add_allowlist_commands(sub)
    _add_network_commands(sub)
    _add_image_commands(sub)
    _add_device_commands(sub)

    return parser


def _ask(question: str) -> str:
    """
    Ask on the terminal; no answer (end of input) is an empty one.
    """
    try:
        return input(question)
    except EOFError:
        return ""


def main(
    argv: Optional[list[str]] = None,
    runner: Runner = bounded.run,
    docker: Optional[str] = None,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = _print_error,
    sleep: Callable[[float], None] = time.sleep,
    ask: Optional[Callable[[str], str]] = None,
    terminal: Optional[bool] = None,
    env: Optional[dict[str, str]] = None,
    resolve: lan.Resolver = lan.resolve,
    this_computer: lan.ThisComputer = lan.is_this_computer,
    host: Optional[devices.Host] = None,
    start_applier: Callable[[Path, profiles.Profile], None] = applier.start,
) -> int:
    """
    Run the command line in *argv* and return the exit code.

    The command asks a question (with *ask*, ``input`` by default) only on a
    *terminal* (by default, when its input is one), and never with
    ``--json``. *env* stands in for the environment (the home, sudo) in
    tests, *resolve* for this machine's name lookups, *this_computer* for
    its test of whether an address is its own, *host* for this computer as
    the devices see it, and *start_applier* for the start of a profile's
    applier.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 2

    if args.command == "profile" and not getattr(args, "action", None):
        err("usage: hardcode profile add NAME [--from P] [--lang LIST] | hardcode profile remove NAME [--yes]")
        return 2

    if args.all and args.command not in ALL_CAPABLE:
        err(f"error: --all isn't supported for '{args.command}'")
        return 2

    if terminal is None:
        terminal = sys.stdin.isatty()

    try:
        home.refuse_sudo(env)
        root = home.resolve(args.config, env)
        app = App(
            root,
            json_output=args.json,
            runner=runner,
            docker=docker,
            out=out,
            sleep=sleep,
            err=err,
            ask=(ask or _ask) if terminal and not args.json else None,
            resolve=resolve,
            this_computer=this_computer,
            host=host,
            start_applier=start_applier,
        )
        return args.func(app, args)
    except (CommandError, *USER_ERRORS) as error:
        err(f"error: {error}")
        return 1
