#!/usr/bin/env bash
# Makes the sandbox's own SSH key, when the home has none yet: an ed25519
# key, ~/.ssh/id_ed25519, without a passphrase (nobody is there to type one
# at the VS Code container's start), its public half beside it to add to a
# git host. The entrypoint runs it at every start:
#
#   hardcode-ssh-key <comment>
#
# A home with a key already, of any kind ssh offers by default, keeps it and
# gets no other. Prints the public half's path when it made a key, and
# nothing when it didn't.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: hardcode-ssh-key <comment>" >&2
  exit 2
fi

ssh_dir="$HOME/.ssh"

# The keys ssh tries when nothing says otherwise (ssh_config's IdentityFile).
for name in id_ed25519 id_ed25519_sk id_ecdsa id_ecdsa_sk id_rsa; do
  if [ -e "$ssh_dir/$name" ]; then
    exit 0
  fi
done

install -d -m 0700 "$ssh_dir"
ssh-keygen \
  -q \
  -t ed25519 \
  -N "" \
  -C "$1" \
  -f "$ssh_dir/id_ed25519"

echo "$ssh_dir/id_ed25519.pub"
