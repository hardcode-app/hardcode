"""
The Kubernetes objects one Profile turns into.

They have the same shape as the Compose stack: a VS Code container pod (VS Code, with a
rootless Docker daemon beside it), squid, the filtering CoreDNS, and
NetworkPolicies that leave the VS Code container no way out but through that squid and
that DNS.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Optional

from hardcode_cli import allowlist, lan, langs, packages

# HardCode's API group, and the prefix of its labels and annotations: the
# project's GitHub organisation's namespace, which only it controls.
GROUP = "hardcode-app.github.io"
VERSION = "v1alpha1"
API_VERSION = f"{GROUP}/{VERSION}"
LABEL_PROFILE = f"{GROUP}/profile"
LABEL_COMPONENT = f"{GROUP}/component"

# On a pod template: the policy or token it was made with, so that a change
# rolls the pods.
ANNOTATION_ALLOWLIST_HASH = f"{GROUP}/allowlist-hash"
ANNOTATION_TOKEN_HASH = f"{GROUP}/token-hash"

# On a profile: when a new VS Code token was last asked for (the admin's
# rotate_token writes it), which the operator records in the profile's
# status once VS Code has it.
ANNOTATION_TOKEN_ROTATED = f"{GROUP}/token-rotated-at"

# The components whose pods a change of a profile can restart.
WORKLOADS = ("vscode", "squid", "coredns")

# Hard memory limits on every container: a runaway build, a misbehaving proxy
# or a nested container must be OOM-killed, never allowed to eat the node.
# The VS Code container's and the Docker sidecar's can be raised by the chart
# (profileDefaults) and by a profile; squid's and CoreDNS's by the chart only
# (profileDefaults.squidResources, .corednsResources), as the CRD has no
# fields for them. Nothing can leave a limit out.
VSCODE_RESOURCES = {"requests": {"cpu": "250m", "memory": "1Gi"}, "limits": {"memory": "8Gi"}}
DOCKER_RESOURCES = {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "4Gi"}}
SQUID_RESOURCES = {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}}
COREDNS_RESOURCES = {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}}
# The allowlist's renderer, the init container of squid's and CoreDNS's
# pods, checks each policy with squid's own parser, which sizes its tables
# for squid.conf's descriptors.
RENDER_RESOURCES = {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "256Mi"}}
SEED_RESOURCES = {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}}

# The resource names in REST paths.
PLURALS = {
    "ConfigMap": "configmaps",
    "Secret": "secrets",
    "Service": "services",
    "Deployment": "deployments",
    "StatefulSet": "statefulsets",
    "NetworkPolicy": "networkpolicies",
    "Ingress": "ingresses",
    "Profile": "profiles",
    "PersistentVolumeClaim": "persistentvolumeclaims",
}

# Kinds a profile may or may not have, depending on its spec. The operator
# removes the ones that are no longer wanted.
OPTIONAL_KINDS = ("Ingress", "NetworkPolicy")

CLAIMS = ("home", "shared", "docker-data")

# Squid's private networks to stay out of, unless the chart says otherwise:
# RFC 1918, shared address space, link-local (cloud metadata) and loopback.
PRIVATE_RANGES = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8"]

SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

# A fresh home PVC is empty, unlike a Docker volume, which is seeded from the
# image. This copies the image's home (the VS Code server, Claude Code,
# dotfiles) in once; the extensions aren't in the image, and the VS Code
# container installs them into the home volume as it runs.
SEED_HOME = """set -e
home="$1"; mnt="$2"
# The marker is written last, so a seeding that didn't finish runs again. The
# entries are copied one by one, so the volume root's own ownership and times,
# which may be root's, are never touched.
if [ ! -e "$mnt/.hardcode-seeded" ]; then
  find "$home" -mindepth 1 -maxdepth 1 -exec cp -a -t "$mnt/" -- {} +
  touch "$mnt/.hardcode-seeded"
