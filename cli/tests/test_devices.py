"""
Tests for the devices of this computer a profile's VS Code container may
use (hardcode_cli.devices): the catalogue and its warnings, whether a
computer has each one, the Compose file written for them, how Compose is
run with it, the operations that switch them, and the commands.
"""

import io
import json
import unittest
from contextlib import redirect_stderr

from hardcode_cli import cli, devices, dockerfile, envfile, profiles, stack, yamlio
from hardcode_cli.compose import Compose, ComposeError
from hardcode_cli.devices import DeviceError
from hardcode_cli.ops import Ops

from .helpers import (
    GPU_NODES,
    KFD_NODES,
    KVM_NODES,
    NVIDIA_RUNTIMES,
    REPO,
    RUNTIMES,
    FakeRunner,
    HomeTestCase,
    fake_host,
    yaml_available,
)
from .test_cli import CliTestCase

# A Linux computer with every device: KVM, an AMD GPU with ROCm's node.
EVERY_NODE = {**KVM_NODES, **GPU_NODES, **KFD_NODES}


def docker_info(listed):
    """
    What ``docker info`` says of the runtimes: the names in *listed*, or,
    for ``None``, that it can't say.
    """

    def answer() -> devices.Runtimes:
        if listed is None:
            return devices.Runtimes(None, "Cannot connect to the Docker daemon")
        return devices.Runtimes(tuple(sorted(listed)))

    return answer


def never_asked() -> devices.Runtimes:
    """
    A ``docker info`` that must not run.
    """
    raise AssertionError("docker info was asked")


class CatalogueTests(unittest.TestCase):
    def test_three_devices_each_saying_what_it_gives_code_in_the_sandbox(self):
        self.assertEqual(devices.KEYS, ("kvm", "gpu", "nvidia"))

        warnings = {device.key: device.warning for device in devices.CATALOGUE}
        self.assertIn("virtualisation interface", warnings["kvm"])
        self.assertIn("Android emulator", warnings["kvm"])
        self.assertIn("a way out of the sandbox", warnings["kvm"])
        self.assertIn("graphics driver", warnings["gpu"])
        self.assertIn("a large part of your kernel", warnings["gpu"])
        self.assertIn("GPU memory may not be cleared between programs", warnings["gpu"])
        self.assertIn("runs as root on your computer", warnings["nvidia"])
        self.assertIn("CVE-2024-0132", warnings["nvidia"])
        self.assertIn("CVE-2025-23266", warnings["nvidia"])

    def test_names_are_read_in_any_case_and_written_in_the_catalogues_order(self):
        self.assertEqual(devices.parse(" NVIDIA, kvm kvm ,"), ["nvidia", "kvm"])
        self.assertEqual(devices.parse(""), [])
        self.assertEqual(devices.ordered(["nvidia", "tpu", "kvm"]), ["kvm", "nvidia"])
        self.assertEqual(devices.unknown(["kvm", "tpu"]), ["tpu"])
        self.assertEqual(devices.join(["nvidia", "kvm"]), "kvm,nvidia")

        self.assertEqual(devices.check(" KVM "), "kvm")
        with self.assertRaisesRegex(DeviceError, r"'tpu' is not a device: kvm, gpu, nvidia"):
            devices.check("tpu")

    def test_dockers_runtimes_are_read_from_docker_info(self):
        self.assertIn("nvidia", devices.parse_runtimes(json.dumps(NVIDIA_RUNTIMES)).names)
        self.assertEqual(devices.parse_runtimes(json.dumps(RUNTIMES)).names, ("io.containerd.runc.v2", "runc"))

        for printed in ("not json", "null", "[]"):
            self.assertIsNone(devices.parse_runtimes(printed).names, printed)


