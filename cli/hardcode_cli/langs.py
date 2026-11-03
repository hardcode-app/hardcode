"""
The languages a profile is built around, the tools beside them, and the
allowlist entries every profile has.

A language is chosen per profile, and it decides three things: what the
VS Code image installs (the ``INSTALL_<LANG>`` build arg), which allowlist
entries are open (its block), and which VS Code extensions the VS Code
container gets. This catalogue is where all three are written down.

It is the only place HardCode's allowlist entries are: a profile's
``allowlist.txt`` holds just its own entries and the provided ones it
switches off, and the blocks (``blocks``) are added to those whenever the
proxy's rules are made, so a newer catalogue reaches every profile. The
extensions aren't in the image either: a profile's list (see
``extension_list``) is handed to its VS Code container, which installs them
in the background while it runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Lang:
    """
    A language, or a tool: its key, its name, the toolchain it puts in the
    image, the allowlist entries it opens and the extensions it brings.

    ``rw`` names the entries among ``domains`` that take uploads (an API
    called with POST): they are written ``rw`` in the allowlist, and the
    others read-only.
    """

    key: str
    name: str
    desc: str
    kind: str = "language"  # or "tool"
    domains: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    rw: tuple[str, ...] = ()

    @property
    def env_key(self) -> str:
        """
        The ``.env`` key and build arg that switch it on and off.
        """
        return f"INSTALL_{self.key.upper()}"


@dataclass(frozen=True)
class Block:
    """
    Allowlist entries HardCode provides, together: those every profile has
    (``EVERY_PROFILE``), or a language's or tool's.

    ``rw`` names the entries among ``domains`` that take uploads (an API
    called with POST); the others are read-only. ``every_profile`` marks the
    blocks that come with every profile rather than with a language.
    """

    key: str
    name: str
    domains: tuple[str, ...]
    rw: tuple[str, ...] = ()
    every_profile: bool = False


# The blocks every profile has, whatever its languages and tools: what VS
# Code itself needs, the package mirrors of the VS Code container's and of
# containers' operating systems, the container registries (all read-only:
# images can be pulled, never pushed), and GitHub's raw files (also Claude
# Code's release notes). Git hosts never whole: a profile adds the
# namespaces it works in, under Custom.
EVERY_PROFILE = (
    Block(
        "vscode",
        "VS Code",
        domains=(
            "update.code.visualstudio.com",
            "vscode.download.prss.microsoft.com",
            "download.visualstudio.microsoft.com",
            "marketplace.visualstudio.com",
            "marketplace.visualstudio.com/_apis/public/gallery",
            ".gallery.vsassets.io",
            ".gallerycdn.vsassets.io",
            ".vscode-cdn.net",
        ),
        rw=("marketplace.visualstudio.com/_apis/public/gallery",),
        every_profile=True,
    ),
    Block(
        "os_packages",
        "OS package mirrors",
        domains=(
            ".archive.ubuntu.com",
            "security.ubuntu.com",
            "ports.ubuntu.com",
            "deb.debian.org",
            "security.debian.org",
            "dl-cdn.alpinelinux.org",
        ),
        every_profile=True,
    ),
    Block(
        "registries",
        "Container registries",
        domains=(
            "registry-1.docker.io",
            "auth.docker.io",
            "production.cloudfront.docker.com",
            "docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com",
            "ghcr.io",
            "pkg-containers.githubusercontent.com",
            ".quay.io",
            "quayio-production-s3.s3.amazonaws.com",
            "registry.access.redhat.com",
            "registry.redhat.io",
            "mcr.microsoft.com",
            ".data.mcr.microsoft.com",
            ".gcr.io",
            ".pkg.dev",
            ".registry.k8s.io",
            "public.ecr.aws",
            "d5l0dvt14r5h8.cloudfront.net",
        ),
        every_profile=True,
    ),
    Block(
        "github_raw",
        "GitHub raw files",
        domains=("raw.githubusercontent.com",),
        every_profile=True,
    ),
)

# What some provided entries are for, where the name doesn't say: shown
# beside them in the allowlist.
NOTES = {
    "marketplace.visualstudio.com": "extension pages, downloads",
    "marketplace.visualstudio.com/_apis/public/gallery": "extension search is a POST",
    "registry-1.docker.io": "Docker Hub",
    "auth.docker.io": "Docker Hub",
    "production.cloudfront.docker.com": "Docker Hub's layers",
    "docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com": "Docker Hub's layers",
    "ghcr.io": "GitHub Container Registry",
    "pkg-containers.githubusercontent.com": "GitHub Container Registry's layers",
    ".quay.io": "Quay (layers from cdnNN.quay.io)",
    "quayio-production-s3.s3.amazonaws.com": "Quay's layers",
    "registry.access.redhat.com": "Red Hat",
    "registry.redhat.io": "Red Hat",
    "mcr.microsoft.com": "Microsoft Container Registry",
    ".data.mcr.microsoft.com": "Microsoft Container Registry's layers",
    ".gcr.io": "Google: gcr.io, regional *.gcr.io, mirror.gcr.io",
    ".pkg.dev": "Google Artifact Registry (where registry.k8s.io redirects)",
    ".registry.k8s.io": "Kubernetes (layers from cdn.registry.k8s.io)",
    "public.ecr.aws": "AWS ECR Public",
    "d5l0dvt14r5h8.cloudfront.net": "AWS ECR Public's layers",
    "raw.githubusercontent.com": "raw file downloads; Claude Code's release notes",
    "dl.google.com/android/repository": "the Android SDK's packages",
    "dl.google.com/dl/android/maven2": "Google's Maven repository",
    "maven.google.com": "Google's Maven repository (redirects to dl.google.com)",
    "f-droid.github.io/android-sdk-transparency-log": "the SDK's checksums, which sdkmanager checks",
    "fdroid.gitlab.io/android-sdk-transparency-log": "the SDK's checksums, which sdkmanager checks",
    "raw.githubusercontent.com/f-droid/android-sdk-transparency-log": "the SDK's checksums, which sdkmanager checks",
    "github.com/vadimcn/codelldb/releases/download": "CodeLLDB's platform packages",
    "release-assets.githubusercontent.com/github-production-release-asset/49407251": (
        "where GitHub sends CodeLLDB's downloads"
    ),
    "dl.k8s.io": "kubectl's downloads",
    "cdn.dl.k8s.io": "kubectl's downloads (where dl.k8s.io redirects)",
    "get.helm.sh": "Helm's downloads",
    "api.anthropic.com": "the API",
    "claude.ai": "account sign-in",
    "platform.claude.com": "OAuth token exchange",
    "claude.com": "sign-in redirect page",
    "downloads.claude.ai": "native installer, auto-updates",
    "auth.openai.com": "sign-in and token refresh",
    "chatgpt.com/backend-api": "the model, with a ChatGPT plan",
    "api.openai.com": "the model, with an API key",
}

# The extensions every VS Code container has, whatever its languages and
# tools. The coding agents' (Claude Code's, Codex's) come with their tools.
CORE_EXTENSIONS = ("redhat.vscode-yaml",)

# Reviewed exceptions to the rule that an extension's publisher, and every
# publisher of what it pulls in, must have a verified domain on the
# Marketplace. CodeLLDB is the Rust debugger VS Code's own Rust documentation
# recommends; its publisher isn't domain-verified.
APPROVED_UNVERIFIED = ("vadimcn.vscode-lldb",)

CATALOGUE = (
    Lang(
        "python",
        "Python",
        "Python 3.14, pip, venv, pipx, black, uv, ruff",
        domains=("pypi.org", "files.pythonhosted.org"),
        extensions=(
            "ms-python.python",
            "ms-python.vscode-pylance",
            "ms-python.debugpy",
            "ms-python.black-formatter",
            "charliermarsh.ruff",
            "ms-toolsai.jupyter",
        ),
    ),
    Lang(
        "node",
        "JavaScript / TypeScript",
        "Node.js 22, npm, yarn, pnpm, TypeScript, ESLint, Prettier",
        domains=("registry.npmjs.org", "registry.yarnpkg.com", "repo.yarnpkg.com"),
        extensions=("dbaeumer.vscode-eslint", "esbenp.prettier-vscode"),
    ),
    Lang(
        "java",
        "Java",
        "OpenJDK 25, Maven, Gradle, Extension Pack for Java",
        domains=(
            "repo.maven.apache.org",
            "repo1.maven.org",
            "services.gradle.org",
            "downloads.gradle.org",
            "plugins.gradle.org",
            "plugins-artifacts.gradle.org",
        ),
        extensions=("vscjava.vscode-java-pack",),
    ),
    Lang(
        "android",
        "Android",
        "OpenJDK 21, sdkmanager, Kotlin by JetBrains, Extension Pack for Java",
        # Google's own paths on dl.google.com, never the whole host (it
        # serves all of Google's downloads): the SDK's packages, which
        # sdkmanager and Android's Gradle plugin fetch, and Google's Maven
        # repository (AndroidX, the Gradle plugin), with the name that
        # redirects to it. F-Droid's signed log of the SDK's checksums,
        # which sdkmanager checks every package against, from whichever of
        # its three copies it picks at random. Then what Java's block opens
        # for Gradle, so that Android builds without Java switched on: Maven
        # Central, Gradle's distributions and its plugin portal.
        domains=(
            "dl.google.com/android/repository",
            "dl.google.com/dl/android/maven2",
            "maven.google.com",
            "f-droid.github.io/android-sdk-transparency-log",
            "fdroid.gitlab.io/android-sdk-transparency-log",
            "raw.githubusercontent.com/f-droid/android-sdk-transparency-log",
            "repo.maven.apache.org",
            "repo1.maven.org",
            "services.gradle.org",
            "downloads.gradle.org",
            "plugins.gradle.org",
            "plugins-artifacts.gradle.org",
        ),
        extensions=("jetbrains.kotlin-server", "vscjava.vscode-java-pack"),
    ),
    Lang(
        "dotnet",
        "C# / .NET",
        ".NET SDK 10, C# Dev Kit",
        domains=("api.nuget.org", "globalcdn.nuget.org", "builds.dotnet.microsoft.com"),
        extensions=("ms-dotnettools.csdevkit", "ms-dotnettools.csharp"),
    ),
    Lang(
        "go",
        "Go",
        "Go 1.26, gopls, delve",
        # The module proxy and the checksum database; then the module
        # proxy's own storage bucket, which it redirects the larger module
        # downloads and every toolchain to (a project whose go.mod asks for
        # a newer Go than the image's makes go fetch that one). Both are
        # checked against the checksum database. Only that bucket, never
        # the whole host: every Google Cloud Storage bucket is on it.
        domains=(
            "proxy.golang.org",
            "sum.golang.org",
            "storage.googleapis.com/proxy-golang-org-prod",
        ),
        extensions=("golang.go",),
    ),
    Lang(
        "rust",
        "Rust",
        "rustc, cargo, rustfmt, clippy, rust-src, rust-analyzer, CodeLLDB",
        # The registry and the toolchain's downloads; then CodeLLDB's own
        # release downloads (its Marketplace package fetches its platform
        # package from there when it first runs): that one project's
        # releases on GitHub, and the storage GitHub redirects them to,
        # under CodeLLDB's repository id. Never the whole git host.
        domains=(
            "crates.io",
            "index.crates.io",
            "static.crates.io",
            "static.rust-lang.org",
            "github.com/vadimcn/codelldb/releases/download",
            "release-assets.githubusercontent.com/github-production-release-asset/49407251",
        ),
        extensions=("rust-lang.rust-analyzer", "vadimcn.vscode-lldb"),
    ),
    Lang(
        "cpp",
        "C / C++",
        "GCC 15, Clang 21, clangd, LLDB, GDB, CMake, Ninja, ccache, Valgrind",
        extensions=("ms-vscode.cpptools", "ms-vscode.cmake-tools", "ms-vscode.makefile-tools"),
    ),
    Lang(
        "ruby",
        "Ruby",
        "Ruby 3.3, bundler, Ruby LSP",
        domains=("rubygems.org", "index.rubygems.org"),
        extensions=("shopify.ruby-lsp",),
    ),
    Lang(
        "perl",
        "Perl",
        "Perl 5.40, cpanminus, local::lib, perltidy, Perl::Critic",
        domains=("www.cpan.org", "cpan.metacpan.org", "fastapi.metacpan.org", "cpanmetadb.plackperl.org"),
    ),
    Lang(
        "build_essentials",
        "Build essentials",
        "gcc/g++, make, autotools, pkg-config, common -dev libraries",
        kind="tool",
    ),
    Lang(
        "docker",
        "Docker CLI",
        "docker, buildx, compose (talks to the rootless daemon)",
        kind="tool",
        extensions=("ms-azuretools.vscode-containers",),
    ),
    Lang(
        "kubernetes",
        "Kubernetes",
        "kubectl, Helm, kind, the Kubernetes extension",
        kind="tool",
        # kubectl's release downloads, on dl.k8s.io and on cdn.dl.k8s.io,
        # where it redirects them: the extension fetches a kubectl to match
        # a cluster there (vs-kubernetes.kubectlVersioning "infer"), and so
        # do Dockerfiles built in the sandbox. Helm's, on get.helm.sh, which
        # its install script and Dockerfiles fetch. Hosts that serve those
        # projects' releases and nothing else. Never a cluster's API server:
        # that is a profile's own entry. Docker Hub (kind's node images) and
        # registry.k8s.io are in every profile's block already.
        domains=("dl.k8s.io", "cdn.dl.k8s.io", "get.helm.sh"),
        extensions=("ms-kubernetes-tools.vscode-kubernetes-tools",),
    ),
    Lang(
        "claude_cli",
        "Claude Code CLI",
        "claude in the terminal and its VS Code extension: Anthropic's coding agent",
        kind="tool",
        # The API; account sign-in; the OAuth token exchange; the sign-in
        # redirect page; the native installer and its updates. Its telemetry
        # and error reporting are off (compose.yaml), and their hosts closed.
        domains=("api.anthropic.com", "claude.ai", "platform.claude.com", "claude.com", "downloads.claude.ai"),
        rw=("api.anthropic.com", "claude.ai", "platform.claude.com"),
        extensions=("anthropic.claude-code",),
    ),
    Lang(
        "codex_cli",
        "Codex CLI",
        "codex in the terminal and its VS Code extension: OpenAI's coding agent, beside Claude Code",
        kind="tool",
        extensions=("openai.chatgpt",),
        # Sign-in and token refresh; the model with a ChatGPT plan; the model
        # with an API key. Its update check, metrics and crash reports go
        # elsewhere, and stay closed.
        domains=("auth.openai.com", "chatgpt.com/backend-api", "api.openai.com"),
        rw=("auth.openai.com", "chatgpt.com/backend-api", "api.openai.com"),
    ),
)
LANGS = tuple(lang.key for lang in CATALOGUE)
BY_KEY = {lang.key: lang for lang in CATALOGUE}


def blocks(enabled: Iterable[str]) -> list[Block]:
    """
    The blocks of allowlist entries a profile with the *enabled* languages
    and tools gets: every profile's, then those of its languages and tools
    that open any, in catalogue order.
    """
    wanted = set(enabled)
    language_blocks = [
        Block(lang.key, lang.name, lang.domains, lang.rw) for lang in CATALOGUE if lang.key in wanted and lang.domains
    ]

    return [*EVERY_PROFILE, *language_blocks]


# A VS Code extension id as the Marketplace writes it: publisher.name.
EXTENSION_ID = re.compile(r"^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*$")

# The names people type for the languages.
ALIASES = {
    "c": "cpp",
    "c++": "cpp",
    "cxx": "cpp",
    "c#": "dotnet",
    "csharp": "dotnet",
    "net": "dotnet",
    ".net": "dotnet",
    "js": "node",
    "javascript": "node",
    "ts": "node",
    "typescript": "node",
    "nodejs": "node",
    "golang": "go",
    "claude": "claude_cli",
    "codex": "codex_cli",
    "claude_code": "claude_cli",
    "build": "build_essentials",
    "build_essential": "build_essentials",
    "essentials": "build_essentials",
    "k8s": "kubernetes",
    "kubectl": "kubernetes",
    "helm": "kubernetes",
}


class LangError(ValueError):
    """
    Not a language the image knows.
    """


def normalise(lang: str) -> str:
    """
    The canonical name of *lang*, which may be one of its aliases.
    """
    key = lang.strip().lower().replace("-", "_")
    key = ALIASES.get(key, key)

    if key not in LANGS:
        raise LangError(f"unknown language '{lang}'. One of: {', '.join(LANGS)}")
    return key


def env_key(lang: str) -> str:
    """
    The ``.env`` key for *lang*.
    """
    return f"INSTALL_{normalise(lang).upper()}"


def parse_list(text: str) -> list[str]:
    """
    Language names from a command line or a form ("python,go node"), each
    normalised, in catalogue order, without repeats.
    """
    named = {normalise(word) for word in re.split(r"[\s,]+", text.strip()) if word}

    return [key for key in LANGS if key in named]


def is_enabled(env: dict[str, str], key: str) -> bool:
    """
    Whether a profile's ``.env`` includes the language *key*.

    Read as the image's Dockerfile reads its build args: enabled only when
    exactly ``true``, and not for anything else (``True``, ``yes``), which
    the build would skip. Unset is off, compose.yaml's default: a language
    added in a newer HardCode stays off in the profiles that exist.
    """
    return env.get(BY_KEY[key].env_key) == "true"


def enabled_keys(env: dict[str, str]) -> list[str]:
    """
    The languages and tools a profile's ``.env`` includes, in catalogue order.
    """
    return [key for key in LANGS if is_enabled(env, key)]


def describe(lang: Lang, enabled: bool) -> dict:
    """
    One catalogue entry as the app and the admin show it.
    """
    return {
        "key": lang.key,
        "name": lang.name,
        "desc": lang.desc,
        "kind": lang.kind,
        "domains": list(lang.domains),
        "rw": list(lang.rw),
        "extensions": list(lang.extensions),
        "enabled": enabled,
    }


def enabled_in(env: dict[str, str]) -> list[dict]:
    """
    The catalogue, with each language's state in a profile's ``.env``.
    """
    return [describe(lang, is_enabled(env, lang.key)) for lang in CATALOGUE]


def normalise_extension(extension: str) -> str:
    """
    A VS Code extension id, lower case; an error for anything that isn't one.
    """
    extension_id = extension.strip().lower()

    if not EXTENSION_ID.match(extension_id):
        raise LangError(f"'{extension}' is not an extension id (publisher.name, as the Marketplace shows it)")

    return extension_id


def parse_extensions(text: str) -> list[str]:
    """
    The extension ids in a ``VSCODE_EXTENSIONS`` value (comma or space
    separated), normalised, without repeats. Anything that isn't an id is
    left out: the value is read, not checked, here.
    """
    found: list[str] = []

    for word in re.split(r"[\s,]+", text.strip()):
        extension_id = word.lower()
        if EXTENSION_ID.match(extension_id) and extension_id not in found:
            found.append(extension_id)

    return found


def provider(extension_id: str, enabled: list[str]) -> str:
    """
    What already brings *extension_id* to a profile's VS Code container:
    ``"every VS Code container"``, the name of one of the *enabled*
    languages, or ``""`` when nothing does.
    """
    if extension_id in CORE_EXTENSIONS:
        return "every VS Code container"

    for key in enabled:
        if extension_id in BY_KEY[key].extensions:
            return BY_KEY[key].name

    return ""


def removal_refusal(extension_id: str, enabled: list[str]) -> str:
    """
    Why *extension_id* can't be removed as one of a profile's own extensions,
    when something else brings it; ``""`` when nothing does.
    """
    if extension_id in CORE_EXTENSIONS:
        return f"{extension_id} comes with every VS Code container and can't be removed"

    name = provider(extension_id, enabled)
    if name:
        return f"{extension_id} comes with {name}; switch that off in Languages / Tools to remove it"

    return ""


def wanted_extensions(enabled: list[str], extras: list[str]) -> list[str]:
    """
    The extensions a profile's VS Code container should have: every
    container's, those of its *enabled* languages and tools, then its own
    (*extras*), each once.
    """
    wanted: list[str] = list(CORE_EXTENSIONS)

    for lang in CATALOGUE:
        if lang.key in enabled:
            wanted += lang.extensions

    wanted += extras
    return list(dict.fromkeys(wanted))


# The line that ends a complete extension list: the VS Code container reads
# a list only once it is all there, never one caught half-written.
EXTENSION_LIST_END = "# end"


def extension_list(enabled: list[str], extras: list[str]) -> str:
    """
    The text of the extension list handed to a profile's VS Code container:
    one id per line, ``unverified-ok`` after a reviewed exception, and the
    end marker last.
    """
    lines = [
        "# The VS Code extensions this profile's VS Code container keeps installed:",
        "# its languages' and tools', and its own. Written by HardCode whenever",
        "# they change; the running container follows (within seconds on Compose,",
        "# about a minute on Kubernetes).",
    ]

    for extension_id in wanted_extensions(enabled, extras):
        approval = " unverified-ok" if extension_id in APPROVED_UNVERIFIED else ""
        lines.append(f"{extension_id}{approval}")

    lines.append(EXTENSION_LIST_END)
    return "\n".join(lines) + "\n"


def extension_sources(enabled: list[str], extras: list[str]) -> list[dict]:
    """
    Every extension a profile's VS Code container gets, and why: ``core`` (always), a
    language's key, or ``custom`` (the profile's own). Those of languages
    that are off are listed too, with ``enabled`` false, so the app can
    show what switching one on would bring.
    """
    rows = [{"id": extension_id, "source": "core", "enabled": True} for extension_id in CORE_EXTENSIONS]

    for lang in CATALOGUE:
        for extension_id in lang.extensions:
            rows.append({"id": extension_id, "source": lang.key, "enabled": lang.key in enabled})

    provided = {row["id"] for row in rows}
    for extension_id in extras:
        rows.append({"id": extension_id, "source": "custom", "enabled": True, "duplicate": extension_id in provided})

    return rows
