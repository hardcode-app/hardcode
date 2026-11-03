#!/usr/bin/env bash
# Sets the sandbox up to work on one GitHub repository over HTTPS, with a
# fine-grained token that reaches only that repository (docs/github.md),
# and gives git and gh that token afterwards:
#
#   hardcode-github OWNER/REPO         the token, git's settings, then a check
#   hardcode-github token OWNER/REPO   print the repository's token (gh's wrapper asks)
#   hardcode-github credential get     git's credential helper (git runs it)
#
# Each repository's token is a file of its own,
# ~/.config/github-tokens/OWNER/REPO, readable by its owner alone: a second
# repository adds a token and replaces none. The sandbox can't change its
# own allowlist, so the check says which entries to add on your computer.
set -euo pipefail

TOKENS="$HOME/.config/github-tokens"

# usage: how to run it, on stderr, and the exit code for a mistake.
usage() {
  echo "usage: hardcode-github OWNER/REPO | token OWNER/REPO | credential get" >&2
  exit 2
}

# repository_name <OWNER/REPO>: the name as the token's file has it (lower
# case, without ".git"), or a failure saying what's wrong. GitHub's names
# ignore case; an owner is letters, digits and inner hyphens, a repository
# letters, digits, ".", "-" and "_".
repository_name() {
  local name="${1%/}"
  name="${name%.git}"

  if ! [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$ ]] || [[ "${name#*/}" =~ ^\.+$ ]]; then
    echo "hardcode-github: '$1' isn't a repository: OWNER/REPO, as in its address on GitHub" >&2
    return 1
  fi

  echo "${name,,}"
}

# token_of <OWNER/REPO>: the repository's saved token, or a failure when it has none.
token_of() {
  local name
  name="$(repository_name "$1")" || return 1

  local file="$TOKENS/$name"
  [ -s "$file" ] || return 1
  head -n 1 "$file"
}

