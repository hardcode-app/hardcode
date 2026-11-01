# The build image: the Ubuntu 26.04 environment everything in this repository
# is built and tested in. Nothing is installed on workstations or CI runners;
# images/build/run.sh runs a command in here with the checkout mounted, and
# the Docker socket for the suites that drive Docker (tests/*.py and
# app/build.sh run themselves in it). Archive packages first; the vendor pins
# below are what the archive doesn't carry, each with its reason.
#
#   images/build/run.sh tests/run_all.py      every suite
#   images/build/run.sh app/build.sh bundle   the app's installers
#   images/build/run.sh                       a shell
# Ubuntu 26.04 (resolute), one dated build of it, by digest: Dependabot moves
# it to the next dated build once that is a week old (docs/development.md,
# "Dependencies").
FROM ubuntu:resolute-20261006@sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      # the suites' tools: Python with what the CLI, the admin and the chart tests import
      ca-certificates curl git file jq openssl openssh-client xvfb \
      python3 python3-venv python3-yaml python3-flask python3-qrcode isort libpython3.14 \
      # the CLI's Python package (app/build.sh wheel)
      python3-setuptools \
      # the app: Rust (and rustfmt) and Tauri's libraries; patchelf and squashfs for the AppImage
      rustc cargo rustfmt build-essential pkg-config libssl-dev patchelf squashfs-tools \
      libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev libayatana-appindicator3-dev \
      # the Docker CLI, buildx and compose, talking to the host's daemon over its socket
      docker.io docker-buildx docker-compose-v2 \
 && rm -rf /var/lib/apt/lists/*

# Kubernetes tooling for the chart and kind suites; the archive carries none
# of it. Published checksums, both architectures. These ARG pins (and the
# Tauri CLI's and OSV-Scanner's) move with maintenance/pins.py vendor, each
# version with its publisher's checksums, a week after its release;
# Dependabot moves the base image and requirements.txt.
ARG KIND_VERSION=0.33.0
ARG KIND_SHA256_AMD64=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d
ARG KIND_SHA256_ARM64=20022bee6cfcd5086cb7234d218e3454e6090022f2a8f55d1fa7fcf42c3867a2
ARG KUBECTL_VERSION=1.37.1
ARG KUBECTL_SHA256_AMD64=65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8
ARG KUBECTL_SHA256_ARM64=ff749f4b78d9c4f1ec87307df9b50119ed819e2094aa9810cb9acffc3286c8c7
ARG HELM_VERSION=4.3.0
ARG HELM_SHA256_AMD64=86584a54def73570558f66f5111cc53dfed56689637ae32c1201205d494f54fb
ARG HELM_SHA256_ARM64=31c5794dd55c66a51e6b7d2e2ac7a114ae8b1de41ff1d9ba51748ac973b06a08
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) kind_sha="$KIND_SHA256_AMD64" kubectl_sha="$KUBECTL_SHA256_AMD64" helm_sha="$HELM_SHA256_AMD64" ;; \
      arm64) kind_sha="$KIND_SHA256_ARM64" kubectl_sha="$KUBECTL_SHA256_ARM64" helm_sha="$HELM_SHA256_ARM64" ;; \
      *) echo "no pinned Kubernetes tools for $arch" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -fsSLo kind "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-linux-${arch}"; \
    curl -fsSLo kubectl "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${arch}/kubectl"; \
    curl -fsSLo helm.tgz "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${arch}.tar.gz"; \
    printf '%s  kind\n%s  kubectl\n%s  helm.tgz\n' "$kind_sha" "$kubectl_sha" "$helm_sha" | sha256sum -c -; \
    install -m 0755 kind kubectl /usr/local/bin/; \
    tar -xzf helm.tgz --strip-components=1 -C /usr/local/bin "linux-${arch}/helm"; \
    rm -f kind kubectl helm.tgz

# OSV-Scanner, for a release's known vulnerabilities (release/scan.sh runs
# it sealed: no network, no token); the archive has no scanner. The
# project's release binaries, checksum-pinned.
ARG OSV_SCANNER_VERSION=2.7.0
ARG OSV_SCANNER_SHA256_AMD64=160c6253e2922e9e0a6a9771c5c4c40a4b3c6090b34a3cdd5e80c96cc2e4218b
ARG OSV_SCANNER_SHA256_ARM64=4061ef4c021ed3c47f203b818f337e351336cc5cd67c0274fc36bd1dfc728dd7
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sha="$OSV_SCANNER_SHA256_AMD64" ;; \
      arm64) sha="$OSV_SCANNER_SHA256_ARM64" ;; \
      *) echo "no pinned OSV-Scanner for $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/osv-scanner "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/osv-scanner_linux_${arch}"; \
    echo "${sha}  /tmp/osv-scanner" | sha256sum -c -; \
    install -m 0755 /tmp/osv-scanner /usr/local/bin/osv-scanner; \
    rm /tmp/osv-scanner

# The Tauri CLI, for the installers: the project's release binary for amd64,
# checksum-pinned; other architectures build the same version from crates.io,
# and so do the release's macOS and Windows builds (release.yml reads it here).
ARG TAURI_CLI_VERSION=2.12.2
ARG TAURI_CLI_SHA256_AMD64=35c7b50266256cb6d5f757608d7097a59a5faeb0c75879ee1f504652a9b6087e
RUN set -eu; \
    if [ "$(dpkg --print-architecture)" = amd64 ]; then \
      release="https://github.com/tauri-apps/tauri/releases/download/tauri-cli-v${TAURI_CLI_VERSION}"; \
      curl -fsSLo /tmp/tauri.tgz "${release}/cargo-tauri-x86_64-unknown-linux-gnu.tgz"; \
      echo "${TAURI_CLI_SHA256_AMD64}  /tmp/tauri.tgz" | sha256sum -c -; \
      tar -xzf /tmp/tauri.tgz -C /usr/local/bin cargo-tauri; \
      rm /tmp/tauri.tgz; \
    else \
      CARGO_HOME=/tmp/cargo cargo install tauri-cli --locked --version "${TAURI_CLI_VERSION}" --root /usr/local; \
      rm -rf /tmp/cargo; \
    fi

# From PyPI (requirements.txt, pinned): PyInstaller for the standalone
# hardcode binary and ruff for formatting; the archive has neither. The venv
# sees the archive's packages, so the binary bundles the archive's PyYAML,
# and the venv's pip builds the CLI's wheel with the archive's setuptools.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv --system-site-packages /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && ln -s /opt/venv/bin/pyinstaller /opt/venv/bin/ruff /opt/venv/bin/pip /usr/local/bin/ \
 && rm /tmp/requirements.txt

COPY entrypoint.sh /usr/local/bin/build-entrypoint
# HARDCODE_BUILD_IMAGE tells the scripts they're inside. Caches live in
# /cache (a host directory run.sh mounts); cargo's target directory is the
# default one in the checkout (mounted at its own path), so builds are
# incremental and the outputs are at hand.
ENV HARDCODE_BUILD_IMAGE=1 HOME=/tmp CARGO_HOME=/cache/cargo XDG_CACHE_HOME=/cache/xdg
ENTRYPOINT ["/usr/local/bin/build-entrypoint"]
CMD ["bash"]
