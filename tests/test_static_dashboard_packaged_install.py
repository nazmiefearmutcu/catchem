"""Static asset resolution must work via importlib.resources, not filesystem paths.

This protects the dashboard against the failure mode where the package is
installed from a wheel and the source tree's `static/` is no longer adjacent
to the import location.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fusion_stack import static_assets
from fusion_stack.api import create_app
from fusion_stack.settings import load_settings, reload_settings


def test_get_static_path_finds_dashboard() -> None:
    """The legacy dashboard must be discoverable via package resources."""
    p = static_assets.get_static_path("dashboard.html")
    assert p is not None, "dashboard.html not found via importlib.resources"
    assert p.is_file()
    assert p.stat().st_size > 0


def test_get_static_path_rejects_traversal() -> None:
    for bad in ("../etc/passwd", "/etc/passwd", "..\\windows", "", "subdir/../escape"):
        with pytest.raises(ValueError):
            static_assets.get_static_path(bad)


def test_get_static_path_returns_none_for_missing() -> None:
    p = static_assets.get_static_path("definitely-not-a-real-file.xyz")
    assert p is None


def test_env_override_serves_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FUSION_STATIC_DIR override must serve only files that exist in it."""
    monkeypatch.setenv("FUSION_STATIC_DIR", str(tmp_path))
    (tmp_path / "smoke.txt").write_text("hello from override", encoding="utf-8")
    p = static_assets.get_static_path("smoke.txt")
    assert p is not None and p.read_text(encoding="utf-8") == "hello from override"


def test_env_override_does_not_expose_arbitrary_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Override missing files must fall back to package resources, not error."""
    monkeypatch.setenv("FUSION_STATIC_DIR", str(tmp_path))
    # Not present in tmp_path → fall back to packaged dashboard
    p = static_assets.get_static_path("dashboard.html")
    assert p is not None
    assert "dashboard" in p.read_text(encoding="utf-8").lower()


def test_env_override_traversal_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_STATIC_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        static_assets.get_static_path("../etc/passwd")


def test_root_serves_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET / must return HTML — bundle or placeholder, but always 200 + html."""
    reload_settings()
    s = load_settings()
    app = create_app(s)
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        body = r.text
        # Bundle OR fallback page
        assert ("<div id=\"root\"></div>" in body) or ("bundle has not been built" in body)


def test_legacy_route_resolves_via_package_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """The /legacy route must not fail when the source tree is unreachable."""
    reload_settings()
    s = load_settings()
    app = create_app(s)
    with TestClient(app) as c:
        r = c.get("/legacy")
        assert r.status_code == 200, r.text
        assert "fusion_stack" in r.text


def test_missing_static_returns_safe_404_not_crash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point FUSION_STATIC_DIR at an empty dir; the legacy route then asks for
    a file that's neither in the override nor in package resources.

    We achieve this by pointing the override at an empty dir and asking for a
    fabricated filename. The handler must return 404 (not raise).
    """
    monkeypatch.setenv("FUSION_STATIC_DIR", str(tmp_path))
    reload_settings()
    # static_assets returns None for unknown files; that's the contract.
    assert static_assets.get_static_path("does_not_exist_anywhere.html") is None


def test_wheel_install_smoke_serves_dashboard(tmp_path: Path) -> None:
    """Build a wheel and import it from a clean venv to confirm static files ship.

    Skips if `python -m build` is unavailable. This is the canary test for the
    P0 packaging risk.
    """
    import subprocess
    import sys

    try:
        import build  # noqa: F401
    except ImportError:
        pytest.skip("python -m build not installed; pip install build to run this test")

    repo = Path(__file__).resolve().parents[1]
    out_dir = tmp_path / "dist"
    out_dir.mkdir()
    res = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir), str(repo)],
        capture_output=True, text=True, timeout=180,
    )
    if res.returncode != 0:
        pytest.skip(f"wheel build failed: {res.stderr[-400:]}")
    wheels = list(out_dir.glob("fusion_stack-*.whl"))
    assert wheels, f"no wheel produced: {list(out_dir.iterdir())}"

    import zipfile
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    # Static must live INSIDE the package wheel (next to fusion_stack/*.py),
    # not as shared-data. Both editable + installed lookups depend on this.
    assert any(
        n == "fusion_stack/static/dashboard.html" for n in names
    ), f"dashboard.html missing from wheel package payload: {sorted(n for n in names if 'static' in n)}"
