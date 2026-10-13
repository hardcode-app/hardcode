# Troubleshooting

### `docker` keeps restarting

With `[rootlesskit:parent] error: failed to start the child: fork/exec /proc/self/exe: operation not permitted`
in `hardcode logs docker`: the host's AppArmor blocks unprivileged user
namespaces (`kernel.apparmor_restrict_unprivileged_userns=1`, the default on
Ubuntu 23.10+). Compose applies Ubuntu's stock `rootlesskit` profile, which
allows them; if your host doesn't have it (`ls /etc/apparmor.d/rootlesskit`),
set `DOCKER_APPARMOR_PROFILE=unconfined` in the profile's `.env` and
`hardcode up`.

### A port is already in use

For VS Code's port, see [VS Code's port is taken](#vs-codes-port-is-taken)
below. `hardcode forward` refuses ports another profile uses but can't know
which ports other programs have.

### A download fails inside the VS Code container

`hardcode blocked` (or the app's *Blocked traffic*). A `CONNECT host:443`
line means the host isn't allowlisted: `hardcode allow host`. A `403` on a
`POST`/`PUT` to an allowlisted host means the entry needs `rw`. A DNS line
means the name isn't under any allowlisted host. Then check the entry is the
right *kind*: `host/path` only allows that path.

### `certificate verify failed` / `unable to get local issuer certificate`

The tool doesn't trust the proxy CA. In the VS Code container the system store and the
common variables (`SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`,
`PIP_CERT`) are set; a tool with its own bundle needs pointing at
`/proxy-ca/ca-bundle.pem`. Inside containers, mount `/proxy-ca` and set the
variable ([Docker inside the sandbox](vscode-image.md#using-docker-from-the-vscode)).

### Java tools ignore the proxy

Maven and Gradle are configured through `~/.m2/settings.xml` and
`~/.gradle/gradle.properties`, generated on start if missing; delete them to
regenerate. Plain `java` programs need
`-Dhttps.proxyHost=squid -Dhttps.proxyPort=3128`.

### `squid` is unhealthy or exits at start

`hardcode logs squid`. `missing; did the allowlist service run?` means the
render failed: `hardcode logs allowlist` shows the offending line of
`proxy-allowlist.txt`, which comes from the profile's `allowlist.txt` (the
app's *Allowlist* marks the line *invalid*). A `FATAL` about certificates means the `pki` volume is
missing or unreadable: `hardcode compose up pki` then `hardcode up`.

### The `172.31.253.0/24` subnet clashes with one of your networks

Change `DEVNET_SUBNET` and `DNS_IP` together in the profile's `.env`
(`DNS_IP` is `.53` of the subnet), then `hardcode down && hardcode up`.

### The VS Code token is lost or wrong

`hardcode open --no-browser` prints the current URL (the logs never show the
token). The token lives in the home volume (`~/.vscode-server/connection-token`);
`hardcode token rotate` (or *New token…* in the app) replaces it, and
`VSCODE_CONNECTION_TOKEN` in `.env` fixes one.

### VS Code shows "could not download the server"

The server self-updates from `update.code.visualstudio.com` and
`vscode.download.prss.microsoft.com`; both are on the default allowlist.
Check *Blocked traffic* if you've trimmed it.

### The app says the hardcode command was not found

A release bundles it beside the app's executable; a development build runs
`cli/hardcode` from the checkout it was built from, with Python 3
(`python3`, or `python` on Windows). `HARDCODE_CLI` can name another one.

### VS Code's port is taken

The start fails with Docker's error (`Bind for 127.0.0.1:11111 failed: port
is already allocated`, or `address already in use`): another program has
the profile's port. `hardcode port <number>` (or *change* beside the port
on the app's Overview) moves the profile to another, and `hardcode up`
starts it there.

### `hardcode` says it doesn't need root

It refuses to run under sudo: Docker's group, Docker Desktop or rootless
Docker are all it needs, and as root it would use root's profiles
(`/root/.config/hardcode`), not yours. Run it as yourself.

### `docker` in the VS Code container says it can't connect

This profile runs without the Docker daemon (`ENABLE_DOCKER=false`, the
switch on the docker row of the app's Overview, or `hardcode docker off`). `hardcode docker on` brings it
back; the VS Code container is recreated.

### The app's status pill says "docker not found"

The CLI couldn't find `docker` on the `PATH` (or in Docker Desktop's usual
places on macOS). Start Docker Desktop, or put `docker` on the PATH of the
shell the app was started from.

### Something ate all the memory

Squid sizes its descriptor tables from the process's open-file limit, which
some container runtimes leave effectively unlimited; `squid.conf` caps it
(`max_filedescriptors 65536`). If you run the images elsewhere, keep that
line, and give every container a memory limit.

### On Kubernetes: the profile stays `Pending`

`kubectl -n <ns> get profile <name> -o jsonpath='{.status.message}'` names
what it is waiting for and the container reason (`ImagePullBackOff`: the
vscode or squid image isn't where `profileDefaults` says; `ContainerCreating`
for a while: PVCs being provisioned). `kubectl describe pod` on the named
component has the rest. The VS Code container needs a privileged container for its
Docker sidecar unless `docker.enabled` is `false`.

### On Kubernetes: the allowlist works but the VS Code container reaches anything

The CNI doesn't enforce NetworkPolicy. The policies are created, but without
enforcement the VS Code container has normal pod networking. Install Calico, Cilium or
another enforcing CNI.

### On Kubernetes: signed out unexpectedly

Sessions are in memory; an admin restart (a rollout, an OOM) signs everyone
out. The session also expires after 2 hours idle and 12 hours in total; an
open tab's background refresh doesn't count as activity.
