# Kubernetes: the Helm chart and the operator

On a cluster, a **`Profile`** object is a sandbox and an **operator** keeps
the resources it needs in step with it. An **admin UI** with email + password
+ authenticator sign-in lets a team create and manage profiles without
`kubectl` ([Admin UI and API](admin.md)). Operator and UI are one Python
process (Flask, gunicorn) in one small Deployment, from the `hardcode-admin`
image.

```
kind: Profile                         what the operator builds from it
apiVersion: hardcode-app.github.io/v1alpha1
metadata: { name: team-a }            team-a-vscode             StatefulSet (vscode + rootless Docker sidecar), Service, [Ingress]
spec:                                 team-a-squid              Deployment + Service: the inspecting proxy
  allowlist: |                        team-a-coredns            Deployment; Service team-a-dns: the filtering DNS, the VS Code container's only nameserver
    api.anthropic.com   rw            team-a-allowlist          ConfigMap, rendered into rule files by an init container in both
    github.com/team-a                 team-a-vscode-extensions  ConfigMap: the VS Code extensions to keep installed, followed without a restart
  languages: [python, docker]         team-a-proxy-ca           Secret (CA key + certificate, generated once); team-a-proxy-ca-cert ConfigMap (certificate)
  image: reg.example.com/vscode:1.0   team-a-vscode-token       Secret
  vscodeHost: a.code.example.com      three NetworkPolicies: the VS Code container pod can reach squid and that DNS, nothing else
```

## Requirements

- A CNI that **enforces NetworkPolicy** (Calico, Cilium, Antrea, …). Without
  it the VS Code container has normal pod networking and the allowlist only applies to
  traffic that chooses to use the proxy.
- For the Docker sidecar, a node that allows a **privileged container**
  (`profileDefaults.docker.privileged`, default `true`): rootless dockerd needs
  user namespaces and `/dev/net/tun`. Set `docker.enabled: false` on profiles
  that don't need Docker.
- A default StorageClass (or `home.storageClass` etc.): each profile claims
  PVCs for the home directory (20Gi), `/shared` (5Gi) and the inner Docker
  daemon (30Gi).
- Images the cluster can pull: the VS Code container and squid images are built from
  this repository and pushed to your registry (releases publish them to
  GHCR as `ghcr.io/<owner>/hardcode-{vscode,squid,gateway,admin}`, and
  development builds as their `edge` and `sha-<commit>` tags).

## Install

```sh
helm install hardcode ./helm/hardcode -n sandbox --create-namespace \
  --set admin.email=you@example.com --set admin.password='a long password' \
  --set profileDefaults.image=registry.example.com/hardcode/vscode:1.0 \
  --set profileDefaults.squidImage=registry.example.com/hardcode/squid:1.0
# or a released chart:
helm install hardcode oci://ghcr.io/<owner>/charts/hardcode --version 0.1.0 -n sandbox --create-namespace …
# or the newest development build's (X.Y.Z-dev.N, with its sha-<commit> images):
helm install hardcode oci://ghcr.io/<owner>/charts/hardcode --devel -n sandbox --create-namespace …
```

The chart installs the CRD, a ServiceAccount with a namespaced Role, the
operator/admin Deployment and Service (and an Ingress if enabled), and any
profiles listed under `profiles:`. `helm install` prints how to reach the
admin UI; sign in with the bootstrap admin and enrol an authenticator app
on first sign-in. The bootstrap credentials are used only while no admins
exist; afterwards accounts live in the `hardcode-admin-users` Secret.

### Values