class AvailabilityTests(unittest.TestCase):
    """
    Whether a computer has each device: its device nodes on Linux, its
    Docker's runtimes for NVIDIA's, and never on macOS or Windows.
    """

    def test_on_linux_kvm_and_a_gpu_are_there_when_their_nodes_are(self):
        everything = fake_host(EVERY_NODE)
        self.assertEqual(devices.unavailable("kvm", everything, never_asked), "")
        self.assertEqual(devices.unavailable("gpu", everything, never_asked), "")

        nothing = fake_host()
        kvm = devices.unavailable("kvm", nothing, never_asked)
        self.assertIn("this computer has no /dev/kvm", kvm)
        self.assertIn("switch virtualisation (Intel VT-x or AMD-V) on", kvm)
        self.assertIn("this computer has no /dev/dri", devices.unavailable("gpu", nothing, never_asked))

    def test_nvidia_is_for_dockers_runtimes_to_say(self):
        host = fake_host()

        self.assertEqual(devices.unavailable("nvidia", host, docker_info(NVIDIA_RUNTIMES)), "")

        without = devices.unavailable("nvidia", host, docker_info(RUNTIMES))
        self.assertIn("Docker has no NVIDIA runtime: install NVIDIA's container toolkit", without)
        self.assertIn("nvidia-ctk runtime configure --runtime=docker", without)

        unknown = devices.unavailable("nvidia", host, docker_info(None))
        self.assertIn("Docker can't say whether it has NVIDIA's runtime (Cannot connect", unknown)
        self.assertIn("start Docker", unknown)

    def test_macos_and_windows_have_none_and_docker_is_not_asked(self):
        for platform, name in (("darwin", "macOS"), ("win32", "Windows"), ("freebsd14", "this system")):
            host = fake_host(EVERY_NODE, platform=platform)

            for key in devices.KEYS:
                reason = devices.unavailable(key, host, never_asked)
                self.assertIn("devices are given to containers on Linux only", reason, platform)
                self.assertIn(f"on {name}, Docker runs them in a virtual machine", reason, platform)

    def test_the_states_say_on_or_off_and_why_not(self):
        asked = []

        def info() -> devices.Runtimes:
            asked.append("info")
            return docker_info(RUNTIMES)()

        rows = devices.states(["gpu"], fake_host(KVM_NODES), info)

        self.assertEqual([row["key"] for row in rows], ["kvm", "gpu", "nvidia"])
        self.assertEqual([row["enabled"] for row in rows], [False, True, False])
        self.assertEqual([row["available"] for row in rows], [True, False, False])
        self.assertEqual(rows[0]["reason"], "")
        self.assertIn("/dev/dri", rows[1]["reason"])
        self.assertEqual(rows[2]["warning"], devices.BY_KEY["nvidia"].warning)
        self.assertEqual(asked, ["info"], "docker info is asked once, for NVIDIA's runtime")


