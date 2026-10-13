"""
YAML in and out through whatever the machine has, so nothing needs installing.

In order of preference:

1. mikefarah's ``yq``, if it is on the PATH. It is the one YAML tool Docker
   users tend to have. (The Python ``yq`` wrapper is a different program with
   the same name, and is skipped.)
2. PyYAML, if the ``yaml`` module imports.
3. yq's container image through Docker, which HardCode needs anyway.

Only parsing and emitting go through the backend: a document is always
re-emitted from the parsed structure, so the result doesn't depend on which
backend ran.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Optional

YQ_IMAGE = "mikefarah/yq:4"

# Like every container HardCode starts, the yq one gets a hard memory cap.
YQ_DOCKER_ARGS = ("run", "--rm", "-i", "--memory", "256m")


class YamlError(RuntimeError):
    """
    No YAML backend is available, or the one in use failed.
    """


@dataclass(frozen=True)
class Backend:
    """
    One way of turning YAML into Python values and back.
    """

    name: str
    load: Callable[[str], Any]
    dump: Callable[[Any], str]


def _run_yq(command: list[str], stdin: str, run: Callable = subprocess.run) -> str:
    """
    Run a yq command line over *stdin* and return what it printed.
    """
    try:
        result = run(command, input=stdin, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise YamlError(f"yq failed: {error}") from error

    if result.returncode != 0:
        raise YamlError(f"yq failed: {(result.stderr or result.stdout).strip()}")

    return result.stdout


def yq_backend(command: list[str], run: Callable = subprocess.run, name: str = "yq") -> Backend:
    """
    A backend over a yq command line: the binary, or ``docker run`` of its image.

    yq converts between YAML and JSON, and Python's own json module does the
    rest.
    """

    def load(text: str) -> Any:
        to_json = [*command, "-p", "yaml", "-o", "json", "-I", "0", "."]
        output = _run_yq(to_json, text, run).strip()
        return json.loads(output) if output else None

    def dump(document: Any) -> str:
        to_yaml = [*command, "-p", "json", "-o", "yaml", "."]
        return _run_yq(to_yaml, json.dumps(document), run)

    return Backend(name, load, dump)


def pyyaml_backend(module: Any) -> Backend:
    """
    A backend over the PyYAML module, passed in so tests can stand in for it.
    """

    def load(text: str) -> Any:
        try:
            return module.safe_load(text)
        except module.YAMLError as error:
            raise YamlError(f"invalid YAML: {str(error).strip()}") from error

    def dump(document: Any) -> str:
        try:
            # A wide line keeps long values (GATEWAY_FORWARDS) on one line.
            return module.safe_dump(document, sort_keys=False, width=1000)
        except module.YAMLError as error:
            raise YamlError(f"can't write YAML: {str(error).strip()}") from error

    return Backend("pyyaml", load, dump)


def _import_pyyaml() -> Optional[Any]:
    """
    The PyYAML module, or ``None`` when it isn't installed.
    """
    try:
        import yaml
    except ImportError:
        return None

    return yaml


def select(
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable = subprocess.run,
    import_pyyaml: Callable[[], Optional[Any]] = _import_pyyaml,
) -> Backend:
    """
    The first available backend, in the order the module describes.
    """
    yq = which("yq")
    if yq:
        # Tell mikefarah's yq from the Python wrapper of the same name.
        try:
            version = run([yq, "--version"], capture_output=True, text=True, timeout=10)
            if "mikefarah" in (version.stdout or "") + (version.stderr or ""):
                return yq_backend([yq], run)
        except (OSError, subprocess.TimeoutExpired):
            pass

    module = import_pyyaml()
    if module is not None:
        return pyyaml_backend(module)

    docker = which("docker")
    if docker:
        return yq_backend([docker, *YQ_DOCKER_ARGS, YQ_IMAGE], run, name="docker-yq")

    raise YamlError(
        "no YAML tool found: install yq (https://github.com/mikefarah/yq), PyYAML (python3-yaml), or Docker"
    )


_selected: Optional[Backend] = None


def backend() -> Backend:
    """
    The backend in use, chosen the first time it is needed.
    """
    global _selected

    if _selected is None:
        _selected = select()

    return _selected


def use(selected: Optional[Backend]) -> None:
    """
    Force a backend, or with ``None`` detect one again; for tests and tools.
    """
    global _selected
    _selected = selected


def load(text: str) -> Any:
    """
    Parse YAML text.
    """
    return backend().load(text)


def dump(document: Any) -> str:
    """
    Write a value as YAML text.
    """
    return backend().dump(document)
