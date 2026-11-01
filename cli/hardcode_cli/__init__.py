"""
HardCode's command line: profiles, allowlists, forwards, mounts and languages
for the sandboxed development environment.
"""

__version__ = "0.1.0"

# A development build's mark, "dev, <commit>", which the release workflow
# writes (release/release.py dev). Empty in a release and in the checkout.
__build__ = ""


def version_text() -> str:
    """
    The version as the command and the app show it: "0.1.0", or
    "0.1.0 (dev, 2c419ae)" for a development build.
    """
    if __build__:
        return f"{__version__} ({__build__})"

    return __version__
