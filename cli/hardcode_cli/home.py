"""
Where HardCode keeps a user's profiles (the home), and its cache.

The home is ``--config PATH``, else ``$HARDCODE_CONFIG``, else the place
each platform keeps an application's configuration:

- Linux and the other Unixes: ``$XDG_CONFIG_HOME/hardcode``, by default
  ``~/.config/hardcode``;
- macOS: ``~/Library/Application Support/HardCode``;
- Windows: ``%LOCALAPPDATA%\\HardCode``, not the roaming profile: a profile
  names this machine's folders and ports.

It holds ``profiles/<name>/``. The cache holds what HardCode can write again
at any time: the stack it runs profiles with (see ``stack``).
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Mapping, Optional

# The folder's name, on the platforms that capitalise application folders
# and on those that don't.
NAME = "HardCode"
LOWER_NAME = "hardcode"


class HomeError(Exception):
    """
    A home that can't be used, or HardCode run where it shouldn't be.
    """


def _user_home(env: Mapping[str, str]) -> Path:
    """
    The user's home directory, as *env* has it.
    """
    configured = env.get("HOME") or env.get("USERPROFILE")
    return Path(configured) if configured else Path.home()


def _xdg(env: Mapping[str, str], variable: str, fallback: str) -> Path:
    """
    An XDG base directory: *variable* when it is set to an absolute path (the
    specification ignores a relative one), else *fallback* under the home
    directory.
    """
    configured = env.get(variable, "")
    if configured and os.path.isabs(configured):
        return Path(configured)

    return _user_home(env) / fallback


def _local_app_data(env: Mapping[str, str]) -> Path:
    """
    Windows' per-machine application data folder.
    """
    configured = env.get("LOCALAPPDATA")
    return Path(configured) if configured else _user_home(env) / "AppData" / "Local"


def default_home(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    """
    The home when neither ``--config`` nor ``$HARDCODE_CONFIG`` names one.
    """
    environment = os.environ if env is None else env
    system = sys.platform if platform is None else platform

    if system == "darwin":
        return _user_home(environment) / "Library" / "Application Support" / NAME
    if system == "win32":
        return _local_app_data(environment) / NAME

    return _xdg(environment, "XDG_CONFIG_HOME", ".config") / LOWER_NAME


def cache_dir(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    """
    Where HardCode keeps what it can write again at any time.
    """
    environment = os.environ if env is None else env
    system = sys.platform if platform is None else platform

    if system == "darwin":
        return _user_home(environment) / "Library" / "Caches" / NAME
    if system == "win32":
        return _local_app_data(environment) / NAME / "Cache"

    return _xdg(environment, "XDG_CACHE_HOME", ".cache") / LOWER_NAME


def resolve(config: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> Path:
    """
    The home to use: *config* (``--config``), else ``$HARDCODE_CONFIG``,
    else the platform's default. ``~`` is expanded, and the path made
    absolute; it is created when a profile is.
    """
    environment = os.environ if env is None else env
    chosen = config or environment.get("HARDCODE_CONFIG", "")

    if not chosen:
        return default_home(environment)

    path = Path(chosen).expanduser()
    if path.exists() and not path.is_dir():
        raise HomeError(f"{chosen} is a file; --config (or HARDCODE_CONFIG) names a folder")

    return path.resolve()


def home_id(home: Path, env: Optional[Mapping[str, str]] = None) -> str:
    """
    A short id for a home other than the default one, which goes into the
    names of its profiles' Compose projects and images, so that its
    ``default`` and the default home's ``default`` are different containers.
    ``""`` for the default home, whose profiles keep the plain names.
    """
    if home.resolve() == default_home(env).resolve():
        return ""

    return hashlib.sha256(str(home.resolve()).encode("utf-8")).hexdigest()[:6]


def refuse_sudo(env: Optional[Mapping[str, str]] = None, euid: Optional[int] = None) -> None:
    """
    Refuse to run under sudo. HardCode never needs root (Docker's group,
    Docker Desktop or rootless Docker are enough), and as root it would use
    root's home, an empty one, and leave files the user can't change.
    """
    environment = os.environ if env is None else env
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else -1

    if euid == 0 and environment.get("SUDO_USER"):
        raise HomeError(
            f"HardCode doesn't need root: run it as {environment['SUDO_USER']}, without sudo "
            "(as root it would use root's profiles, not yours)"
        )
