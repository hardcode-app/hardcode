# GitHub from the sandbox

The narrowest way to work on a GitHub repository from inside a sandbox:
HTTPS, one repository, and a token that reaches only that repository.
Code running in the sandbox can push to it, and nowhere else on GitHub.

## Set it up

**1. Allow the repository.** On your computer, or on the app's
**Allowlist** page (type the same into **Entry**, read-write):

```sh
hardcode allow github:OWNER/REPO rw
```

`OWNER/REPO` is the repository's owner and name, as in its address on
GitHub. It adds the repository's two entries: git's address for it
(`github.com/OWNER/REPO.git`) and GitHub's API for it
(`api.github.com/repos/OWNER/REPO`), and nothing else on GitHub.

**2. Give the sandbox a token for it.** In the sandbox's terminal:

```sh
hardcode-github OWNER/REPO
```

It prints a link to GitHub's page for a new token, with the form already
filled in: the owner, 30 days, and only the permissions below. On that
page, choose **Only select repositories** and the repository (the one part
a link can't fill in), check that **Resource owner** is the owner, then
**Generate token**, and paste the token back into the terminal. It isn't
shown as you paste.

It then saves the token, sets git up to use it, and checks: that the
allowlist lets the repository through, that the token reaches it and may
push, and when the token expires. What isn't right, it says how to fix.

**3. Clone and work as usual.**

```sh
git clone https://github.com/OWNER/REPO.git
```

A clone with an SSH address (`git@github.com:…`), such as one in a folder
shared from your computer, goes over HTTPS in the sandbox too, without
being changed.

For an organisation's repository, the organisation may have to allow
fine-grained tokens first: its **Settings → Personal access tokens**.

## The token

| Permission | Why |
|---|---|
| Contents, read and write | to push |
| Pull requests, read and write | to open them |
| Actions, read and write | to run workflows again |
| Workflows | to change files in `.github/workflows` |

Leave out the rest, *Administration* especially. Each repository's token
is a file of its own, `~/.config/github-tokens/OWNER/REPO`, readable by
you alone: `hardcode-github` for a second repository adds a token, and
replaces none. Running it again for the same one checks it, and Enter
keeps the saved token. To stop, delete the token on GitHub and its file.

## What works

- `git clone`, `pull` and `push`, for that repository.
- `gh` with the repository's token, chosen for you: the repository a
  `gh api repos/OWNER/REPO/…` path or `-R OWNER/REPO` names, or else the
  folder's `origin`. A `GH_TOKEN` of your own wins.
- GitHub's REST API for that repository, such as pull requests, issues and
  workflow runs. To open a pull request:
  `gh api repos/OWNER/REPO/pulls -f title="…" -f head=my-branch -f base=main`.

`gh auth login` and the `gh` commands built on GitHub's GraphQL API, such
as `gh pr list` and `gh pr create`, are refused by the proxy, for the
reasons below.

## Why not SSH, or `gh auth login`?

- **An `ssh` entry for github.com opens all of GitHub**, with nothing
  inspected: code in the sandbox could push anywhere on GitHub, with any
  key, including one of its own, and so send your code somewhere else.
- **`gh auth login` needs GitHub's sign-in and its GraphQL API open for
  uploads.** GraphQL is one address for all of GitHub, so that opens the
  same door. The token it makes also reaches every repository your account
  can, and doesn't expire.

The allowlist keeps the token from being used anywhere but that
repository from inside the sandbox; the fine-grained token keeps a copy
that leaves the sandbox from being worth more than that one repository.
Anything running in a sandbox can read the tokens in its home, so give a
project you trust differently a profile of its own.
