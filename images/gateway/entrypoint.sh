#!/bin/bash
# Forwards TCP ports from the host into the internal network, which has no
# other way in.
#
# GATEWAY_FORWARDS is a space-separated list of listen_port:target_host:target_port,
# e.g. "8000:vscode:8000 8080:docker:8080".
set -euo pipefail

for forward in ${GATEWAY_FORWARDS:?GATEWAY_FORWARDS is empty}; do
  IFS=: read -r listen host port <<<"$forward"

  echo "gateway: :${listen} -> ${host}:${port}"
  socat "TCP-LISTEN:${listen},fork,reuseaddr" "TCP:${host}:${port}" &
done

# If any forwarder dies, exit, so Docker restarts the container with all of them.
wait -n
exit 1
