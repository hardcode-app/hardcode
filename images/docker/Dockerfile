# The Docker daemon beside the VS Code container: Docker's own rootless
# image, as it is (its dockerd, containerd and runc, its entrypoint, TLS and
# rootlesskit set-up), with HardCode's runc wrapper first on PATH.
#
# Docker's image rather than the Ubuntu archive's docker.io: the archive's
# (29.1) is older than the Docker in use, and the daemon never goes back.
# By digest: Dependabot moves it once a release is a week old, together
# with the chart's docker.image (docs/development.md, "Dependencies").
FROM docker:29.9.2-dind-rootless@sha256:ae083cc7de51e7a8e5a597f8fb774ca7914c408a95fc0f1477826a913479518a

# Python for the wrapper (standard library only), from the base image's own
# Alpine branch, which the digest above fixes. Pinned to its minor version,
# not a build: Alpine's mirrors keep only the newest build of a package in a
# branch, so an exact pin breaks with its next security update, while a new
# minor only comes with a newer branch, which fails this line until it is
# moved forward. The image's openssl makes the Java trust stores.
USER root
RUN apk add --no-cache "python3~3.14"

# /usr/local/sbin is ahead of /usr/local/bin, where Docker's runc is: the
# wrapper runs that one by its path, and nothing of Docker's is moved.
# containerd's shim and dockerd's BuildKit (RUN steps) both find "runc" on
# PATH.
COPY --chmod=0755 hardcode_runc.py /usr/local/sbin/runc

USER rootless