# credential <action>: git's credential helper. On "get", git's request
# (key=value lines, then a blank one) names the address; with useHttpPath
# its path is OWNER/REPO.git, and the answer is that repository's token.
# "store" and "erase" keep and forget nothing: the token is a file of yours.
credential() {
  local host="" path="" key value
  while IFS='=' read -r key value; do
    [ -n "$key" ] || break
    case "$key" in
      host) host="$value" ;;
      path) path="$value" ;;
    esac
  done

  [ "$1" = get ] && [ "$host" = github.com ] || return 0

  # OWNER/REPO.git, or more after it (git LFS): its first two parts.
  local owner="${path%%/*}"
  local rest="${path#*/}"
  local token
  token="$(token_of "$owner/${rest%%/*}" 2>/dev/null)" || return 0

  printf 'username=x-access-token\npassword=%s\n' "$token"
}

# token_link <owner> <repo>: GitHub's page for a new fine-grained token,
# filled in by its documented URL parameters: the name, the owner, 30 days
# and the permissions docs/github.md lists. Only the repository can't be
# filled in; that one is chosen on the page.
token_link() {
  local name="hardcode: $1/$2"
  local query
  query="$(jq -rn \
    --arg name "${name:0:40}" \
    --arg description "For a HardCode sandbox: $1/$2 only" \
    --arg owner "$1" \
    '"name=\($name|@uri)&description=\($description|@uri)&target_name=\($owner|@uri)"')"

  echo "https://github.com/settings/personal-access-tokens/new?${query}&expires_in=30&contents=write&pull_requests=write&actions=write&workflows=write"
}

# save_token <file> <token>: the token in its file, readable by its owner alone.
save_token() {
  install -d -m 0700 "$TOKENS" "$(dirname "$1")"
  (
    umask 077
    printf '%s\n' "$2" > "$1.new"
  )
  mv "$1.new" "$1"
}

# configure_git: git's settings for GitHub, in the sandbox's own ~/.gitconfig.
# The token of the repository each address names (useHttpPath hands the
# helper the path), from this helper alone: an empty value first clears any
# helper set before it. SSH addresses go over HTTPS.
configure_git() {
  git config --global credential.https://github.com.useHttpPath true
  git config --global --replace-all credential.https://github.com.helper ""
  git config --global --add credential.https://github.com.helper "!hardcode-github credential"
  git config --global --replace-all url.https://github.com/.insteadOf "git@github.com:"
  git config --global --add url.https://github.com/.insteadOf "ssh://git@github.com/"
}

# request <url> [bearer|basic] <token>: GET the address through the proxy,
# with the token on curl's stdin rather than its command line, and print
# the answer's last status code and its headers. Fails as curl does when
# the proxy refuses the host outright.
request() {
  local url="$1" kind="${2:-}" token="${3:-}"
  local config=""
  case "$kind" in
    bearer) config="header = \"Authorization: Bearer $token\"" ;;
    basic) config="user = \"x-access-token:$token\"" ;;
  esac

  printf '%s\n' "$config" | curl -sS -o /dev/null -D - -K - "$url"
}

# status <headers>: the status code of the last answer in them (a proxy's
# "Connection established" comes first).
status() {
  tr -d '\r' <<<"$1" | awk '/^HTTP\// { code = $2 } END { print code }'
}

# allowed <url>: whether the proxy lets a GET to the address through to
# GitHub (whatever GitHub then answers).
allowed() {
  local headers
  headers="$(request "$1" 2>/dev/null)" || return 1
  ! grep -qi '^x-squid-error:' <<<"$headers"
}

# check <owner> <repo> <token>: what works, and for what doesn't, what to do.
# Exits 1 when anything doesn't.
check() {
  local owner="$1" repo="$2" token="$3"
  local problems=0

  echo
  echo "Checking $owner/$repo:"

  local git_url="https://github.com/$owner/$repo.git/info/refs?service=git-upload-pack"
  local api_url="https://api.github.com/repos/$owner/$repo"

  # Until the proxy lets both through, the token can't be checked either.
  if allowed "$git_url" && allowed "$api_url"; then
    echo "  ok       the allowlist lets git and GitHub's API reach it"
  else
    echo "  missing  the allowlist entries for it. On your computer, for this profile:"
    echo "             hardcode allow github:$owner/$repo rw"
    echo "           or add github:$owner/$repo, read-write, on the app's Allowlist page."
    echo
    echo "Then run hardcode-github $owner/$repo again (Enter keeps the saved token) to check the token."
    return 1
  fi

  local headers code
  headers="$(request "$api_url" bearer "$token" 2>/dev/null)" || true
  code="$(status "$headers")"
  case "$code" in
    200)
      local expires
      expires="$(tr -d '\r' <<<"$headers" | awk -F': ' 'tolower($1) == "github-authentication-token-expiration" { print $2 }')"
      echo "  ok       the token reaches it${expires:+ (it expires $expires)}"
      ;;
    401)
      echo "  refused  GitHub doesn't accept the token: copied in full? expired or deleted?"
      problems=1
      ;;
    404)
      echo "  unseen   the token can't see $owner/$repo. On the token's page on GitHub, check:"
      echo "             its Resource owner is $owner (GitHub can make it under your own account instead)"
      echo "             $repo is among its repositories"
      echo "           An organisation may also have to approve it first: its Settings → Personal access tokens."
      problems=1
      ;;
    *)
      echo "  unknown  GitHub's API answered ${code:-nothing}: try again in a moment"
      problems=1
      ;;
  esac

  # Asking for the push service is answered only for a token that may push.
  headers="$(request "https://github.com/$owner/$repo.git/info/refs?service=git-receive-pack" basic "$token" 2>/dev/null)" || true
  code="$(status "$headers")"
  if [ "$code" = 200 ]; then
    echo "  ok       the token may push"
  elif [ "$problems" = 0 ]; then
    echo "  read     the token may read but not push: give it Contents, read and write"
    problems=1
  fi

  echo
  if [ "$problems" = 0 ]; then
    echo "Ready. Clone it with:   git clone https://github.com/$owner/$repo.git"
    echo "gh works in its folder: gh api repos/$owner/$repo/pulls ... (docs/github.md)"
  else
    echo "Fix what's above, then run hardcode-github $owner/$repo again (Enter keeps the saved token)."
  fi

  return "$problems"
}

# setup <OWNER/REPO>: the token, git's settings, then the check.
setup() {
  local name
  name="$(repository_name "$1")" || exit 2
  local owner="${name%%/*}" repo="${name#*/}"
  local file="$TOKENS/$name"

  if [ -s "$file" ]; then
    echo "A token for $owner/$repo is saved. Paste a new one to replace it, or press Enter to keep it."
  else
    echo "Make a token for $owner/$repo on GitHub. Open this link (it fills the form in), then:"
    echo "  under Repository access, choose Only select repositories, then $repo;"
    echo "  check that Resource owner says $owner;"
    echo "  Generate token, and copy it."
  fi
  echo
  echo "  $(token_link "$owner" "$repo")"
  echo

  # Typed without showing on a terminal; read as it comes otherwise.
  local token=""
  if [ -t 0 ]; then
    read -r -s -p "Token: " token
    echo
  else
    read -r token || true
  fi

  if [ -n "$token" ]; then
    case "$token" in
      github_pat_*) ;;
      *)
        echo "hardcode-github: that isn't a fine-grained token (they start github_pat_); a classic one reaches every repository you can" >&2
        exit 1
        ;;
    esac
    save_token "$file" "$token"
    echo "Saved: $file"
  elif [ ! -s "$file" ]; then
    echo "hardcode-github: no token given" >&2
    exit 1
  fi

  # A token saved by hand may have been left readable by others.
  chmod 0700 "$TOKENS" "$(dirname "$file")"
  chmod 0600 "$file"

  configure_git
  check "$owner" "$repo" "$(head -n 1 "$file")"
}

case "${1:-}" in
  "" | -h | --help) usage ;;
  credential)
    [ "$#" -eq 2 ] || usage
    credential "$2"
    ;;
  token)
    [ "$#" -eq 2 ] || usage
    token_of "$2"
    ;;
  *)
    [ "$#" -eq 1 ] || usage
    setup "$1"
    ;;
esac
