# Tiny TCP forwarder. The VS Code container network is internal (no route out), and Docker
# doesn't publish ports from internal networks, so this container sits on both
# that network and a normal one and forwards the published ports inward.
# Ubuntu 26.04 (resolute), one dated build of it, by digest: Dependabot moves
# it to the next dated build once that is a week old (docs/development.md,
# "Dependencies").
FROM ubuntu:resolute-20261006@sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944

RUN apt-get update \
 && apt-get install -y --no-install-recommends socat \
 && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 entrypoint.sh /usr/local/bin/gateway

USER nobody
ENTRYPOINT ["/usr/local/bin/gateway"]
