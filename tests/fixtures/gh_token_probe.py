#!/usr/bin/env python3
"""
A stand-in for the real gh, for tests/images.py: prints the GH_TOKEN it was
started with, or "(none)", so that the suite can see which token gh's
wrapper (images/vscode/gh.sh) handed it.
"""

import os

print(os.environ.get("GH_TOKEN", "(none)"))
