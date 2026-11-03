# Security model

HardCode is a sandbox for running code and AI agents you don't fully
trust, with a narrow path to the outside that you can inspect. This page is
the complete account: what is enforced, where, and what isn't. For
practical advice on using HardCode so that you stay protected, read
[Staying safe](staying-safe.md).

## The boundary

The sandbox is the **container and its network**, not the user account.
`dev`, the user inside, has `sudo` without a password, and that's fine:
even root in the VS Code container has no route out and no capability to
make one.

```
  trusted: your computer, your browser, the hardcode command and its stack, your profiles
  ─────────────────────────────────────────────────────────────────
  untrusted: everything inside vscode and docker (code, tools, Claude, containers)
             their only exits: squid (allowlisted URLs) and CoreDNS (allowlisted names)
```

`vscode` is the container VS Code and your tools run in, `docker` the
Docker daemon beside it, `squid` the proxy that checks every web request,
and CoreDNS the DNS server that answers name lookups.
[Architecture](architecture.md) shows how they fit together.

## Enforced

### The network

- **No route out.** `vscode` and `docker` are attached only to an
  `internal` Docker network in **isolated gateway mode** (Docker 28+): no
  default route, and no address on the host's side of the bridge, so
  services listening on your computer can't be reached through it either.
  On Kubernetes, a namespace-wide policy the chart installs denies all
  traffic to and from every sandbox pod before any profile exists; each
  profile's own policies then allow the VS Code container exactly two
  destinations, its squid and its CoreDNS. Those policies are applied
  before the pods they isolate, and an Ingress the API refuses can't keep
  them from being applied. Sandbox pods carry no service-account token.
- **Every HTTP(S) request is checked by URL and method.** Traffic leaves
  only through squid, on ports 80 and 443 (and the port of each `ssh`
  entry, to its host), to host names only (no IP addresses; the allowlist
  refuses them). Squid decrypts HTTPS with the
  profile's own certificate authority, so read-only entries can't be used
  to upload, and a path entry really is limited to that path: dot segments
  (`/allowed/../secret`, `%2e%2e`, `..%2f`, `..;`) and backslashes, plain or
  `%5c`-encoded, are refused in every request, since a server may tidy them
  away (servers written with Windows in mind treat a backslash as a
  separator). Refused requests trigger no DNS lookups. `tests/proxy.py`
  checks all of this against a real squid and a server that tidies paths,
  over plain HTTP and through an inspected HTTPS tunnel, and that an
  upstream certificate nobody vouches for is refused.
- **An allowed name can't point inside.** After the name and URL checks,
  squid resolves the name and refuses loopback, link-local (cloud
  metadata), private and shared address space (`EGRESS_INTERNAL_RANGES`;
  `internalRanges` on Kubernetes, where the NetworkPolicy excludes the same
  ranges). So a name someone allowed from *Blocked traffic* can't reach
  your computer, your local network or a metadata service, even if it
  resolves there.
- **DNS can't carry data out.** A name that isn't allowed gets NXDOMAIN
  ("no such name") from CoreDNS and is logged; queries straight to
  outside resolvers have no route.
- **No packet games.** The VS Code container drops `NET_RAW` and never had
  `NET_ADMIN`: no ARP or packet spoofing towards the containers beside it,
  no route changes. The containers on both the internal network and
  another one (squid, CoreDNS and the gateway) have IP forwarding
  switched off, so none of them can route for the internal network.
  CoreDNS runs read-only with every capability dropped but
  `NET_BIND_SERVICE`; squid and the gateway drop all capabilities and
  can't gain privileges.

### The proxy's certificate authority

- **It is per profile, and its key stays with squid.** The `pki` one-shot
  container (the operator, on Kubernetes) generates it into a volume (a
  Secret); the VS Code container and `docker` receive only the
  certificate. Squid checks upstream certificates against the public
  roots; a bad upstream certificate is an error, never silently accepted.

### The policy in force

- **An empty allowlist allows nothing.** Switching every entry off renders
  a deny-all policy and a DNS that answers NXDOMAIN to everything, not an
  error that would leave the previous policy running.
