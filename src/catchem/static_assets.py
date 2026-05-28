"""Package-resource-aware static asset resolver.

Why this exists
---------------
`Path(__file__).parent / "static"` works in editable installs but breaks the
moment the package is installed from a wheel into a path where the package
directory is not on the filesystem next to a writable `static/` subtree (e.g.
zipapp, namespace packages, or wheels where Hatch installed `static/` as
"shared data" rather than package data).

This module wraps `importlib.resources` so the lookup is robust across:
  * editable installs (`pip install -e .`)
  * wheel installs (`pip install dist/*.whl`)
  * the CATCHEM_STATIC_DIR override (useful for dev rebuilds without reinstall)

It never accepts a `name` containing `..` or absolute paths — only flat names
relative to the package's `static/` folder.

`get_static_path(name)` returns a string filesystem path that the caller can
hand to FastAPI's `FileResponse`. When the resource is inside a zipped wheel
(rare for us but possible), `as_file` extracts to a temp location and we keep
the path alive for the process lifetime via the contextmanager exit dance.

`open_static_bytes(name)` returns bytes — preferred when you just need to read
the file once (e.g. inline HTML for the legacy dashboard fallback).
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from importlib.resources import as_file, files
from pathlib import Path

# Track extracted-from-zip temp paths so they stay valid for the process lifetime.
_KEEPALIVE = ExitStack()


def _validate_name(name: str) -> str:
    if not name or not isinstance(name, str):
        raise ValueError("static asset name must be a non-empty string")
    if ".." in name or name.startswith("/") or "\\" in name:
        raise ValueError(f"unsafe static asset name: {name!r}")
    return name


def _env_override(name: str) -> Path | None:
    """If CATCHEM_STATIC_DIR is set, prefer it but only for files that exist there.

    The env override is a dev convenience: it lets you rebuild the React bundle
    into a sibling directory and refresh the page without reinstalling the
    package. It is strictly file-based — we never expose arbitrary dirs.
    """
    env_dir = os.environ.get("CATCHEM_STATIC_DIR")
    if not env_dir:
        return None
    base = Path(env_dir).expanduser().resolve()
    if not base.is_dir():
        return None
    target = (base / name).resolve()
    # Defense in depth: ensure target stays inside base.
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


def static_dir() -> Path:
    """Filesystem dir of the package's static assets (for StaticFiles mount).

    Falls back to the CATCHEM_STATIC_DIR override when set and valid.
    """
    env_dir = os.environ.get("CATCHEM_STATIC_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        if p.is_dir():
            return p
    resource = files("catchem").joinpath("static")
    return Path(_KEEPALIVE.enter_context(as_file(resource)))


def get_static_path(name: str) -> Path | None:
    """Return the filesystem path to a packaged static asset, or None if missing.

    Args:
        name: a flat filename (no path components). May contain a single
              subdirectory like 'app/index.html' — validated to forbid traversal.

    Returns:
        Path on success, None if the asset is not present.
    """
    name = _validate_name(name)

    override = _env_override(name)
    if override is not None:
        return override

    try:
        resource = files("catchem").joinpath("static", name)
    except (FileNotFoundError, ModuleNotFoundError):
        return None

    if not resource.is_file():
        return None
    return Path(_KEEPALIVE.enter_context(as_file(resource)))


def open_static_bytes(name: str) -> bytes | None:
    """Read a packaged static asset to bytes. None if missing."""
    p = get_static_path(name)
    if p is None or not p.exists():
        return None
    return p.read_bytes()


__all__ = ["get_static_path", "open_static_bytes", "static_dir"]
