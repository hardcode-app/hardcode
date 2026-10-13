# Squid, OpenSSL build, from the Ubuntu 26.04 archive. The OpenSSL build is
# the one that can inspect TLS (ssl-bump); Canonical's ubuntu/squid image is
# the GnuTLS build, which can't.
#
# Also carries the allowlist renderer; Compose, the CLI and the Kubernetes
# operator all run it from this one image.
ARG UBUNTU_VERSION=26.04
FROM ubuntu:${UBUNTU_VERSION}

RUN apt-get update \
 && apt-get install -y --no-install-recommends squid-openssl openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --chmod=0644 squid.conf /etc/squid/squid.conf
COPY --chmod=0755 entrypoint.sh /usr/local/bin/squid-entrypoint
COPY --chmod=0755 make-ca.sh /usr/local/bin/squid-make-ca
COPY --chmod=0755 allowlist-render.sh /usr/local/bin/allowlist-render

USER proxy
ENTRYPOINT ["/usr/local/bin/squid-entrypoint"]
