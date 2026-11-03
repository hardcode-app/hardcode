# Staying safe

HardCode lets you run AI agents, packages you haven't vetted and code you
didn't write without handing them your computer. It is a strong boundary,
but it enforces the rules you give it: a sandbox is exactly as open as its
allowlist, its shared folders and the credentials you put in it. This page
says what HardCode protects you from, what it can't, and the habits that
keep you protected.

The complete, technical account is the [security model](security.md).

## What HardCode protects you from

Everything that runs inside a sandbox is kept in it: Claude Code, Codex,
the packages you or they install, VS Code extensions, and any container
started there. None of it:

- **can see your computer's files**, except the folders you share with
  the sandbox (mounts);
- **can read the credentials on your computer**: your SSH keys, cloud
  logins, browser sessions, or secrets in your other projects;
- **can reach your computer's own services or your local network**: the
  sandbox has no route to them, and a name on the allowlist that points at
  a private address (your router, a cloud provider's metadata service) is
  refused;
- **can reach any internet address that isn't on the allowlist**. HTTPS is
  inspected, so an entry can be one path rather than a whole site:
  `github.com/your-org` allows your organisation and nothing else on
  GitHub;
- **can upload anywhere that isn't marked `rw`**. Entries are read-only
  unless you say otherwise: a package registry can be downloaded from but
  not published to, a repository cloned but not pushed to;
- **can send data out through name lookups (DNS)**. Names that aren't
  allowed are answered by HardCode itself and never leave your computer;
- **can take your machine down**: each sandbox has hard limits on memory
  and on the number of processes.

VS Code is published only on your own computer, behind a token. And the
VS Code extensions a profile installs come only from Marketplace publishers
whose domain the Marketplace has verified.

## What HardCode can't protect you from

These are the limits of the boundary. Each one comes with what to do about
it.

- **Anything allowed `rw` can receive your data.** If the sandbox holds
  credentials for a service it may upload to (the Claude API, a repository
  you push to), anything inside can send data there. Keep `rw` entries
  few, and make them paths rather than whole hosts.
- **Read-only means "no uploads by HTTP method", not "no data out".** A
  read-only entry still allows requests whose address carries data (in a
  query string, say), and a service that accepted uploads that way would
  get them. None of the entries HardCode provides does, but it is one more
  reason to keep your own list short.
- **An `ssh` entry is a whole host, both ways, uninspected.** SSH can't be
  looked into, so paths and read-only don't apply to it. Use HTTPS with a
  path where you can.
- **A writable shared folder is a way out.** Anything inside, Claude
  included, can change files in a writable mount, among them files your
  computer runs later: `.git/hooks`, `.git/config`, build scripts. Your
  computer runs those the next time you use that project there. Mount
  read-only what you only read, and review changes before you run them on
  your computer (see [below](#share-folders-carefully)). Files written there
  as root inside the sandbox are root's on your computer too: expect to
  need `sudo` to clean up.
- **Whatever you sign in to inside the sandbox, everything in it can use.**
  Claude Code's sign-in, a GitHub token, an API key: any code running in
  the sandbox can read them, and use them wherever the allowlist reaches.
  Use tokens limited to what the sandbox needs, and revoke them when
  you're done.
- **Inside the sandbox, anything can become root.** `sudo` needs no
  password there. The boundary is the container and its network, not the
  user account, so don't rely on file permissions inside.
- **Docker inside the sandbox is powerful.** Its daemon runs with some of
  Docker's own restrictions relaxed, as running Docker without root
  requires. Containers you start inside are still behind the allowlist.
  When a profile doesn't need containers, switch Docker off (see
  [below](#use-the-harder-sandbox-when-you-can)).
- **Name lookups for subdomains of allowed names go out.** Allowing
  `example.com` lets the sandbox look up `anything.example.com`, which
  reaches example.com's name servers. Those belong to the same operator
  as the site you allowed.
- **The proxy sees your traffic unencrypted.** It decrypts HTTPS to check
  it, and logs addresses, not contents. On your computer, whoever can use
  your Docker can see into it.
- **The app's updates are only as trustworthy as the key that signs
  them.** The app checks GitHub for a newer release once as it starts.
  That check comes from your computer, not from a sandbox: the allowlist
  doesn't apply to the app.
- **Code in the sandbox can install VS Code extensions by itself.** The
  verified-publisher rule applies to the profile's own list, not to what
  runs inside.

Running HardCode for a team on Kubernetes has a few more:
[Keeping a team's sandboxes safe](kubernetes.md#keeping-a-teams-sandboxes-safe)
says what to do about them.

## Habits that keep you protected

### Keep the allowlist narrow

- **Add paths, never whole git hosts.** `github.com/your-org`, not
  `github.com`. A whole host lets anything in the sandbox read every
  public repository there, and, with your credentials, write to any
  repository you can.
- **`rw` only where you must upload**: the one repository you push to
  (`github:your-org/app rw`), an API that you call with `POST`.
- **Add only your own servers to On your network**, and only the ones
  the sandbox needs. Each one lets the sandbox reach a machine on your
  network.
- **Be careful with `.example.com` entries** (a host and all its
  subdomains) on services where anyone can have a subdomain, such as
  hosting and "pages" services: one such entry opens every one of their
  users' sites.
- **Switch off what you don't use.** Every language and tool you switch on
  opens its hosts: leave out the ones you don't need. If you only use
  Claude Code, switch the Codex CLI off, and its `rw` hosts close with it.
  An entry HardCode provides that you never need (a container registry,
  say) can be switched off too.

### Read Blocked traffic before you allow anything

**Blocked traffic** (in the app; `hardcode blocked` in a terminal) lists
what the sandbox tried to reach and couldn't. It is how you find out what
a tool needs, and it is also where something misbehaving shows up. Before
you allow a row, ask:

- **Do I know this host, and does what I'm doing need it?** A package
  registry for a language is better opened by switching the language on
  than by adding its hosts one by one.
- **Is it telemetry?** Most refusals are analytics and error reporting.
  Leave them blocked.
- **Is it an upload?** A refused `POST` or `PUT` to a host that is already
  allowed means the entry would need `rw`. Only allow it if you mean the
  sandbox to send data there.
- **Did it start after I installed something new?** Unknown hosts showing
  up right after you add a package or an extension are worth looking into
  before anything else.

When you do allow something, prefer **Allow URL** (a path) to **Allow
host**, and read-only to `rw`. Nothing is allowed until you add it.

### Share folders carefully

- **Prefer cloning inside the sandbox** to sharing a folder: then nothing
  the sandbox does touches your computer.
- **Mount read-only** (`--ro`, or tick **read-only** in the app) whatever
  you only need to read.
- **Before you build, run or commit a writable folder's contents on your
  computer, look at what changed**: `git status` and `git diff`, and also
  the parts of a repository that `git diff` doesn't show, such as
  `.git/hooks` and `git config --local --list`.
- **Never share** your home folder, `~/.ssh`, your cloud credentials'
  folders, HardCode's own folder (where profiles are kept), or anything
  else holding secrets. `hardcode mount` refuses HardCode's own folder:
  a profile's own Dockerfile steps are kept there, and they run on your
  computer at the next rebuild.
- **If you need writable mounts often**, run Docker on your computer in a
  way that makes root inside a container an ordinary user outside it
  (rootless Docker, or Docker's user-namespace remapping), and keep those
  folders on a filesystem mounted `nosuid`. Then a file written as root in
  the sandbox can't act as root on your computer.

### Keep secrets out of the sandbox

- Don't copy SSH keys, cloud credentials, password files or production
  `.env` files into the sandbox.
- When the sandbox needs a credential, make one just for it: a token
  scoped to one repository or one project, short-lived if the service
  allows, revoked when you're done.
- For GitHub, follow [GitHub from the sandbox](github.md). The sandbox
  gets one repository, with a token for that repository only.
- For other git servers over SSH, the sandbox has an SSH key of its own,
  made the first time it starts. `cat ~/.ssh/id_ed25519.pub` shows it. Add
  it only to the repositories the sandbox works on, not to your account,
  because anything in the sandbox can use it. Remove it when you're done.
- **Leave devices off.** A device you give a profile (KVM, a GPU) is a
  way out of the sandbox if it has a bug. Give one only to a profile for
  that work, and switch it off when you're done.
- Use one profile per level of trust: a client's code in its own profile
  with its own allowlist, an experiment with a tool you don't trust in
  another. Profiles don't share containers, networks, storage or
  certificates.

### Keep VS Code to yourself

- Leave VS Code on `127.0.0.1` (`VSCODE_BIND`, the default). To share a
  sandbox with someone, put HTTPS and a sign-in in front of it, or run
  HardCode on [Kubernetes](kubernetes.md).
- The token in VS Code's address is a password. If it leaks, replace it:
  **New token…** in the app, or `hardcode token rotate`.

### Use the harder sandbox when you can

A profile that doesn't run containers doesn't need the Docker daemon.
Switching it off leaves out the one part of the sandbox that runs with
relaxed restrictions: the switch on the **docker** row of the app's
Overview, or `hardcode docker off`.

### Keep HardCode up to date

- **Install updates.** The app offers each new release in its sidebar; for
  the command, download the newer file, or install the newer package, the
  way you installed the first.
- **Rebuild after an update.** A newer HardCode brings its allowlist fixes
  to every profile at its next start, but each profile's VS Code image is
  only rebuilt when you ask: **Rebuild images** on the app's Overview, or
  `hardcode rebuild`.
- VS Code and Claude Code keep themselves up to date inside the sandbox.
  Codex comes with the image: rebuilding updates it.

### Treat a profile's folder as security settings

A profile's `allowlist.txt` and `.env` say what its sandbox may do. Review
a change to them as carefully as a change to code, and keep the folder on
your computer only: nothing in a sandbox can reach it unless you share it.

## A checklist

- [ ] Only the languages and tools I use are switched on.
- [ ] No whole git hosts on the allowlist; `rw` only where I upload.
- [ ] Shared folders are read-only unless I need to write, and none of
      them holds secrets.
- [ ] Credentials in the sandbox are narrow, and I can revoke them.
- [ ] Docker is off in profiles that don't run containers.
- [ ] I look at changes in a writable shared folder before running them on
      my computer.
- [ ] HardCode is up to date, and I rebuilt my profiles after updating.

## Found a way out?

If you find a way out of the sandbox, please report it privately, through
the *Security* tab of the
[HardCode repository](https://github.com/hardcode-app/hardcode), not in a
public issue.
