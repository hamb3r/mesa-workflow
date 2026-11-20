"""State persistence, README status dashboard rendering, and GitHub Issue lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mesa_canary.backport import (
    BackportCandidate,
    diagnose_backport_local,
    diagnose_backport_remote,
)
from mesa_canary.config import GlobalConfig, TargetConfig
from mesa_canary.upstream import _http_request

STATUS_START_MARKER = "<!-- STATUS:START -->"
STATUS_END_MARKER = "<!-- STATUS:END -->"

_LABEL_COLORS: dict[str, tuple[str, str]] = {
    "mesa-canary": ("1d76db", "Automated Mesa forward-compatibility canary alert"),
    "priority:high": ("d73a4a", "Failure against an official stable upstream release"),
    "priority:warning": ("fbca04", "Early warning failure against preview or main branch"),
    "backport-candidate": ("0e8a16", "Probable fix commit found in Mesa main"),
    "channel:release": ("b60205", "Stable upstream release channel"),
    "channel:preview": ("d93f0b", "Upstream preview/prerelease channel"),
    "channel:main": ("e99695", "Upstream main branch canary channel"),
}


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return json.loads(json.dumps(default))
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def issue_group_key(target_id: str, channel: str, mesa_branch: str) -> str:
    return f"{target_id}:{channel}:{mesa_branch}"


def _issue_marker(key: str) -> str:
    return f"<!-- mesa-canary-key: {key} -->"


class GitHubIssuesClient:
    """Minimal GitHub Issues REST API client scoped strictly to THIS repository."""

    def __init__(
        self,
        repo: str,
        token: str | None = None,
        api_base: str = "https://api.github.com",
    ) -> None:
        self.repo = repo
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.api_base = api_base.rstrip("/")
        self._known_labels: set[str] | None = None

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if not self.token:
            raise RuntimeError("GITHUB_TOKEN is required to manage GitHub Issues")
        url = f"{self.api_base}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        raw, _ = _http_request(url, headers=headers, method=method, body=body)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def ensure_labels(self, labels: list[str]) -> None:
        if self._known_labels is None:
            existing = self._request("GET", f"/repos/{self.repo}/labels?per_page=100")
            self._known_labels = {str(item["name"]) for item in existing} if isinstance(existing, list) else set()
        for label in labels:
            if label in self._known_labels:
                continue
            color, desc = _LABEL_COLORS.get(label, ("5319e7", "Mesa Canary CI label"))
            try:
                self._request(
                    "POST",
                    f"/repos/{self.repo}/labels",
                    {"name": label, "color": color, "description": desc},
                )
                self._known_labels.add(label)
            except Exception:
                # Ignore race or already-exists errors.
                self._known_labels.add(label)

    def find_open_issue_by_key(self, key: str, base_label: str) -> dict[str, Any] | None:
        marker = _issue_marker(key)
        encoded_label = urllib.parse.quote(base_label, safe="")
        issues = self._request(
            "GET",
            f"/repos/{self.repo}/issues?state=open&labels={encoded_label}&per_page=100",
        )
        if not isinstance(issues, list):
            return None
        for issue in issues:
            if marker in str(issue.get("body") or ""):
                return issue
        return None

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        self.ensure_labels(labels)
        return self._request(
            "POST",
            f"/repos/{self.repo}/issues",
            {"title": title, "body": body, "labels": labels},
        )

    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        if labels is not None:
            self.ensure_labels(labels)
            payload["labels"] = labels
        return self._request("PATCH", f"/repos/{self.repo}/issues/{number}", payload)

    def add_comment(self, number: int, body: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{self.repo}/issues/{number}/comments",
            {"body": body},
        )


def _group_fingerprint(
    cells: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    payload = {
        "cells": [
            {
                "tc": c.get("toolchain"),
                "status": c.get("status"),
                "dep_ref": c.get("dep_ref"),
                "dep_sha": (c.get("dep_sha") or "")[:12],
                "error": c.get("error_summary"),
            }
            for c in sorted(cells, key=lambda x: str(x.get("toolchain", "")))
        ],
        "candidates": [
            {
                "sha": cand.get("sha"),
                "cp": cand.get("cherry_pick_status"),
            }
            for cand in candidates
        ],
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _render_issue_title(
    target: TargetConfig,
    channel: str,
    severity: str,
    dep_ref: str,
    mesa_branch: str,
    failed_count: int,
    total_count: int,
) -> str:
    prefix = f"[{target.name} {dep_ref}]" if severity == "critical" else f"[Canary:{channel} {target.name} {dep_ref}]"
    return f"{prefix} Build failure on Mesa `{mesa_branch}` ({failed_count}/{total_count} toolchains)"


def _render_issue_body(
    key: str,
    target: TargetConfig,
    channel: str,
    severity: str,
    mesa_branch: str,
    cells: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    main_passing: bool | None,
) -> str:
    failing = [c for c in cells if c.get("status") == "FAIL"]
    sample = failing[0] if failing else cells[0]
    dep_ref = sample.get("dep_ref", "unknown")
    dep_sha = (sample.get("dep_sha") or "")[:12]
    mesa_sha = (sample.get("mesa_sha") or "")[:12]
    dep_ver = sample.get("dep_resolved_version") or dep_ref

    sev_badge = "🔴 CRITICAL (official release)" if severity == "critical" else f"🟡 WARNING ({channel} canary)"
    lines = [
        _issue_marker(key),
        f"## {target.name} Forward-Compatibility Failure on Mesa `{mesa_branch}`",
        "",
        f"- **Severity:** {sev_badge}",
        f"- **Dependency:** `{target.upstream.repo}` @ `{dep_ref}` (`{dep_sha}`, resolved version `{dep_ver}`)",
        f"- **Mesa branch:** `{mesa_branch}` @ `{mesa_sha}`",
        f"- **Failing toolchains:** {len(failing)} / {len(cells)}",
    ]
    if mesa_branch != "main" and main_passing is not None:
        lines.append(
            f"- **Mesa `main` status on same dependency:** {'🟢 PASS (likely missing backport!)' if main_passing else '🔴 ALSO FAILING'}"
        )

    lines.extend([
        "",
        "### Toolchain Matrix",
        "| Toolchain | Status | Stage | Duration | Run |",
        "| --- | --- | --- | ---: | --- |",
    ])
    for c in sorted(cells, key=lambda x: str(x.get("toolchain", ""))):
        st = "🟢 PASS" if c.get("status") == "PASS" else ("🔴 FAIL" if severity == "critical" else "🟡 FAIL")
        stage = c.get("error_stage") or "—"
        dur = f"{c.get('duration_sec', 0)}s"
        run_url = c.get("run_url")
        run_md = f"[logs]({run_url})" if run_url else "—"
        lines.append(f"| `{c.get('toolchain')}` ({c.get('toolchain_label', '')}) | {st} | {stage} | {dur} | {run_md} |")

    # Aggregate unique failed files across failing toolchains
    all_files: list[str] = []
    for c in failing:
        for f in c.get("failed_files") or []:
            if f not in all_files:
                all_files.append(f)

    if all_files:
        lines.extend(["", "### Affected Mesa Source Files"])
        for f in all_files[:15]:
            lines.append(f"- `{f}`")

    if candidates:
        lines.extend([
            "",
            "### Candidate Backport Commits Found in `main`",
            "| Commit | Cherry-Pick | MR | Subject | Matched Files |",
            "| --- | --- | --- | --- | --- |",
        ])
        for cand in candidates:
            cp = cand.get("cherry_pick_status", "SKIPPED")
            cp_badge = {
                "CLEAN": "✅ CLEAN",
                "CONFLICT": "⚠️ CONFLICT",
                "ALREADY_APPLIED": "ℹ️ ALREADY_APPLIED",
            }.get(cp, cp)
            mr = cand.get("mr_url")
            mr_md = f"[MR]({mr})" if mr else "—"
            files_md = ", ".join(f"`{f}`" for f in (cand.get("matched_files") or [])[:3])
            subj = str(cand.get("subject", "")).replace("|", "\\|")
            lines.append(f"| `{cand.get('short_sha')}` | {cp_badge} | {mr_md} | {subj} | {files_md} |")
            if cand.get("conflict_files"):
                cfiles = ", ".join(f"`{f}`" for f in cand["conflict_files"][:5])
                lines.append(f"  - Conflict files for `{cand.get('short_sha')}`: {cfiles}")

    lines.extend(["", "### Compiler / Build Error Excerpts"])
    for c in failing[:3]:
        excerpt = (c.get("error_excerpt") or c.get("error_summary") or "No excerpt captured").strip()
        lines.extend([
            f"<details><summary><code>{c.get('toolchain')}</code> — {str(c.get('error_summary') or '')[:120]}</summary>",
            "",
            "```text",
            excerpt[:2500],
            "```",
            "</details>",
            "",
        ])

    lines.append(
        "> *Automated report inside this repository only. Upstream Mesa (`gitlab.freedesktop.org/mesa/mesa`) is never contacted automatically.*"
    )
    return "\n".join(lines) + "\n"


def render_readme_section(
    targets: dict[str, TargetConfig],
    state: dict[str, Any],
    status: dict[str, Any],
    repo_slug: str | None = None,
) -> str:
    """Render the Markdown dashboard inserted between STATUS markers in README.md."""
    updated = status.get("updated_at") or state.get("last_heartbeat_at") or "not yet run"
    active_branches: list[str] = list((state.get("mesa_branches") or {}).get("active") or [])
    branch_heads: dict[str, str] = dict((state.get("mesa_branches") or {}).get("heads") or {})
    cells: dict[str, dict[str, Any]] = status.get("cells") or {}
    issues: dict[str, dict[str, Any]] = status.get("issues") or {}

    lines: list[str] = [
        f"_Last updated: **{updated}**_",
        "",
    ]

    # 1. Tracked upstream overview
    lines.extend([
        "### Tracked Upstream Versions",
        "",
        "| Dependency | Channel | Severity | Latest Ref | Commit | Last Tested |",
        "| --- | --- | --- | --- | --- | --- |",
    ])
    any_channel_rows = False
    for target_id, target in targets.items():
        ch_state = ((state.get("targets") or {}).get(target_id) or {}).get("channels") or {}
        for ch_name, ch_cfg in target.upstream.channels.items():
            info = ch_state.get(ch_name) or {}
            ref = info.get("ref") or "—"
            sha = (info.get("sha") or "")[:10] or "—"
            tested = info.get("tested_at") or "—"
            sev = "🔴 critical" if ch_cfg.severity == "critical" else "🟡 warning"
            lines.append(
                f"| **{target.name}** (`{target.upstream.repo}`) | `{ch_name}` | {sev} | `{ref}` | `{sha}` | {tested} |"
            )
            any_channel_rows = True
    if not any_channel_rows:
        lines.append("| — | — | — | — | — | — |")

    # 2. Per-target, per-channel status matrices
    active_failures: list[tuple[str, TargetConfig, str, str, list[dict[str, Any]]]] = []

    for target_id, target in targets.items():
        ch_state = ((state.get("targets") or {}).get(target_id) or {}).get("channels") or {}
        for ch_name, ch_cfg in target.upstream.channels.items():
            ref = (ch_state.get(ch_name) or {}).get("ref") or "untested"
            sev_icon = "🔴" if ch_cfg.severity == "critical" else "🟡"
            lines.extend([
                "",
                f"### {target.name} — `{ch_name}` (`{ref}`) {sev_icon}",
                "",
            ])
            header = ["Mesa Branch", "HEAD", *[tc.label for tc in target.toolchains], "Issue"]
            lines.append("| " + " | ".join(header) + " |")
            lines.append("| " + " | ".join(["---"] * len(header)) + " |")

            branches_to_show = active_branches or sorted(
                {
                    str(c.get("mesa_branch"))
                    for c in cells.values()
                    if c.get("target") == target_id and c.get("channel") == ch_name
                }
            )
            if not branches_to_show:
                lines.append("| _(awaiting first run)_ | — | " + " | ".join(["⚪"] * len(target.toolchains)) + " | — |")
                continue

            for branch in branches_to_show:
                head_short = (branch_heads.get(branch) or "")[:8] or "—"
                row = [f"`{branch}`", f"`{head_short}`"]
                branch_cells: list[dict[str, Any]] = []
                has_fail = False
                for tc in target.toolchains:
                    cid = f"{target_id}:{ch_name}:{branch}:{tc.id}"
                    cell = cells.get(cid)
                    if not cell:
                        row.append("⚪")
                        continue
                    branch_cells.append(cell)
                    st = cell.get("status")
                    run_url = cell.get("run_url")
                    if st == "PASS":
                        badge = "🟢 PASS"
                    else:
                        has_fail = True
                        badge = "🔴 FAIL" if ch_cfg.severity == "critical" else "🟡 FAIL"
                    row.append(f"[{badge}]({run_url})" if run_url else badge)

                ikey = issue_group_key(target_id, ch_name, branch)
                issue_info = issues.get(ikey)
                if issue_info and issue_info.get("state") == "open" and issue_info.get("number"):
                    num = issue_info["number"]
                    url = issue_info.get("url") or (
                        f"https://github.com/{repo_slug}/issues/{num}" if repo_slug else ""
                    )
                    row.append(f"[#{num}]({url})" if url else f"#{num}")
                else:
                    row.append("—")

                lines.append("| " + " | ".join(row) + " |")
                if has_fail:
                    active_failures.append((ikey, target, ch_name, branch, branch_cells))

    # 3. Active failures & backport candidates summary
    if active_failures:
        lines.extend(["", "### Active Failures & Backport Candidates", ""])
        for ikey, target, ch_name, branch, branch_cells in active_failures:
            failing = [c for c in branch_cells if c.get("status") == "FAIL"]
            first = failing[0]
            issue_info = issues.get(ikey) or {}
            candidates = issue_info.get("backport_candidates") or first.get("backport_candidates") or []
            lines.append(
                f"- **`{target.name}` ({ch_name}: `{first.get('dep_ref')}`) on Mesa `{branch}`** — "
                f"{len(failing)}/{len(branch_cells)} failed: `{first.get('error_summary') or 'see logs'}`"
            )
            for cand in candidates[:3]:
                cp = cand.get("cherry_pick_status", "SKIPPED")
                mr_part = f" ([MR]({cand['mr_url']}))" if cand.get("mr_url") else ""
                lines.append(
                    f"  - Candidate fix in `main`: `{cand.get('short_sha')}`{mr_part} — **{cp}** — {cand.get('subject')}"
                )

    return "\n".join(lines).strip() + "\n"


def update_readme_file(readme_path: Path, rendered_section: str) -> bool:
    """Replace content between STATUS_START_MARKER and STATUS_END_MARKER in README.md."""
    text = readme_path.read_text(encoding="utf-8")
    start_idx = text.find(STATUS_START_MARKER)
    end_idx = text.find(STATUS_END_MARKER)
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        raise ValueError(
            f"{readme_path} must contain {STATUS_START_MARKER} and {STATUS_END_MARKER} markers"
        )
    before = text[: start_idx + len(STATUS_START_MARKER)]
    after = text[end_idx:]
    updated = f"{before}\n\n{rendered_section}\n{after}"
    if updated == text:
        return False
    readme_path.write_text(updated, encoding="utf-8")
    return True


def collect_result_artifacts(artifacts_dir: Path) -> list[dict[str, Any]]:
    """Recursively load all result.json files under artifacts_dir."""
    if not artifacts_dir.exists():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(artifacts_dir.rglob("result.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "cell_id" in data:
                results.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return results


def process_report(
    repo_root: Path,
    global_cfg: GlobalConfig,
    targets: dict[str, TargetConfig],
    *,
    artifacts_dir: Path,
    snapshot_path: Path | None = None,
    repo_slug: str | None = None,
    sync_issues: bool = True,
    issues_client: GitHubIssuesClient | None = None,
    local_mesa_repo: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Merge build results, run backport diagnostics, update state/README, and sync Issues."""
    now_iso = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path = repo_root / "state" / "state.json"
    status_path = repo_root / "state" / "status.json"
    readme_path = repo_root / "README.md"

    state = load_json(
        state_path,
        {"schema_version": 1, "last_heartbeat_at": None, "mesa_branches": {"active": [], "heads": {}}, "targets": {}},
    )
    status = load_json(
        status_path,
        {"schema_version": 1, "updated_at": None, "cells": {}, "issues": {}},
    )

    state["last_heartbeat_at"] = now_iso

    # Apply upstream snapshot (active Mesa branches + latest dependency channel refs).
    snapshot_data: dict[str, Any] = {}
    if snapshot_path and snapshot_path.is_file():
        snapshot_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if snapshot_data.get("mesa"):
            state["mesa_branches"] = {
                "active": snapshot_data["mesa"].get("active_branches", []),
                "heads": snapshot_data["mesa"].get("heads", {}),
            }

    results = collect_result_artifacts(artifacts_dir)
    touched_groups: set[tuple[str, str, str]] = set()
    cells_map: dict[str, dict[str, Any]] = status.setdefault("cells", {})
    issues_map: dict[str, dict[str, Any]] = status.setdefault("issues", {})

    # Track tested channels from actual build results so deferred/skipped channels don't falsely advance state.
    tested_channel_meta: dict[tuple[str, str], tuple[str, str]] = {}

    for res in results:
        cid = str(res["cell_id"])
        cells_map[cid] = res
        t_id = str(res["target"])
        ch = str(res["channel"])
        branch = str(res["mesa_branch"])
        touched_groups.add((t_id, ch, branch))
        if res.get("dep_ref") and res.get("dep_sha"):
            tested_channel_meta[(t_id, ch)] = (str(res["dep_ref"]), str(res["dep_sha"]))

    # Mirror deduplicated branch channels (e.g. main == release SHA) so state & dashboard stay complete.
    snap_targets = snapshot_data.get("targets") or {}
    for t_id, target in targets.items():
        snap_channels = snap_targets.get(t_id) or {}
        for ch_name, ch_cfg in target.upstream.channels.items():
            if ch_cfg.kind != "branch" or (t_id, ch_name) in tested_channel_meta:
                continue
            ch_info = snap_channels.get(ch_name) or {}
            ch_sha = str(ch_info.get("sha") or "")
            if not ch_sha:
                continue
            source_ch = next(
                (
                    other_ch
                    for (other_tid, other_ch), (_, other_sha) in tested_channel_meta.items()
                    if other_tid == t_id and other_sha == ch_sha
                ),
                None,
            )
            if not source_ch:
                continue
            tested_channel_meta[(t_id, ch_name)] = (str(ch_info.get("ref") or ch_name), ch_sha)
            for res in results:
                if res.get("target") == t_id and res.get("channel") == source_ch:
                    mirrored_cid = f"{t_id}:{ch_name}:{res['mesa_branch']}:{res['toolchain']}"
                    mirrored = {
                        **res,
                        "cell_id": mirrored_cid,
                        "channel": ch_name,
                        "severity": ch_cfg.severity,
                        "dep_ref": str(ch_info.get("ref") or ch_name),
                        "mirrored_from": source_ch,
                    }
                    cells_map[mirrored_cid] = mirrored

    for (t_id, ch), (dep_ref, dep_sha) in tested_channel_meta.items():
        t_entry = state.setdefault("targets", {}).setdefault(t_id, {}).setdefault("channels", {})
        t_entry[ch] = {
            "ref": dep_ref,
            "sha": dep_sha,
            "tested_at": now_iso,
        }

    if results:
        status["updated_at"] = now_iso

    # Stage 2: Backport diagnostics + GitHub Issue lifecycle per touched (target, channel, mesa_branch)
    effective_repo = repo_slug or os.environ.get("GITHUB_REPOSITORY")
    gh_client = issues_client
    if sync_issues and global_cfg.issues.enabled and gh_client is None and effective_repo and os.environ.get("GITHUB_TOKEN"):
        gh_client = GitHubIssuesClient(effective_repo)

    issue_actions: list[str] = []

    for t_id, ch, branch in sorted(touched_groups):
        target = targets.get(t_id)
        if not target or ch == "manual":
            continue
        group_cells = [
            c
            for c in cells_map.values()
            if c.get("target") == t_id and c.get("channel") == ch and c.get("mesa_branch") == branch
        ]
        if not group_cells:
            continue

        failing = [c for c in group_cells if c.get("status") == "FAIL"]
        ikey = issue_group_key(t_id, ch, branch)
        severity = target.upstream.channels[ch].severity if ch in target.upstream.channels else "warning"

        # Check whether Mesa `main` passed for the same (target, channel)
        main_cells = [
            c
            for c in cells_map.values()
            if c.get("target") == t_id and c.get("channel") == ch and c.get("mesa_branch") == "main"
        ]
        main_passing: bool | None = None
        if main_cells:
            main_passing = all(c.get("status") == "PASS" for c in main_cells)

        # Backport diagnosis when a non-main branch fails
        candidates_dicts: list[dict[str, Any]] = []
        if failing and branch != "main" and global_cfg.backport.enabled:
            failed_files: list[str] = []
            for fc in failing:
                for f in fc.get("failed_files") or []:
                    if f not in failed_files:
                        failed_files.append(f)
            if failed_files:
                cands: list[BackportCandidate]
                if local_mesa_repo:
                    cands = diagnose_backport_local(
                        local_mesa_repo,
                        failing_ref=branch,
                        failed_files=failed_files,
                        max_candidates=global_cfg.backport.max_candidates_per_failure,
                        test_cherry_pick=global_cfg.backport.test_cherry_pick,
                    )
                else:
                    cands = diagnose_backport_remote(
                        global_cfg.mesa,
                        global_cfg.backport,
                        failing_ref=branch,
                        failed_files=failed_files,
                    )
                candidates_dicts = [c.to_dict() for c in cands]
                for fc in failing:
                    fc["backport_candidates"] = candidates_dicts

        prev_issue = issues_map.get(ikey) or {}
        fp = _group_fingerprint(group_cells, candidates_dicts)

        if failing:
            sample = failing[0]
            dep_ref = str(sample.get("dep_ref", "unknown"))
            title = _render_issue_title(
                target, ch, severity, dep_ref, branch, len(failing), len(group_cells)
            )
            body = _render_issue_body(
                ikey, target, ch, severity, branch, group_cells, candidates_dicts, main_passing
            )
            labels = [
                global_cfg.issues.base_label,
                f"target:{t_id}",
                f"channel:{ch}",
                "priority:high" if severity == "critical" else "priority:warning",
            ]
            if candidates_dicts:
                labels.append("backport-candidate")

            if gh_client is not None:
                open_issue = None
                if prev_issue.get("state") == "open" and prev_issue.get("number"):
                    open_issue = {"number": prev_issue["number"], "html_url": prev_issue.get("url")}
                else:
                    open_issue = gh_client.find_open_issue_by_key(ikey, global_cfg.issues.base_label)

                if open_issue is None:
                    created = gh_client.create_issue(title=title, body=body, labels=labels)
                    issues_map[ikey] = {
                        "number": created["number"],
                        "url": created.get("html_url", ""),
                        "state": "open",
                        "fingerprint": fp,
                        "backport_candidates": candidates_dicts,
                        "updated_at": now_iso,
                    }
                    issue_actions.append(f"opened #{created['number']} for {ikey}")
                else:
                    num = int(open_issue["number"])
                    if prev_issue.get("fingerprint") != fp:
                        updated = gh_client.update_issue(num, title=title, body=body, labels=labels)
                        gh_client.add_comment(
                            num,
                            f"Updated canary status ({now_iso}): `{dep_ref}` — "
                            f"{len(failing)}/{len(group_cells)} toolchains failing on `{branch}`"
                            + (
                                f"; {len(candidates_dicts)} backport candidate(s) identified in `main`."
                                if candidates_dicts
                                else "."
                            ),
                        )
                        issues_map[ikey] = {
                            "number": num,
                            "url": updated.get("html_url", open_issue.get("html_url", "")),
                            "state": "open",
                            "fingerprint": fp,
                            "backport_candidates": candidates_dicts,
                            "updated_at": now_iso,
                        }
                        issue_actions.append(f"updated #{num} for {ikey}")
                    else:
                        issues_map[ikey] = {
                            **prev_issue,
                            "number": num,
                            "state": "open",
                            "backport_candidates": candidates_dicts,
                        }
            else:
                issues_map[ikey] = {
                    **prev_issue,
                    "state": prev_issue.get("state", "pending"),
                    "fingerprint": fp,
                    "backport_candidates": candidates_dicts,
                    "updated_at": now_iso,
                }
        else:
            # All toolchains in this group are PASS -> auto-close open issue if present!
            if gh_client is not None:
                open_issue = None
                if prev_issue.get("state") == "open" and prev_issue.get("number"):
                    open_issue = {"number": prev_issue["number"], "html_url": prev_issue.get("url")}
                else:
                    open_issue = gh_client.find_open_issue_by_key(ikey, global_cfg.issues.base_label)

                if open_issue is not None:
                    num = int(open_issue["number"])
                    sample = group_cells[0]
                    mesa_sha = (sample.get("mesa_sha") or "")[:12]
                    dep_ref = sample.get("dep_ref", "")
                    run_url = sample.get("run_url", "")
                    run_part = f" ([workflow run]({run_url}))" if run_url else ""
                    gh_client.add_comment(
                        num,
                        f"🟢 **Resolved ({now_iso}):** All {len(group_cells)} toolchains are now passing "
                        f"on Mesa `{branch}` @ `{mesa_sha}` with `{target.name}` `{dep_ref}`{run_part}. "
                        "Closing automatically.",
                    )
                    gh_client.update_issue(num, state="closed")
                    issues_map[ikey] = {
                        "number": num,
                        "url": open_issue.get("html_url", prev_issue.get("url", "")),
                        "state": "closed",
                        "fingerprint": fp,
                        "backport_candidates": [],
                        "updated_at": now_iso,
                    }
                    issue_actions.append(f"closed #{num} for {ikey}")

    save_json(state_path, state)
    save_json(status_path, status)

    readme_changed = False
    if readme_path.is_file():
        section = render_readme_section(targets, state, status, repo_slug=effective_repo)
        readme_changed = update_readme_file(readme_path, section)

    return {
        "results_merged": len(results),
        "readme_changed": readme_changed,
        "issue_actions": issue_actions,
        "updated_at": now_iso,
    }
