"""
Building the hardcode-cli package (``pip install ./cli``, ``pipx install
./cli``): pyproject.toml says what the package is; this adds the stack
(compose.yaml, the images' build contexts, the template) to it, copied from
the checkout it is built in, so that an installed hardcode has what it runs
profiles with. See hardcode_cli/stack.py.
"""

import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hardcode_cli import stack  # noqa: E402


class BuildWithStack(build_py):
    """
    The package as setuptools builds it, then the checkout's stack copied
    into it.
    """

    def run(self) -> None:
        """
        Build, then stage the stack beside the modules.
        """
        super().run()
        stack.stage(stack.CHECKOUT, Path(self.build_lib) / "hardcode_cli" / "stack")


setup(cmdclass={"build_py": BuildWithStack})
