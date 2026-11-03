"""
Unit tests for hardcode_runc.py, the Docker sidecar's runc wrapper.

They need Linux 5.6 or newer (openat2), and nothing else: the images are
folders made here, and the certificates are PEM blocks of made-up bytes,
which is all the wrapper reads of them. tests/docker.py runs these, then
the wrapper with the real runc on real images.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

import hardcode_runc as wrapper


def pem(name: str) -> bytes:
    """
    A PEM block holding *name*'s bytes: a "certificate" as the wrapper sees one.
    """
    body = base64.b64encode(name.encode() * 20).decode()
    lines = textwrap.wrap(body, 64)
    return ("-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n").encode()


PROXY = pem("proxy")
COMPANY = pem("company")
PUBLIC = pem("public-1") + pem("public-2")


def authority() -> wrapper.Authority:
    """
    The proxy's CA, as the wrapper reads it.
    """
    return wrapper.Authority(PROXY, frozenset(wrapper.fingerprints(PROXY)))


class ParseTests(unittest.TestCase):
    """
    runc's command lines, as containerd's shim and BuildKit write them.
    """

    def test_containerd_create(self):
        command = wrapper.parse(
            [
                "--root",
                "/run/runc/moby",
                "--log",
                "/run/log.json",
                "--log-format",
                "json",
                "--rootless=true",
                "create",
                "--bundle",
                "/run/bundle",
                "--pid-file",
                "/run/bundle/init.pid",
                "abc",
            ]
        )

        self.assertEqual(command.name, "create")
        self.assertEqual(
            command.global_options,
            ["--root", "/run/runc/moby", "--log", "/run/log.json", "--log-format", "json", "--rootless=true"],
        )
        self.assertEqual(command.options["bundle"], "/run/bundle")
        self.assertEqual(command.positional, ["abc"])

    def test_buildkit_run(self):
        command = wrapper.parse(["--log", "/x/runc-log.json", "--log-format", "json", "run", "--bundle", "/b", "step"])

        self.assertEqual((command.name, command.options, command.positional), ("run", {"bundle": "/b"}, ["step"]))

    def test_short_and_equals_forms(self):
        self.assertEqual(wrapper.parse(["create", "-b", "/b", "id"]).options, {"bundle": "/b"})
        self.assertEqual(wrapper.parse(["create", "--bundle=/b", "id"]).options, {"bundle": "/b"})
        self.assertEqual(wrapper.parse(["run", "-d", "--bundle", "/b", "id"]).options, {"detach": "", "bundle": "/b"})

    def test_exec_keeps_the_command(self):
        command = wrapper.parse(["exec", "--process", "/p.json", "--detach", "id"])
        self.assertEqual((command.options, command.positional), ({"process": "/p.json", "detach": ""}, ["id"]))

        command = wrapper.parse(["exec", "-t", "id", "sh", "-c", "true"])
        self.assertEqual(command.positional, ["id", "sh", "-c", "true"])

    def test_other_commands_only_by_name(self):
        command = wrapper.parse(["--root", "/r", "delete", "--force", "id"])
        self.assertEqual((command.name, command.options), ("delete", {}))

    def test_unknown_options_and_no_command_pass_through(self):
        self.assertIsNone(wrapper.parse(["--version"]))
        self.assertIsNone(wrapper.parse([]))
        self.assertIsNone(wrapper.parse(["create", "--something-new", "id"]))


class CertificateTests(unittest.TestCase):
    """
    Adding the proxy's CA to a bundle and taking it out again.
    """

    def test_merged_adds_it_once(self):
        once = wrapper.merged(PUBLIC, authority())

        self.assertTrue(once.startswith(PUBLIC))
        self.assertTrue(wrapper.has_authority(once, authority()))
        self.assertEqual(wrapper.merged(once, authority()), once)

    def test_a_bundle_without_a_last_newline(self):
        bundle = PUBLIC.rstrip(b"\n")
        added = wrapper.merged(bundle, authority())

        self.assertEqual(wrapper.certificates(added), wrapper.certificates(PUBLIC + PROXY))

    def test_strip_takes_out_the_block_and_keeps_the_rest(self):
        rewritten = wrapper.merged(PUBLIC, authority()) + COMPANY

        self.assertEqual(wrapper.strip(rewritten, authority()), PUBLIC + COMPANY)

    def test_strip_finds_the_certificate_without_its_markers(self):
        self.assertEqual(wrapper.strip(PUBLIC + PROXY + COMPANY, authority()), PUBLIC + COMPANY)

    def test_invalid_base64_is_not_a_certificate(self):
        self.assertEqual(wrapper.certificates(b"-----BEGIN CERTIFICATE-----\n!!!\n-----END CERTIFICATE-----\n"), [])

    def test_read_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ca.pem"
            self.assertIsNone(wrapper.read_authority(str(path)), "no file")

            path.write_bytes(b"nothing here\n")
            self.assertIsNone(wrapper.read_authority(str(path)), "no certificate")

            path.write_bytes(PROXY.rstrip(b"\n"))
            self.assertEqual(wrapper.read_authority(str(path)).pem, PROXY)


class JavaOptionTests(unittest.TestCase):
    """
    The proxy settings Java is given when they are switched on.
    """

    def test_no_proxy_as_non_proxy_hosts(self):
        hosts = wrapper.java_non_proxy_hosts("localhost, 127.0.0.1,::1,docker,.example.com,10.0.0.0/8,")
        self.assertEqual(hosts, "localhost|127.0.0.1|[::1]|docker|*.example.com")

    def test_options_from_the_container_proxy(self):
        env = ["HTTPS_PROXY=http://squid:3128", "NO_PROXY=localhost,docker"]
        self.assertEqual(
            wrapper.java_options(env),
            "-Dhttps.proxyHost=squid -Dhttps.proxyPort=3128 -Dhttp.proxyHost=squid -Dhttp.proxyPort=3128 "
            "-Dhttp.nonProxyHosts=localhost|docker",
        )

    def test_lower_case_and_no_scheme(self):
        self.assertIn("-Dhttps.proxyHost=proxy", wrapper.java_options(["https_proxy=proxy:8080"]))

    def test_no_proxy_no_options(self):
        self.assertIsNone(wrapper.java_options(["PATH=/bin"]))


class ImageTests(unittest.TestCase):
    """
    The wrapper against a made-up image: a root filesystem in a temporary
    folder, with a bundle folder beside it as containerd and BuildKit lay
    them out.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

        self.bundle = Path(self.temporary.name) / "bundle"
        self.rootfs = self.bundle / "rootfs"
        self.bundle_file = self.rootfs / "etc/ssl/certs/ca-certificates.crt"
        self.bundle_file.parent.mkdir(parents=True)
        self.bundle_file.write_bytes(PUBLIC)
        self.bundle_file.chmod(0o644)
        os.utime(self.bundle_file, ns=(1_000_000_000, 2_000_000_000))

        # A file outside the image that a symlink in it names.
        self.secret = Path(self.temporary.name) / "secret.pem"
        self.secret.write_bytes(pem("secret"))

        self.own = Path(self.temporary.name) / "own-bundle.crt"
        self.own.write_bytes(pem("sidecar-public"))

        self.stores_made = []

    def spec(self, env=(), mounts=(), readonly=False) -> dict:
        """
        A config.json as runc gets it.
        """
        return {
            "process": {"env": ["PATH=/usr/bin:/bin", *env]},
            "root": {"path": "rootfs", "readonly": readonly},
            "mounts": [{"destination": "/proc", "type": "proc"}, *mounts],
        }

    def trust_store(self, certificates_pem: bytes, folder: str) -> str:
        """
        A stand-in for make_trust_store: records what it was given.
        """
        self.stores_made.append(certificates_pem)
        path = os.path.join(folder, "java-cacerts.p12")
        Path(path).write_bytes(b"store:" + certificates_pem)
        return path

    def prepare(self, spec, in_place=False, environment=None, the_authority="proxy"):
        """
        Run prepare() on this image.
        """
        changes = wrapper.Changes()
        wrapper.prepare(
            spec,
            str(self.bundle),
            in_place,
            changes,
            authority() if the_authority == "proxy" else None,
            environment or {},
            self.trust_store,
            str(self.own),
        )
        return changes

    def added_mounts(self, spec) -> dict:
        """
        The mounts prepare() added, destination to source.
        """
        return {mount["destination"]: mount["source"] for mount in spec["mounts"] if mount.get("type") == "bind"}

    def test_a_container_gets_a_copy_mounted_and_the_image_is_untouched(self):
        spec = self.spec()
        changes = self.prepare(spec)
        wrapper.finish(changes)

        mounts = self.added_mounts(spec)
        self.assertEqual(list(mounts), ["/etc/ssl/certs/ca-certificates.crt"])

        copy = Path(mounts["/etc/ssl/certs/ca-certificates.crt"])
        self.assertEqual(copy.parent, self.bundle / wrapper.COPIES, "kept in the bundle folder")
        self.assertEqual(copy.read_bytes(), wrapper.merged(PUBLIC, authority()))
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o644)

        self.assertEqual(self.bundle_file.read_bytes(), PUBLIC)
        self.assertEqual(self.bundle_file.stat().st_mtime_ns, 2_000_000_000)

        self.assertIn("SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt", spec["process"]["env"])
        self.assertIn("NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt", spec["process"]["env"])
        recorded = json.loads((self.bundle / wrapper.COPIES / wrapper.ADDED_ENV).read_text(encoding="utf-8"))
        self.assertEqual(recorded, changes.env)

    def test_the_containers_own_variables_stay(self):
        spec = self.spec(env=["SSL_CERT_FILE=/mine.pem"])
        self.prepare(spec)

        env = spec["process"]["env"]
        self.assertIn("SSL_CERT_FILE=/mine.pem", env)
        self.assertEqual(len([entry for entry in env if entry.startswith("SSL_CERT_FILE=")]), 1)

    def test_symlinked_paths_are_one_file(self):
        (self.rootfs / "etc/ssl/cert.pem").symlink_to("certs/ca-certificates.crt")
        (self.rootfs / "etc/pki/tls/certs").mkdir(parents=True)
        (self.rootfs / "etc/pki/tls/certs/ca-bundle.crt").symlink_to("/etc/ssl/certs/ca-certificates.crt")

        spec = self.spec()
        self.prepare(spec)

        self.assertEqual(list(self.added_mounts(spec)), ["/etc/ssl/certs/ca-certificates.crt"])

    def test_the_mount_goes_where_the_symlink_leads(self):
        target = self.rootfs / "etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"
        target.parent.mkdir(parents=True)
        self.bundle_file.rename(target)
        (self.rootfs / "etc/pki/tls/certs").mkdir(parents=True)
        (self.rootfs / "etc/pki/tls/certs/ca-bundle.crt").symlink_to("../../ca-trust/extracted/pem/tls-ca-bundle.pem")

        spec = self.spec()
        self.prepare(spec)

        self.assertEqual(list(self.added_mounts(spec)), ["/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"])

    def test_nothing_outside_the_image_is_read(self):
        self.bundle_file.unlink()
        self.bundle_file.symlink_to(self.secret)
        relative = os.path.relpath(self.secret, self.bundle_file.parent)
        (self.rootfs / "etc/ssl/cert.pem").symlink_to(relative)

        spec = self.spec()
        self.prepare(spec)

        self.assertEqual(self.added_mounts(spec), {})
        self.assertFalse(any("secret" in entry for entry in spec["process"]["env"]))
        for copy in (self.bundle / wrapper.COPIES).glob("*.pem"):
            self.assertNotIn(pem("secret"), copy.read_bytes())

    def test_a_symlink_inside_the_image_is_followed_inside_it(self):
        self.bundle_file.unlink()
        self.bundle_file.symlink_to(self.secret)
        inside = self.rootfs / self.secret.relative_to("/")
        inside.parent.mkdir(parents=True)
        inside.write_bytes(PUBLIC)

        spec = self.spec()
        self.prepare(spec)

        self.assertEqual(list(self.added_mounts(spec)), [str(self.secret)])

    def test_fifos_and_large_files_are_left_alone(self):
        self.bundle_file.unlink()
        os.mkfifo(self.bundle_file)
        large = self.rootfs / "etc/ssl/cert.pem"
        with open(large, "wb") as file:
            file.truncate(wrapper.MAX_BYTES + 1)

        spec = self.spec()
        self.prepare(spec)

        self.assertEqual(self.added_mounts(spec), {})

    def test_a_mount_of_the_containers_own_covers_the_path(self):
        for destination in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/certs", "/etc/ssl/certs/", "/etc"):
            spec = self.spec(mounts=[{"destination": destination, "type": "bind", "source": "/x"}])
            self.prepare(spec)

            self.assertEqual(list(self.added_mounts(spec)), [destination], destination)
            self.assertFalse(any(entry.startswith("SSL_CERT_FILE=") for entry in spec["process"]["env"]))

    def test_opting_out_and_no_authority_leave_it_alone(self):
        for env, the_authority in ((["HARDCODE_PROXY_CA=off"], "proxy"), ([], None)):
            spec = self.spec(env=env)
            self.prepare(spec, the_authority=the_authority)

            self.assertEqual(self.added_mounts(spec), {})
            self.assertFalse((self.bundle / wrapper.COPIES).exists())

    def test_a_step_gets_the_ca_in_place_and_it_goes_again(self):
        spec = self.spec()
        changes = self.prepare(spec, in_place=True)

        self.assertEqual(self.added_mounts(spec), {}, "nothing mounted for a step")
        self.assertTrue(wrapper.has_authority(self.bundle_file.read_bytes(), authority()))
        self.assertIn("SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt", spec["process"]["env"])

        self.assertEqual(wrapper.finish(changes), [])
        self.assertEqual(self.bundle_file.read_bytes(), PUBLIC)
        self.assertEqual(self.bundle_file.stat().st_mtime_ns, 2_000_000_000, "its time is put back too")

    def test_what_a_step_adds_stays(self):
        changes = self.prepare(self.spec(), in_place=True)
        with open(self.bundle_file, "ab") as file:
            file.write(COMPANY)

        self.assertEqual(wrapper.finish(changes), [])
        self.assertEqual(self.bundle_file.read_bytes(), PUBLIC + COMPANY)

    def test_a_bundle_the_step_made_again_is_left_as_it_is(self):
        changes = self.prepare(self.spec(), in_place=True)

        # update-ca-certificates writes a new file and renames it over the old one.
        replacement = self.bundle_file.with_name("new")
        replacement.write_bytes(PUBLIC + COMPANY)
        replacement.rename(self.bundle_file)

        self.assertEqual(wrapper.finish(changes), [])
        self.assertEqual(self.bundle_file.read_bytes(), PUBLIC + COMPANY)

    def test_a_bundle_grown_too_large_to_check_fails_the_step(self):
        changes = self.prepare(self.spec(), in_place=True)
        with open(self.bundle_file, "ab") as file:
            file.truncate(wrapper.MAX_BYTES + 1)

        problems = wrapper.finish(changes)
        self.assertEqual(len(problems), 1)
        self.assertIn("grew past", problems[0])

    def test_a_step_that_swaps_in_a_symlink_doesnt_lead_outside(self):
        changes = self.prepare(self.spec(), in_place=True)
        self.bundle_file.unlink()
        self.bundle_file.symlink_to(self.secret)
        self.secret.write_bytes(wrapper.merged(PUBLIC, authority()))

        self.assertEqual(wrapper.finish(changes), [])
        self.assertEqual(self.secret.read_bytes(), wrapper.merged(PUBLIC, authority()), "outside: untouched")

    def test_a_read_only_root_gets_mounts_even_for_a_step(self):
        spec = self.spec(readonly=True)
        changes = self.prepare(spec, in_place=True)

        self.assertEqual(list(self.added_mounts(spec)), ["/etc/ssl/certs/ca-certificates.crt"])
        self.assertEqual(changes.appended, [])
        self.assertEqual(self.bundle_file.read_bytes(), PUBLIC)

    def make_jdks(self) -> None:
        """
        Two JDKs: Temurin's layout (its own cacerts), and Debian's (a
        symlink to the system's Java store).
        """
        temurin = self.rootfs / "opt/java/openjdk/lib/security/cacerts"
        temurin.parent.mkdir(parents=True)
        temurin.write_bytes(b"temurin store")

        system = self.rootfs / "etc/ssl/certs/java/cacerts"
        system.parent.mkdir(parents=True)
        system.write_bytes(b"debian store")
        debian = self.rootfs / "usr/lib/jvm/java-21-openjdk-amd64/lib/security/cacerts"
        debian.parent.mkdir(parents=True)
        debian.symlink_to("/etc/ssl/certs/java/cacerts")

    def test_java_trust_stores_are_covered(self):
        self.make_jdks()

        spec = self.spec()
        self.prepare(spec)

        mounts = self.added_mounts(spec)
        self.assertEqual(
            sorted(mounts),
            [
                "/etc/ssl/certs/ca-certificates.crt",
                "/etc/ssl/certs/java/cacerts",
                "/opt/java/openjdk/lib/security/cacerts",
            ],
        )
        self.assertEqual(self.stores_made, [wrapper.merged(PUBLIC, authority())], "made once, from the merged bundle")
        self.assertEqual(mounts["/opt/java/openjdk/lib/security/cacerts"], mounts["/etc/ssl/certs/java/cacerts"])

    def test_without_a_bundle_the_store_is_made_from_the_sidecars(self):
        self.bundle_file.unlink()
        self.make_jdks()

        self.prepare(self.spec())

        self.assertEqual(self.stores_made, [wrapper.merged(pem("sidecar-public"), authority())])

    def test_a_step_mustnt_change_the_trust_store(self):
        self.make_jdks()

        changes = self.prepare(self.spec(), in_place=True)
        self.assertEqual(wrapper.finish(changes), [])

        changes = self.prepare(self.spec(), in_place=True)
        Path(changes.store[0]).write_bytes(b"keytool -importcert was here")
        problems = wrapper.finish(changes)
        self.assertEqual(len(problems), 1)
        self.assertIn("HARDCODE_PROXY_CA=off", problems[0])

    def test_the_java_proxy_only_when_switched_on(self):
        proxied = ["HTTPS_PROXY=http://squid:3128"]

        spec = self.spec(env=proxied)
        self.prepare(spec)
        self.assertIsNone(wrapper.env_value(spec["process"]["env"], "JAVA_TOOL_OPTIONS"), "off by default")

        spec = self.spec(env=proxied)
        self.prepare(spec, environment={"HARDCODE_JAVA_PROXY": "true"})
        self.assertIn("-Dhttps.proxyHost=squid", wrapper.env_value(spec["process"]["env"], "JAVA_TOOL_OPTIONS"))

        spec = self.spec(env=[*proxied, "JAVA_TOOL_OPTIONS=-Xmx1g"])
        self.prepare(spec, environment={"HARDCODE_JAVA_PROXY": "true"})
        self.assertEqual(
            wrapper.env_value(spec["process"]["env"], "JAVA_TOOL_OPTIONS"), "-Xmx1g", "the container's own"
        )


class ExecTests(unittest.TestCase):
    """
    ``docker exec`` gets the variables its container was given.
    """

    def test_missing_variables_are_added(self):
        process = {"env": ["PATH=/bin", "SSL_CERT_FILE=/mine.pem"]}
        added = [
            "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt",
            "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt",
        ]

        self.assertTrue(wrapper.add_exec_env(process, added))
        self.assertEqual(
            process["env"],
            ["PATH=/bin", "SSL_CERT_FILE=/mine.pem", "NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt"],
        )
        self.assertFalse(wrapper.add_exec_env(process, added), "nothing more the second time")

    def test_an_exec_can_opt_out(self):
        process = {"env": ["HARDCODE_PROXY_CA=off"]}
        self.assertFalse(wrapper.add_exec_env(process, ["SSL_CERT_FILE=/x"]))


if __name__ == "__main__":
    unittest.main()
