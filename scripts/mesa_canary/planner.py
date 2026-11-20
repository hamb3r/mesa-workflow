"""Diff-driven build matrix planner.

Schedules builds strictly when needed:
  1. New upstream dependency release / preview / main commit (filtered by relevant_paths + cooldown).
  2. Newly discovered active Mesa staging branch.
  3. Recovery re-check (FAIL -> PASS): only failing cells whose Mesa branch tip advanced.
  4. Missing cells (e.g. initial bootstrap or newly added toolchain variant).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mesa_canary.config import ChannelConfig, GlobalConfig, TargetConfig, ToolchainConfig
from mesa_canary.upstream import ChannelSnapshot, MesaSnapshot, TargetSnapshot


def cell_key(target_id: str, channel: str, mesa_branch: str, toolchain_id: str) -> str:
    return f"{target_id}:{channel}:{mesa_branch}:{toolchain_id}"


def make_artifact_slug(cell_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", cell_id).strip("_")


@dataclass(frozen=True)
class MatrixJob:
    cell_id: str
    artifact_slug: str
    target_id: str
    workflow: str
    channel: str
    severity: str
    mesa_ref: str
    mesa_sha: str
    dep_ref: str
    dep_sha: str
    toolchain: str
    toolchain_label: str
    os: str
    kind: str
    runner: str
    msys2_sys: str
    msys2_prefix: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PlanResult:
    jobs: tuple[MatrixJob, ...]
    needs_keepalive: bool
    reasons_summary: tuple[str, ...]

    @property
    def has_work(self) -> bool:
        return len(self.jobs) > 0

    def jobs_for_workflow(self, workflow_name: str) -> list[dict[str, Any]]:
        return [j.to_dict() for j in self.jobs if j.workflow == workflow_name]


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _paths_match_relevant(
    changed_paths: tuple[str, ...] | None,
    relevant_paths: tuple[str, ...],
) -> bool:
    if not relevant_paths:
        return True
    if changed_paths is None:
        # Compare API unavailable or truncated: conservatively treat as relevant.
        return True
    for changed in changed_paths:
        norm = changed.lstrip("./")
        for prefix in relevant_paths:
            p = prefix.lstrip("./")
            if p.endswith("/") and norm.startswith(p):
                return True
            if norm == p:
                return True
    return False


def _within_cooldown(tested_at: str | None, min_interval_hours: int, now: datetime) -> bool:
    if min_interval_hours <= 0:
        return False
    prev_dt = _parse_iso(tested_at)
    if prev_dt is None:
        return False
    return (now - prev_dt) < timedelta(hours=min_interval_hours)


def _make_job(
    global_cfg: GlobalConfig,
    target: TargetConfig,
    tc: ToolchainConfig,
    ch: ChannelSnapshot,
    mesa_ref: str,
    mesa_sha: str,
    reason: str,
    runner_provider: str | None = None,
) -> MatrixJob:
    cid = cell_key(target.id, ch.channel, mesa_ref, tc.id)
    return MatrixJob(
        cell_id=cid,
        artifact_slug=make_artifact_slug(cid),
        target_id=target.id,
        workflow=target.workflow,
        channel=ch.channel,
        severity=ch.severity,
        mesa_ref=mesa_ref,
        mesa_sha=mesa_sha,
        dep_ref=ch.ref,
        dep_sha=ch.sha,
        toolchain=tc.id,
        toolchain_label=tc.label,
        os=tc.os,
        kind=tc.kind,
        runner=global_cfg.runners.resolve(tc.os, provider_override=runner_provider),
        msys2_sys=tc.msys2_sys or "",
        msys2_prefix=tc.msys2_prefix or "",
        reason=reason,
    )


def plan_runs(
    global_cfg: GlobalConfig,
    targets: dict[str, TargetConfig],
    mesa_snap: MesaSnapshot,
    target_snaps: dict[str, TargetSnapshot],
    state: dict[str, Any],
    status: dict[str, Any],
    *,
    now: datetime | None = None,
    force_all: bool = False,
    channel_filter: set[str] | None = None,
    toolchain_filter: set[str] | None = None,
    runner_provider: str | None = None,
) -> PlanResult:
    """Compute the minimal set of matrix jobs to run."""
    now_dt = now or datetime.now(UTC)
    prev_branches = set((state.get("mesa_branches") or {}).get("active") or [])
    new_branches = set(mesa_snap.active_branches) - prev_branches
    existing_cells: dict[str, dict[str, Any]] = status.get("cells") or {}

    jobs: list[MatrixJob] = []
    seen_ids: set[str] = set()
    reasons: list[str] = []

    def add_job(job: MatrixJob) -> None:
        if job.cell_id not in seen_ids:
            seen_ids.add(job.cell_id)
            jobs.append(job)

    for target_id, target in targets.items():
        snap = target_snaps.get(target_id)
        if not snap:
            continue
        prev_target_channels = (
            ((state.get("targets") or {}).get(target_id) or {}).get("channels") or {}
        )

        # Deduplicate branch channels (e.g. `main`) when their commit SHA is already covered
        # by an official tag channel (e.g. `release` tagged directly on `main` HEAD).
        covered_tag_shas: dict[str, str] = {}
        for ch_name, ch_snap in snap.channels.items():
            ch_cfg = target.upstream.channels[ch_name]
            if ch_cfg.kind in {"semver_stable", "semver_prerelease"} and ch_snap.sha:
                covered_tag_shas[ch_snap.sha] = ch_name

        for ch_name, ch_snap in snap.channels.items():
            if channel_filter and ch_name not in channel_filter:
                continue
            ch_cfg: ChannelConfig = target.upstream.channels[ch_name]
            if (
                not force_all
                and ch_cfg.kind == "branch"
                and ch_snap.sha in covered_tag_shas
                and (not channel_filter or covered_tag_shas[ch_snap.sha] in channel_filter)
            ):
                alias_ch = covered_tag_shas[ch_snap.sha]
                reasons.append(
                    f"Dedup {target_id}:{ch_name} ({ch_snap.sha[:8]}): identical commit to '{alias_ch}'"
                )
                continue
            prev_ch = prev_target_channels.get(ch_name) or {}
            prev_ref = prev_ch.get("ref")
            prev_sha = prev_ch.get("sha")
            prev_tested_at = prev_ch.get("tested_at")

            dep_changed = (prev_ref != ch_snap.ref) or (prev_sha != ch_snap.sha)
            dep_trigger_allowed = False
            dep_reason = ""

            if force_all:
                dep_trigger_allowed = True
                dep_reason = f"forced:{ch_name}:{ch_snap.ref}"
            elif dep_changed:
                if ch_cfg.kind == "branch" and prev_sha:
                    if not _paths_match_relevant(ch_snap.changed_paths, ch_cfg.relevant_paths):
                        reasons.append(
                            f"Skip {target_id}:{ch_name} ({ch_snap.sha[:8]}): no changes in relevant_paths"
                        )
                    elif _within_cooldown(prev_tested_at, ch_cfg.min_interval_hours, now_dt):
                        reasons.append(
                            f"Defer {target_id}:{ch_name} ({ch_snap.sha[:8]}): within {ch_cfg.min_interval_hours}h cooldown"
                        )
                    else:
                        dep_trigger_allowed = True
                        dep_reason = f"dep-updated:{ch_snap.ref}@{ch_snap.sha[:8]}"
                else:
                    dep_trigger_allowed = True
                    dep_reason = f"dep-updated:{ch_snap.ref}"

            if dep_trigger_allowed:
                reasons.append(f"Trigger all cells for {target_id}:{ch_name} ({dep_reason})")

            for mesa_branch in mesa_snap.active_branches:
                mesa_sha = mesa_snap.heads[mesa_branch]
                is_new_branch = mesa_branch in new_branches
                for tc in target.toolchains:
                    if toolchain_filter and tc.id not in toolchain_filter:
                        continue
                    cid = cell_key(target_id, ch_name, mesa_branch, tc.id)
                    existing = existing_cells.get(cid)

                    if dep_trigger_allowed:
                        add_job(
                            _make_job(
                                global_cfg,
                                target,
                                tc,
                                ch_snap,
                                mesa_branch,
                                mesa_sha,
                                dep_reason,
                                runner_provider,
                            )
                        )
                    elif is_new_branch:
                        add_job(
                            _make_job(
                                global_cfg,
                                target,
                                tc,
                                ch_snap,
                                mesa_branch,
                                mesa_sha,
                                f"new-mesa-branch:{mesa_branch}",
                                runner_provider,
                            )
                        )
                    elif existing is None:
                        add_job(
                            _make_job(
                                global_cfg,
                                target,
                                tc,
                                ch_snap,
                                mesa_branch,
                                mesa_sha,
                                "initial-cell",
                                runner_provider,
                            )
                        )
                    elif existing.get("status") == "FAIL" and existing.get("mesa_sha") != mesa_sha:
                        add_job(
                            _make_job(
                                global_cfg,
                                target,
                                tc,
                                ch_snap,
                                mesa_branch,
                                mesa_sha,
                                f"recovery-check:{mesa_branch}@{mesa_sha[:8]}",
                                runner_provider,
                            )
                        )

    # Check 60-day GitHub Actions cron keepalive policy.
    needs_keepalive = False
    last_hb = _parse_iso(state.get("last_heartbeat_at"))
    if last_hb is None or (now_dt - last_hb) >= timedelta(days=global_cfg.keepalive_days):
        needs_keepalive = True

    return PlanResult(
        jobs=tuple(jobs),
        needs_keepalive=needs_keepalive,
        reasons_summary=tuple(reasons),
    )


def build_manual_matrix(
    global_cfg: GlobalConfig,
    target: TargetConfig,
    mesa_refs: list[tuple[str, str]],
    dep_ref: str,
    dep_sha: str,
    channel: str = "manual",
    severity: str = "critical",
    toolchain_ids: list[str] | None = None,
    runner_provider: str | None = None,
) -> list[MatrixJob]:
    """Build an explicit matrix for workflow_dispatch (e.g. smoke-testing #16936)."""
    selected_tcs = (
        [target.get_toolchain(tid) for tid in toolchain_ids]
        if toolchain_ids
        else list(target.toolchains)
    )
    ch_snap = ChannelSnapshot(
        channel=channel,
        ref=dep_ref,
        sha=dep_sha,
        severity=severity,
    )
    jobs: list[MatrixJob] = []
    for mesa_ref, mesa_sha in mesa_refs:
        for tc in selected_tcs:
            jobs.append(
                _make_job(
                    global_cfg=global_cfg,
                    target=target,
                    tc=tc,
                    ch=ch_snap,
                    mesa_ref=mesa_ref,
                    mesa_sha=mesa_sha,
                    reason=f"manual:{mesa_ref}+{dep_ref}",
                    runner_provider=runner_provider,
                )
            )
    return jobs
