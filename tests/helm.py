#!/usr/bin/env python3
"""
The Helm chart: lint it, render it, and check the objects it renders.
"""

from __future__ import annotations

import json

import harness
from harness import ROOT, Suite

CHART = ROOT / "helm" / "hardcode"
BUNDLED_ALLOWLIST = CHART / "files" / "allowlist.txt"
BOOTSTRAP_ADMIN = ["--set", "admin.email=a@b.co", "--set", "admin.password=secretsecret"]

# The languages team-a is declared with.
TEAM_A_LANGUAGES = ["python", "go"]

# Every object the chart renders with a bootstrap admin, an Ingress for the
# admin, and two profiles. What a profile turns into (its ConfigMaps,
# Services, pods, ...) is the operator's to create, not the chart's:
# admin/tests and tests/e2e_kind.py check those.
EXPECTED_OBJECTS = [
    "CustomResourceDefinition/profiles.hardcode-app.github.io",
    "Deployment/hc-hardcode-admin",
    "Ingress/hc-hardcode-admin",
    "NetworkPolicy/hc-hardcode-sandbox-default-deny",
    "Profile/default",
    "Profile/team-a",
    "Role/hc-hardcode-admin",
    "RoleBinding/hc-hardcode-admin",
    "Secret/hc-hardcode-admin-bootstrap",
    "Service/hc-hardcode-admin",
    "ServiceAccount/hc-hardcode-admin",
]


def check_lint(suite: Suite) -> None:
    lint = harness.run(["helm", "lint", CHART, *BOOTSTRAP_ADMIN])
    suite.check("lint", "0 chart(s) failed" in lint.text, lint.text)

    # A bootstrap admin needs both halves: an email without a password is an
    # account nobody can sign in to.
    lint = harness.run(["helm", "lint", CHART, "--set", "admin.email=a@b.co"])
    suite.check("email without password is refused", "admin.password is required" in lint.text, lint.text)

    lint = harness.run(["helm", "lint", CHART])
    suite.check("lint without a bootstrap admin", "0 chart(s) failed" in lint.text, lint.text)


def render() -> harness.Output:
    """
    The chart rendered with a bit of everything switched on.
    """
    settings = [
        "admin.email=a@b.co",
        "admin.password=secretsecret",
        "admin.ingress.enabled=true",
        "profiles[0].name=default",
        "profiles[1].name=team-a",
        "profiles[1].vscodeHost=a.example.com",
        "profiles[1].suspended=true",
        f"profiles[1].languages={{{','.join(TEAM_A_LANGUAGES)}}}",
        "profiles[1].extensions={eamodio.gitlens}",
    ]
    allowlist = ROOT / "template" / "allowlist.txt"

    command = ["helm", "template", "hc", CHART, "--namespace", "sandbox", "--include-crds"]
    for setting in settings:
        command += ["--set", setting]
    command += ["--set-file", f"profiles[0].allowlist={allowlist}"]

    return harness.run(command)


def render_profiles(suite: Suite, *settings: str) -> list[dict]:
    """
    Just the Profile objects the chart renders with *settings* (``--set``
    values).
    """
    import yaml

    command = ["helm", "template", "hc", CHART, "--namespace", "sandbox", "--show-only", "templates/profiles.yaml"]
    for setting in settings:
        command += ["--set", setting]

    rendered = harness.run(command)
    suite.require(f"profiles render with {' '.join(settings)}", rendered.ok, rendered.text)

    return [document for document in yaml.safe_load_all(rendered.text) if document]


def check_profile_allowlists(suite: Suite, team_a: dict) -> None:
    """
    A chart profile carries its own allowlist as it is, whatever its
    languages: the entries HardCode provides to them are the operator's to
    add, so the chart has nothing to cut or add.
    """
    bundled = BUNDLED_ALLOWLIST.read_text(encoding="utf-8")

    suite.check(
        "a profile with languages of its own gets the bundled allowlist as it is",
        team_a["allowlist"] == bundled and team_a["languages"] == TEAM_A_LANGUAGES,
        team_a["allowlist"],
    )

    # The chart's default languages, for a profile without its own.
    profiles = render_profiles(suite, "profileDefaults.languages={rust,perl}", "profiles[0].name=inherits")
    spec = profiles[0]["spec"]
    suite.check(
        "the chart's default languages don't pin a profile's, nor change its allowlist",
        spec["allowlist"] == bundled and "languages" not in spec,
        spec["allowlist"],
    )

    spec = render_profiles(suite, "profiles[0].name=everything")[0]["spec"]
    suite.check(
        "without languages, the bundled allowlist as it is",
        spec["allowlist"] == bundled and "languages" not in spec and "extensions" not in spec,
        spec["allowlist"],
    )


