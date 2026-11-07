# Troubleshooting

Find what you're seeing below. Most problems show up in one of three
places: the state next to the page title in the app (or `hardcode ps`),
**Blocked traffic** (or `hardcode blocked`), and a container's log (click
it on the app's Overview, or `hardcode logs <container>`).

## Installing and starting

### `hardcode` says it doesn't need root

It refuses to run under `sudo`: as root it would use root's profiles
(`/root/.config/hardcode`), not yours. Run it as yourself. On Linux, if
`docker` itself needs `sudo`, follow Docker's
[post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/);
Docker's group, Docker Desktop or rootless Docker are all HardCode needs.

### "Cannot connect to the Docker daemon", or the app says *docker not running*

Docker isn't running. Start Docker Desktop (macOS, Windows), or the Docker
service (Linux), and try again.

### The app says *docker not found*

The `docker` command isn't on the `PATH` of the app (HardCode also looks
where Docker Desktop installs it on macOS). Install Docker, start Docker
Desktop, or start the app from a shell whose `PATH` has `docker`.

### The app says *docker snap unsupported*, or Docker can't open a profile's file

Docker on this computer is the snap. The snap can't open HardCode's
folders, and it refuses the security settings HardCode's containers run
with, so it can't run a sandbox. Remove it, then install Docker as
[Getting started](getting-started.md#1-install-docker) says:

```sh
sudo snap remove --purge docker
```

That also deletes the snap's containers, images and volumes, so copy out
anything you need from them first. Your profiles are kept, and work as
they are with the new Docker.

### The app says the hardcode command was not found

A release has its own copy beside the app; reinstall the app if it has
gone. A development build runs `cli/hardcode` from the checkout it was
built from, with Python 3 (`python3`, or `python` on Windows).
`HARDCODE_CLI` can name another one.

### `hardcode up` says there are no profiles yet

Run without a terminal to ask on (in a script, say), `hardcode up` can't
ask what to build the profile around. Make it first:
`hardcode profile add default --lang python,docker,claude`.

### VS Code's port is taken

The start fails with Docker's error (`Bind for 127.0.0.1:11111 failed: port
is already allocated`, or `address already in use`): another program has
the profile's port. `hardcode port <number>` (or **change** beside the
port on the app's Overview) moves the profile to another, and
`hardcode up` starts it there.

### A port forward's port is in use

`hardcode forward` refuses ports another profile uses, but can't know
which ports other programs have: when the gateway can't start with the
forward, the forward is undone and the error says why. Choose another
port.

### The `docker` container keeps restarting

With `[rootlesskit:parent] error: failed to start the child: fork/exec
/proc/self/exe: operation not permitted` in `hardcode logs docker`: your
computer's AppArmor blocks unprivileged user namespaces
(`kernel.apparmor_restrict_unprivileged_userns=1`, the default on Ubuntu
23.10 and newer). HardCode applies Ubuntu's own `rootlesskit` AppArmor
profile, which allows them; if your computer doesn't have it
(`ls /etc/apparmor.d/rootlesskit`), set
`DOCKER_APPARMOR_PROFILE=unconfined` in the profile's `.env` and run
`hardcode up`.

### The internal network `172.31.253.0/24` clashes with one of your networks

Change `DEVNET_SUBNET` and `DNS_IP` together in the profile's `.env`
(`DNS_IP` is `.53` of the network), then `hardcode down` and `hardcode up`.

### `squid` is unhealthy, or stops as it starts

Look at `hardcode logs squid`.

- `… missing; did the allowlist service run?`: the allowlist couldn't be
  turned into rules. `hardcode logs allowlist` shows the offending line;
  it comes from the profile's `allowlist.txt` (the app's Allowlist marks
  it *invalid*). Fix or remove it, then `hardcode up`.
- `… missing or unreadable; did the pki service run?`: the proxy's
  certificate wasn't made. `hardcode compose up pki`, then `hardcode up`.

## Inside the sandbox

### A download or a request fails

Look at **Blocked traffic** (or `hardcode blocked`):

- `CONNECT host:443`: the host isn't on the allowlist. If the sandbox
  should reach it, allow it: `hardcode allow host`.
- `POST` or `PUT` to a host that is allowed: the entry is read-only, and
  this is an upload. It needs `rw`, if you mean to allow uploads there.
- a `DNS` row: the name isn't covered by any entry.
- nothing at all: check the entry's kind. A path entry (`host/path`)
  allows only that path.

Before you allow anything, read
[Staying safe](staying-safe.md#read-blocked-traffic-before-you-allow-anything).

### The app counts down, or says *allowlist not applied*

*allowlist in 3s*, or *gateway in 3s*: your change is made five seconds
after the last one, so several in a row are made together. Nothing is
wrong; it goes away once the change is made. To have it at once, click
**Apply now** on the Allowlist page, or run `hardcode apply`.

*allowlist not applied*: the running proxy doesn't have the allowlist as
it is now. Hover over it for why. Either you edited `allowlist.txt` by
hand, or the proxy refused your changes and kept the rules it had:
`hardcode logs allowlist-watch squid` shows what it said. Fix the line it
names, if it names one, then click **Apply now**.

### The app says *changes undone*

A container couldn't start with your changes, so they were undone and the
sandbox runs as it did before them. Hover over it for which changes, and
why; `hardcode logs applier` has the same. The usual reason is a port that
another program on your computer already uses: choose another port, and
make the change again.

### `git push` is refused

Pushing needs read-write access to the repository, and a way for the
sandbox to sign in. For a GitHub repository, follow
[GitHub from the sandbox](github.md): `hardcode allow github:OWNER/REPO rw`
allows it, and `hardcode-github OWNER/REPO` sets up the sign-in.

### A server on your own network is refused

The proxy refuses names that point inside your network, even allowed
ones. Add the name to **On your network**: when you allow it, HardCode
asks, or use `hardcode lan add <name>`
([a server on your own network](allowlist.md#a-server-on-your-own-network)).
A server on your own computer can't be added this way: a sandbox never
reaches the computer it runs on.

### `certificate verify failed`, or `unable to get local issuer certificate`

The program doesn't trust the proxy's certificate. HardCode sets this up in
the sandbox and in Docker containers, so this usually means the program
keeps its own list of certificates.

- **In the sandbox's terminal**, point the program at
  `/proxy-ca/ca-bundle.pem`. Most programs already find it through
  `SSL_CERT_FILE`.
- **In a Docker container or an image build**, it's one of these:
  - The image has no certificates at all, for example `FROM scratch`:
    install the image's `ca-certificates` package.
  - It fails in the `RUN` line that installs `ca-certificates` or runs
    `update-ca-certificates`: move the commands that need HTTPS to the
    next `RUN` line.
  - The container mounts its own certificate file or folder: add
    `/proxy-ca/ca.pem` to what you mount.
  - The container was started with `HARDCODE_PROXY_CA=off`: start it
    without.

### `update-ca-certificates` says "Device or resource busy" in a container

Run it in the Dockerfile instead. In a running container, HardCode is
using the certificate list.

### A build fails with "this step changed a Java trust store"

Put `ARG HARDCODE_PROXY_CA=off` before that `RUN` line.

### Java tools ignore the proxy

Maven and Gradle are set up for you, in `~/.m2/settings.xml` and
`~/.gradle/gradle.properties`; delete a file to have it written again at
the next start. Other Java programs need
`-Dhttps.proxyHost=squid -Dhttps.proxyPort=3128`. For Java in Docker
containers and image builds, set `DOCKER_JAVA_PROXY=true` in the profile's
settings and run `hardcode up`.

### A language's command isn't found after switching it on

Its allowlist entries and extensions arrive at once, but the software
itself comes with the next image rebuild: **Rebuild image** on the app's
Languages / Tools page, or `hardcode rebuild`.

### `docker` in the sandbox says it can't connect

The profile runs without its Docker daemon (`ENABLE_DOCKER=false`, the
switch on the docker row of the app's Overview, or `hardcode docker off`).
`hardcode docker on` brings it back; VS Code's container restarts a few
seconds later. If
`docker` itself isn't found, the profile doesn't have the **Docker CLI**
tool: switch it on and rebuild.

### Something I installed with `sudo apt install` has gone

Everything outside your home folder and `/shared` is reset whenever the
container is recreated: after a rebuild, a change of shared folders, or
Docker switched on or off. Install it again, or keep to what lives in your
home folder ([what is kept](vscode-image.md#what-is-kept-and-what-isnt)).
To have a package in every container, add it to the image instead:
`hardcode package add NAME`, then `hardcode rebuild`.

### `hardcode up` says a device isn't available

The profile is set to use a device this computer doesn't have, or the
computer isn't running Linux. Switch it off with `hardcode device off
NAME`, or its switch on the Overview page, and start again.

### A rebuild says `Unable to locate package`

One of the profile's Ubuntu packages isn't in Ubuntu 26.04 under that
name. Check the spelling, or remove it with `hardcode package remove
NAME` (or its **✕** on the Image page), and rebuild.

### A rebuild fails in your own Dockerfile steps

The Console shows the step that failed. Fix it on the app's **Image**
page and rebuild. To go back to HardCode's image alone, delete the
Dockerfile (`hardcode image` shows where it is) and rebuild.

### A server in the sandbox can't be reached at `localhost:<port>`

Forward the port first (`hardcode forward 3000`, or the app's **Port
forwards**), and make the server listen on `0.0.0.0`, not on `localhost`:
the forward reaches it from outside its container.

### Files in a shared folder have the wrong owner

Files written as root inside the sandbox (with `sudo`) are root's on your
computer too: remove them with `sudo`.

The sandbox's user, `dev`, has user id 1000. If yours on your computer
isn't 1000 (`id -u`), the files the sandbox writes in a shared folder
belong to someone else there. Set `DEV_UID` and `DEV_GID` in the profile's
`.env` to your ids (`id -u`, `id -g`) and rebuild. The Docker daemon
always runs as 1000, so Docker containers inside then see those files as
owned by "nobody".

### An extension doesn't appear

A running sandbox installs extensions in the background: reload VS Code's
browser window. The app's **Extensions** page (or `hardcode extensions`)
says how each is doing. *refused* means its publisher (or one of what it
pulls in) has no verified domain on the Marketplace, and it won't be
installed; *couldn't install* means the Marketplace didn't answer, and it
is tried again a minute later.

### The VS Code token is lost or wrong

`hardcode open --no-browser` prints the current address (the logs never
show the token). The token lives in the home folder
(`~/.vscode-server/connection-token`); `hardcode token rotate` (or **New
token…** in the app) replaces it, and `VSCODE_CONNECTION_TOKEN` in `.env`
fixes one.

### VS Code says it could not download the server

VS Code updates its server from `update.code.visualstudio.com` and
`vscode.download.prss.microsoft.com`, both on every profile's allowlist.
Check **Blocked traffic** if you switched either off.

### Something ate all the memory

Every container HardCode starts has a memory limit. The proxy, squid, sizes
its tables from the number of files a process may open, which some
container runtimes leave practically unlimited; `squid.conf` caps it
(`max_filedescriptors 65536`). If you run the images elsewhere, keep that
line, and give every container a memory limit.

## On Kubernetes

### The profile stays `Pending`

`kubectl -n <ns> get profile <name> -o jsonpath='{.status.message}'` names
what it is waiting for and the container's reason (`ImagePullBackOff`: the
VS Code or squid image isn't where `profileDefaults` says;
`ContainerCreating` for a while: volumes being provisioned).
`kubectl describe pod` on the component it names has the rest. The VS Code
pod needs a privileged container for its Docker sidecar unless
`docker.enabled` is `false`.

### The allowlist works, but the sandbox reaches anything

The cluster's network plugin (CNI) doesn't enforce NetworkPolicy. The
policies are created, but without enforcement the VS Code pod has normal
pod networking. Install Calico, Cilium or another CNI that enforces them.

### A member doesn't see a profile

The profile isn't theirs: its **Owner** (on its **Settings** tab) isn't
their email. An admin sets it there. A member sees only the profiles they
own.

### A member's **Ask** or **Add** is refused

Their rights don't allow that kind of entry: an admin changes them with
**Edit…** on the **People** page. Read-write entries, ssh tunnels and names
on your network each need their own tick.

### Admins get no email about requests

Requests still appear on the **Requests** page. For the emails, the chart
needs `admin.smtp.host` and `admin.smtp.from`; when a send fails, the
admin's log says why (`kubectl -n <ns> logs deploy/<release>-admin`, the
line `couldn't email the admins about request …`). If the email arrives but
its link goes to the wrong place, set `admin.url` to the admin UI's address.

### Signed out of the admin UI unexpectedly

Sessions are kept in memory, so an admin restart (a rollout, running out of
memory) signs everyone out. A session also ends after 2 hours idle and 12
hours in all; an open tab's background refresh doesn't count as activity.
