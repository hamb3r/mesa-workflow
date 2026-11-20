"""Unit tests for state/README updates and GitHub Issues lifecycle."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesa_canary.config import load_all_targets, load_global_config
from mesa_canary.reporter import STATUS_END_MARKER, STATUS_START_MARKER, process_report

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeIssuesClient:
    def __init__(self) -> None:
        self.next_num = 1
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[str]] = {}

    def find_open_issue_by_key(self, key: str, base_label: str) -> dict[str, Any] | None:
        marker = f"<!-- mesa-canary-key: {key} -->"
        for issue in self.issues.values():
            if issue["state"] == "open" and marker in issue["body"]:
                return issue
        return None

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        num = self.next_num
        self.next_num += 1
        issue = {
            "number": num,
            "html_url": f"https://github.com/example/mesa-workflow/issues/{num}",
            "title": title,
            "body": body,
            "labels": list(labels),
            "state": "open",
        }
        self.issues[num] = issue
        self.comments[num] = []
        return issue

    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        issue = self.issues[number]
        if title is not None:
            issue["title"] = title
        if body is not None:
            issue["body"] = body
        if state is not None:
            issue["state"] = state
        if labels is not None:
            issue["labels"] = list(labels)
        return issue

    def add_comment(self, number: int, body: str) -> dict[str, Any]:
        self.comments[number].append(body)
        return {"body": body}


def _setup_temp_repo(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state" / "status.json").write_text("{}", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        f"# Test\n\n{STATUS_START_MARKER}\nold\n{STATUS_END_MARKER}\n",
        encoding="utf-8",
    )
    return tmp_path


def _write_result(artifacts_dir: Path, name: str, payload: dict[str, Any]) -> None:
    d = artifacts_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps(payload), encoding="utf-8")


def test_issue_open_dedup_and_autoclose_and_readme_render(tmp_path: Path) -> None:
    repo = _setup_temp_repo(tmp_path)
    global_cfg = load_global_config(repo)
    # Disable remote backport lookup in pure unit test
    global_cfg = type(global_cfg)(
        mesa=global_cfg.mesa,
        runners=global_cfg.runners,
        keepalive_days=global_cfg.keepalive_days,
        backport=type(global_cfg.backport)(enabled=False),
        issues=global_cfg.issues,
    )
    targets = load_all_targets(repo)
    fake_gh = FakeIssuesClient()

    snap = tmp_path / "snapshot.json"
    snap.write_text(
        json.dumps(
            {
                "mesa": {
                    "active_branches": ["main", "staging/26.2"],
                    "heads": {"main": "aaaa1111", "staging/26.2": "bbbb2222"},
                }
            }
        ),
        encoding="utf-8",
    )

    # 1. PASS -> FAIL opens one consolidated issue
    art1 = tmp_path / "art1"
    _write_result(
        art1,
        "msvc",
        {
            "cell_id": "directx-headers:release:staging/26.2:msvc",
            "target": "directx-headers",
            "target_name": "DirectX-Headers",
            "channel": "release",
            "severity": "critical",
            "mesa_branch": "staging/26.2",
            "mesa_sha": "bbbb2222",
            "dep_ref": "v1.620.1",
            "dep_sha": "a2b1aa9f",
            "dep_resolved_version": "1.620.1",
            "toolchain": "msvc",
            "toolchain_label": "Win MSVC",
            "status": "FAIL",
            "error_stage": "compile",
            "duration_sec": 120.5,
            "run_url": "https://github.com/example/mesa-workflow/actions/runs/1",
            "error_summary": "error C2664: cannot convert const",
            "error_excerpt": "d3d12_video_enc.cpp(2028): error C2664",
            "failed_files": ["src/gallium/drivers/d3d12/d3d12_video_enc.cpp"],
        },
    )
    _write_result(
        art1,
        "main_msvc",
        {
            "cell_id": "directx-headers:release:main:msvc",
            "target": "directx-headers",
            "target_name": "DirectX-Headers",
            "channel": "release",
            "severity": "critical",
            "mesa_branch": "main",
            "mesa_sha": "aaaa1111",
            "dep_ref": "v1.620.1",
            "dep_sha": "a2b1aa9f",
            "dep_resolved_version": "1.620.1",
            "toolchain": "msvc",
            "toolchain_label": "Win MSVC",
            "status": "PASS",
            "error_stage": None,
            "duration_sec": 115.0,
            "run_url": "https://github.com/example/mesa-workflow/actions/runs/1",
            "error_summary": None,
            "error_excerpt": "",
            "failed_files": [],
        },
    )

    out1 = process_report(
        repo,
        global_cfg,
        targets,
        artifacts_dir=art1,
        snapshot_path=snap,
        repo_slug="example/mesa-workflow",
        issues_client=fake_gh,
        now=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
    )
    assert out1["readme_changed"] is True
    assert len(fake_gh.issues) == 1
    assert fake_gh.issues[1]["state"] == "open"
    assert "priority:high" in fake_gh.issues[1]["labels"]
    assert "likely missing backport" in fake_gh.issues[1]["body"]

    readme_text = (repo / "README.md").read_text(encoding="utf-8")
    assert "🔴 FAIL" in readme_text
    assert "🟢 PASS" in readme_text
    assert "[#1]" in readme_text

    # 2. Same failure re-reported -> no duplicate issue, no comment spam
    process_report(
        repo,
        global_cfg,
        targets,
        artifacts_dir=art1,
        snapshot_path=snap,
        repo_slug="example/mesa-workflow",
        issues_client=fake_gh,
        now=datetime(2026, 11, 20, 18, 0, tzinfo=UTC),
    )
    assert len(fake_gh.issues) == 1
    assert len(fake_gh.comments[1]) == 0

    # 3. FAIL -> PASS auto-closes issue with resolution comment
    art2 = tmp_path / "art2"
    _write_result(
        art2,
        "msvc_fixed",
        {
            "cell_id": "directx-headers:release:staging/26.2:msvc",
            "target": "directx-headers",
            "target_name": "DirectX-Headers",
            "channel": "release",
            "severity": "critical",
            "mesa_branch": "staging/26.2",
            "mesa_sha": "cccc3333",
            "dep_ref": "v1.620.1",
            "dep_sha": "a2b1aa9f",
            "dep_resolved_version": "1.620.1",
            "toolchain": "msvc",
            "toolchain_label": "Win MSVC",
            "status": "PASS",
            "error_stage": None,
            "duration_sec": 110.0,
            "run_url": "https://github.com/example/mesa-workflow/actions/runs/2",
            "error_summary": None,
            "error_excerpt": "",
            "failed_files": [],
        },
    )
    out3 = process_report(
        repo,
        global_cfg,
        targets,
        artifacts_dir=art2,
        snapshot_path=snap,
        repo_slug="example/mesa-workflow",
        issues_client=fake_gh,
        now=datetime(2026, 11, 21, 0, 0, tzinfo=UTC),
    )
    assert "closed #1" in out3["issue_actions"][0]
    assert fake_gh.issues[1]["state"] == "closed"
    assert len(fake_gh.comments[1]) == 1
    assert "Resolved" in fake_gh.comments[1][0]