- **HardCode's own entries stay current, and a profile's exceptions stay
  put.** The entries HardCode provides (every profile's, and its languages'
  and tools') live in one place, the catalogue, and aren't copied into
  profiles. A fix to them (a host added, or one dropped because it
  shouldn't have been there) reaches every profile at its next start or
  reload, or on Kubernetes at the operator's next pass, and is printed or
  logged. A provided entry a profile switched off stays off through every
  update.
- **The proxy runs the policy the profile says.** `hardcode` records what
  it saw the running proxy load, and a start or rebuild reloads a proxy
  that has anything else: a removed entry stays removed, even after a
  reload or start that failed half-way.

### Docker inside the sandbox

- **Docker is rootless, and not `--privileged`**, on Compose. Containers
  you start inside, `--privileged` and `--network host` included, are
  inside the same network sandbox and go through the same proxy.
- **The daemon is Docker's own image, with one layer added.** The image
  (`images/docker`) is `docker:dind-rootless`, pinned by digest. Its
  dockerd, containerd, runc, rootlesskit and entrypoint are unchanged,
  which `tests/docker.py` checks. The added layer holds the image's own
  Python and a `runc` wrapper. The container keeps the security options,
  devices, volumes and limits it had before. On Kubernetes, the sidecar
  still runs Docker's image without the wrapper.
- **Containers and builds trust the proxy's certificate, and nothing else
  changes.** The wrapper runs before the real `runc`:
  - For a container, it mounts a copy of each certificate bundle in the
    image, with the proxy's certificate added. The copy is removed with the
    container.
  - For a build step, it adds the certificate to the image's bundle and
    removes it before the layer is written. If it can't remove it, the step
    fails, so a built image never contains it.
  - It covers Java trust stores, and sets `SSL_CERT_FILE` and
    `NODE_EXTRA_CA_CERTS`.

  It adds no capabilities, namespaces, devices or other mounts, and passes
  every other `runc` command through unchanged. Trusting the proxy's
  certificate adds no trust: all of a container's HTTPS goes through that
  proxy anyway.
- **An image's files are treated as untrusted.** The wrapper opens them
  only inside the image's root filesystem (`openat2` with
  `RESOLVE_IN_ROOT`), so a symlink can't point it at the sidecar's own
  files, such as the daemon's TLS key. It reads regular files only, up to
  10 MiB each.
- **The Docker daemon is optional.** `hardcode docker off` (the switch on
  the docker row of the app's Overview, `docker.enabled: false` on
  Kubernetes) leaves the rootless daemon out of the profile entirely:
  nothing runs with `seccomp=unconfined` or privileged for it, and there
  is no `docker` inside. That is the harder sandbox, for profiles that
  don't need containers.

### The coding agents

- **Claude Code's telemetry and error reporting are off**, and their hosts
  (and the claude.ai connector proxy) aren't on the allowlist. Its own
  hosts (the API, sign-in, downloads) are the Claude Code CLI's block, on
  by default: `hardcode disable claude` closes them and removes its
  extension, as for Codex.
- **The Codex CLI** (on by default) can upload to OpenAI: its block opens
  `auth.openai.com`, `chatgpt.com/backend-api` and `api.openai.com`, all
  `rw`, as an agent that calls a model API must. Its metrics, crash
  reports and update check are off in its first configuration, and their
  hosts closed. `hardcode disable codex` closes the block. Codex's own
  sandbox (bubblewrap) needs user namespaces, which the VS Code container
  doesn't allow, so its first configuration runs it without one
  (`sandbox_mode = "danger-full-access"`): the container, its proxy and
  its network are the boundary, as for Claude Code. Allowing user
  namespaces in the container to give Codex its own would weaken the
  container for everything else.

"On by default" means that both are ticked when you make a profile from
the template, are what an empty answer to `hardcode up`'s first question
gives, and, on Kubernetes, are on in a profile that doesn't list its
languages while the chart's `profileDefaults.languages` is empty.

### VS Code and its extensions

- **VS Code is local and token-protected.** It is published on
  `127.0.0.1` only, with a connection token kept in the home volume. The
  token is treated as a credential: never written to a log (VS Code's own
  mention of it is hidden as it prints), hidden in the app and the admin
  until asked for, and replaceable at once (`hardcode token rotate`,
  *New token…*), which makes every copy of the old one useless.
- **Only verified VS Code extension publishers.** Each extension is
  checked before the running container installs it, with everything the
  Marketplace says it pulls in (dependencies, pack members), and checked
  again as it arrives; the whole set is removed if any of it fails. There
  are explicit, reviewed exceptions. The rule keeps a mistaken or
  look-alike choice off a profile's list; it doesn't stop code inside the
  sandbox from installing extensions itself.

### Your computer's resources

- **Nothing can eat the host.** Every container has a hard memory limit:
  on Compose, the VS Code container (`VSCODE_MEMORY`, 8g) and the Docker
  daemon (`DOCKER_MEMORY`, 4g) from the profile's `.env`, squid 512m,
  CoreDNS 128m, the gateway and the one-shots less; on Kubernetes the same
  for every container (`resources`, `dockerResources`; a profile that
  leaves the memory limit out still gets the default). Squid's descriptor
  table is capped as well (`max_filedescriptors 65536`; an unlimited
  container limit once made it allocate tens of GB). The VS Code
  container and the Docker daemon also have a process limit
  (`VSCODE_PIDS`, `DOCKER_PIDS`).
- **What your computer reads from a sandbox is bounded.** Every
  container's log is rotated (at most 3 files of 10 MB, `x-logging` in
  `compose.yaml`), so a sandbox can't fill your disk through the proxy's
  log, which it writes just by making requests, and *Blocked traffic* and
  `hardcode logs` read a bounded amount. The limits are kept outside the
  sandbox, where it can't change them: `hardcode` stops any command it
  runs once it has printed more than 64 MiB (the extension status the VS
  Code container writes: 64 KiB, and taken only when it has the fields and
  sizes the extension sync writes), and the app stops the command at
  128 MiB and cuts a streamed line at 64 KiB. On Kubernetes the kubelet
  rotates logs, and the admin reads at most 16 MiB of one.