class FragmentTests(unittest.TestCase):
    """
    The Compose file that gives the VS Code container the devices.
    """

    def setUp(self):
        if not yaml_available():
            self.skipTest("no YAML backend (yq, PyYAML or Docker)")

    def vscode(self, keys, nodes=EVERY_NODE):
        """
        What the file for *keys* sets on the VS Code container, read back
        as Compose reads it; it sets nothing on any other service.
        """
        document = yamlio.load(devices.fragment(keys, fake_host(nodes)))

        self.assertEqual(list(document["services"]), ["vscode"], "only the VS Code container gets devices")
        return document["services"]["vscode"]

    def test_kvm_is_its_node_and_its_group(self):
        self.assertEqual(self.vscode(["kvm"]), {"devices": ["/dev/kvm:/dev/kvm"], "group_add": ["108"]})

    def test_a_gpu_is_its_folder_and_the_groups_of_its_nodes(self):
        # The video group's card, the render group's node; the folder of
        # links beside them is no device.
        self.assertEqual(
            self.vscode(["gpu"], GPU_NODES),
            {"devices": ["/dev/dri:/dev/dri"], "group_add": ["44", "992"]},
        )

    def test_a_gpu_brings_rocms_node_when_the_computer_has_it(self):
        self.assertEqual(
            self.vscode(["gpu"]),
            {"devices": ["/dev/dri:/dev/dri", "/dev/kfd:/dev/kfd"], "group_add": ["44", "992"]},
        )

    def test_nvidia_is_a_request_for_every_gpu(self):
        request = {"driver": "nvidia", "count": "all", "capabilities": ["gpu"]}

        self.assertEqual(self.vscode(["nvidia"]), {"deploy": {"resources": {"reservations": {"devices": [request]}}}})

    def test_all_three_together(self):
        vscode = self.vscode(["nvidia", "gpu", "kvm"])

        self.assertEqual(vscode["devices"], ["/dev/kvm:/dev/kvm", "/dev/dri:/dev/dri", "/dev/kfd:/dev/kfd"])
        self.assertEqual(vscode["group_add"], ["44", "108", "992"])
        self.assertEqual(vscode["deploy"]["resources"]["reservations"]["devices"][0]["driver"], "nvidia")

    def test_roots_group_is_never_given(self):
        self.assertEqual(self.vscode(["kvm"], {"/dev/kvm": 0}), {"devices": ["/dev/kvm:/dev/kvm"]})

    def test_it_says_who_writes_it_and_what_it_does(self):
        text = devices.fragment(["kvm", "gpu"], fake_host(EVERY_NODE))

        self.assertTrue(text.startswith("# Written by HardCode from VSCODE_DEVICES"))
        self.assertIn("edits here are lost", text)
        self.assertIn("devices of this computer: kvm, gpu", text)
        self.assertIn("Each one weakens the sandbox", text)

    def test_none_is_no_file(self):
        self.assertEqual(devices.fragment([], fake_host(EVERY_NODE)), "")
        self.assertEqual(devices.fragment(["tpu"], fake_host(EVERY_NODE)), "")


class WriteTests(HomeTestCase):
    def test_the_file_is_written_kept_and_removed(self):
        path = self.root / devices.FRAGMENT
        host = fake_host(EVERY_NODE)

        self.assertTrue(devices.write(path, ["kvm"], host))
        self.assertIn("/dev/kvm:/dev/kvm", path.read_text(encoding="utf-8"))
        self.assertFalse(devices.write(path, ["kvm"], host), "the same: unchanged")

        self.assertTrue(devices.write(path, ["kvm"], fake_host({"/dev/kvm": 109})), "a group that moved")
        self.assertIn('"109"', path.read_text(encoding="utf-8"))

        self.assertTrue(devices.write(path, [], host))
        self.assertFalse(path.exists())
        self.assertFalse(devices.write(path, [], host), "none, and no file: unchanged")


class StackTests(unittest.TestCase):
    """
    The stack gives no devices: a profile without any runs exactly as before.
    """

    def setUp(self):
        if not yaml_available():
            self.skipTest("no YAML backend (yq, PyYAML or Docker)")

    def test_compose_yaml_gives_the_vscode_container_nothing(self):
        text = (REPO / "compose.yaml").read_text(encoding="utf-8")
        services = yamlio.load(text)["services"]

        for key in ("devices", "group_add", "deploy"):
            self.assertNotIn(key, services["vscode"], key)
        self.assertNotIn("group_add", services["docker"])
        self.assertNotIn("${VSCODE_DEVICES", text, "the CLI reads the setting, not Compose")

    def test_the_template_names_the_setting_and_says_what_it_does(self):
        env = (REPO / "template" / ".env").read_text(encoding="utf-8")

        self.assertIn(f"\n#{devices.ENV_KEY}=\n", env)
        self.assertIn("Each one weakens the sandbox", env)


