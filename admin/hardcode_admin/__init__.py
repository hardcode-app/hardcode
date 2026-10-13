"""
HardCode on Kubernetes: the operator that turns Profile objects into
sandboxes, and the admin web UI (Flask) that manages them.

The allowlist, blocked-traffic and language code is shared with the
``hardcode`` command line, whose package this one imports.
"""

import sys
from pathlib import Path

__version__ = "0.1.0"

# In the image both packages are on PYTHONPATH. From a checkout, the CLI's
# package lives next door; it is appended rather than prepended, so an
# installed copy wins when there is one.
try:
    import hardcode_cli  # noqa: F401
except ImportError:  # pragma: no cover - depends on how we're run
    sys.path.append(str(Path(__file__).resolve().parent.parent.parent / "cli"))
