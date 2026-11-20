"""Unit tests for strict subproject resolution verification."""

from __future__ import annotations

from pathlib import Path

from mesa_canary.build_d3d12 import _filter_supported_meson_args, verify_subproject_resolution
from mesa_canary.config import load_all_targets

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_verify_subproject_resolution_success(tmp_path: Path) -> None:
    target = load_all_targets(REPO_ROOT)["directx-headers"]
    build_dir = tmp_path / "build"
    (build_dir / "meson-logs").mkdir(parents=True)
    (build_dir / "subprojects" / "DirectX-Headers-1.0").mkdir(parents=True)
    (build_dir / "meson-logs" / "meson-log.txt").write_text(
        "Run-time dependency directx-headers found: NO (tried pkgconfig and cmake)\n"
        "Looking for a fallback subproject for the dependency DirectX-Headers\n"
        "Dependency DirectX-Headers from subproject subprojects/DirectX-Headers-1.0 found: YES 1.620.1\n",
        encoding="utf-8",
    )
    ok, ver, _ = verify_subproject_resolution(build_dir, target, "DirectX-Headers-1.0", "1.620.1")
    assert ok is True
    assert ver == "1.620.1"


def test_verify_subproject_resolution_modern_meson_overridden_wording(tmp_path: Path) -> None:
    target = load_all_targets(REPO_ROOT)["directx-headers"]
    build_dir = tmp_path / "build"
    (build_dir / "meson-logs").mkdir(parents=True)
    (build_dir / "subprojects" / "DirectX-Headers-1.0").mkdir(parents=True)
    (build_dir / "meson-logs" / "meson-log.txt").write_text(
        "Executing subproject DirectX-Headers for machine: host\n"
        "DirectX-Headers| Project name: DirectX-Headers\n"
        "DirectX-Headers| Project version: 1.620.1\n"
        "DirectX-Headers| Subproject DirectX-Headers finished.\n"
        "Dependency directx-headers for host machine from subproject subprojects/DirectX-Headers-1.0 found: NO\n"
        "Dependency DirectX-Headers for host machine found: YES 1.620.1 (overridden)\n",
        encoding="utf-8",
    )
    ok, ver, _ = verify_subproject_resolution(build_dir, target, "DirectX-Headers-1.0", "1.620.1")
    assert ok is True
    assert ver == "1.620.1"


def test_verify_subproject_resolution_catches_system_hijack(tmp_path: Path) -> None:
    target = load_all_targets(REPO_ROOT)["directx-headers"]
    build_dir = tmp_path / "build"
    (build_dir / "meson-logs").mkdir(parents=True)
    (build_dir / "subprojects" / "DirectX-Headers-1.0").mkdir(parents=True)
    (build_dir / "meson-logs" / "meson-log.txt").write_text(
        "Run-time dependency directx-headers found: YES 1.619.1\n",
        encoding="utf-8",
    )
    ok, _, msg = verify_subproject_resolution(build_dir, target, "DirectX-Headers-1.0", "1.620.1")
    assert ok is False
    assert "hijacked" in msg.lower()


def test_verify_subproject_resolution_catches_version_mismatch(tmp_path: Path) -> None:
    target = load_all_targets(REPO_ROOT)["directx-headers"]
    build_dir = tmp_path / "build"
    (build_dir / "meson-logs").mkdir(parents=True)
    (build_dir / "subprojects" / "DirectX-Headers-1.0").mkdir(parents=True)
    (build_dir / "meson-logs" / "meson-log.txt").write_text(
        "Run-time dependency directx-headers found: NO\n"
        "Dependency DirectX-Headers from subproject subprojects/DirectX-Headers-1.0 found: YES 1.619.1\n",
        encoding="utf-8",
    )
    ok, ver, msg = verify_subproject_resolution(build_dir, target, "DirectX-Headers-1.0", "1.620.1")
    assert ok is False
    assert ver == "1.619.1"
    assert "expected '1.620.1'" in msg


def test_filter_supported_meson_args_drops_removed_and_keeps_builtins(tmp_path: Path) -> None:
    mesa_dir = tmp_path / "mesa"
    mesa_dir.mkdir()
    (mesa_dir / "meson.options").write_text(
        "option('gallium-drivers', type : 'array')\n"
        "option('llvm', type : 'feature')\n",
        encoding="utf-8",
    )
    args = [
        "--buildtype=release",
        "-Db_ndebug=false",
        "-Db_vscrt=mt",
        "-Dgallium-drivers=d3d12",
        "-Dllvm=disabled",
        "-Dremoved-in-mesa-28=disabled",
        "-Dwayland-protocols:tests=false",
    ]
    filtered = _filter_supported_meson_args(mesa_dir, args)
    assert filtered == [
        "--buildtype=release",
        "-Db_ndebug=false",
        "-Db_vscrt=mt",
        "-Dgallium-drivers=d3d12",
        "-Dllvm=disabled",
        "-Dwayland-protocols:tests=false",
    ]
