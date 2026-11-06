# The build image: the Ubuntu 26.04 environment everything in this repository
# is built and tested in. Nothing is installed on workstations or CI runners;
# images/build/run.sh runs a command in here with the checkout mounted, and
# the Docker socket for the suites that drive Docker (tests/*.py and
# app/build.sh run themselves in it). Archive packages first; the vendor pins
# below are what the archive doesn't carry, each with its reason. Python
# libraries are the exception: uv installs them all from PyPI, as uv.lock
# locks them by hash, and none come from the archive, so the two never mix.
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
      # the suites' tools; Python, venv (and its pip) to install uv, and the
      # shared library PyInstaller bundles; the libraries are in uv.lock;
      # iproute2's ip for tests/proxy.py, which routes squid to its origins
      ca-certificates curl git file jq openssl openssh-client xvfb iproute2 \
      python3 python3-venv libpython3.14 \
      # the app: Rust (and rustfmt) and Tauri's libraries; patchelf and squashfs for the AppImage
      rustc cargo rustfmt build-essential pkg-config libssl-dev patchelf squashfs-tools \
      libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev libayatana-appindicator3-dev \
      # the Docker CLI, buildx and compose, talking to the host's daemon over its socket
      docker.io docker-buildx docker-compose-v2 \
      # one platform of an image, straight from its registry, for the vulnerability scan
      skopeo \
 && rm -rf /var/lib/apt/lists/*

# Kubernetes tooling for the chart and kind suites; the archive carries none
# of it. Published checksums, both architectures. These ARG pins (and the
# Tauri CLI's and OSV-Scanner's) move with maintenance/pins.py vendor, each
# version with its publisher's checksums, a week after its release;
# Dependabot moves the base image, uv and uv.lock.
ARG KIND_VERSION=0.33.0
ARG KIND_SHA256_AMD64=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d
ARG KIND_SHA256_ARM64=20022bee6cfcd5086cb7234d218e3454e6090022f2a8f55d1fa7fcf42c3867a2
ARG KUBECTL_VERSION=1.37.2
ARG KUBECTL_SHA256_AMD64=c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4
ARG KUBECTL_SHA256_ARM64=fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509
ARG HELM_VERSION=4.3.1
ARG HELM_SHA256_AMD64=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b
ARG HELM_SHA256_ARM64=8cf8f66ecbc252ac8f7f06cca68aba88508ddfde3a6a34feb1678ad1694b4f88
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
ARG TAURI_CLI_VERSION=2.12.4
ARG TAURI_CLI_SHA256_AMD64=820ae3c7580ca637cad15f65638960ee2a96e5a491e224691c86d6f25e6c9e8a
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

# The AppImage's tools, which the Tauri CLI would otherwise download without
# checking them each time it makes an AppImage, one from a moving tag:
# AppRun and linuxdeploy at the release tags this Tauri CLI names, and
# linuxdeploy's AppImage plugin at a numbered release rather than its
# "continuous" one. In /opt/tauri-tools under the names the CLI looks for;
# app/build.sh gives them to it, and fails a bundle for which it fetched
# anything itself. GitHub's digests where it publishes them; the plugin's
# release predates those, so its are the files' own, taken when it was
# pinned. They move with the Tauri CLI, by hand: pins.py ages dates them.
#
# And the runtime the app's AppImage starts with, which the plugin's
# appimagetool would otherwise download as it packages the app, unchecked,
# from type2-runtime's moving "continuous" release: the one at the front of
# the pinned plugin, taken out once its checksum has been checked, with
# what the plugin's own build wrote into it cleared (appimage-runtime.sh).
# app/build.sh hands it to the plugin (LDAI_RUNTIME_FILE).
ARG APPRUN_TAG=apprun-old
ARG APPRUN_SHA256_AMD64=f30140a43a0a59e46db21bdefdf749b9e9f2c6946e92afabbacf98b8ae73fb4f
ARG APPRUN_SHA256_ARM64=072f17c0895a85c490282fe5395c5007e5fc75da727e553b3b8fb680feb11578
ARG LINUXDEPLOY_TAG=linuxdeploy-07333c6
ARG LINUXDEPLOY_SHA256_AMD64=36a2d7e274d12e1050d0e9ecfe11d339ed54720b2bec464c286d53f8b07f5c62
ARG LINUXDEPLOY_SHA256_ARM64=556ab80baa98e600aa80f0dcedfb70bca0e1ce7e9f147fb345be3fcc3e91b2b1
ARG LINUXDEPLOY_PLUGIN_APPIMAGE_TAG=1-alpha-20250213-1
ARG LINUXDEPLOY_PLUGIN_APPIMAGE_SHA256_AMD64=992d502a248e14ab185448ddf6f6e7d25558cb84d4623c354c3af350c25fccb3
ARG LINUXDEPLOY_PLUGIN_APPIMAGE_SHA256_ARM64=83c292149274965a865dcd44c135cfca8ba28c6b7de3eb628d4b8b5f248af17c
COPY appimage-runtime.sh /tmp/appimage-runtime.sh
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) arch=x86_64 apprun_sha="$APPRUN_SHA256_AMD64" linuxdeploy_sha="$LINUXDEPLOY_SHA256_AMD64" plugin_sha="$LINUXDEPLOY_PLUGIN_APPIMAGE_SHA256_AMD64" ;; \
      arm64) arch=aarch64 apprun_sha="$APPRUN_SHA256_ARM64" linuxdeploy_sha="$LINUXDEPLOY_SHA256_ARM64" plugin_sha="$LINUXDEPLOY_PLUGIN_APPIMAGE_SHA256_ARM64" ;; \
      *) echo "no pinned AppImage tools for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    tauri="https://github.com/tauri-apps/binary-releases/releases/download"; \
    plugin="https://github.com/linuxdeploy/linuxdeploy-plugin-appimage/releases/download"; \
    mkdir -p /opt/tauri-tools; \
    cd /opt/tauri-tools; \
    curl -fsSLo "AppRun-${arch}" "${tauri}/${APPRUN_TAG}/AppRun-${arch}"; \
    curl -fsSLo "${LINUXDEPLOY_TAG}-${arch}.AppImage" "${tauri}/${LINUXDEPLOY_TAG}/linuxdeploy-${arch}.AppImage"; \
    curl -fsSLo linuxdeploy-plugin-appimage.AppImage "${plugin}/${LINUXDEPLOY_PLUGIN_APPIMAGE_TAG}/linuxdeploy-plugin-appimage-${arch}.AppImage"; \
    printf '%s  %s\n' \
      "$apprun_sha" "AppRun-${arch}" \
      "$linuxdeploy_sha" "${LINUXDEPLOY_TAG}-${arch}.AppImage" \
      "$plugin_sha" linuxdeploy-plugin-appimage.AppImage \
      | sha256sum -c -; \
    chmod 0755 ./*; \
    bash /tmp/appimage-runtime.sh ./linuxdeploy-plugin-appimage.AppImage "runtime-${arch}"; \
    rm /tmp/appimage-runtime.sh

# uv, from PyPI as uv/requirements.txt pins it, every file checked against its
# hash: the archive's pip installs it into a venv of its own, /opt/uv; the
# archive has no uv. It stays, to lock the Python packages again.
COPY uv/requirements.txt /tmp/uv-requirements.txt
RUN python3 -m venv /opt/uv \
 && /opt/uv/bin/pip install \
      --no-cache-dir \
      --require-hashes \
      --only-binary :all: \
      -r /tmp/uv-requirements.txt \
 && ln -s /opt/uv/bin/uv /usr/local/bin/uv \
 && rm /tmp/uv-requirements.txt

# The Python libraries, as uv.lock locks them, into /opt/venv: uv checks every
# file against its hash, refuses a lock that doesn't match pyproject.toml,
# takes wheels only (so no package's build script runs), and uses the
# archive's Python, never downloading one (UV_PYTHON_DOWNLOADS). First on
# PATH, so python3, pyinstaller, ruff and isort are the venv's everywhere.
ENV UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock /tmp/build-python/
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync \
      --project /tmp/build-python \
      --locked \
      --no-build \
      --no-cache \
      --python /usr/bin/python3 \
 && rm -r /tmp/build-python
ENV PATH="/opt/venv/bin:$PATH"

COPY entrypoint.sh /usr/local/bin/build-entrypoint
# HARDCODE_BUILD_IMAGE tells the scripts they're inside. Caches live in
# /cache (a host directory run.sh mounts); cargo's target directory is the
# default one in the checkout (mounted at its own path), so builds are
# incremental and the outputs are at hand.
ENV HARDCODE_BUILD_IMAGE=1 HOME=/tmp CARGO_HOME=/cache/cargo XDG_CACHE_HOME=/cache/xdg
ENTRYPOINT ["/usr/local/bin/build-entrypoint"]
CMD ["bash"]
