# Tiny TCP forwarder. The VS Code container network is internal (no route out), and Docker
# doesn't publish ports from internal networks, so this container sits on both
# that network and a normal one and forwards the published ports inward.
ARG UBUNTU_VERSION=26.04
FROM ubuntu:${UBUNTU_VERSION}

RUN apt-get update \
 && apt-get install -y --no-install-recommends socat \
 && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 entrypoint.sh /usr/local/bin/gateway

USER nobody
ENTRYPOINT ["/usr/local/bin/gateway"]
