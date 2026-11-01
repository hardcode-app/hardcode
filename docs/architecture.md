# Architecture

```
browser ─► gateway ─┐
                    │      devnet (internal: no route to anywhere)
                    ├─► vscode  VS Code web as user "dev" (password-less sudo), Claude Code, toolchains
                    │      │ docker CLI, TLS :2376
                    │      ▼
                    ├─► docker    rootless dockerd   ── /shared volume in both
                    │
                    ├─► squid ───► egress ─► internet   allowlisted URLs only (HTTPS decrypted and inspected)
                    └─► coredns ─► egress ─► internet   DNS for allowlisted names only, NXDOMAIN for the rest
```

`vscode` and `docker` are attached to **devnet only**, a Docker network marked
`internal` and in isolated gateway mode: no default route, and no address on
the host's side of the bridge, so nothing on it can reach the host, the LAN
or the internet directly. `docker` is optional (`hardcode docker off` leaves it
out of the profile). Their only ways out are the two sidecars, which sit on
devnet *and* on a normal network (`egress`) and enforce the profile's
allowlist. The browser reaches VS Code through `gateway`, a small forwarder
that sits on devnet and a third network (`frontend`), because Docker cannot
publish ports from internal networks.

## Services

| Service | Image | Role |
|---|---|---|
| `vscode` | built from [`images/vscode/Dockerfile`](../images/vscode/Dockerfile), tagged `hardcode-vscode:<profile>` | VS Code (`code serve-web`) as user `dev`, toolchains, Claude Code. [Details](vscode-image.md). |
| `docker` | `docker:29.9.2-dind-rootless` (by digest) | The VS Code container's Docker daemon. Rootless, not `--privileged`; talks TLS on `docker:2376`. Behind the `docker` Compose profile, which the CLI activates unless the profile's `ENABLE_DOCKER=false`. |
| `squid` | built from [`images/squid/Dockerfile`](../images/squid/Dockerfile) (the archive's `squid-openssl`) | Egress proxy on `squid:3128`. Decrypts HTTPS with the profile's own CA and allows requests by URL and method. |
| `coredns` | `coredns/coredns:1.15.0` (by digest) | The VS Code container's only nameserver. Allowlisted names resolve upstream; everything else is answered NXDOMAIN locally. |
| `allowlist` | the squid image, one shot | Renders the profile's allowlist into Squid's and CoreDNS's rule files on the `egress-config` volume before they start. It is given `proxy-allowlist.txt`, which `hardcode` writes: the entries HardCode provides (every profile's, and the profile's languages' and tools'), less those switched off, with the profile's own from `allowlist.txt`. |
| `pki` | the squid image, one shot | Generates the proxy CA once per profile. Squid gets key and certificate; vscode and docker only the certificate. |
| `gateway` | built from [`images/gateway/Dockerfile`](../images/gateway/Dockerfile) (socat) | Publishes VS Code (`127.0.0.1:<VSCODE_PORT>` → `vscode:8000`) and any ports added with `hardcode forward`. |

## Networks and volumes

| Network | Who | Purpose |
|---|---|---|
| `devnet` (`internal`, subnet `DEVNET_SUBNET`, default `172.31.253.0/24`) | all services except `pki`/`allowlist` | The sandbox. CoreDNS has the fixed address `DNS_IP` (`.53`), which the VS Code container and docker use as their only `dns`. |
| `egress` | `squid`, `coredns` | Their way to the internet. Both have IP forwarding switched off (`net.ipv4.ip_forward=0`), so neither can route for devnet. |
| `frontend` | `gateway` | Where the published ports live. |

| Volume | Mounted in | Holds |
|---|---|---|
| `home` | vscode `/home/dev` | the user's home: VS Code server, extensions, Claude Code, caches, projects |
| `shared` | vscode and docker `/shared` | files both the VS Code container and its containers can see |
| `docker-data` | docker | images, containers, volumes of the inner daemon |
| `docker-certs`, `docker-certs-client` | docker; client half read-only in vscode at `/certs/client` | the daemon's TLS material |
| `egress-config` | allowlist (writes), squid (`/etc/squid/generated`, subpath `squid`), coredns (`/etc/coredns`) | the rendered rule files and Corefile |
| `proxy-ca` | pki, squid | the CA key and certificate |
| `proxy-ca-pub` | pki, vscode and docker at `/proxy-ca` | the certificate and a `ca-bundle.pem` (public roots + ours) |

All of these are per profile: each profile is its own Compose project
(`hardcode-<name>`), so its volumes and networks are named after it.

## The path of a request

1. A tool in the VS Code container resolves `pypi.org`. The VS Code container's resolver is CoreDNS.
   If the name is under an allowlisted host (or its wildcard), CoreDNS forwards
   the query upstream (`UPSTREAM_DNS`, by default the host's resolvers through
   Docker). Otherwise it answers NXDOMAIN itself and logs
   `BLOCKED A <name>`; nothing leaves the host.
2. The tool connects to `squid:3128` (every proxy variable is set, and the
   entrypoint configures apt, Docker, Maven and Gradle, which don't read
   them). A tool that ignores the proxy and tries to connect directly finds
   no route.
3. For HTTPS the tool sends `CONNECT pypi.org:443`. Squid allows the tunnel
   only if the host is on the allowlist, then **peeks at the TLS server name
   and bumps**: it terminates TLS with a certificate for `pypi.org` signed by
   the profile's CA (which the VS Code container trusts) and opens its own TLS
   connection upstream, verifying the real certificate against the public
   roots.
4. Every request inside the tunnel is now visible and is matched against the
   URL rules: an entry allows `GET`/`HEAD`/`OPTIONS` (plus the `POST` git uses
   to fetch) under its prefix; an entry marked `rw` allows every method.
   Anything else is answered `403` and logged `TCP_DENIED`. A refused tunnel
   is reset outright, so non-TLS clients don't hang.
5. SSH works the same way through `CONNECT` to a host with an `ssh` entry;
   it can't be inspected, so such entries are whole-host and read-write.

The CLI's `hardcode blocked`, the app's *Blocked traffic* view and the admin
UI all read the `TCP_DENIED` and `BLOCKED` log lines.

## Profiles and Compose

`compose.yaml` is one stack parameterised by environment: the project name,
the VS Code image's tag and the profile's folder come from what `hardcode`
sets; the subnet, the DNS address, the published port and the build
arguments from the profile's `.env` (only what differs; `compose.yaml` has
every default); and the profile's `compose.override.yaml` (mounts, forwards,
anything else) is merged on top. The stack (`compose.yaml`, the images'
build files, the template) ships inside the command and is written out per
version to its cache folder; the profiles are in the user's home
([Profiles](profiles.md#where-profiles-are)). `hardcode` runs

```
HARDCODE_PROJECT=hardcode-NAME HARDCODE_PROFILE_DIR=<home>/profiles/NAME HARDCODE_VSCODE_TAG=NAME \
HARDCODE_SQUID_TAG=<hash> HARDCODE_GATEWAY_TAG=<hash> COMPOSE_PROFILES=docker \
docker compose -p hardcode-NAME --project-directory <stack> --env-file <home>/profiles/NAME/.env \
  -f <stack>/compose.yaml -f <home>/profiles/NAME/compose.override.yaml <args>
```

(outside the default home, the project and the tag carry the home's id:
`hardcode-<id>-NAME`, `<id>-NAME`; the squid and gateway images are tagged
with a hash of their build files, so a changed one is built again)

with `COMPOSE_PROFILES` empty when the profile's `.env` says
`ENABLE_DOCKER=false` (the Docker daemon's service sits behind that Compose
profile),

so `hardcode -p NAME compose <anything>` is always available for what the
command doesn't cover. [Profiles](profiles.md) has the details.

## The same shape on Kubernetes

The [operator](kubernetes.md) builds the same thing per `Profile` object: a
StatefulSet with the VS Code container and the docker sidecar in one pod, a Deployment and
Service each for squid and CoreDNS, the allowlist rendered by the same init
container, and NetworkPolicies in place of the internal network. The
[Helm docs](kubernetes.md#how-it-maps-to-compose) map one to the other.

## Code layout

| Part | Language | Talks to |
|---|---|---|
| `cli/hardcode_cli` | Python 3.9+, standard library | `docker compose`; edits the profile's files |
| `app/` | Rust (Tauri 2) + plain HTML/JS | the CLI, as `hardcode --json …` subprocesses |
| `admin/hardcode_admin` | Python, Flask | the Kubernetes API; imports `hardcode_cli` for the catalogue, the allowlist (its edits, and what the proxy is given) and log parsing |
| `images/*` | Dockerfiles and bash | — |
| `helm/hardcode` | Helm | installs the CRD, the operator/admin Deployment and `Profile` objects |

The CLI is the single source of behaviour for the Compose stack; the app has
no logic of its own, and the admin shares the parsers, so the three never
disagree about what an allowlist entry means.
