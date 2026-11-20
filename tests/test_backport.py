"""Unit tests for Stage 2 backport candidate discovery and cherry-pick dry-run."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mesa_canary.backport import diagnose_backport_local, extract_mr_url


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=str(repo),
        text=True,
        encoding="utf-8",
    ).strip()


def test_diagnose_backport_clean_conflict_and_already_picked(tmp_path: Path) -> None:
    repo = tmp_path / "mesa"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")

    target_dir = repo / "src" / "gallium" / "drivers" / "d3d12"
    target_dir.mkdir(parents=True)
    enc = target_dir / "d3d12_video_enc.cpp"
    hevc = target_dir / "d3d12_video_enc_hevc.cpp"
    enc.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    hevc.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base commit")

    # Branch staging/26.2 off base
    _git(repo, "branch", "staging/26.2")

    # Commit 1 on main: clean fix touching enc (not yet on staging/26.2)
    enc.write_text("line1\nconst_fixed_line2\nline3\nline4\nline5\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(
        repo,
        "commit",
        "-m",
        "d3d12: Fix const qualifiers\n\nPart-of: <https://gitlab.freedesktop.org/mesa/mesa/-/merge_requests/43715>",
    )
    clean_sha = _git(repo, "rev-parse", "HEAD")

    # Commit 2 on main: already cherry-picked commit
    enc.write_text("line1\nconst_fixed_line2\nline3\nline4\nalready_picked\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "d3d12: Minor cleanup")
    picked_sha = _git(repo, "rev-parse", "HEAD")

    # Commit 3 on main: conflicting change on hevc
    hevc.write_text("alpha\nmain_conflict_beta\ngamma\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "d3d12: HEVC refactor")
    conflict_sha = _git(repo, "rev-parse", "HEAD")

    # Advance staging/26.2: apply a conflicting edit on hevc and cherry-pick commit 2
    _git(repo, "checkout", "staging/26.2")
    hevc.write_text("alpha\nstable_divergent_beta\ngamma\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "d3d12: Stable-only HEVC tweak")

    enc.write_text("line1\nline2\nline3\nline4\nalready_picked\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(
        repo,
        "commit",
        "-m",
        f"d3d12: Minor cleanup\n\n(cherry picked from commit {picked_sha})",
    )
    _git(repo, "checkout", "main")

    cands = diagnose_backport_local(
        repo,
        failing_ref="staging/26.2",
        failed_files=[
            "src/gallium/drivers/d3d12/d3d12_video_enc.cpp",
            "src/gallium/drivers/d3d12/d3d12_video_enc_hevc.cpp",
        ],
        main_ref="main",
        max_candidates=5,
        test_cherry_pick=True,
    )
    by_sha = {c.sha: c for c in cands}

    assert picked_sha not in by_sha
    assert clean_sha in by_sha
    assert by_sha[clean_sha].cherry_pick_status == "CLEAN"
    assert by_sha[clean_sha].mr_url == "https://gitlab.freedesktop.org/mesa/mesa/-/merge_requests/43715"
    assert conflict_sha in by_sha
    assert by_sha[conflict_sha].cherry_pick_status == "CONFLICT"
    assert "src/gallium/drivers/d3d12/d3d12_video_enc_hevc.cpp" in by_sha[conflict_sha].conflict_files


def test_extract_mr_url() -> None:
    msg = "Subject\n\nPart-of: <https://gitlab.freedesktop.org/mesa/mesa/-/merge_requests/43715>\n"
    assert extract_mr_url(msg) == "https://gitlab.freedesktop.org/mesa/mesa/-/merge_requests/43715"