class ComposeTests(HomeTestCase):
    def test_the_file_comes_after_the_stacks_and_before_the_override(self):
        profiles.create(self.root, "default", languages=[])
        profile = profiles.get(self.root, "default")
        project = Compose(profile, runner=self.runner, docker="docker")

        def files() -> list[str]:
            return [part for part in project.argv("up", "-d") if part.endswith(".yaml")]

        stack_dir = self.stack.as_posix()
        override = profile.override_file.as_posix()
        self.assertEqual(files(), [f"{stack_dir}/compose.yaml", override], "no devices, no file")

        devices.write(profile.devices_file, ["kvm"], fake_host(EVERY_NODE))
        self.assertEqual(files(), [f"{stack_dir}/compose.yaml", profile.devices_file.as_posix(), override])

        dockerfile.save(profile.image_dir, "FROM hardcode\n")
        self.assertEqual(
            files(),
            [
                f"{stack_dir}/compose.yaml",
                f"{stack_dir}/{stack.IMAGE_STEPS}",
                profile.devices_file.as_posix(),
                override,
            ],
        )


class OpsTests(HomeTestCase):
    """
    The operations that switch a device, with ``docker compose`` faked.
    """

    def setUp(self):
        super().setUp()
        self.warnings = []

    def ops(self, nodes=None, running=False, runtimes=NVIDIA_RUNTIMES, platform="linux"):
        """
        Operations on the default profile (created when missing), on a
        computer with *nodes* (every device's by default) and a Docker whose
        runtimes are *runtimes*.
        """
        if not profiles.exists(self.root, "default"):
            profiles.create(self.root, "default", languages=[])

        self.runner = FakeRunner(running=running)
        self.runner.runtimes = runtimes
        return Ops(
            self.root,
            profiles.get(self.root, "default"),
            runner=self.runner,
            docker="docker",
            sleep=lambda _: None,
            warn=self.warnings.append,
            host=fake_host(EVERY_NODE if nodes is None else nodes, platform),
        )

    def stored(self, ops):
        """
        What the profile's .env says: its VSCODE_DEVICES, or None.
        """
        return envfile.read(ops.profile.env_file).get(devices.ENV_KEY)

    def test_on_asks_then_writes_the_env_and_the_file(self):
        ops = self.ops()
        asked = []

        result = ops.set_device("kvm", True, lambda device: asked.append(device.key) or True)

        self.assertEqual(asked, ["kvm"])
        self.assertEqual((result.message, result.changed, result.applied), ("device kvm on", True, False))
        self.assertEqual(self.stored(ops), "kvm")
        self.assertIn("/dev/kvm:/dev/kvm", ops.profile.devices_file.read_text(encoding="utf-8"))
        self.assertTrue(self.runner.commands("config --quiet"), "Compose is asked whether it takes the file")
        self.assertIn(ops.profile.devices_file.as_posix(), self.runner.commands("config --quiet")[-1])

    def test_a_running_vscode_container_is_recreated_alone(self):
        ops = self.ops(running=True)

        result = ops.set_device("gpu", True, lambda _: True)

        self.assertTrue(result.applied)
        recreated = self.runner.commands(" up -d")
        self.assertEqual(len(recreated), 1)
        self.assertTrue(recreated[0].endswith(" up -d vscode"), "the Docker daemon gets no devices")

    def test_off_asks_nothing_and_the_last_one_takes_the_file_away(self):
        ops = self.ops()
        ops.set_device("kvm", True, lambda _: True)
        ops.set_device("nvidia", True, lambda _: True)
        self.assertEqual(self.stored(ops), "kvm,nvidia")

        def refuse(_):
            raise AssertionError("off asks nothing")

        self.assertEqual(ops.set_device("kvm", False, refuse).message, "device kvm off")
        self.assertEqual(self.stored(ops), "nvidia")
        self.assertNotIn("/dev/kvm", ops.profile.devices_file.read_text(encoding="utf-8"))

        ops.set_device("nvidia", False, refuse)
        self.assertIsNone(self.stored(ops), "none left is unset")
        self.assertFalse(ops.profile.devices_file.exists())

    def test_what_is_so_already_changes_nothing(self):
        ops = self.ops()
        self.assertFalse(ops.set_device("kvm", False, lambda _: True).changed)

        ops.set_device("kvm", True, lambda _: True)
        again = ops.set_device("KVM", True, lambda _: True)
        self.assertEqual((again.message, again.changed), ("device kvm is already on", False))

    def test_a_device_this_computer_lacks_is_refused_before_anything_is_asked(self):
        ops = self.ops(nodes={}, runtimes=RUNTIMES)

        for key, why in (("kvm", "no /dev/kvm"), ("gpu", "no /dev/dri"), ("nvidia", "no NVIDIA runtime")):
            refusal = f"device {key} can't be given to the VS Code container here: .*{why}"
            with self.assertRaisesRegex(DeviceError, refusal):
                ops.set_device(key, True, never_asked)

        self.assertIsNone(self.stored(ops))
        self.assertFalse(ops.profile.devices_file.exists())

    def test_macos_has_none(self):
        ops = self.ops(platform="darwin")

        with self.assertRaisesRegex(DeviceError, "Linux only: on macOS"):
            ops.set_device("kvm", True, lambda _: True)

        self.assertEqual([row["available"] for row in ops.device_states()], [False, False, False])
        self.assertEqual(self.runner.commands(" info "), [], "Docker isn't asked: no device is given there")

    def test_a_no_leaves_it_off(self):
        ops = self.ops()

        result = ops.set_device("kvm", True, lambda _: False)

        self.assertEqual((result.message, result.changed), ("device kvm stays off", False))
        self.assertIsNone(self.stored(ops))
        self.assertFalse(ops.profile.devices_file.exists())

    def test_a_change_compose_rejects_is_undone(self):
        ops = self.ops()
        ops.set_device("kvm", True, lambda _: True)
        before = ops.profile.devices_file.read_text(encoding="utf-8")

        self.runner.fail_on.add("config --quiet")
        with self.assertRaisesRegex(DeviceError, "compose rejected the change; it was undone: simulated"):
            ops.set_device("gpu", True, lambda _: True)

        self.assertEqual(self.stored(ops), "kvm")
        self.assertEqual(ops.profile.devices_file.read_text(encoding="utf-8"), before)

    def test_a_change_the_container_cannot_take_is_undone(self):
        ops = self.ops(running=True)

        self.runner.fail_on.add("up -d vscode")
        with self.assertRaisesRegex(ComposeError, "the change was undone"):
            ops.set_device("kvm", True, lambda _: True)

        self.assertIsNone(self.stored(ops))
        self.assertFalse(ops.profile.devices_file.exists(), "a file that wasn't there goes again")
        self.assertEqual(len(self.runner.commands("up -d vscode")), 2, "recreated from the files as they were")
        self.assertIn("couldn't be started again as before", self.warnings[0])

    def test_the_state_lists_every_device(self):
        ops = self.ops(nodes=KVM_NODES, runtimes=None)
        ops.set_device("kvm", True, lambda _: True)

        rows = ops.state()["devices"]

        self.assertEqual([(row["key"], row["enabled"], row["available"]) for row in rows][0], ("kvm", True, True))
        self.assertFalse(rows[1]["available"])
        self.assertIn("Docker can't say whether it has NVIDIA's runtime (Cannot connect", rows[2]["reason"])

    def test_a_start_refuses_a_device_this_computer_lacks(self):
        ops = self.ops()
        ops.set_device("kvm", True, lambda _: True)

        lacking = self.ops(nodes={})
        with self.assertRaisesRegex(
            DeviceError,
            r"profile 'default' has device kvm on, but this computer has no /dev/kvm: .*; "
            r"or switch it off: hardcode -p default device off kvm",
        ):
            lacking.check_devices()

    def test_a_start_refuses_what_isnt_a_device(self):
        ops = self.ops()
        envfile.set_value(ops.profile.env_file, devices.ENV_KEY, "kvm,tpu")

        with self.assertRaisesRegex(DeviceError, r"VSCODE_DEVICES in profile 'default''s \.env names tpu"):
            ops.check_devices()

        self.assertEqual(ops.devices_on(), ["kvm"], "what isn't a device gives nothing")

    def test_a_start_writes_the_groups_as_they_are_now(self):
        ops = self.ops()
        ops.check_devices()
        self.assertFalse(ops.profile.devices_file.exists())
        self.assertEqual(self.runner.commands(" info "), [], "a profile without devices asks Docker nothing")

        ops.set_device("kvm", True, lambda _: True)
        self.assertIn('"108"', ops.profile.devices_file.read_text(encoding="utf-8"))

        moved = self.ops(nodes={"/dev/kvm": 109})
        moved.check_devices()
        self.assertIn('"109"', moved.profile.devices_file.read_text(encoding="utf-8"))

        # Switched off by hand in the .env: the next start takes the file away.
        envfile.unset(moved.profile.env_file, devices.ENV_KEY)
        moved.check_devices()
        self.assertFalse(moved.profile.devices_file.exists())

    def test_a_copy_gets_none_of_its_sources_devices(self):
        ops = self.ops()
        ops.set_device("kvm", True, lambda _: True)

        profiles.create(self.root, "copy", source="default")
        copy = profiles.get(self.root, "copy")

        self.assertNotIn(devices.ENV_KEY, envfile.read(copy.env_file))
        self.assertFalse(copy.devices_file.exists())


