# Squid, OpenSSL build, from the Ubuntu 26.04 archive. The OpenSSL build is
# the one that can inspect TLS (ssl-bump); Canonical's ubuntu/squid image is
# the GnuTLS build, which can't.
#
# Also carries the allowlist renderer; Compose, the CLI and the Kubernetes
# operator all run it from this one image.
# Ubuntu 26.04 (resolute), one dated build of it, by digest: Dependabot moves
# it to the next dated build once that is a week old (docs/development.md,
# "Dependencies").
FROM ubuntu:resolute-20261006@sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944

RUN apt-get update \
 && apt-get install -y --no-install-recommends squid-openssl openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --chmod=0644 squid.conf /etc/squid/squid.conf
COPY --chmod=0755 entrypoint.sh /usr/local/bin/squid-entrypoint
COPY --chmod=0755 make-ca.sh /usr/local/bin/squid-make-ca
COPY --chmod=0755 allowlist-render.sh /usr/local/bin/allowlist-render
COPY --chmod=0755 own-networks.sh /usr/local/bin/squid-own-networks

USER proxy
ENTRYPOINT ["/usr/local/bin/squid-entrypoint"]
