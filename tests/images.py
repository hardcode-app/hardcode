#!/usr/bin/env python3
"""
The VS Code image's start-up scripts, run for real without the 12 GB image.

- The SSH proxy stanza the entrypoint writes, read back by the real OpenSSH
  client: ``%h`` and ``%p`` must reach the proxy command as the destination's
  host and port.
- The sandbox's own SSH key, which the entrypoint makes once when the home
  has none: made by the real ssh-keygen, kept private, and never made
  beside a key the home already has.
- GitHub, one repository at a time: hardcode-github saving each
  repository's token in a file of its own and setting git up, the real git
  then answered with the right token, gh's wrapper handing gh the right
  one, and what's missing said plainly. Against a fake proxy and GitHub
  (tests/fixtures/fake_github_curl.py).
- The Kubernetes home-seeding script, run as uid 1000 in a container onto a
  volume root that belongs to root and is world-writable, as a freshly
  provisioned PVC is.
- The VS Code extensions: how the VS Code container keeps the home's
  extensions in step with the profile's list (hardcode-sync-extensions,
  once and watching) and checks their publishers
  (hardcode-verify-extensions). Both run for real against a fake VS Code
  CLI and a fake Marketplace (tests/fixtures/fake_code.py and
  fake_marketplace_curl.py).
- The build image's runner, images/build/run.sh: the caps it starts the
  container with, against a fake ``docker`` (tests/fixtures/fake_docker.py).
- The profile's own Ubuntu packages, which the entrypoint installs on
  Kubernetes (hardcode-install-packages): names checked before apt runs,
  apt as root, and apt's own message for a package the archive doesn't
  have. Against a fake sudo and apt-get (tests/fixtures/fake_sudo.py and
  fake_apt.py).

Every container is memory-capped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import harness
from harness import ROOT, Docker, Suite

VSCODE = ROOT / "images" / "vscode"
SSH_PROXY_CONFIG = VSCODE / "ssh-proxy-config.sh"
SSH_KEY = VSCODE / "ssh-key.sh"
GITHUB = VSCODE / "github.sh"
GH_WRAPPER = VSCODE / "gh.sh"
INSTALL_PACKAGES = VSCODE / "install-packages.sh"
FIXTURES = ROOT / "tests" / "fixtures"

# The fake Marketplace: what each extension needs. One pulls in a
# dependency, one is a pack, and two publishers have no verified domain, one
# of them a reviewed exception.
MARKETPLACE = {
    "core.one": {},
    "py.one": {"extensionDependencies": ["py.dep"]},
    "py.dep": {},
    "go.pack": {"extensionPack": ["go.member"]},
    "go.member": {},
    "me.mine": {"extensionDependencies": ["me.dep"]},
    "me.dep": {},
    "bad.one": {},
    "lldb.reviewed": {},
    "user.own": {},
    "user.two": {},
    # A verified extension that depends on an unverified one, and one whose
    # package pulls it in though the Marketplace doesn't list it.
    "good.parent": {"extensionDependencies": ["bad.one"]},
    "sly.parent": {"extensionDependencies": ["bad.one"], "listed": {"extensionDependencies": []}},
}
UNVERIFIED = "bad.one,lldb.reviewed"


def check_ssh_proxy_stanza(suite: Suite, scratch: Path) -> None:
    """
    OpenSSH parses the generated stanza with its host and port tokens intact.

    ``ssh -G`` prints the configuration as OpenSSH understood it, with ``%h``
    and ``%p`` still unexpanded (they are filled in at connect time). A
    doubled percent sign in the stanza would reach ``nc`` as a literal "%h".
    """
    config = scratch / "ssh_config"

    for proxy in ("squid:3128", "p-squid.sandbox.svc.cluster.local:3128"):
        host, port = proxy.rsplit(":", 1)
        stanza = harness.run(["bash", SSH_PROXY_CONFIG, host, port])
        config.write_text(stanza.text, encoding="utf-8")

        parsed = harness.run(["ssh", "-G", "-F", config, "-p", "443", "ssh.github.com"])
        got = next((line for line in parsed.text.splitlines() if line.lower().startswith("proxycommand ")), "")
        want = f"proxycommand nc -X connect -x {proxy} %h %p"

        suite.check(
            f"OpenSSH reads the stanza for {proxy} with its host/port tokens intact",
            got == want,
            f"got:  {got}\nwant: {want}",
        )

    usage = harness.run(["bash", SSH_PROXY_CONFIG, "only-one"])
    suite.check("usage is checked", not usage.ok, usage.text)


def check_ssh_key(suite: Suite, scratch: Path) -> None:
    """
    A home with no SSH key gets an ed25519 one, private to its owner, once;
    a home with a key of its own keeps it and gets no other.
    """
    home = scratch / "ssh-home"
    home.mkdir()
    environment = {**os.environ, "HOME": str(home)}

    def make() -> harness.Output:
        return harness.run(["bash", SSH_KEY, "dev@hardcode"], env=environment)

    first = make()
    private = home / ".ssh" / "id_ed25519"
    public = home / ".ssh" / "id_ed25519.pub"
    made = first.ok and private.exists() and public.exists()
    suite.check(
        "a home with no key gets one, and is told where its public half is",
        made and first.text.strip() == str(public),
        f"exit {first.code}\n{first.text}",
    )
    if not made:
        return

    suite.check(
        "the key is ed25519, with the comment it was given",
        public.read_text(encoding="utf-8").split()[::2] == ["ssh-ed25519", "dev@hardcode"],
        public.read_text(encoding="utf-8"),
    )

    # ssh refuses a private key others can read, and a folder others can write.
    modes = (oct(private.stat().st_mode & 0o777), oct((home / ".ssh").stat().st_mode & 0o777))
    suite.check("only its owner can read the key or its folder", modes == ("0o600", "0o700"), str(modes))

    key = private.read_bytes()
    second = make()
    suite.check(
        "the next start keeps it, and says nothing",
        second.ok and second.text == "" and private.read_bytes() == key,
        f"exit {second.code}\n{second.text}",
    )

    # A home whose key is of another kind keeps that one alone.
    other = scratch / "rsa-home"
    (other / ".ssh").mkdir(parents=True)
    (other / ".ssh" / "id_rsa").write_text("a key of the user's own\n", encoding="utf-8")
    kept = harness.run(["bash", SSH_KEY, "dev@hardcode"], env={**os.environ, "HOME": str(other)})
    suite.check(
        "a home with a key of its own gets no other",
        kept.ok and not (other / ".ssh" / "id_ed25519").exists(),
        f"exit {kept.code}\n{kept.text}",
    )

    usage = harness.run(["bash", SSH_KEY], env=environment)
    suite.check("usage is checked", not usage.ok, usage.text)


def check_github(suite: Suite, scratch: Path) -> None:
    """
    hardcode-github and gh's wrapper, run for real with the real git.
    """
    home = scratch / "github-home"
    home.mkdir()

    # The helper, the wrapper, and in place of curl and the real gh, the fakes.
    bin_dir = scratch / "github-bin"
    bin_dir.mkdir()
    for name, source in (
        ("hardcode-github", GITHUB),
        ("gh", GH_WRAPPER),
        ("curl", FIXTURES / "fake_github_curl.py"),
        ("real-gh", FIXTURES / "gh_token_probe.py"),
    ):
        shutil.copyfile(source, bin_dir / name)
        (bin_dir / name).chmod(0o755)

    environment = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HARDCODE_REAL_GH": str(bin_dir / "real-gh"),
        "FAKE_TOKENS": "github_pat_app=me/app,github_pat_other=me/other",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        environment.pop(name, None)

    def run(*command: object, input: Optional[str] = None, cwd: Optional[Path] = None, **extra: str) -> harness.Output:
        return harness.run(command, input=input, cwd=cwd, env={**environment, **extra})

    tokens = home / ".config" / "github-tokens"
    app_token = tokens / "me" / "app"

    # Setting a repository up: the token in its own file, git's settings, the check.
    first = run("hardcode-github", "Me/App", input="github_pat_app\n")
    saved = app_token.exists() and app_token.read_text(encoding="utf-8") == "github_pat_app\n"
    suite.check(
        "a repository's token is saved in a file of its own",
        first.ok and saved,
        f"exit {first.code}\n{first.text}",
    )
    if not saved:
        return

    modes = tuple(oct(path.stat().st_mode & 0o777) for path in (app_token, app_token.parent, tokens))
    suite.check("only its owner can read it, or its folders", modes == ("0o600", "0o700", "0o700"), str(modes))

    link = next((line.strip() for line in first.text.splitlines() if "personal-access-tokens/new" in line), "")
    filled = all(part in link for part in ("target_name=me", "expires_in=30", "contents=write", "pull_requests=write"))
    # Nothing that reaches the repository's CI, which runs with its secrets.
    filled = filled and "actions=" not in link and "workflows=" not in link
    suite.check("the link fills GitHub's form in", filled, link)
    suite.check(
        "the check says it's ready, and when the token expires",
        "git clone https://github.com/me/app.git" in first.text and "expires 2026-12-03" in first.text,
        first.text,
    )

    # The real git asks the helper, which answers for the repository the address names.
    asked = run("git", "credential", "fill", input="url=https://github.com/Me/App.git\n\n")
    suite.check("git gets that repository's token", "password=github_pat_app" in asked.text, asked.text)
    elsewhere = run("git", "credential", "fill", input="url=https://github.com/someone/else.git\n\n")
    suite.check("and none for a repository without one", "github_pat" not in elsewhere.text, elsewhere.text)

    rewrites = run("git", "config", "--global", "--get-all", "url.https://github.com/.insteadof")
    suite.check(
        "SSH addresses go over HTTPS",
        rewrites.text.split() == ["git@github.com:", "ssh://git@github.com/"],
        rewrites.text,
    )

    settings = (home / ".gitconfig").read_text(encoding="utf-8")

    # A token saved by hand, readable by others, is made private again.
    app_token.chmod(0o644)
    tokens.chmod(0o755)
    again = run("hardcode-github", "me/app", input="\n")
    suite.check(
        "running it again keeps the token, and git's settings as they were",
        again.ok and saved and (home / ".gitconfig").read_text(encoding="utf-8") == settings,
        f"exit {again.code}\n{again.text}",
    )
    modes = tuple(oct(path.stat().st_mode & 0o777) for path in (app_token, tokens))
    suite.check("...and a token left readable by others private again", modes == ("0o600", "0o700"), str(modes))

    second = run("hardcode-github", "me/other", input="github_pat_other\n")
    suite.check(
        "a second repository adds a token, and replaces none",
        second.ok
        and app_token.read_text(encoding="utf-8") == "github_pat_app\n"
        and (tokens / "me" / "other").read_text(encoding="utf-8") == "github_pat_other\n",
        f"exit {second.code}\n{second.text}",
    )

    # gh's wrapper hands gh the token of the repository it works on.
    def token_gh_got(*arguments: str, cwd: Optional[Path] = None, **extra: str) -> str:
        return run("gh", *arguments, cwd=cwd, **extra).text.strip()

    clone = scratch / "github-clone"
    run("git", "init", "-q", clone)
    run("git", "-C", clone, "remote", "add", "origin", "git@github.com:me/app.git")

    for description, got, wanted in (
        ("gh api repos/OWNER/REPO: that repository's", token_gh_got("api", "repos/me/other/pulls"), "github_pat_other"),
        ("gh -R OWNER/REPO: that repository's", token_gh_got("-R", "me/app", "run", "list"), "github_pat_app"),
        ("gh in a clone: its origin's", token_gh_got("run", "list", cwd=clone), "github_pat_app"),
        ("a GH_TOKEN of your own wins", token_gh_got("api", "repos/me/app", GH_TOKEN="mine"), "mine"),
        ("a repository without a token: none", token_gh_got("api", "repos/someone/else"), "(none)"),
    ):
        suite.check(f"gh's token: {description}", got == wanted, f"got {got}, wanted {wanted}")

    # What doesn't work is said, with what to do.
    refused = run("hardcode-github", "me/app", input="\n", FAKE_REFUSED="https://api.github.com/repos/me/app")
    suite.check(
        "a missing allowlist entry: the command that adds it",
        not refused.ok and "hardcode allow github:me/app rw" in refused.text,
        refused.text,
    )

    unseen = run("hardcode-github", "me/third", input="github_pat_app\n")
    suite.check(
        "a token that can't see the repository: what to check on GitHub",
        not unseen.ok and "Resource owner is me" in unseen.text,
        unseen.text,
    )

    read_only = run("hardcode-github", "me/app", input="\n", FAKE_READ_ONLY="github_pat_app")
    suite.check(
        "a token that may not push: says so",
        not read_only.ok and "may read but not push" in read_only.text,
        read_only.text,
    )

    classic = run("hardcode-github", "me/fourth", input="ghp_classic\n")
    suite.check(
        "a classic token is refused, and not saved",
        not classic.ok and not (tokens / "me" / "fourth").exists() and "fine-grained" in classic.text,
        classic.text,
    )

    for bad in ("me", "me/..", "me/app/more"):
        refusal = run("hardcode-github", bad)
        suite.check(
            f"'{bad}' isn't a repository, and is refused",
            refusal.code == 2,
            f"exit {refusal.code}\n{refusal.text}",
        )


def seed_home_script() -> str:
    """
    The seeding script, as the operator puts it into the VS Code container pod.
    """
    sys.path[:0] = [str(ROOT / "cli"), str(ROOT / "admin")]
    from hardcode_admin import manifests

    return manifests.SEED_HOME


def check_home_seeding(suite: Suite, docker: Docker, scratch: Path) -> None:
    """
    The image's home is copied onto an empty volume once, by uid 1000,
    without needing to own the volume root.
    """
    # A small stand-in for the image's home: a tool, a dotfile, and the
    # link to the shared volume.
    home = scratch / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    claude = home / ".local" / "bin" / "claude"
    claude.write_text("#!/bin/sh\n", encoding="utf-8")
    claude.chmod(0o755)
    (home / ".bashrc").write_text("# rc\n", encoding="utf-8")
    (home / "shared").symlink_to("/shared")

    # The volume root as a provisioner leaves it: root's, mode 777, with
    # lost+found inside.
    volume = scratch / "pvc"
    provision = "mkdir -p /t/pvc/lost+found && chown 0:0 /t/pvc && chmod 777 /t/pvc"
    docker.run(harness.BUILD_IMAGE, "sh", "-c", provision, memory="64m", volumes=[f"{scratch}:/t"])

    # The script takes the image's home and the volume as $1 and $2, as the
    # pod's init container passes them.
    seed_command = ["bash", "-c", seed_home_script(), "seed-home", "/home/dev", "/mnt/home"]

    def seed() -> harness.Output:
        return docker.run(
            "ubuntu:26.04",
            *seed_command,
            memory="128m",
            user="1000:1000",
            volumes=[f"{home}:/home/dev:ro", f"{volume}:/mnt/home"],
        )

    first = seed()
    claude_copy = volume / ".local" / "bin" / "claude"
    shared_link = volume / "shared"
    seeded = (
        first.ok
        and (volume / ".hardcode-seeded").exists()
        and claude_copy.exists()
        and claude_copy.stat().st_mode & 0o111 != 0
        and shared_link.is_symlink()
        and str(shared_link.readlink()) == "/shared"
    )
    listing = harness.run(["ls", "-la", volume]).text
    suite.check(
        "seeded as uid 1000 without touching the root-owned volume root",
        seeded,
        f"exit {first.code}\n{first.text}\n{listing}",
    )

    second = seed()
    suite.check("a second start is a no-op", second.ok, second.text)


class ExtensionWorld:
    """
    A VS Code container's extension setup in a scratch directory: the fake
    Marketplace, the profile's list, a home, and a PATH where ``code`` and
    ``curl`` are the fakes and ``hardcode-verify-extensions`` the real script.
    """

    def __init__(self, scratch: Path):
        self.root = scratch / "extensions"
        self.market = self.root / "market"
        self.home = self.root / "home"
        self.log = self.root / "code.log"
        self.wanted = self.root / "wanted"

        self.market.mkdir(parents=True)
        self.home.mkdir()
        for extension_id, needs in MARKETPLACE.items():
            publisher, name = extension_id.split(".")
            manifest = {"publisher": publisher, "name": name, **needs}
            (self.market / f"{extension_id}.json").write_text(json.dumps(manifest), encoding="utf-8")

        # Copies, made executable as the image's COPY --chmod makes them: the
        # checkout keeps its scripts at 0644.
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, source in (
            ("code", FIXTURES / "fake_code.py"),
            ("curl", FIXTURES / "fake_marketplace_curl.py"),
            ("hardcode-verify-extensions", VSCODE / "verify-extensions.sh"),
        ):
            shutil.copyfile(source, bin_dir / name)
            (bin_dir / name).chmod(0o755)
        self.path = f"{bin_dir}:{os.environ['PATH']}"

    def want(self, *lines: str, complete: bool = True) -> None:
        """
        Write the profile's list, as the CLI does: in place, ending with the
        end marker unless it is caught half-written.
        """
        text = "# the profile's extensions\n" + "".join(f"{line}\n" for line in lines)
        if complete:
            text += "# end\n"

        with open(self.wanted, "w", encoding="utf-8") as handle:
            handle.write(text)

    def environment(self, **variables: str) -> dict:
        """
        The scripts' environment in this world, with *variables* added.
        """
        return {
            **os.environ,
            "PATH": self.path,
            "HOME": str(self.home),
            "FAKE_MARKETPLACE": str(self.market),
            "FAKE_CODE_LOG": str(self.log),
            "FAKE_UNVERIFIED": UNVERIFIED,
            "HARDCODE_EXTENSIONS_WANTED": str(self.wanted),
            **variables,
        }

    def sync(self, **variables: str) -> harness.Output:
        """
        One pass of the sync, as a start does.
        """
        self.log.write_text("", encoding="utf-8")
        return harness.run(["bash", VSCODE / "sync-extensions.sh"], env=self.environment(**variables))

    def installed(self) -> set[str]:
        """
        What the home has now.
        """
        extensions = self.home / ".vscode-server" / "extensions"
        found = set()

        for manifest in extensions.glob("*/package.json"):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            found.add(f"{data['publisher']}.{data['name']}")

        return found

    def calls(self) -> list[str]:
        """
        The installs and uninstalls of the last pass.
        """
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def status(self) -> dict:
        """
        What the sync last said about how far it got.
        """
        path = self.home / ".vscode-server" / ".hardcode-extensions-status.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def install_by_hand(self, extension_id: str) -> None:
        """
        Install an extension the way someone would in VS Code itself.
        """
        harness.run(
            [
                sys.executable,
                FIXTURES / "fake_code.py",
                "--extensions-dir",
                self.home / ".vscode-server" / "extensions",
                "--install-extension",
                extension_id,
            ],
            env=self.environment(),
        )


def check_extension_sync(suite: Suite, world: ExtensionWorld) -> None:
    """
    Each pass brings the home in step with the profile's list, and only
    touches what it answers for.
    """
    everything = {"core.one", "py.one", "py.dep", "go.pack", "go.member"}

    world.want("core.one", "py.one", "go.pack")
    first = world.sync()
    suite.check(
        "the first start installs the list, with dependencies and pack members",
        first.ok and world.installed() == everything,
        f"{first.text}\n{sorted(world.installed())}",
    )
    suite.check(
        "and says so",
        world.status().get("state") == "done"
        and world.status().get("installed") == 3
        and world.status().get("total") == 3,
        world.status(),
    )

    unchanged = world.sync()
    suite.check("a pass with nothing changed touches nothing", unchanged.ok and world.calls() == [], unchanged.text)

    # Something the user installed in VS Code themselves: never touched.
    world.install_by_hand("user.own")

    world.want("core.one", "py.one")
    go_off = world.sync()
    suite.check(
        "a language switched off takes its extensions, pack members included",
        go_off.ok and world.installed() == {"core.one", "py.one", "py.dep", "user.own"},
        f"{go_off.text}\n{sorted(world.installed())}",
    )

    world.want("core.one", "py.one", "me.mine")
    mine = world.sync()
    suite.check(
        "the profile's own extension is installed and checked",
        mine.ok and {"me.mine", "me.dep"} <= world.installed(),
        f"{mine.text}\n{sorted(world.installed())}",
    )

    world.want("core.one", "py.one", "me.mine", "bad.one", "lldb.reviewed unverified-ok")
    bad = world.sync()
    refused_file = world.home / ".vscode-server" / ".hardcode-extensions-refused"
    suite.check(
        "an extension from an unverified publisher is removed again and remembered",
        bad.ok
        and "bad.one" not in world.installed()
        and refused_file.read_text(encoding="utf-8").split() == ["bad.one"]
        and world.status().get("refused") == ["bad.one"],
        f"{bad.text}\n{sorted(world.installed())}\n{world.status()}",
    )
    suite.check(
        "...never installed, not even while it is checked: VS Code is running",
        "install bad.one" not in world.calls(),
        "\n".join(world.calls()),
    )
    suite.check("a reviewed exception is installed", "lldb.reviewed" in world.installed(), sorted(world.installed()))

    # Every question to the Marketplace has a time limit, so a stalled one
    # can't hold the sync up.
    curl_log = world.root / "curl.log"
    curl_log.write_text("", encoding="utf-8")
    world.want("core.one", "py.one", "me.mine", "user.two")
    world.sync(FAKE_CURL_LOG=str(curl_log))
    queries = [line for line in curl_log.read_text(encoding="utf-8").splitlines() if line]
    suite.check(
        "every Marketplace query has a time limit",
        bool(queries) and all("--connect-timeout" in query and "--max-time" in query for query in queries),
        "\n".join(queries) or "no queries",
    )
    world.want("core.one", "py.one", "me.mine", "bad.one", "lldb.reviewed unverified-ok")
    world.sync()

    again = world.sync()
    suite.check("a refused extension isn't fetched again", again.ok and world.calls() == [], again.text)

    world.want("core.one", "py.one", "go.pack")
    down = world.sync(FAKE_MARKETPLACE_DOWN="1")
    suite.check(
        "with the Marketplace unreachable, what can't be checked doesn't stay, and is to be tried again",
        down.ok and "go.pack" not in world.installed() and world.status().get("failed") == ["go.pack"],
        f"{down.text}\n{sorted(world.installed())}\n{world.status()}",
    )

    back = world.sync()
    suite.check(
        "and the next pass brings it",
        back.ok and {"go.pack", "go.member"} <= world.installed() and world.status().get("failed") == [],
        f"{back.text}\n{sorted(world.installed())}",
    )
    suite.check(
        "extensions dropped from the list are removed; the one the user added stays",
        not {"me.mine", "me.dep", "lldb.reviewed"} & world.installed() and "user.own" in world.installed(),
        sorted(world.installed()),
    )

    # The CLI caught mid-write: the list has no end marker yet.
    world.want("core.one", complete=False)
    half = world.sync()
    suite.check(
        "a list caught half-written changes nothing",
        half.ok and world.calls() == [] and {"py.one", "go.pack"} <= world.installed(),
        f"{half.text}\n{sorted(world.installed())}",
    )


def check_extension_dependencies(suite: Suite, world: ExtensionWorld) -> None:
    """
    What an extension pulls in (dependencies, a pack's members) is checked
    before anything is installed, as far as the Marketplace lists it: VS
    Code is running, and would pick up whatever lands in its folder. What a
    package pulls in beyond that is checked on arrival, and takes the whole
    set with it, the extension asked for included.
    """
    world.want("good.parent")
    listed = world.sync()
    suite.check(
        "a verified extension needing an unverified one is refused before anything is installed",
        listed.ok
        and not {"good.parent", "bad.one"} & world.installed()
        and not [call for call in world.calls() if call.startswith("install ")],
        f"{listed.text}\n{world.calls()}\n{sorted(world.installed())}",
    )
    suite.check(
        "...and said so: refused, nothing installed",
        world.status().get("refused") == ["good.parent"] and world.status().get("installed") == 0,
        str(world.status()),
    )

    world.want("sly.parent")
    unlisted = world.sync()
    suite.check(
        "one whose package pulls in more than the Marketplace lists: the whole set removed on arrival",
        unlisted.ok and not {"sly.parent", "bad.one"} & world.installed() and "uninstall sly.parent" in world.calls(),
        f"{unlisted.text}\n{world.calls()}\n{sorted(world.installed())}",
    )
    suite.check(
        "...and said so: refused, nothing installed",
        world.status().get("refused") == ["sly.parent"] and world.status().get("installed") == 0,
        str(world.status()),
    )


def check_extension_watch(suite: Suite, world: ExtensionWorld) -> None:
    """
    Watching, the sync follows a change to the list within seconds, with
    VS Code already up; nothing restarts.
    """
    world.want("core.one", "py.one", "go.pack")
    world.sync()

    watcher = subprocess.Popen(
        ["bash", VSCODE / "sync-extensions.sh", "--watch"],
        env=world.environment(HARDCODE_EXTENSIONS_POLL="0.2"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        world.want("core.one", "py.one", "go.pack", "me.mine")

        deadline = time.time() + 30
        while time.time() < deadline and "me.mine" not in world.installed():
            time.sleep(0.2)

        suite.check(
            "a change to the list is installed by the watcher, within seconds",
            "me.mine" in world.installed() and watcher.poll() is None,
            sorted(world.installed()),
        )
    finally:
        watcher.terminate()
        watcher.wait(timeout=10)


def check_build_runner_cpus(suite: Suite, scratch: Path) -> None:
    """
    images/build/run.sh caps the container's CPUs at BUILD_CPUS (4), but
    never above what the Docker daemon has: Docker refuses a larger cap, and
    a CI runner may have 2.
    """
    bin_dir = scratch / "fake-docker"
    bin_dir.mkdir()
    shutil.copyfile(FIXTURES / "fake_docker.py", bin_dir / "docker")
    (bin_dir / "docker").chmod(0o755)

    # As on a machine: outside the build image, with the real socket path.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("HARDCODE_BUILD_IMAGE", "BUILD_CPUS", "BUILD_IMAGE_REBUILD")
    }
    environment.update(
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "HARDCODE_BUILD_CACHE": str(scratch / "build-cache"),
            "TMPDIR": str(scratch),
        }
    )

    def cpus(**variables: str) -> str:
        output = harness.run(
            ["bash", ROOT / "images" / "build" / "run.sh", "true"],
            env={**environment, **variables},
        )
        lines = output.text.splitlines()
        return lines[lines.index("--cpus") + 1] if "--cpus" in lines else output.text.strip()

    cases = [
        ("a daemon with 2 CPUs: 2", {"FAKE_DOCKER_CPUS": "2"}, "2"),
        ("with 8: the cap, 4", {"FAKE_DOCKER_CPUS": "8"}, "4"),
        ("a smaller BUILD_CPUS as it is", {"FAKE_DOCKER_CPUS": "2", "BUILD_CPUS": "1.5"}, "1.5"),
        ("a daemon that can't say: the cap", {}, "4"),
    ]
    for name, variables, expected in cases:
        got = cpus(**variables)
        suite.check(f"run.sh's CPU cap, {name}", got == expected, f"wanted {expected}, got {got}")


def check_package_install(suite: Suite, scratch: Path) -> None:
    """
    hardcode-install-packages, which the entrypoint runs on Kubernetes for
    the profile's own Ubuntu packages, against a fake sudo and apt-get:
    every name is checked before apt runs, then the archive's lists are
    read and the packages installed, as root and without recommends; a
    package the archive doesn't have fails with apt's own message.
    """
    bin_dir = scratch / "apt-bin"
    bin_dir.mkdir()
    for name, source in (
        ("hardcode-install-packages", INSTALL_PACKAGES),
        ("sudo", FIXTURES / "fake_sudo.py"),
        ("apt-get", FIXTURES / "fake_apt.py"),
    ):
        shutil.copyfile(source, bin_dir / name)
        (bin_dir / name).chmod(0o755)

    log = scratch / "apt-calls.jsonl"
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_APT_LOG": str(log),
        "FAKE_APT_MISSING": "no-such-package",
    }

    def install(*names: str) -> harness.Output:
        log.write_text("", encoding="utf-8")
        return harness.run(["hardcode-install-packages", *names], env=environment)

    def calls() -> list[dict]:
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]

    done = install("jq", "postgresql-client")
    wanted = [["update", "-q"], ["install", "-y", "-q", "--no-install-recommends", "jq", "postgresql-client"]]
    suite.check(
        "the packages are installed after the archive's lists are read",
        done.ok and [call["args"] for call in calls()] == wanted,
        f"exit {done.code}\n{done.text}\n{calls()}",
    )
    suite.check(
        "...as root, with nobody asked anything",
        all(call["sudo"] and call["frontend"] == "noninteractive" for call in calls()),
        str(calls()),
    )

    refused = install("jq", "jq;rm -rf ~")
    suite.check(
        "a name that isn't a package's is refused before apt runs",
        not refused.ok and "not an Ubuntu package name: jq;rm -rf ~" in refused.text and calls() == [],
        f"exit {refused.code}\n{refused.text}\n{calls()}",
    )

    missing = install("jq", "no-such-package")
    suite.check(
        "a package the archive doesn't have fails, with apt's own message",
        not missing.ok and "E: Unable to locate package no-such-package" in missing.text,
        f"exit {missing.code}\n{missing.text}",
    )

    usage = install()
    suite.check("usage is checked", not usage.ok and "usage:" in usage.text, usage.text)


def run_checks(suite: Suite) -> None:
    with harness.scratch_directory("hc-images-") as scratch, Docker() as docker:
        suite.section("ssh proxy configuration")
        check_ssh_proxy_stanza(suite, scratch)

        suite.section("the sandbox's own SSH key")
        check_ssh_key(suite, scratch)

        suite.section("GitHub, one repository at a time")
        check_github(suite, scratch)

        suite.section("seeding a home onto a root-owned volume")
        check_home_seeding(suite, docker, scratch)

        suite.section("VS Code extensions, installed by the running container")
        world = ExtensionWorld(scratch)
        check_extension_sync(suite, world)
        check_extension_watch(suite, ExtensionWorld(scratch / "watch"))
        check_extension_dependencies(suite, ExtensionWorld(scratch / "dependencies"))

        suite.section("the build image's runner")
        check_build_runner_cpus(suite, scratch)

        suite.section("the profile's own Ubuntu packages, installed at start on Kubernetes")
        check_package_install(suite, scratch)


if __name__ == "__main__":
    harness.main("images", run_checks)