class CliTests(CliTestCase):
    """
    ``hardcode devices`` and ``hardcode device on|off``, end to end with
    docker compose faked, on a Linux computer with every device and a
    Docker without NVIDIA's runtime.
    """

    QUESTION = "Turn kvm on for profile 'default'? [y/N] "

    def setUp(self):
        super().setUp()
        self.host = fake_host(EVERY_NODE)

    def stored(self):
        """
        The default profile's VS Code container's devices, as its .env has them; None for none.
        """
        return envfile.read(profiles.get(self.root, "default").env_file).get(devices.ENV_KEY)

    def test_devices_lists_each_one_and_whether_it_is_here(self):
        code, out, _ = self.run_cli("devices")

        self.assertEqual(code, 0)
        self.assertIn("off  kvm     /dev/kvm: virtual machines, such as the Android emulator\n", out)
        self.assertIn("\n" + " " * 13 + "available here\n", out)
        self.assertIn("\n" + " " * 13 + "not available here: Docker has no NVIDIA runtime", out)
        self.assertNotIn("weakening", out)

        self.run_cli("device", "on", "kvm", "--yes")
        _, out, _ = self.run_cli("devices")
        self.assertIn("on   kvm", out)
        self.assertIn("on, each weakening the sandbox: kvm", out)

    def test_devices_as_json(self):
        _, out, _ = self.run_cli("--json", "devices")
        rows = json.loads(out)["devices"]

        self.assertEqual([row["key"] for row in rows], ["kvm", "gpu", "nvidia"])
        self.assertEqual([row["available"] for row in rows], [True, True, False])
        self.assertEqual({row["key"]: row["warning"] for row in rows}["gpu"], devices.BY_KEY["gpu"].warning)
        self.assertEqual(
            sorted(rows[0]),
            ["available", "enabled", "key", "name", "reason", "summary", "warning"],
        )

    def test_on_asks_on_a_terminal_after_the_warning(self):
        code, out, err = self.run_cli("device", "on", "kvm", answers=["y"])

        self.assertEqual((code, self.asked), (0, [self.QUESTION]))
        self.assertIn("Turning kvm on weakens profile 'default''s sandbox. " + devices.BY_KEY["kvm"].warning, err)
        self.assertEqual(out, "device kvm on (profile 'default' isn't running; applies when it starts)")
        self.assertEqual(self.stored(), "kvm")

    def test_a_no_on_a_terminal_leaves_it_off(self):
        code, out, _ = self.run_cli("device", "on", "kvm", answers=["n"])

        self.assertEqual((code, out), (1, "device kvm stays off"))
        self.assertIsNone(self.stored())

    def test_yes_asks_nothing(self):
        code, _, _ = self.run_cli("device", "on", "gpu", "--yes", answers=[])

        self.assertEqual((code, self.asked), (0, []))
        self.assertEqual(self.stored(), "gpu")

    def test_without_a_terminal_on_needs_yes(self):
        for argv in (["device", "on", "kvm"], ["--json", "device", "on", "kvm"]):
            code, out, err = self.run_cli(*argv)

            self.assertEqual((code, out), (1, ""), argv)
            self.assertIn("device on kvm weakens the sandbox, and there is no terminal to ask on", err)
            self.assertIn("hardcode -p default device on kvm --yes", err)
            self.assertIsNone(self.stored())

        code, out, _ = self.run_cli("--json", "device", "on", "kvm", "--yes")
        result = json.loads(out)["results"][0]
        self.assertEqual((code, result["message"], result["applied"]), (0, "device kvm on", False))

    def test_off_asks_nothing(self):
        self.run_cli("device", "on", "kvm", "--yes")

        code, out, _ = self.run_cli("device", "off", "kvm")

        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("device kvm off"))
        self.assertIsNone(self.stored())

    def test_a_running_vscode_container_is_recreated_with_it(self):
        code, out, _ = self.run_cli("device", "on", "kvm", "--yes", running=True)

        self.assertEqual((code, out), (0, "device kvm on (applied)"))
        self.assertEqual(len(self.runner.commands(" up -d vscode")), 1)

    def test_a_device_this_computer_lacks_is_refused_without_asking(self):
        code, _, err = self.run_cli("device", "on", "nvidia", answers=[])

        self.assertEqual((code, self.asked), (1, []))
        self.assertIn("device nvidia can't be given to the VS Code container here: Docker has no NVIDIA runtime", err)

    def test_show_says_which_are_on(self):
        self.assertIn("devices:   none", self.run_cli("show")[1])

        self.run_cli("device", "on", "kvm", "--yes")
        self.run_cli("device", "on", "gpu", "--yes")

        self.assertIn("devices:   kvm, gpu (each weakens the sandbox)", self.run_cli("show")[1])
        rows = json.loads(self.run_cli("--json", "show")[1])["devices"]
        self.assertEqual([row["enabled"] for row in rows], [True, True, False])

    def test_one_profile_at_a_time(self):
        code, _, err = self.run_cli("--all", "device", "on", "kvm", "--yes")

        self.assertEqual(code, 2)
        self.assertIn("--all isn't supported for 'device'", err)

    def test_only_the_three_are_devices(self):
        with redirect_stderr(io.StringIO()) as complaint, self.assertRaises(SystemExit):
            self.run_cli("device", "on", "tpu", "--yes")

        self.assertIn("invalid choice: 'tpu'", complaint.getvalue())

    def test_a_start_refuses_a_device_this_computer_lacks(self):
        self.run_cli("device", "on", "kvm", "--yes")
        self.host = fake_host(GPU_NODES)

        code, _, err = self.run_cli("up")

        self.assertEqual(code, 1)
        self.assertIn("profile 'default' has device kvm on, but this computer has no /dev/kvm", err)
        self.assertEqual(self.runner.commands(" up -d"), [], "nothing started")

    def test_a_start_gives_compose_the_file_after_the_stacks(self):
        self.run_cli("device", "on", "kvm", "--yes")

        code, _, _ = self.run_cli("up")

        profile = profiles.get(self.root, "default")
        started = next(call for call in self.runner.calls if call[-2:] == ["up", "-d"])
        files = [part for part in started if part.endswith(".yaml")]
        self.assertEqual(code, 0)
        self.assertEqual(
            files,
            [
                f"{self.stack.as_posix()}/compose.yaml",
                profile.devices_file.as_posix(),
                profile.override_file.as_posix(),
            ],
        )

    def test_the_usage_and_help_name_them(self):
        self.assertIn("devices ", cli.USAGE)
        self.assertIn("device on|off <name> [--yes]", cli.USAGE)

        parser = cli.build_parser()
        self.assertIn("weakens the sandbox", parser.format_help())


if __name__ == "__main__":
    unittest.main()
