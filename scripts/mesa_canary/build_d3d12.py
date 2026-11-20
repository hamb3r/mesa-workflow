"""Cross-toolchain Mesa D3D12 / DZN / Video / MediaFoundation canary builder.

Supports all 7 toolchain variants:
  Windows: msvc, clang-cl, msys2-ucrt64, msys2-mingw64, msys2-clang64
  Linux:   linux-gcc, linux-clang
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesa_canary.config import GlobalConfig, TargetConfig, ToolchainConfig
from mesa_canary.log_parser import parse_build_log
from mesa_canary.planner import cell_key

_MESON_VER_RE = re.compile(
    r"project\s*\([^)]*?version\s*:\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)


def _run_streamed(
    cmd: list[str],
    cwd: Path,
    log_fh: Any,
    env: dict[str, str] | None = None,
) -> int:
    """Run a command, streaming stdout+stderr both to console and to log_fh."""
    header = f"\n$ {' '.join(cmd)}\n"
    sys.stdout.write(header)
    sys.stdout.flush()
    log_fh.write(header)
    log_fh.flush()

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        log_fh.write(line)
    sys.stdout.flush()
    log_fh.flush()
    return proc.wait()


def _shallow_fetch_checkout(
    repo_url: str,
    ref: str,
    dest_dir: Path,
    log_fh: Any,
) -> str:
    """Clone exactly one ref (branch, tag, or commit SHA) with --depth=1."""
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    for cmd in (
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", repo_url],
        ["git", "fetch", "--depth=1", "--no-tags", "origin", ref],
        ["git", "checkout", "--detach", "-q", "FETCH_HEAD"],
    ):
        rc = _run_streamed(cmd, cwd=dest_dir, log_fh=log_fh)
        if rc != 0:
            raise RuntimeError(f"git command failed (rc={rc}): {' '.join(cmd)}")

    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=str(dest_dir),
        text=True,
    ).strip()
    return sha


def _read_wrap_directory(wrap_path: Path, default_dir: str) -> str:
    if not wrap_path.is_file():
        return default_dir
    parser = configparser.ConfigParser()
    parser.read(wrap_path, encoding="utf-8")
    for section in parser.sections():
        if parser.has_option(section, "directory"):
            return parser.get(section, "directory").strip()
    return default_dir


def _extract_subproject_version(subproj_dir: Path) -> str | None:
    meson_build = subproj_dir / "meson.build"
    if not meson_build.is_file():
        return None
    text = meson_build.read_text(encoding="utf-8", errors="replace")
    match = _MESON_VER_RE.search(text)
    return match.group(1).strip() if match else None


def _override_subproject(
    mesa_dir: Path,
    target: TargetConfig,
    dep_ref: str,
    log_fh: Any,
) -> tuple[str, str | None, str]:
    """Replace Mesa's target subproject with an exact checkout of dep_ref."""
    wrap_path = mesa_dir / target.subproject.wrap_path
    subproj_dir_name = _read_wrap_directory(wrap_path, target.subproject.default_directory)
    subproj_dir = mesa_dir / "subprojects" / subproj_dir_name

    dep_sha = _shallow_fetch_checkout(
        repo_url=target.upstream.git_url,
        ref=dep_ref,
        dest_dir=subproj_dir,
        log_fh=log_fh,
    )
    declared_ver = _extract_subproject_version(subproj_dir)

    # Also rewrite wrap revision so any wrap tooling reflects the exact tested SHA.
    if wrap_path.is_file():
        wrap_text = wrap_path.read_text(encoding="utf-8", errors="replace")
        wrap_text = re.sub(
            r"(?m)^revision\s*=.*$",
            f"revision = {dep_sha}",
            wrap_text,
        )
        wrap_path.write_text(wrap_text, encoding="utf-8")

    return dep_sha, declared_ver, subproj_dir_name


