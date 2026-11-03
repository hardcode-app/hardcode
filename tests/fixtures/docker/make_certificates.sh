#!/bin/bash
# The certificates tests/docker.py's cases use, written into DIR:
#
#   ca.pem, ca.key           a CA standing for the profile's proxy CA
#   origin.pem, origin.key   a certificate from it for the origin ("origin",
#                            "localhost" and 127.0.0.1), as squid would make
#   company.crt              another CA, standing for a company's, that a
#                            build adds to an image
#
# The suite runs it in the build image (for its bash and openssl):
#
#   make_certificates.sh DIR
set -euo pipefail

directory="$1"

# A self-signed CA: NAME, its subject.
make_ca() {
  local name="$1"
  local subject="$2"

  openssl req -x509 \
    -newkey rsa:2048 \
    -nodes \
    -days 2 \
    -subj "$subject" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$directory/$name.key" \
    -out "$directory/$name.pem"
}

make_ca ca "/CN=HardCode test proxy CA"
make_ca company "/CN=HardCode test company CA"
mv "$directory/company.pem" "$directory/company.crt"

# Strict clients (Python 3.13 and newer) want the key identifiers and the
# key usages a CA's certificates carry.
cat > "$directory/origin.cnf" <<'EOF'
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:origin, DNS:localhost, IP:127.0.0.1
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
EOF

openssl req \
  -newkey rsa:2048 \
  -nodes \
  -subj "/CN=origin" \
  -keyout "$directory/origin.key" \
  -out "$directory/origin.csr"
openssl x509 -req \
  -in "$directory/origin.csr" \
  -CA "$directory/ca.pem" \
  -CAkey "$directory/ca.key" \
  -CAcreateserial \
  -days 2 \
  -extfile "$directory/origin.cnf" \
  -out "$directory/origin.pem"

chmod 0644 "$directory"/*