### HardCode's own releases

- **The app installs only signed updates.** A release's app takes an
  update from the project's newest GitHub release only when its signature
  checks out against the key the app was built with
  (`release/updater.pub`) and was made for the version announced, so a
  tampered `latest.json` can't pass an older release off as newer; only
  over HTTPS; and only when you say so
  ([releasing.md](releasing.md#the-apps-updater)). Development and local
  builds have no updater. The key that signs updates is given to one job,
  which builds nothing and runs only the Tauri CLI's signer over the
  finished installers: the app's builds, and the hundreds of crates' build
  scripts they run, never hold it.
- **Known vulnerabilities are reported with every build.** A release's
  and a development build's description ends with what OSV-Scanner finds
  in what HardCode is built from (the app's crates, the Python pins) and
  in the images that are its boundary (squid, the gateway, the admin,
  the Docker sidecar, CoreDNS), with the full list as
  `vulnerabilities.json`. The scanner, checksum-pinned in the build image,
  runs with no network, no token and a read-only filesystem against
  databases downloaded before it starts, so a compromised scanner could
  only misreport. Findings don't stop a release: a base image always
  carries some nobody has fixed yet.
- **Releases are built from nothing cached.** Any job that runs on `main`
  can write to the repository's Actions cache: CI's, and with it any build
  script or tool those jobs run. The release workflow restores none of it
  (no BuildKit cache, no cargo cache, no `target/`), so what it publishes
  comes from the commit and what the build downloads, pinned as the
  Dockerfiles and lock files say. Its actions are pinned by commit, and
  only the jobs that publish hold the permissions to; those run no
  third-party action, and no checkout leaves its token behind.
- **What is published can't be changed, and says where it came from.**
  Releases are immutable: once published, their files and tags are locked,
  and GitHub attests each release. Every file and image is also attested
  as built by the release workflow from its commit, which
  `gh attestation verify` checks ([releasing.md](releasing.md#what-a-release-publishes)).
  Only the repository's admins can push a release tag; no one can move or
  delete one, nor rewrite `main`'s history, and nothing merges into `main`
  until CI has passed.
- **What builds a release is pinned and a week old.** The BuildKit every
  image is built with, the build image's tools (the AppImage's included,
  which the Tauri CLI would otherwise download unchecked), the Rust and
  Python of the macOS and Windows builds, the crates and the Python
  packages: each pinned, checked by checksum or hash where it is
  downloaded, and taken only once it has been published for a week
  ([development.md](development.md#dependencies)).

## Not covered

- **`rw` and `ssh` entries can receive data.** With credentials, anything
  allowed `rw` (the Claude API, a git namespace you push to) can be
  written to, and an `ssh` entry is a whole host with nothing inspected.
  The allowlist limits where traffic can go, not what is sent. Keep both
  short, and prefer paths to hosts.
- **Read-only is by HTTP method.** A service that accepts uploads through
  `GET` query strings would get through; none of the default entries do.
- **Credentials in the sandbox are available to everything in it.** What
  you sign in to inside the sandbox (Claude Code, Codex, git, an API key in
  an environment variable) is stored there, where any code running in the
  sandbox can read it and use it against whatever the allowlist allows.
  Give the sandbox narrowly scoped credentials, and revoke them when you
  no longer need them.
- **Squid sees plaintext.** It logs URLs, not bodies, but anyone with
  access to the `squid` container (or pod) can see decrypted traffic.
- **DNS allows subdomains** of every listed name, exact-host entries
  included. Their name servers belong to the same operator.
- **A writable host mount is a way out.** Anything inside, Claude
  included, can edit `.git/hooks`, `.git/config`, build scripts and the
  like in a mounted folder, and your computer runs them the next time you
  use that repository there. Mount read-only what you only need to read,
  and review changes before running them on your computer. And since
  `dev` has `sudo`, files written there as root are root's on your
  computer too (the usual file capabilities aren't dropped): expect to
  need `sudo` to clean up, and keep such mounts on filesystems mounted
  `nosuid`. Docker's user-namespace remapping, or rootless Docker, on your
  computer makes root in the container an unprivileged user outside: worth
  it for setups that need writable mounts.
- **`sudo` is root in the VS Code container.** The boundary is the
  container and the network, not the user account.
- **`docker` runs with `seccomp=unconfined` and `systempaths=unconfined`**
  on Compose (rootless user namespaces need them; the daemon itself is an
  unprivileged user), and **privileged by default on Kubernetes**. Switch
  it off on profiles that don't need Docker (`hardcode docker off`).
- **The token is the only protection for VS Code behind an Ingress** on
  Kubernetes. Put TLS and your own sign-in in front of it.
- **The admin's login throttle keys on the client address.** Behind a
  proxy, it believes `X-Forwarded-For` only from the addresses in
  `admin.trustedProxies`, read from the right, so a client can't pick its
  own; set them, or every client looks like the proxy. Password hashing
  is limited to two at a time, so a burst of sign-ins can't exhaust the
  admin's memory; it can make sign-ins wait.
- **The app's updates are as trustworthy as the signing key.** Whoever
  holds it, and can publish a release in the project's repository, can
  ship an update every installed app accepts; keep it offline, with a
  password. The update check itself (one request to GitHub as the app
  starts) comes from your computer, not from a sandbox: the allowlist
  doesn't apply to the app.
- **NetworkPolicy doesn't isolate a pod from its own node.** Kubernetes
  allows traffic between a pod and the node hosting it regardless of
  policy, so the Compose stack's host isolation has no exact equivalent:
  node-local services (a metadata endpoint, a node-level DNS cache, the
  kubelet) need a node firewall or a CNI with host policies. Squid's own
  refusal of internal addresses still covers proxied traffic.

## Practices

[Staying safe](staying-safe.md) explains these step by step.

- Add git **namespaces**, never whole hosts (`github.com/ourcompany`, not
  `github.com`): a whole host lets anything inside read every public
  repository, and, with credentials, write to any it can reach.
- Give `rw` only to the hosts that need `POST`: the Claude API, the
  Marketplace, sign-in flows, the namespace you push to.
- Watch *Blocked traffic* before widening the list; most refusals are
  telemetry.
- One profile per trust level: a client's code in its own profile with its
  own allowlist, image and volumes; `hardcode docker off` where containers
  aren't needed.
- Keep `VSCODE_BIND=127.0.0.1`. To share a sandbox, put TLS and
  authentication in front, or use the Kubernetes chart.
- Treat a profile's folder (in your HardCode home) as configuration that
  encodes policy: review changes to its `allowlist.txt` like code.
- Keep HardCode up to date, and rebuild each profile's image after an
  update.

## Reporting a vulnerability

Found a way out of the sandbox? Please report it privately, through the
*Security* tab of the
[repository](https://github.com/hardcode-app/hardcode), rather than in an
issue.