| Value | Default | Meaning |
|---|---|---|
| `admin.image.repository`, `.tag`, `.pullPolicy` | `hardcode-admin`, `latest`, `IfNotPresent` | the operator/admin image |
| `admin.email`, `admin.password` | empty | the first admin (12+ character password). Either both, or `admin.existingSecret` |
| `admin.existingSecret` | empty | a Secret with keys `email` and `password` instead of the two above |
| `admin.ui.enabled` | `true` | serve the web UI and API; the operator runs regardless |
| `admin.service.type`, `.port` | `ClusterIP`, `80` | |
| `admin.ingress.enabled`, `.className`, `.annotations`, `.host`, `.tls` | off | an Ingress for the admin UI; put TLS on it |
| `admin.cookieSecure` | `auto` | `auto`: the session cookie is `Secure` when the request came over HTTPS (`X-Forwarded-Proto`); `always`; `never` (plain-HTTP port-forward) |
| `admin.trustedProxies` | `[]` | addresses/CIDRs (the Ingress controller's pods or nodes) whose `X-Forwarded-For` the login throttle believes, read from the right (the first address none of them added is the client); from anyone else the header is ignored |
| `admin.resources`, `.nodeSelector`, `.tolerations` | 96Mi request / 256Mi limit | |
| `imagePullSecrets` | `[]` | for the admin image |
| `profileDefaults.image`, `.squidImage`, `.docker.image`, `.coredns.image` | `hardcode-vscode:latest`, `hardcode-squid:latest`, `docker:29.9.2-dind-rootless`, `coredns/coredns:1.15.0` (each by digest) | images every profile uses unless its spec says otherwise. The last two are pinned as `compose.yaml` pins them (Dependabot moves both together), and the operator has no copy of its own: a profile whose Docker sidecar or CoreDNS has no image is reported with an error |
| `profileDefaults.imagePullPolicy`, `.imagePullSecrets` | `IfNotPresent`, `[]` | |
| `profileDefaults.languages` | `[]` (all of them) | the languages and tools of a profile that doesn't list its own (`spec.languages`); see [Languages on Kubernetes](#languages-on-kubernetes) |
| `profileDefaults.upstreamDNS` | `/etc/resolv.conf` | where allowlisted names resolve (the node's resolvers), or IPs |
| `profileDefaults.home`, `.shared`, `.docker` | sizes 20Gi / 5Gi / 30Gi, default StorageClass; `docker.enabled: true`, `docker.privileged: true` | volumes and the Docker sidecar |
| `profileDefaults.resources`, `.dockerResources` | 1Gi request / 8Gi limit; 256Mi / 4Gi | the VS Code container's and the sidecar's resources. A profile that sets its own and leaves the memory limit out gets the limit set here (and, without one here either, the built-in 8Gi / 4Gi): every container is capped |
| `profileDefaults.networkPolicy.enabled` | `true` | the three NetworkPolicies per profile |
| `profileDefaults.networkPolicy.defaultDeny` | `true` | a policy the chart installs that denies everything to and from every pod the operator creates, before any profile exists; a profile's own policies then allow what its pods need (so a profile with `networkPolicy.enabled: false` is fully isolated while this is on) |
| `profileDefaults.networkPolicy.squidEgressCIDR`, `.squidEgressExcept` | `0.0.0.0/0` except `10/8`, `172.16/12`, `192.168/16` | where squid may connect. Private ranges are excluded so the sandbox can't reach cluster or LAN services through the proxy; relax for an internal registry |
| `profileDefaults.internalRanges`, `.clientRanges` | empty (squid's defaults) | address ranges an allowlisted name must never resolve to (squid refuses the request); who may use a profile's proxy. Space-separated; narrow the first for a registry on an internal address, widen the second for an unusual pod network |
| `profileDefaults.ingressClassName`, `.ingressAnnotations`, `.ingressTLSSecret` | empty | the Ingress a profile with `vscodeHost` gets |
| `profiles[]` | `[]` | profiles to create with the chart: `name`, and optionally `allowlist` (default: the bundled `files/allowlist.txt`; `--set-file 'profiles[0].allowlist=template/allowlist.txt'` for the repo's), `languages` (empty: `profileDefaults.languages`), `extensions`, `image`, `vscodeHost`, `suspended`, `home`, `docker`, `resources`, `env`. The allowlist is the profile's own part, as it is: the operator adds the entries HardCode provides to the profile's languages, its own or the default |

## The `Profile` resource

```yaml
apiVersion: hardcode-app.github.io/v1alpha1
kind: Profile
metadata:
  name: team-a
spec:
  allowlist: |            # same format as a profile's allowlist.txt
    api.anthropic.com   rw
    github.com/team-a   rw
  languages: [python, node, build_essentials, docker, claude_cli]
  extensions: [eamodio.gitlens]
  image: registry.example.com/hardcode/vscode:1.0
  vscodeHost: team-a.code.example.com
  home: { size: 50Gi }
```

| `spec.` field | Default | Meaning |
|---|---|---|
| `allowlist` | empty (the UI starts new profiles from the template) | the egress policy |
| `languages` | `profileDefaults.languages`, or all of them | the languages and tools the profile is built around: `python`, `node`, `java`, `dotnet`, `go`, `rust`, `cpp`, `ruby`, `perl`, `build_essentials`, `docker`, `claude_cli`, `codex_cli` (an empty list: none). They decide the VS Code container's VS Code extensions and the allowlist entries HardCode provides. See below |
| `extensions` | `[]` | VS Code extensions of the profile's own (`publisher.name`, up to 100), kept installed besides those every VS Code container has and those of its languages; a change is installed or removed in the running container, without a restart; only publishers with a domain the Marketplace has verified |
| `image`, `squidImage`, `docker.image`, `coredns.image`, `imagePullPolicy`, `imagePullSecrets` | `profileDefaults` | images |
| `suspended` | `false` | scale the VS Code container, squid and CoreDNS to 0; volumes stay |
| `user` | `dev` | the VS Code container user (the image's `USERNAME`); sets the home mount path |
| `upstreamDNS`, `internalRanges`, `clientRanges` | `profileDefaults` | |
| `vscodeHost` | empty | create an Ingress for VS Code on this host; empty means port-forward only |
| `vscodeToken` | empty | a fixed connection token; empty means generated once and kept in the token Secret |
| `home`, `shared`, `docker` (`size`, `storageClass`; `docker.enabled`, `docker.privileged`) | `profileDefaults` | sizes apply to new volumes and grow existing ones where the StorageClass allows expansion; they can't shrink. `docker.enabled: false` runs the profile without the privileged sidecar |
| `resources`, `dockerResources` | `profileDefaults` | the VS Code container's and the sidecar's resources; a memory limit is always applied (`profileDefaults`' when these leave it out) |
| `env` | `{}` | extra environment for the VS Code container (`VSCODE_DEFAULT_FOLDER`, …); a name the operator also sets (the proxy, the CA bundle) takes this value instead |
| `extraVolumes`, `extraVolumeMounts` | `[]` | mounted into both the VS Code container and docker, the Kubernetes equivalent of `hardcode mount` |
| `networkPolicy.enabled`, `.squidEgressCIDR`, `.squidEgressExcept` | `profileDefaults` | |

Names follow the admin's rule (`a-z`, `0-9` and `-`, up to 32 characters,
starting with a letter and not ending in `-`); the CRD refuses others, since
the name prefixes every Service and pod the profile gets, and a Service name
must start with a letter. (The CLI's local profiles may start with a digit:
Compose has no such limit.)

`status` carries `phase` (`Pending`, `Ready`, `Suspended`, `Error`; the
admin shows `Deleting` while Kubernetes removes a deleted profile),
`message` (what it is waiting for, e.g. `waiting for vscode (ImagePullBackOff)`),
`observedGeneration`, `dnsClusterIP`, `proxyURL`, `vscodeService`,
`tokenSecret` and `lastReconciled`. `kubectl get profiles` (short name
`hcp`) shows phase, image, suspended and age.

## Languages on Kubernetes

A profile is built around languages, as on Compose: each one opens its
block of the allowlist and brings its VS Code extensions. What differs is
the image. With Compose, a profile's languages are build arguments, and
`hardcode` builds the VS Code image with just those toolchains. On
Kubernetes the image is built beforehand and named in `image` (or
`profileDefaults.image`): **the image decides which toolchains a VS Code container
has**, and the default image has them all. A language there decides:

- **the allowlist**: its block of provided entries (its registries). The
  operator gives the proxy the provided entries of the profile's languages
  and tools with `spec.allowlist`, the profile's own entries and the
  provided ones it switches off (`off <entry>`), whenever it applies the
  profile, so the proxy follows a change of `spec.languages` within
  seconds, however it was made. An upgrade of HardCode whose blocks changed
  reaches every profile at the operator's next pass, which logs what
  changed (`profile team-a: allowlist Rust: + newrustsite.com`) and rolls
  the proxy; a provided entry a profile switched off stays off.
- **the extensions**: the operator writes the extensions the profile
  wants (every VS Code container's, those of its languages and tools, and
  its own `extensions`) into the ConfigMap `<profile>-vscode-extensions`
  (key `wanted`), which is mounted into the VS Code container as a
  directory, `/etc/hardcode/extensions`. The kubelet updates a ConfigMap
  mounted that way in a running pod (one mounted with `subPath` it never
  would), on its own sync period, typically within a minute.
  `hardcode-sync-extensions --watch` runs in the background in the
  container, notices the new list within seconds, installs what the
  profile now has and removes what it no longer has (extensions installed
  by hand in VS Code are left alone), one at a time, each held to the
  verified-publisher rule. The pod template doesn't change, so **nothing
  restarts**; the container picks a change up within about a minute. The sync
  logs its progress in the container's log (`[vscode] extensions: 9 of 14
  installed, installing 5` while it works, `[vscode] extensions: 14 of 14
  installed`, with `, 1 refused` or `, 1 to try again` when there are any,
  once it is done), which the admin's *Extensions* tab reads.

A profile without `spec.languages` has the chart's
`profileDefaults.languages`, or all of them when that is empty; changing
`profileDefaults.languages` later changes the extensions and the provided
allowlist entries of such profiles at their next reconcile.

## What the operator does

- Watches `Profile` objects in its namespace (and re-lists every 60 s);
  events for one profile within 0.3 s are coalesced into one reconcile.
- On reconcile it **ensures** the CA Secret (an RSA-3072 CA valid 10 years,
  generated with `openssl`, once) and the token Secret (24 random bytes, or
  `spec.vscodeToken`; a changed fixed token is written to the Secret and,
  through a hash in the pod template, **restarts the VS Code container** so the running
  server uses it; so does a new token written into the Secret, which is how
  the admin's *New token…* works), applies the DNS Service first so Kubernetes allocates its
  ClusterIP, then **server-side applies** the rest in this order:
  configuration, Services, the **NetworkPolicies**, and only then the pods,
  with that IP in the VS Code container pod's `dnsConfig` (`dnsPolicy: None`: CoreDNS
  is the only nameserver). The Ingress comes last and on its own: a host the
  API refuses puts the profile in `Error` with the reason but leaves the
  isolated pods in place. It **removes** an Ingress or NetworkPolicies the
  profile no longer wants (host cleared, policies switched off).
- The proxy's allowlist is **computed on every pass**: `spec.allowlist` with
  the entries this version of HardCode provides to the profile's
  languages, into the `<profile>-allowlist` ConfigMap; when that differs
  from what the ConfigMap had, each changed entry is logged.
- **Ready means the current policy is what's running.** A squid or CoreDNS
  pod counts only if it is Ready, not being deleted, and carries the hash of
  the allowlist just applied (the VS Code container likewise with its token hash);
  otherwise the status says `… still on the previous policy`. The proxy and
  DNS Deployments use the `Recreate` strategy: a policy change stops the old
  pod before the new one starts, so a replacement that can't start means no
  proxy rather than the previous policy carrying on.
- A StatefulSet's claim templates are immutable, so every VS Code container is created
  with all three (`home`, `shared`, `docker-data`; the last unused while the
  sidecar is off) and they are never touched again; the sidecar can be
  switched on and off freely. A size change is applied to the existing PVC
  (expansion, where the StorageClass allows it); a smaller size is reported
  in the status, not applied.
- A fresh home PVC is empty, unlike a Docker volume. A `seed-home` init
  container copies the image's home directory (VS Code server, Claude
  Code, dotfiles; the extensions aren't in the image) in once, entry by entry so the volume root's own
  ownership (often root's) is never touched, and marks it `.hardcode-seeded`
  last, so a copy that was interrupted is done again and a seeded home is
  left alone.
  `tests/images.py` runs it as uid 1000 onto a root-owned volume root.
- Every object carries an **owner reference** to the Profile, so deleting
  the Profile deletes all of it; the StatefulSet's
  `persistentVolumeClaimRetentionPolicy` (`whenDeleted: Delete`,
  `whenScaled: Retain`) removes the volumes with it and keeps them across
  suspend/resume.
- The squid and CoreDNS pod templates carry a hash of the allowlist, so
  **changing `spec.allowlist` rolls both** and the new rules are live within
  seconds; the VS Code container isn't restarted. Changing `spec.languages` or
  `spec.extensions` rewrites the extension ConfigMap only: the running
  VS Code container follows it, and isn't restarted either.
- Status is written through the `status` subresource when it changed (a
  write is a watch event; the operator ignores events that only carried its
  own status, so an unchanged profile settles instead of reconciling itself
  forever); pods becoming ready reach the status through the resync. An
  exception during reconcile puts the profile in `Error` with the message.
- Memory limits on every container: vscode 8Gi and sidecar 4Gi by default
  (`resources`, `dockerResources`), squid 512Mi, CoreDNS 128Mi, the init
  containers 64Mi/256Mi, the admin itself 256Mi. A profile that sets
  resources without a memory limit gets the chart's (`profileDefaults`),
  or the built-in one when the chart has none. Squid's and CoreDNS's can be
  raised only in the chart (`profileDefaults.squidResources`,
  `.corednsResources`): the CRD has no fields for them. A runaway process
  is OOM-killed, never allowed to eat the node.

The VS Code container pod: the `vscode` container (uid 1000, `NET_RAW` dropped, proxy
variables and CA paths set exactly as in Compose, `ENABLE_DOCKER` telling it
whether a daemon is beside it, the extension list mounted read-only at
`/etc/hardcode/extensions`, 1Gi `/dev/shm`) and the `docker` sidecar
(privileged by default, proxy variables, `SSL_CERT_FILE` pointing at the
proxy CA), sharing the `shared` PVC and the daemon's client certificates.
No sandbox pod mounts a service-account token. NetworkPolicies: the VS Code container pod may talk to its squid on 3128
and its CoreDNS on 53 and accept 8000; squid may reach `squidEgressCIDR`
(minus the exceptions) and kube-system DNS; CoreDNS may send DNS anywhere.

## Reaching VS Code

With `vscodeHost`, the operator creates an Ingress (`ingressClassName`,
`ingressAnnotations`, `ingressTLSSecret` from the defaults); the admin UI's
*Access* tab shows the URL with the token. Put TLS and your own sign-in
(OAuth proxy, VPN) in front of it; the token is the only protection
otherwise. Without a host name:

```sh
kubectl -n sandbox port-forward svc/team-a-vscode 8000:8000
kubectl -n sandbox get secret team-a-vscode-token -o jsonpath='{.data.token}' | base64 -d
```

## RBAC

The operator reads its service-account token from the projected file each
time it is used, so the kubelet's rotation never leaves it with an expired
credential. Its ServiceAccount has a namespaced Role: full access to
`profiles` and `profiles/status`, `configmaps`, `secrets`, `services`,
`deployments`, `statefulsets`, `networkpolicies`, `ingresses`; read access
to `pods` and `pods/log` (for the blocked-traffic view, and how far a
VS Code container has got with its extensions); read and patch on
`persistentvolumeclaims` (to grow volumes). Nothing cluster-wide: install
one release per namespace.

## How it maps to Compose

| Compose stack | Chart |
|---|---|
| `profiles/<name>/` (`.env`, `allowlist.txt`, override) | a `Profile` object: `spec.allowlist`, `spec.languages`, `spec.extensions`, `spec.image`, sizes, mounts |
| `hardcode -p name up` | the operator, as soon as the Profile exists |
| `hardcode --all allow …` | *All allowlists* in the admin UI, or `POST /api/allowlist/bulk` |
| `hardcode enable` / `disable` (a language) | `spec.languages`, switched on the admin's *Languages / Tools* tab (its allowlist entries and extensions; the image stays as it is) |
| `hardcode allowlist` | the admin's *Allowlist* tab, or `GET /api/profiles/{name}` (its `allowlist`) |
| `profiles/<name>/proxy-allowlist.txt` | the `<profile>-allowlist` ConfigMap |
| `hardcode extension add` / `remove` | `spec.extensions`, on the admin's *Extensions* tab, which also shows how far the installing has got |
| `hardcode mount` | `spec.extraVolumes` + `spec.extraVolumeMounts`, applied to vscode and docker |
| `hardcode forward` | `kubectl port-forward` to the VS Code container pod |
| the gateway | an Ingress per profile (`vscodeHost`), or `kubectl port-forward` |
| `hardcode down -v` / `profile remove` | `suspended: true` / deleting the Profile |
| the `pki` and `allowlist` one-shots | the CA Secret (once) and the render init container (every pod start) |

## Chart layout

```
helm/hardcode/Chart.yaml
helm/hardcode/values.yaml
helm/hardcode/crds/profiles.hardcode-app.github.io.yaml   the CRD (installed by helm on first install; upgrade it with kubectl apply)
helm/hardcode/templates/operator.yaml           ServiceAccount, Role, RoleBinding, bootstrap Secret, Deployment, Service, Ingress
helm/hardcode/templates/profiles.yaml           the profiles from values
helm/hardcode/files/allowlist.txt               the bundled allowlist new profiles start with: their own part (the template's)
```

`tests/helm.py` lints and renders the chart (and checks that a chart
profile carries its allowlist as it is); `tests/e2e_kind.py` installs it
on a kind cluster, signs in through the API, drives the operator for real and
checks from a pod in the VS Code container's position that the policies are enforced
([Testing](testing.md)).