def check_rendered_objects(suite: Suite, documents: list[dict]) -> None:
    def first(kind: str) -> dict:
        return next(document for document in documents if document["kind"] == kind)

    kinds = sorted(f"{document['kind']}/{document['metadata']['name']}" for document in documents)
    suite.check("the expected objects are rendered", kinds == EXPECTED_OBJECTS, "\n".join(kinds))

    profiles = {document["metadata"]["name"]: document for document in documents if document["kind"] == "Profile"}
    suite.check(
        "a profile carries the allowlist it was given, as it is",
        profiles["default"]["spec"]["allowlist"] == (ROOT / "template" / "allowlist.txt").read_text(encoding="utf-8"),
    )
    team_a = profiles["team-a"]["spec"]
    suite.check(
        "a profile carries its other settings",
        team_a.get("suspended") is True
        and team_a.get("vscodeHost") == "a.example.com"
        and team_a.get("extensions") == ["eamodio.gitlens"],
        json.dumps(team_a, indent=2),
    )
    check_profile_allowlists(suite, team_a)

    # The operator reads the bootstrap admin from the Secret, and the
    # defaults for every profile from one JSON value.
    container = first("Deployment")["spec"]["template"]["spec"]["containers"][0]
    environment = {variable["name"]: variable for variable in container["env"]}
    suite.check(
        "the admin's bootstrap account comes from the Secret",
        environment["ADMIN_EMAIL"]["valueFrom"]["secretKeyRef"]["name"] == "hc-hardcode-admin-bootstrap",
    )

    defaults = json.loads(environment["PROFILE_DEFAULTS_JSON"]["value"])
    suite.check(
        "profile defaults: network policies on, a privileged Docker daemon",
        defaults["networkPolicy"]["enabled"] is True and defaults["docker"]["privileged"] is True,
        json.dumps(defaults, indent=2),
    )
    suite.check(
        "profile defaults: memory limits on the VS Code container and the Docker daemon",
        defaults["resources"]["limits"]["memory"] == "8Gi" and defaults["dockerResources"]["limits"]["memory"] == "4Gi",
        json.dumps(defaults, indent=2),
    )
    suite.check(
        "profile defaults: no default languages, which means all of them",
        defaults["languages"] == [],
        json.dumps(defaults, indent=2),
    )

    rules = first("Role")["rules"]
    groups = {group for rule in rules for group in rule["apiGroups"]}
    suite.check(
        "the Role covers exactly the API groups the operator uses",
        groups == {"hardcode-app.github.io", "", "apps", "networking.k8s.io"},
        str(groups),
    )
    claims = next(rule for rule in rules if "persistentvolumeclaims" in rule["resources"])
    suite.check(
        "the operator may read and resize claims, nothing more",
        set(claims["verbs"]) == {"get", "list", "watch", "patch"},
        str(claims),
    )

    # The admin reads the proxies' logs (blocked traffic) and the VS Code
    # containers' (how far their extensions have got).
    pod_logs = [rule for rule in rules if "pods/log" in rule["resources"]]
    suite.check(
        "the admin may read pods' logs, nothing more",
        [rule["verbs"] for rule in pod_logs] == [["get"]],
        str(pod_logs),
    )

    crd = first("CustomResourceDefinition")
    suite.check("Profile has a status subresource", crd["spec"]["versions"][0]["subresources"] == {"status": {}})

    # Everything the operator creates is shut off by default; the profile's
    # own policies then open exactly what each component needs.
    deny = first("NetworkPolicy")["spec"]
    suite.check(
        "the namespace default-deny selects everything the operator manages",
        deny
        == {
            "podSelector": {"matchLabels": {"app.kubernetes.io/managed-by": "hardcode-operator"}},
            "policyTypes": ["Ingress", "Egress"],
        },
        json.dumps(deny, indent=2),
    )


def run_checks(suite: Suite) -> None:
    # PyYAML comes from the build image; imported here rather than at the top
    # because this file first runs on the workstation, to get into the image.
    import yaml

    suite.section("lint")
    check_lint(suite)

    suite.section("render")
    rendered = render()
    suite.require("template renders", rendered.ok, rendered.text)

    documents = [document for document in yaml.safe_load_all(rendered.text) if document]
    suite.note(f"{len(documents)} documents")
    check_rendered_objects(suite, documents)


if __name__ == "__main__":
    harness.main("helm", run_checks)
