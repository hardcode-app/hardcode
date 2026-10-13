#!/usr/bin/env bash
# Prints the ssh_config stanza that sends every SSH connection through the
# egress proxy, which only tunnels to hosts with an "ssh" entry in the
# allowlist:
#
#   hardcode-ssh-proxy-config <proxy host> <proxy port>
#
# OpenSSH expands %h and %p to the destination host and port at connect
# time; printf needs them written %%h and %%p.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: hardcode-ssh-proxy-config <proxy host> <proxy port>" >&2
  exit 2
fi

printf 'Host *\n    ProxyCommand nc -X connect -x %s:%s %%h %%p\n' "$1" "$2"
