#!/bin/bash
# The one-shot "pki" service: creates the CA squid signs inspected tunnels
# with, once, and publishes its certificate (never the key) for the VS Code container
# and the Docker daemon to trust.
#
#   /proxy-ca       the key and the certificate   squid only
#   /proxy-ca-pub   the certificate only          the VS Code container, docker
set -euo pipefail

if [ ! -s /proxy-ca/ca.key ] || [ ! -s /proxy-ca/ca.pem ]; then
  echo "pki: generating a new proxy CA"
  openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
    -subj "/CN=vscode egress proxy CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout /proxy-ca/ca.key \
    -out /proxy-ca/ca.pem \
    2>/dev/null
fi

# Squid's user reads the key; nobody else does.
chown proxy:proxy /proxy-ca/ca.key /proxy-ca/ca.pem
chmod 0400 /proxy-ca/ca.key
chmod 0444 /proxy-ca/ca.pem

install -m 0444 /proxy-ca/ca.pem /proxy-ca-pub/ca.pem

# The usual public roots plus ours, for tools that take a single bundle file.
cat /etc/ssl/certs/ca-certificates.crt /proxy-ca/ca.pem > /proxy-ca-pub/ca-bundle.pem.tmp
mv /proxy-ca-pub/ca-bundle.pem.tmp /proxy-ca-pub/ca-bundle.pem
chmod 0444 /proxy-ca-pub/ca-bundle.pem

openssl x509 -in /proxy-ca/ca.pem -noout -subject -enddate -fingerprint -sha256 | sed 's/^/pki: /'