def _write_native_file(
    path: Path,
    tc: ToolchainConfig,
    sccache_bin: str | None,
) -> None:
    def fmt_bin(exe: str) -> str:
        if sccache_bin:
            return f"['{sccache_bin.replace(chr(92), '/')}', '{exe}']"
        return f"'{exe}'"

    lines = [
        "[binaries]",
        f"c = {fmt_bin(tc.c_compiler)}",
        f"cpp = {fmt_bin(tc.cpp_compiler)}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolve_gallium_va_args(mesa_dir: Path, tc: ToolchainConfig) -> tuple[list[str], str]:
    """Determine gallium-va Meson args cleanly on Windows vs Linux.

    Note: upstream intel/libva subproject tarball does not mirror win32/va_win32.h
    into a flat va/ directory in-tree (Mesa Windows CI installs libva separately),
    so on Windows we only enable gallium-va when pkg-config finds an installed libva-win32.
    All d3d12_video_*.cpp sources still compile into libd3d12/libgallium_wgl regardless.
    """
    _ = mesa_dir
    if tc.gallium_va == "enabled":
        return [], "configured enabled"
    if tc.gallium_va == "disabled":
        return ["-Dgallium-va=disabled"], "configured disabled"

    pkg_cfg = shutil.which("pkg-config") or shutil.which("pkgconf")
    if pkg_cfg:
        probe = subprocess.run(
            [pkg_cfg, "--exists", "libva-win32"],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return ["-Dgallium-va=enabled"], "enabled via system pkg-config libva-win32"

    return ["-Dgallium-va=disabled"], "disabled (no preinstalled libva-win32 on Windows)"


def _filter_supported_meson_args(mesa_dir: Path, args: list[str]) -> list[str]:
    """Drop MediaFoundation flags only if building a historic branch lacking those options."""
    opts_file = mesa_dir / "meson.options"
    if not opts_file.is_file():
        opts_file = mesa_dir / "meson_options.txt"
    if not opts_file.is_file():
        return args
    opts_text = opts_file.read_text(encoding="utf-8", errors="replace")
    filtered: list[str] = []
    for arg in args:
        if "mediafoundation" in arg and "gallium-mediafoundation" not in opts_text:
            continue
        filtered.append(arg)
    return filtered


def verify_subproject_resolution(
    build_dir: Path,
    target: TargetConfig,
    subproj_dir_name: str,
    expected_version: str | None,
) -> tuple[bool, str | None, str]:
    """Verify Meson resolved the target dependency from our overridden subproject.

    Prevents silent false-greens where a preinstalled system package (e.g. lowercase
    `directx-headers.pc` in MSYS2) hijacks Mesa's dependency lookup.
    """
    meson_log = build_dir / "meson-logs" / "meson-log.txt"
    if not meson_log.is_file():
        return False, None, "meson-logs/meson-log.txt not found after configure"

    log_text = meson_log.read_text(encoding="utf-8", errors="replace")
    sp_name = target.subproject.name

    # Check if a system pkg-config/cmake dependency hijacked lookup (without '(overridden)').
    for dep_name in target.subproject.meson_dependency_names:
        hijack_re = re.compile(
            rf"Run-time dependency {re.escape(dep_name)}(?:\s+for\s+\w+\s+machine)?\s+found:\s+YES\b(?!.*\(overridden\))",
            re.IGNORECASE,
        )
        if hijack_re.search(log_text):
            return (
                False,
                None,
                f"System dependency '{dep_name}' hijacked lookup instead of subproject '{sp_name}'!",
            )

    # Confirm subproject itself executed and provided the overridden/fallback dependency.
    resolved_ver: str | None = None
    patterns = (
        # Meson >= 1.x with meson.override_dependency(...)
        re.compile(
            rf"Dependency {re.escape(sp_name)}(?:\s+for\s+\w+\s+machine)?\s+found:\s+YES\s+([^\s\r\n(]+)\s+\(overridden\)",
            re.IGNORECASE,
        ),
        # Meson fallback dependency without override_dependency
        re.compile(
            rf"Dependency {re.escape(sp_name)}(?:\s+for\s+\w+\s+machine)?\s+from subproject subprojects/{re.escape(subproj_dir_name)}\s+found:\s+YES\s+([^\s\r\n(]+)",
            re.IGNORECASE,
        ),
        # Subproject project version header emitted during subproject execution
        re.compile(
            rf"{re.escape(sp_name)}\|\s*Project version:\s*([^\s\r\n]+)",
            re.IGNORECASE,
        ),
    )
    for pat in patterns:
        m = pat.search(log_text)
        if m:
            resolved_ver = m.group(1).strip()
            break

    subproj_executed = bool(
        re.search(
            rf"(?:Subproject {re.escape(sp_name)} (?:finished|is buildable:\s*YES)|Executing subproject {re.escape(sp_name)}\b)",
            log_text,
            re.IGNORECASE,
        )
    )
    if not subproj_executed and resolved_ver is None:
        return (
            False,
            None,
            f"Could not verify subproject '{sp_name}' (subprojects/{subproj_dir_name}) in meson-log.txt",
        )
    if resolved_ver is None:
        resolved_ver = expected_version

    if expected_version and resolved_ver and resolved_ver != expected_version:
        return (
            False,
            resolved_ver,
            f"Resolved subproject version '{resolved_ver}' != expected '{expected_version}'",
        )

    subproj_build_dir = build_dir / "subprojects" / subproj_dir_name
    if not subproj_build_dir.is_dir():
        return (
            False,
            resolved_ver,
            f"Expected subproject build directory missing: {subproj_build_dir}",
        )

    return True, resolved_ver, f"Verified subproject {sp_name} {resolved_ver or '(unknown)'}"


def run_build(
    global_cfg: GlobalConfig,
    target: TargetConfig,
    *,
    mesa_ref: str,
    dep_ref: str,
    toolchain_id: str,
    channel: str,
    severity: str,
    work_dir: Path,
    output_dir: Path,
    packagecache_dir: Path | None = None,
    run_url: str = "",
    local_mesa_url: str | None = None,
    local_dep_url: str | None = None,
) -> dict[str, Any]:
    """Execute one full canary matrix build and emit result.json + error.log."""
    started = time.monotonic()
    tc = target.get_toolchain(toolchain_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = work_dir / "build.log"

    mesa_dir = work_dir / "mesa"
    build_dir = work_dir / "build"
    native_file = work_dir / "native-toolchain.ini"

    mesa_sha = ""
    dep_sha = ""
    dep_declared_ver: str | None = None
    dep_resolved_ver: str | None = None
    status = "FAIL"
    error_stage: str | None = None
    error_summary: str | None = None
    error_excerpt = ""
    failed_files: list[str] = []

    mesa_repo_url = local_mesa_url or global_cfg.mesa.git_url
    if local_dep_url:
        # Override upstream git_url for local offline tests.
        target = TargetConfig(
            id=target.id,
            name=target.name,
            workflow=target.workflow,
            enabled=target.enabled,
            upstream=type(target.upstream)(
                type=target.upstream.type,
                repo=target.upstream.repo,
                git_url=local_dep_url,
                channels=target.upstream.channels,
            ),
            subproject=target.subproject,
            meson_common_args=target.meson_common_args,
            all_toolchains=target.all_toolchains,
        )

    with log_path.open("w", encoding="utf-8") as log_fh:
        try:
            # 0. Pre-flight tool verification
            error_stage = "setup"
            if tc.needs_tracewpp and not shutil.which("tracewpp"):
                raise RuntimeError(
                    "tracewpp.exe not found in PATH (required for gallium-mediafoundation). "
                    "Ensure Windows SDK BuildTools / WDK bin directory is in PATH."
                )

            # 1. Shallow clone Mesa
            error_stage = "clone"
            mesa_sha = _shallow_fetch_checkout(mesa_repo_url, mesa_ref, mesa_dir, log_fh)
            log_fh.write(f"[info] Mesa {mesa_ref} -> {mesa_sha}\n")

            # Attach shared Meson subprojects/packagecache if provided (accelerates zlib/libva/libdrm wraps)
            if packagecache_dir is not None:
                packagecache_dir.mkdir(parents=True, exist_ok=True)
                target_pc = mesa_dir / "subprojects" / "packagecache"
                if target_pc.exists():
                    shutil.rmtree(target_pc)
                shutil.copytree(packagecache_dir, target_pc)

            # 2. Override target subproject (DirectX-Headers)
            dep_sha, dep_declared_ver, subproj_dir_name = _override_subproject(
                mesa_dir, target, dep_ref, log_fh
            )
            log_fh.write(
                f"[info] Subproject {target.subproject.name} ({dep_ref}) -> {dep_sha} "
                f"(meson.build version: {dep_declared_ver})\n"
            )

            # 3. Generate Meson native-file (with sccache if available)
            error_stage = "setup"
            sccache_bin = shutil.which("sccache") if tc.use_sccache else None
            _write_native_file(native_file, tc, sccache_bin)
            log_fh.write(f"[info] sccache: {sccache_bin or 'disabled'}\n")

            va_args, va_note = _resolve_gallium_va_args(mesa_dir, tc)
            log_fh.write(f"[info] gallium-va: {va_note}\n")

            meson_args = list(target.meson_common_args) + list(tc.meson_args) + va_args
            meson_args = _filter_supported_meson_args(mesa_dir, meson_args)

            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            if tc.os == "windows":
                # Avoid Meson picking up stray system DirectX-Headers via CMake/pkg-config
                env.setdefault("MESON_FORCE_BACKTRACE", "1")

            setup_cmd = [
                "meson",
                "setup",
                str(build_dir),
                str(mesa_dir),
                "--native-file",
                str(native_file),
                *meson_args,
            ]
            rc = _run_streamed(setup_cmd, cwd=work_dir, log_fh=log_fh, env=env)
            if rc != 0:
                raise RuntimeError(f"meson setup failed with exit code {rc}")

            # Sync newly downloaded wrap archives back into shared packagecache
            if packagecache_dir is not None:
                src_pc = mesa_dir / "subprojects" / "packagecache"
                if src_pc.is_dir():
                    for item in src_pc.iterdir():
                        dst = packagecache_dir / item.name
                        if item.is_file() and not dst.exists():
                            shutil.copy2(item, dst)

            # 4. Strict verification that our overridden subproject was actually used
            error_stage = "verify"
            ok, dep_resolved_ver, verify_msg = verify_subproject_resolution(
                build_dir, target, subproj_dir_name, dep_declared_ver
            )
            log_fh.write(f"[verify] {verify_msg}\n")
            if not ok:
                raise RuntimeError(f"Subproject verification failed: {verify_msg}")

            # 5. Compile with ninja (-k 10 captures multiple broken TUs for backport lookup)
            error_stage = "compile"
            ninja_cmd = ["ninja", "-C", str(build_dir), "-k", "10"]
            rc = _run_streamed(ninja_cmd, cwd=work_dir, log_fh=log_fh, env=env)
            if rc != 0:
                raise RuntimeError(f"ninja build failed with exit code {rc}")

            status = "PASS"
            error_stage = None

            if sccache_bin:
                _run_streamed([sccache_bin, "--show-stats"], cwd=work_dir, log_fh=log_fh, env=env)

        except Exception as exc:
            status = "FAIL"
            log_fh.write(f"\n[ERROR:{error_stage}] {exc}\n")

    duration_sec = round(time.monotonic() - started, 1)
    if status == "FAIL":
        raw_log = log_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_build_log(raw_log, source_root=str(mesa_dir))
        error_summary = parsed.summary
        error_excerpt = parsed.excerpt
        failed_files = list(parsed.failed_files)
        (output_dir / "error.log").write_text(error_excerpt + "\n", encoding="utf-8")

    result: dict[str, Any] = {
        "cell_id": cell_key(target.id, channel, mesa_ref, tc.id),
        "target": target.id,
        "target_name": target.name,
        "channel": channel,
        "severity": severity,
        "mesa_branch": mesa_ref,
        "mesa_sha": mesa_sha,
        "dep_ref": dep_ref,
        "dep_sha": dep_sha,
        "dep_declared_version": dep_declared_ver,
        "dep_resolved_version": dep_resolved_ver or dep_declared_ver,
        "toolchain": tc.id,
        "toolchain_label": tc.label,
        "os": tc.os,
        "status": status,
        "error_stage": error_stage,
        "duration_sec": duration_sec,
        "run_url": run_url,
        "error_summary": error_summary,
        "error_excerpt": error_excerpt,
        "failed_files": failed_files,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    (output_dir / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return result
