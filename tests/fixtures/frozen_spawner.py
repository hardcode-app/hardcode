"""
Frozen by tests/release.py exactly as the app's command is (PyInstaller,
--onefile), to start a copy of itself with hardcode_cli.applier's own
_spawn, as a change that recreates containers starts the profile's applier,
and end at once. The copy waits a moment, as the applier waits for the
changes to be due, then loads modules it hadn't yet, and says it lived.

    frozen_spawner start MARKER   start the copy, and end
    frozen_spawner copy MARKER    what start runs: wait, then write MARKER
"""

import sys
import time
from pathlib import Path

from hardcode_cli import applier


def main(argv: list[str]) -> int:
    """
    Start the copy and end, or, as the copy, outlive the one that started it.
    """
    mode, marker = argv

    if mode == "start":
        applier._spawn([sys.executable, "copy", marker])
        return 0

    # By now the one that started this has ended, and with it the folder it
    # had unpacked itself into.
    time.sleep(3)
    import hashlib
    import sqlite3
    import ssl

    Path(marker).write_text(
        f"the copy lived ({hashlib.__name__}, {sqlite3.__name__}, {ssl.__name__})", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