fi
[ -L "$mnt/shared" ] || [ -e "$mnt/shared" ] || ln -s /shared "$mnt/shared"
"""

# A Kubernetes quantity: a number with an optional decimal or binary suffix.
_QUANTITY = re.compile(r"^(\d+(?:\.\d+)?)([KMGTPE]i?|[kmun]?)$")
_SCALE = {
    "": 1,
    "k": 10**3,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
    "m": 1e-3,
    "u": 1e-6,
    "n": 1e-9,
}


def is_quantity(value: str) -> bool:
    """
    Whether *value* is a size the operator can read: a number with an
    optional decimal or binary suffix (``20Gi``, ``500Mi``, ``1.5T``), as
    Kubernetes writes one. Anything else (``50GB``) would put the profile
    in Error when its volumes are compared.
    """
    return bool(_QUANTITY.match(value))


def quantity_bytes(value: str) -> float:
    """
    A Kubernetes quantity (``20Gi``, ``500M``, ``1e9``) as a plain number.
    """
    text = str(value).strip()

    match = _QUANTITY.match(text)
    if match:
        return float(match.group(1)) * _SCALE[match.group(2)]

    return float(text)


@dataclass(frozen=True)
class Names:
    """
    The names of everything a profile owns, all prefixed with the profile's.
    """

    profile: str

    @property
    def allowlist(self) -> str:
        return f"{self.profile}-allowlist"

    @property
    def extensions(self) -> str:
        return f"{self.profile}-vscode-extensions"

    @property
    def proxy_ca(self) -> str:
        return f"{self.profile}-proxy-ca"

    @property
    def proxy_ca_public(self) -> str:
        return f"{self.profile}-proxy-ca-cert"

    @property
    def token(self) -> str:
        return f"{self.profile}-vscode-token"

    @property
    def squid(self) -> str:
        return f"{self.profile}-squid"

    @property
    def dns(self) -> str:
        return f"{self.profile}-dns"

    @property
    def coredns(self) -> str:
        return f"{self.profile}-coredns"

    @property
    def vscode(self) -> str:
        return f"{self.profile}-vscode"

    @property
    def vscode_pod(self) -> str:
        """
        The VS Code StatefulSet's one pod.
        """
        return f"{self.vscode}-0"

    def pvc(self, claim: str) -> str:
        """
        The PVC a StatefulSet's claim template becomes for its one pod.
        """
        return f"{claim}-{self.vscode}-0"


# ---------------------------------------------------------------------------
# The spec, with defaults
# ---------------------------------------------------------------------------


def _pick(spec: dict, defaults: dict, *keys: str, fallback: Any = None) -> Any:
    """
    The first of ``spec[k0][k1]...``, ``defaults[k0][k1]...`` and *fallback*
    that is set.

    An empty string counts as not set: an empty field in the UI means
    "inherit".
    """
    for source in (spec, defaults):
        value: Any = source
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None

        if value is not None and value != "":
            return value

    return fallback


def pinned_image(spec: dict, key: str, name: str) -> str:
    """
    The image the effective *spec* has under *key*. The chart pins it
    (``profileDefaults.<name>``), as compose.yaml does, and the operator
    keeps no copy of its own to fall out of step: without one, the
    ``ValueError`` says which is missing, and the operator reports it on
    the profile.
    """
    image = spec.get(key)
    if not image:
        raise ValueError(f"no {name}: neither the profile's spec nor the chart's profileDefaults sets it")

    return image


def with_memory_limit(resources: Any, default: dict) -> dict:
    """
    *resources* as given, but never without a memory limit.
    """
    merged = copy.deepcopy(resources) if isinstance(resources, dict) else {}

    limits = merged.setdefault("limits", {})
    if not isinstance(limits, dict):
        merged["limits"] = limits = {}

    if not limits.get("memory"):
        limits["memory"] = default["limits"]["memory"]

    return merged


def effective_languages(spec: dict, defaults: Optional[dict] = None) -> list[str]:
    """
    The languages and tools a profile is built around, in catalogue order.

    The profile's own list when it has one (an empty list means none), else
    the chart's ``profileDefaults.languages`` when that names any, else every
    one, as the default image has them all. Keys the catalogue doesn't know
    are left out.
    """
    chosen = spec.get("languages")

    if chosen is None:
        chosen = (defaults or {}).get("languages") or list(langs.LANGS)

    return [key for key in langs.LANGS if key in chosen]


def effective_packages(spec: dict) -> list[str]:
    """
    The profile's own Ubuntu packages, in their order and without repeats.
    Anything that isn't a package's name by Debian's rule is left out, as
    the CRD refuses it: only names ever reach the VS Code container.
    """
    named = [item for item in spec.get("packages") or [] if isinstance(item, str)]

    return list(dict.fromkeys(name for name in named if packages.PACKAGE_NAME.match(name)))


def lan_settings(defaults: Optional[dict] = None) -> dict:
    """
    Whether this cluster lets a profile's names reach an address on its
    network (``profileDefaults.lan.enabled``, off unless the chart says
    so), and the ranges no name may be pinned into (``.clusterRanges``):
    the cluster's own pod and service ranges, which the operator can't read
    for itself with the namespace's permissions only. Without them, the
    operator takes no pin at all (``lan_off``).

    Only the chart decides: a profile's spec can't switch it on.
    """
    settings = (defaults or {}).get("lan") or {}
    if not isinstance(settings, dict):
        settings = {}

    cluster_ranges = settings.get("clusterRanges") or []
    if not isinstance(cluster_ranges, list):
        cluster_ranges = []

    return {"enabled": settings.get("enabled") is True, "clusterRanges": [str(item) for item in cluster_ranges]}


# Why the operator ignores a pin, when it does.
LAN_OFF = "this cluster doesn't let names reach its network (the chart's profileDefaults.lan.enabled)"
LAN_NO_CLUSTER_RANGES = "list the cluster's pod and service ranges in profileDefaults.lan.clusterRanges first"
LAN_CLUSTER = "inside the cluster's own pod or service ranges (the chart's profileDefaults.lan.clusterRanges)"

# The sandbox's own network that pins are checked against: none, since a
# cluster has no devnet. The operator gives the renderer an empty
# DEVNET_SUBNET to match, and the cluster's own ranges (CLUSTER_RANGES).
CLUSTER_DEVNET: tuple[str, ...] = ()


def lan_off(settings: dict) -> str:
    """
    Why the operator takes none of a profile's pins on this cluster (with
    the *settings* of ``lan_settings``): the chart doesn't switch them on,
    or it doesn't list the cluster's own ranges, without which a pin could
    reach the API server, a pod or a node. ``""`` when it takes them.
    """
    if not settings["enabled"]:
        return LAN_OFF
    if not settings["clusterRanges"]:
        return LAN_NO_CLUSTER_RANGES

    return ""


def effective_pins(text: str, internal_ranges: str, settings: dict) -> list[dict]:
    """
    The names a profile's allowlist (*text*) pins to an address on the
    network, each as ``allowlist.Pin.as_dict`` with ``ignored``: why the
    operator leaves it out of the proxy's rules and the NetworkPolicy
    (it takes no pins on this cluster, ``lan_off``, or the address is in
    the cluster's own ranges), ``""`` when it doesn't. A pin with a problem
    of its own (``problem``) is passed on, and the proxy refuses to start
    with it, as it does with any invalid line.
    """
    ranges = lan.internal_ranges(internal_ranges)
    off = lan_off(settings)
    found = []

    for pin in allowlist.pins(text, ranges, CLUSTER_DEVNET):
        ignored = off
        if not ignored and not pin.problem and lan.in_ranges(pin.address, settings["clusterRanges"]):
            ignored = LAN_CLUSTER

        found.append({**pin.as_dict(), "ignored": ignored})

    return found


def effective_spec(profile: dict, defaults: Optional[dict] = None) -> dict:
    """
    The profile's spec, with the operator's defaults filled in, and
    ``proxyAllowlist``: the allowlist the proxy is given, the profile's own
    (``allowlist``) with the entries this HardCode provides to its languages,
    and without the pins the operator ignores (``lan``, ``effective_pins``).

    The Docker sidecar's and CoreDNS's images have no built-in default: the
    chart pins them (``profileDefaults``), and they are None without one
    (``pinned_image`` refuses to render them).
    """
    spec = profile.get("spec") or {}

    def pick(*keys: str, fallback: Any = None) -> Any:
        return _pick(spec, defaults or {}, *keys, fallback=fallback)

    def resources(key: str, built_in: dict) -> dict:
        # A profile's resources without a memory limit inherit the chart's
        # (profileDefaults), and only without one there the built-in limit.
        inherited = with_memory_limit((defaults or {}).get(key), built_in)

        return with_memory_limit(pick(key, fallback=built_in), inherited)

    own_allowlist = spec.get("allowlist") or ""
    languages = effective_languages(spec, defaults)

    internal_ranges = pick("internalRanges", fallback="")
    lan_config = lan_settings(defaults)
    pins = effective_pins(own_allowlist, internal_ranges, lan_config)
    ignored = {pin["name"]: pin["ignored"] for pin in pins if pin["ignored"]}

    return {
        "image": pick("image", fallback="hardcode-vscode:latest"),
        "squidImage": pick("squidImage", fallback="hardcode-squid:latest"),
        "dockerImage": pick("docker", "image"),
        "corednsImage": pick("coredns", "image"),
        "imagePullPolicy": pick("imagePullPolicy", fallback="IfNotPresent"),
        "imagePullSecrets": pick("imagePullSecrets", fallback=[]),
        "allowlist": own_allowlist,
        "proxyAllowlist": allowlist.render(allowlist.ignore_pins(own_allowlist, ignored), languages),
        "lan": {**lan_config, "pins": pins},
        "languages": languages,
        "extensions": list(spec.get("extensions") or []),
        "packages": effective_packages(spec),
        "suspended": bool(spec.get("suspended")),
        "user": spec.get("user") or "dev",
        "upstreamDNS": pick("upstreamDNS", fallback="/etc/resolv.conf"),
        "internalRanges": internal_ranges,
        "clientRanges": pick("clientRanges", fallback=""),
        "home": {
            "size": pick("home", "size", fallback="20Gi"),
            "storageClass": pick("home", "storageClass", fallback=""),
        },
        "shared": {
            "size": pick("shared", "size", fallback="5Gi"),
            "storageClass": pick("shared", "storageClass", fallback=""),
        },
        "docker": {
            "enabled": pick("docker", "enabled", fallback=True),
            "privileged": pick("docker", "privileged", fallback=True),
            "size": pick("docker", "size", fallback="30Gi"),
            "storageClass": pick("docker", "storageClass", fallback=""),
        },
        "resources": resources("resources", VSCODE_RESOURCES),
        "dockerResources": resources("dockerResources", DOCKER_RESOURCES),
        "squidResources": resources("squidResources", SQUID_RESOURCES),
        "corednsResources": resources("corednsResources", COREDNS_RESOURCES),
        "env": spec.get("env") or {},
        "extraVolumes": spec.get("extraVolumes") or [],
        "extraVolumeMounts": spec.get("extraVolumeMounts") or [],
        "vscodeHost": spec.get("vscodeHost") or "",
        "vscodeToken": spec.get("vscodeToken") or "",
        "networkPolicy": {
            "enabled": pick("networkPolicy", "enabled", fallback=True),
            "squidEgressCIDR": pick("networkPolicy", "squidEgressCIDR", fallback="0.0.0.0/0"),
            "squidEgressExcept": pick("networkPolicy", "squidEgressExcept", fallback=list(PRIVATE_RANGES)),
        },
    }


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def labels(profile: dict, component: str) -> dict:
    """
    The labels on everything a profile owns.
    """
    return {
        "app.kubernetes.io/name": "hardcode",
        "app.kubernetes.io/managed-by": "hardcode-operator",
        LABEL_PROFILE: profile["metadata"]["name"],
        LABEL_COMPONENT: component,
    }


def selector(profile: dict, component: str) -> dict:
    """
    The labels that pick one component's pods.
    """
    return {LABEL_PROFILE: profile["metadata"]["name"], LABEL_COMPONENT: component}


def owner_reference(profile: dict) -> dict:
    """
    Makes the Profile the owner: deleting it deletes what it owns.
    """
    return {
        "apiVersion": API_VERSION,
        "kind": "Profile",
        "name": profile["metadata"]["name"],
        "uid": profile["metadata"]["uid"],
        "controller": True,
        "blockOwnerDeletion": True,
    }


def meta(profile: dict, name: str, component: str) -> dict:
    """
    The metadata of an object the profile owns.
    """
    return {
        "name": name,
        "namespace": profile["metadata"]["namespace"],
        "labels": labels(profile, component),
        "ownerReferences": [owner_reference(profile)],
    }


def proxy_env(proxy_url: str) -> list[dict]:
    """
    The proxy variables, in both the capitalised and the lower-case spelling,
    since tools disagree on which they read.
    """
    no_proxy = "localhost,127.0.0.1,::1"
    variables = (
        ("HTTP_PROXY", proxy_url),
        ("HTTPS_PROXY", proxy_url),
        ("NO_PROXY", no_proxy),
        ("http_proxy", proxy_url),
        ("https_proxy", proxy_url),
        ("no_proxy", no_proxy),
    )

    return [{"name": name, "value": value} for name, value in variables]


def path_for(obj: dict) -> str:
    """
    The REST path of an object.
    """
    metadata = obj["metadata"]
    return f"{collection_path(metadata['namespace'], obj['apiVersion'], obj['kind'])}/{metadata['name']}"


def collection_path(namespace: str, api_version: str, kind: str) -> str:
    """
    The REST path of the objects of one kind in a namespace.
    """
    base = "/api/v1" if api_version == "v1" else f"/apis/{api_version}"
    return f"{base}/namespaces/{namespace}/{PLURALS[kind]}"


def pvc_template(name: str, size: str, storage_class: str) -> dict:
    """
    A StatefulSet claim template; the cluster's default storage class unless
    one is named.
    """
    spec: dict = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}}
    if storage_class:
        spec["storageClassName"] = storage_class

    return {"metadata": {"name": name}, "spec": spec}


def claim_templates(spec: dict) -> list[dict]:
    """
    The three claims every VS Code container StatefulSet is created with.

    The Docker data claim is there even with the daemon switched off: a
    StatefulSet's claim templates can't change afterwards, and the daemon can
    be switched on later.
    """
    return [
        pvc_template("home", spec["home"]["size"], spec["home"]["storageClass"]),
        pvc_template("shared", spec["shared"]["size"], spec["shared"]["storageClass"]),
        pvc_template("docker-data", spec["docker"]["size"], spec["docker"]["storageClass"]),
    ]


# ---------------------------------------------------------------------------
# One profile's objects
# ---------------------------------------------------------------------------


@dataclass
class _Build:
    """
    What every part of a profile's rendering needs to know.
    """

    profile: dict
    spec: dict
    defaults: dict
    ctx: dict
    names: Names
    namespace: str

    @property
    def squid_fqdn(self) -> str:
        return f"{self.names.squid}.{self.namespace}.svc.cluster.local"

    @property
    def proxy_url(self) -> str:
        return f"http://{self.squid_fqdn}:3128"

    @property
    def home(self) -> str:
        return f"/home/{self.spec['user']}"

    @property
    def replicas(self) -> int:
        return 0 if self.spec["suspended"] else 1

    @property
    def dns_ip(self) -> Optional[str]:
        return self.ctx.get("dnsClusterIP")

    def meta(self, name: str, component: str) -> dict:
        return meta(self.profile, name, component)

    def labels(self, component: str) -> dict:
        return labels(self.profile, component)

    def selector(self, component: str) -> dict:
        return selector(self.profile, component)

    def pods_of(self, component: str) -> dict:
        """
        A NetworkPolicy peer: the pods of one of this profile's components.
        """
        return {"podSelector": {"matchLabels": self.selector(component)}}


def _render_allowlist_init(build: _Build) -> dict:
    """
    The init container that renders the allowlist into squid's and CoreDNS's
    configuration, as the Compose stack's one-shot ``allowlist`` service does.
    """
    spec = build.spec

    return {
        "name": "render-allowlist",
        "image": spec["squidImage"],
        "imagePullPolicy": spec["imagePullPolicy"],
        "command": ["/usr/local/bin/allowlist-render"],
        "resources": RENDER_RESOURCES,
        "securityContext": {"runAsUser": 0},
        "env": [
            {"name": "UPSTREAM_DNS", "value": spec["upstreamDNS"]},
            # The VS Code container reaches squid by name, so the DNS filter answers for it.
            {"name": "EXTRA_ZONES", "value": build.squid_fqdn},
            {"name": "INTERNAL_RANGES", "value": spec["internalRanges"]},
            {"name": "CLIENT_RANGES", "value": spec["clientRanges"]},
            # A cluster has no devnet (the renderer's default is Compose's);
            # its own pod and service ranges are what no pin, and no request
            # through the proxy, may reach.
            {"name": "DEVNET_SUBNET", "value": " ".join(CLUSTER_DEVNET)},
            {"name": "CLUSTER_RANGES", "value": " ".join(spec["lan"]["clusterRanges"])},
        ],
        "volumeMounts": [
            {"name": "allowlist", "mountPath": "/config", "readOnly": True},
            {"name": "generated", "mountPath": "/out"},
        ],
    }


def _config_maps(build: _Build) -> list[dict]:
    """
    The allowlist, the VS Code extensions the profile wants, and the public
    half of the profile's proxy CA.

    The extension list is mounted as a directory, which the kubelet keeps up
    to date in a running pod: the VS Code container watches it and installs
    or removes extensions by itself, so a change doesn't restart the pod.
    """
    spec = build.spec
    wanted = langs.extension_list(spec["languages"], spec["extensions"])

    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": build.meta(build.names.allowlist, "config"),
            "data": {"allowlist.txt": spec["proxyAllowlist"]},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": build.meta(build.names.extensions, "config"),
            "data": {"wanted": wanted},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": build.meta(build.names.proxy_ca_public, "config"),
            "data": {"ca.pem": build.ctx["caCert"]},
        },
    ]


def _squid_service(build: _Build) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": build.meta(build.names.squid, "squid"),
        "spec": {
            "type": "ClusterIP",
            "selector": build.selector("squid"),
            "ports": [{"name": "proxy", "port": 3128, "targetPort": "proxy"}],
        },
    }


def _squid_deployment(build: _Build) -> dict:
    spec, names = build.spec, build.names

    squid = {
        "name": "squid",
        "image": spec["squidImage"],
        "imagePullPolicy": spec["imagePullPolicy"],
        "ports": [{"name": "proxy", "containerPort": 3128}],
        "securityContext": {
            "runAsUser": 13,
            "runAsGroup": 13,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
        "readinessProbe": {"tcpSocket": {"port": "proxy"}, "periodSeconds": 5},
        "livenessProbe": {"tcpSocket": {"port": "proxy"}, "periodSeconds": 20},
        "resources": spec["squidResources"],
        "volumeMounts": [
            {"name": "generated", "mountPath": "/etc/squid/generated", "subPath": "squid", "readOnly": True},
            {"name": "proxy-ca", "mountPath": "/proxy-ca", "readOnly": True},
            {"name": "spool", "mountPath": "/var/spool/squid"},
        ],
    }

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": build.meta(names.squid, "squid"),
        "spec": {
            "replicas": build.replicas,
            # Recreate: a policy change stops the old proxy before the new one
            # starts. A replacement that can't start means no proxy, never the
            # previous policy carrying on.
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": build.selector("squid")},
            "template": {
                "metadata": {
                    "labels": build.labels("squid"),
                    # A new allowlist changes the pod template, which rolls the pod.
                    "annotations": {ANNOTATION_ALLOWLIST_HASH: build.ctx["allowlistHash"]},
                },
                "spec": {
                    "automountServiceAccountToken": False,
                    "imagePullSecrets": spec["imagePullSecrets"],
                    "securityContext": {"fsGroup": 13},  # squid's group reads the CA
                    "initContainers": [_render_allowlist_init(build)],
                    "containers": [squid],
                    "volumes": [
                        {"name": "allowlist", "configMap": {"name": names.allowlist}},
                        {"name": "generated", "emptyDir": {}},
                        {"name": "spool", "emptyDir": {}},
                        {"name": "proxy-ca", "secret": {"secretName": names.proxy_ca, "defaultMode": 0o440}},
                    ],
                },
            },
        },
    }


def _dns_service(build: _Build) -> dict:
    service: dict = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": build.meta(build.names.dns, "coredns"),
        "spec": {
            "type": "ClusterIP",
            "selector": build.selector("coredns"),
            "ports": [
                {"name": "dns-udp", "port": 53, "targetPort": "dns-udp", "protocol": "UDP"},
                {"name": "dns-tcp", "port": 53, "targetPort": "dns-tcp", "protocol": "TCP"},
            ],
        },
    }

    # The VS Code container's resolver is this Service's address, so once Kubernetes has
    # allocated one it is kept; the first time, it is left to Kubernetes.
    if build.dns_ip:
        service["spec"]["clusterIP"] = build.dns_ip

    return service


def _coredns_deployment(build: _Build) -> dict:
    spec, names = build.spec, build.names

    coredns = {
        "name": "coredns",
        "image": pinned_image(spec, "corednsImage", "coredns.image"),
        "imagePullPolicy": spec["imagePullPolicy"],
        "args": ["-conf", "/etc/coredns/Corefile"],
        "ports": [
            {"name": "dns-udp", "containerPort": 53, "protocol": "UDP"},
            {"name": "dns-tcp", "containerPort": 53, "protocol": "TCP"},
        ],
        "securityContext": {
            "runAsUser": 65532,
            "runAsGroup": 65532,
            "readOnlyRootFilesystem": True,
            "allowPrivilegeEscalation": False,
            # Port 53 is a privileged port; that is the one capability it keeps.
            "capabilities": {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]},
        },
        "readinessProbe": {"tcpSocket": {"port": "dns-tcp"}, "periodSeconds": 5},
        "resources": spec["corednsResources"],
        "volumeMounts": [{"name": "generated", "mountPath": "/etc/coredns", "readOnly": True}],
    }

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": build.meta(names.coredns, "coredns"),
        "spec": {
            "replicas": build.replicas,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": build.selector("coredns")},
            "template": {
                "metadata": {
                    "labels": build.labels("coredns"),
                    "annotations": {ANNOTATION_ALLOWLIST_HASH: build.ctx["allowlistHash"]},
                },
                "spec": {
                    "automountServiceAccountToken": False,
                    "imagePullSecrets": spec["imagePullSecrets"],
                    "initContainers": [_render_allowlist_init(build)],
                    "containers": [coredns],
                    "volumes": [
                        {"name": "allowlist", "configMap": {"name": names.allowlist}},
                        {"name": "generated", "emptyDir": {}},
                    ],
                },
            },
        },
    }


def _vscode_service(build: _Build) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": build.meta(build.names.vscode, "vscode"),
        "spec": {
            "type": "ClusterIP",
            "selector": build.selector("vscode"),
            "ports": [{"name": "vscode", "port": 8000, "targetPort": "vscode"}],
        },
    }


def _vscode_env(build: _Build) -> list[dict]:
    """
    The VS Code container's environment: the proxy, the Docker daemon beside
    it when there is one, the VS Code token, the CA bundle for tools that
    don't read the system's, the profile's own Ubuntu packages when it has
    any, and last the profile's own variables.

    The image is shared here, not built per profile, so the packages are
    installed as the container starts (HARDCODE_PACKAGES, which the
    entrypoint reads): a change of them restarts the pod. Without any,
    the variable isn't there, and the pod is as it was.

    The profile's own win: one of ours with the same name is left out, since
    the same variable twice in one container is something server-side apply
    refuses, and the profile would never start.
    """
    spec = build.spec
    docker_enabled = spec["docker"]["enabled"]

    docker = []
    if docker_enabled:
        docker = [
            {"name": "DOCKER_HOST", "value": "tcp://localhost:2376"},
            {"name": "DOCKER_TLS_VERIFY", "value": "1"},
            {"name": "DOCKER_CERT_PATH", "value": "/certs/client"},
            {"name": "TESTCONTAINERS_HOST_OVERRIDE", "value": "localhost"},
        ]

    settings = [
        {"name": "ENABLE_DOCKER", "value": "true" if docker_enabled else "false"},
        {"name": "VSCODE_PUBLISHED_PORT", "value": "8000"},
        {"name": "VSCODE_CONNECTION_TOKEN", "valueFrom": {"secretKeyRef": {"name": build.names.token, "key": "token"}}},
        {"name": "DISABLE_TELEMETRY", "value": "1"},
        {"name": "DISABLE_ERROR_REPORTING", "value": "1"},
        {"name": "ENABLE_CLAUDEAI_MCP_SERVERS", "value": "false"},
        {"name": "SSL_CERT_FILE", "value": SYSTEM_CA_BUNDLE},
        {"name": "NODE_EXTRA_CA_CERTS", "value": "/usr/local/share/ca-certificates/hardcode-proxy-ca.crt"},
        {"name": "REQUESTS_CA_BUNDLE", "value": SYSTEM_CA_BUNDLE},
        {"name": "PIP_CERT", "value": SYSTEM_CA_BUNDLE},
        # Codex reads this one for its WebSocket connections too.
        {"name": "CODEX_CA_CERTIFICATE", "value": SYSTEM_CA_BUNDLE},
    ]

    wanted = []
    if spec["packages"]:
        wanted = [{"name": "HARDCODE_PACKAGES", "value": packages.join(spec["packages"])}]

    ours = proxy_env(build.proxy_url) + docker + settings + wanted
    custom = [{"name": name, "value": str(value)} for name, value in spec["env"].items()]

    return [variable for variable in ours if variable["name"] not in spec["env"]] + custom


def _vscode_container(build: _Build) -> dict:
    spec = build.spec

    return {
        "name": "vscode",
        "image": spec["image"],
        "imagePullPolicy": spec["imagePullPolicy"],
        "ports": [{"name": "vscode", "containerPort": 8000}],
        "env": _vscode_env(build),
        # NET_RAW goes: no raw sockets, so no crafting packets past the proxy.
        "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "capabilities": {"drop": ["NET_RAW"]}},
        "readinessProbe": {"tcpSocket": {"port": "vscode"}, "periodSeconds": 5},
        "resources": spec["resources"],
        "volumeMounts": [
            {"name": "home", "mountPath": build.home},
            {"name": "shared", "mountPath": "/shared"},
            {"name": "proxy-ca", "mountPath": "/proxy-ca", "readOnly": True},
            {"name": "docker-certs-client", "mountPath": "/certs/client", "readOnly": True},
            {"name": "dshm", "mountPath": "/dev/shm"},
            # A directory, not a subPath: only then does a change reach the pod.
            {"name": "extensions", "mountPath": "/etc/hardcode/extensions", "readOnly": True},
            *spec["extraVolumeMounts"],
        ],
    }


def _docker_container(build: _Build) -> dict:
    """
    The rootless Docker daemon beside the VS Code container, reached on localhost over TLS.
    """
    spec = build.spec
    docker_info = "DOCKER_HOST=unix:///run/user/1000/docker.sock docker info >/dev/null 2>&1"

    return {
        "name": "docker",
        "image": pinned_image(spec, "dockerImage", "docker.image"),
        "imagePullPolicy": spec["imagePullPolicy"],
        "env": proxy_env(build.proxy_url)
        + [
            {"name": "DOCKER_TLS_CERTDIR", "value": "/certs"},
            {"name": "SSL_CERT_FILE", "value": "/proxy-ca/ca.pem"},
        ],
        "securityContext": {"privileged": spec["docker"]["privileged"]},
        "readinessProbe": {
            "exec": {"command": ["sh", "-c", docker_info]},
            "periodSeconds": 10,
            "initialDelaySeconds": 10,
        },
        "resources": spec["dockerResources"],
        "volumeMounts": [
            {"name": "docker-data", "mountPath": "/home/rootless/.local/share/docker"},
            {"name": "docker-certs", "mountPath": "/certs"},
            {"name": "docker-certs-client", "mountPath": "/certs/client"},
            {"name": "shared", "mountPath": "/shared"},
            {"name": "proxy-ca", "mountPath": "/proxy-ca", "readOnly": True},
            *spec["extraVolumeMounts"],
        ],
    }


def _vscode_statefulset(build: _Build) -> dict:
    spec, names = build.spec, build.names

    # A StatefulSet's claim templates are immutable: an existing one keeps its own.
    claims = build.ctx.get("existingClaims") or claim_templates(spec)

    containers = [_vscode_container(build)]
    volumes = [
        {"name": "proxy-ca", "configMap": {"name": names.proxy_ca_public}},
        {"name": "extensions", "configMap": {"name": names.extensions}},
        {"name": "docker-certs", "emptyDir": {}},
        {"name": "docker-certs-client", "emptyDir": {}},
        {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}},
        *spec["extraVolumes"],
    ]

    if spec["docker"]["enabled"]:
        containers.append(_docker_container(build))

    seed_home = {
        "name": "seed-home",
        "image": spec["image"],
        "imagePullPolicy": spec["imagePullPolicy"],
        "command": ["bash", "-c", SEED_HOME, "seed-home", build.home, "/mnt/home"],
        "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
        "resources": SEED_RESOURCES,
        "volumeMounts": [{"name": "home", "mountPath": "/mnt/home"}],
    }

    # Names resolve only through the profile's own filtering DNS. Before its
    # Service has an address (the very first render), nothing resolves.
    dns_config = {
        "nameservers": [build.dns_ip or "127.0.0.1"],
        "searches": [f"{build.namespace}.svc.cluster.local", "svc.cluster.local", "cluster.local"],
    }

    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": build.meta(names.vscode, "vscode"),
        "spec": {
            "serviceName": names.vscode,
            "replicas": build.replicas,
            "selector": {"matchLabels": build.selector("vscode")},
            "persistentVolumeClaimRetentionPolicy": {"whenDeleted": "Delete", "whenScaled": "Retain"},
            "template": {
                "metadata": {
                    "labels": build.labels("vscode"),
                    # A new token changes the pod template, which restarts the server with it.
                    "annotations": {ANNOTATION_TOKEN_HASH: build.ctx.get("tokenHash", "")},
                },
                "spec": {
                    "automountServiceAccountToken": False,
                    "imagePullSecrets": spec["imagePullSecrets"],
                    "dnsPolicy": "None",
                    "dnsConfig": dns_config,
                    "securityContext": {"fsGroup": 1000},
                    "initContainers": [seed_home],
                    "containers": containers,
                    "volumes": volumes,
                },
            },
            "volumeClaimTemplates": claims,
        },
    }


def _ingress(build: _Build) -> Optional[dict]:
    """
    The VS Code container's Ingress, when the profile has a host name for it.
    """
    host = build.spec["vscodeHost"]
    if not host:
        return None

    defaults = build.defaults
    backend = {"service": {"name": build.names.vscode, "port": {"number": 8000}}}
    spec: dict = {
        "rules": [
            {
                "host": host,
                "http": {"paths": [{"path": "/", "pathType": "Prefix", "backend": backend}]},
            }
        ]
    }

    if defaults.get("ingressClassName"):
        spec["ingressClassName"] = defaults["ingressClassName"]
    if defaults.get("ingressTLSSecret"):
        spec["tls"] = [{"secretName": defaults["ingressTLSSecret"], "hosts": [host]}]

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            **build.meta(build.names.vscode, "ingress"),
            "annotations": defaults.get("ingressAnnotations") or {},
        },
        "spec": spec,
    }


def _network_policies(build: _Build) -> list[dict]:
    """
    The isolation: the VS Code container may talk to its squid and its DNS and nothing
    else; squid may reach the internet but not private networks; the DNS
    filter may talk to the cluster's DNS.
    """
    if not build.spec["networkPolicy"]["enabled"]:
        return []

    egress_settings = build.spec["networkPolicy"]
    dns_ports = [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]
    proxy_port = [{"port": 3128, "protocol": "TCP"}]

    def policy(name: str, component: str, rules: dict) -> dict:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": build.meta(name, "policy"),
            "spec": {
                "podSelector": {"matchLabels": build.selector(component)},
                "policyTypes": ["Ingress", "Egress"],
                **rules,
            },
        }

    vscode = policy(
        build.names.vscode,
        "vscode",
        {
            "ingress": [{"ports": [{"port": 8000, "protocol": "TCP"}]}],
            "egress": [
                {"to": [build.pods_of("squid")], "ports": proxy_port},
                {"to": [build.pods_of("coredns")], "ports": dns_ports},
            ],
        },
    )

    internet = {"cidr": egress_settings["squidEgressCIDR"], "except": egress_settings["squidEgressExcept"]}
    cluster_dns = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}
    squid = policy(
        build.names.squid,
        "squid",
        {
            "ingress": [{"from": [build.pods_of("vscode")], "ports": proxy_port}],
            "egress": [
                {"to": [{"ipBlock": internet}]},
                {"to": [cluster_dns], "ports": dns_ports},
                *_pin_rules(build),
            ],
        },
    )

    coredns = policy(
        build.names.coredns,
        "coredns",
        {
            "ingress": [{"from": [build.pods_of("vscode")], "ports": dns_ports}],
            "egress": [{"ports": dns_ports}],
        },
    )

    return [vscode, squid, coredns]


def _pin_rules(build: _Build) -> list[dict]:
    """
    The squid egress rules the names pinned to an address on the network
    need: one per pin the operator takes, to that one address (a /32, or a
    /128), on the ports its allowlist entries use (an SSH entry's port, or
    80 and 443). A pin no entry uses gets none, as it allows nothing.
    """
    entries = allowlist.effective(build.spec["allowlist"], build.spec["languages"])
    rules = []

    for pin in build.spec["lan"]["pins"]:
        if pin["ignored"] or pin["problem"]:
            continue

        ports = allowlist.entry_ports(entries, pin["name"])
        if not ports:
            continue

        address = ipaddress.ip_address(pin["address"])
        rules.append(
            {
                "to": [{"ipBlock": {"cidr": f"{address}/{address.max_prefixlen}"}}],
                "ports": [{"port": port, "protocol": "TCP"} for port in ports],
            }
        )

    return rules


def content_hash(text: str) -> str:
    """
    A short fingerprint of a text, for pod template annotations: a change
    of the text rolls the pods.
    """
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def restarts(before: dict, after: dict, defaults: Optional[dict]) -> list[str]:
    """
    The components (``WORKLOADS``) whose pods a change of a profile from
    *before* to *after* restarts: those whose pod template it changes, by
    the allowlist's hash too, with which the proxy and the DNS server
    restart. What the operator finds in the cluster (the DNS server's
    address, the CA, the token) is the same stand-in for both: a change of
    the spec is all this looks at. Suspending or resuming restarts nothing;
    it stops or starts VS Code. Nothing is said without the settings the
    chart gives (its pinned images).
    """

    def templates(profile: dict) -> dict:
        spec = effective_spec(profile, defaults)
        ctx = {
            "dnsClusterIP": "10.0.0.10",
            "caCert": "",
            "allowlistHash": content_hash(spec["proxyAllowlist"]),
            "tokenHash": "",
            "existingClaims": None,
        }

        found = {}
        for obj in render(profile, defaults, ctx):
            if obj["kind"] in ("Deployment", "StatefulSet"):
                found[obj["metadata"]["labels"][LABEL_COMPONENT]] = obj["spec"]["template"]

        return found

    # A setting the chart should give and doesn't (an image) leaves nothing
    # to compare: the operator reports it on the profile.
    try:
        old = templates(before)
        new = templates(after)
    except ValueError:
        return []

    return [component for component in WORKLOADS if old.get(component) != new.get(component)]


def render(profile: dict, defaults: Optional[dict], ctx: dict) -> list[dict]:
    """
    Everything a profile owns, in the order to apply it.

    *ctx* carries what the operator found or computed: ``dnsClusterIP``
    (absent the first time, so Kubernetes allocates one), ``caCert``,
    ``allowlistHash`` and ``tokenHash`` (changes roll the pods), and
    ``existingClaims`` (the current StatefulSet's claim templates, which are
    immutable, when there is one).
    """
    build = _Build(
        profile=profile,
        spec=effective_spec(profile, defaults),
        defaults=defaults or {},
        ctx=ctx,
        names=Names(profile["metadata"]["name"]),
        namespace=profile["metadata"]["namespace"],
    )

    configs = _config_maps(build)
    services = [_squid_service(build), _dns_service(build), _vscode_service(build)]
    policies = _network_policies(build)
    workloads = [_squid_deployment(build), _coredns_deployment(build), _vscode_statefulset(build)]

    ingress = _ingress(build)
    exposure = [ingress] if ingress else []

    # Configuration, then services, then the policies that isolate the pods,
    # and only then the pods themselves. The Ingress, the one thing that can
    # be refused for reasons of its own, comes last.
    return configs + services + policies + workloads + exposure
