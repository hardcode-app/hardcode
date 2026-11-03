#!/usr/bin/env bash
# gh, with the token of the GitHub repository it works on (docs/github.md).
# Installed as /usr/local/bin/gh, ahead of the real one on the PATH:
#
#   gh api repos/OWNER/REPO/...   that repository's token
#   gh -R OWNER/REPO ...          that repository's token
#   gh ...                        the token of the current folder's origin
#
# The tokens are hardcode-github's. A GH_TOKEN or GITHUB_TOKEN of your own
# wins, and a repository with no token saved leaves gh as it would be.
set -euo pipefail

real_gh="${HARDCODE_REAL_GH:-/usr/bin/gh}"

# repository_of <arguments...>: the OWNER/REPO the arguments name, if any.
repository_of() {
  local previous="" argument
  for argument in "$@"; do
    if [ "$previous" = -R ] || [ "$previous" = --repo ]; then
      echo "$argument"
      return
    fi

    case "$argument" in
      --repo=*)
        echo "${argument#--repo=}"
        return
        ;;
    esac

    # An API path: repos/OWNER/REPO, then more.
    if [[ "$argument" =~ ^/?repos/([^/?]+)/([^/?]+) ]]; then
      echo "${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
      return
    fi

    previous="$argument"
  done
}

# origin_repository: the OWNER/REPO of the current folder's origin, when it is on GitHub.
origin_repository() {
  local origin
  origin="$(git remote get-url origin 2>/dev/null)" || return 0

  case "$origin" in
    https://github.com/*) echo "${origin#https://github.com/}" ;;
    git@github.com:*) echo "${origin#git@github.com:}" ;;
    ssh://git@github.com/*) echo "${origin#ssh://git@github.com/}" ;;
  esac
}

if [ -z "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]; then
  repository="$(repository_of "$@")"
  if [ -z "$repository" ]; then
    repository="$(origin_repository)"
  fi

  if [ -n "$repository" ] && token="$(hardcode-github token "$repository" 2>/dev/null)"; then
    export GH_TOKEN="$token"
  fi
fi

exec "$real_gh" "$@"
