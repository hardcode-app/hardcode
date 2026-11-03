# GitHub from the sandbox

This page shows how to work on a GitHub repository from inside a sandbox.
The sandbox gets access to that one repository, with a token that works
for that repository only.

## Set it up

1. **Allow the repository.** In the app, open **Allowlist**, type
   `github:OWNER/REPO` in **Entry**, choose read-write under **Access**,
   and click **Add**. Or run this on your computer:

   ```sh
   hardcode allow github:OWNER/REPO rw
   ```

   Replace `OWNER/REPO` with the repository's owner and name, as they
   appear in its address on GitHub.

2. **Give the sandbox a token.** In the sandbox's terminal, run:

   ```sh
   hardcode-github OWNER/REPO
   ```

   It prints a link to GitHub's page for a new token, with the form already
   filled in. On that page, choose **Only select repositories**, pick the
   repository, and click **Generate token**. Copy the token and paste it
   into the terminal.

   The command saves the token, sets up git, and checks that everything
   works. If something is missing, it tells you what to do.

3. **Clone the repository:**

   ```sh
   git clone https://github.com/OWNER/REPO.git
   ```

   A clone that uses an SSH address (`git@github.com:…`) works too: in the
   sandbox, it now goes over HTTPS.

If the repository belongs to an organisation, the organisation may need to
allow these tokens first. On GitHub, see the organisation's
**Settings → Personal access tokens**.

## The token

The link asks for these permissions, and nothing else:

| Permission | Lets the sandbox |
|---|---|
| Contents: read and write | push |
| Pull requests: read and write | open pull requests |
| Actions: read and write | run workflows again |
| Workflows | change files in `.github/workflows` |

The token expires after 30 days. When it does, run `hardcode-github
OWNER/REPO` again with a new one. To stop using a token, delete it on
GitHub.

Each repository has its own token. Running `hardcode-github` for a second
repository adds a second token, and the first keeps working.

## What works

- `git clone`, `pull` and `push`, for that repository.
- The `gh` command, which uses that repository's token automatically.

A few `gh` commands, such as `gh pr list` and `gh pr create`, don't work,
because they would need access to all of GitHub. Use `gh api` instead. For
example, to open a pull request:

```sh
gh api repos/OWNER/REPO/pulls -f title="My change" -f head=my-branch -f base=main
```

## Why not SSH, or `gh auth login`?

Both give the sandbox much more than one repository:

- **SSH** opens all of GitHub. Anything running in the sandbox could send
  your code to any repository.
- **`gh auth login`** creates a token for every repository your account
  can reach, and it doesn't expire.

With the steps above, the sandbox reaches one repository, and a token that
leaks is only good for that repository.

Anything running in a sandbox can read the tokens saved in it. Keep a
project you trust less in a profile of its own.
