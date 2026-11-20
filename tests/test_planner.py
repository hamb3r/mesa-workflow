"""Unit tests for the diff-driven build matrix planner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from mesa_canary.config import load_all_targets, load_global_config
from mesa_canary.planner import build_manual_matrix, cell_key, plan_runs
from mesa_canary.upstream import ChannelSnapshot, MesaSnapshot, TargetSnapshot

REPO_ROOT = Path(__file__).resolve().parent.parent


def _base_fixtures():
    global_cfg = load_global_config(REPO_ROOT)
    targets = load_all_targets(REPO_ROOT)
    mesa_snap = MesaSnapshot(
        active_branches=("main", "staging/26.3", "staging/26.2"),
        heads={"main": "m111", "staging/26.3": "s333", "staging/26.2": "s222"},
    )
    target_snaps = {
        "directx-headers": TargetSnapshot(
            target_id="directx-headers",
            channels={
                "release": ChannelSnapshot("release", "v1.620.2", "dxrel1", "critical"),
                "preview": ChannelSnapshot("preview", "v1.722.1-preview", "dxpre1", "warning"),
                "main": ChannelSnapshot("main", "main", "dxmain1", "warning", changed_paths=("include/directx/d3d12.h",)),
            },
        )
    }
    return global_cfg, targets, mesa_snap, target_snaps


def _full_passing_state_and_status(targets, mesa_snap, target_snaps, now: datetime):
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state = {
        "schema_version": 1,
        "last_heartbeat_at": now_iso,
        "mesa_branches": {
            "active": list(mesa_snap.active_branches),
            "heads": dict(mesa_snap.heads),
        },
        "targets": {
            "directx-headers": {
                "channels": {
                    ch: {"ref": snap.ref, "sha": snap.sha, "tested_at": now_iso}
                    for ch, snap in target_snaps["directx-headers"].channels.items()
                }
            }
        },
    }
    cells = {}
    t = targets["directx-headers"]
    for ch, snap in target_snaps["directx-headers"].channels.items():
        for branch in mesa_snap.active_branches:
            for tc in t.toolchains:
                cid = cell_key(t.id, ch, branch, tc.id)
                cells[cid] = {
                    "cell_id": cid,
                    "target": t.id,
                    "channel": ch,
                    "mesa_branch": branch,
                    "mesa_sha": mesa_snap.heads[branch],
                    "dep_ref": snap.ref,
                    "dep_sha": snap.sha,
                    "toolchain": tc.id,
                    "status": "PASS",
                }
    return state, {"schema_version": 1, "updated_at": now_iso, "cells": cells, "issues": {}}


def test_steady_state_triggers_zero_jobs() -> None:
    global_cfg, targets, mesa_snap, target_snaps = _base_fixtures()
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    state, status = _full_passing_state_and_status(targets, mesa_snap, target_snaps, now)

    plan = plan_runs(global_cfg, targets, mesa_snap, target_snaps, state, status, now=now + timedelta(hours=6))
    assert not plan.has_work
    assert not plan.needs_keepalive


def test_new_release_triggers_only_release_channel() -> None:
    global_cfg, targets, mesa_snap, target_snaps = _base_fixtures()
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    state, status = _full_passing_state_and_status(targets, mesa_snap, target_snaps, now)

    # Bump release to v2.0.0
    updated_snaps = {
        "directx-headers": TargetSnapshot(
            target_id="directx-headers",
            channels={
                **target_snaps["directx-headers"].channels,
                "release": ChannelSnapshot("release", "v2.0.0", "dxrel2", "critical"),
            },
        )
    }
    plan = plan_runs(global_cfg, targets, mesa_snap, updated_snaps, state, status, now=now + timedelta(hours=6))
    assert plan.has_work
    assert len(plan.jobs) == len(mesa_snap.active_branches) * len(targets["directx-headers"].toolchains)
    assert all(j.channel == "release" and j.dep_ref == "v2.0.0" for j in plan.jobs)


def test_main_channel_skips_irrelevant_paths_and_respects_cooldown() -> None:
    global_cfg, targets, mesa_snap, target_snaps = _base_fixtures()
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    state, status = _full_passing_state_and_status(targets, mesa_snap, target_snaps, now)

    # README-only change -> skipped
    readme_only = {
        "directx-headers": TargetSnapshot(
            target_id="directx-headers",
            channels={
                **target_snaps["directx-headers"].channels,
                "main": ChannelSnapshot("main", "main", "dxmain2", "warning", changed_paths=("README.md",)),
            },
        )
    }
    plan1 = plan_runs(global_cfg, targets, mesa_snap, readme_only, state, status, now=now + timedelta(hours=30))
    assert not plan1.has_work

    # Header change within 24h cooldown -> deferred
    header_change = {
        "directx-headers": TargetSnapshot(
            target_id="directx-headers",
            channels={
                **target_snaps["directx-headers"].channels,
                "main": ChannelSnapshot("main", "main", "dxmain2", "warning", changed_paths=("include/directx/d3d12video.h",)),
            },
        )
    }
    plan2 = plan_runs(global_cfg, targets, mesa_snap, header_change, state, status, now=now + timedelta(hours=6))
    assert not plan2.has_work

    # Header change after 24h cooldown -> triggered
    plan3 = plan_runs(global_cfg, targets, mesa_snap, header_change, state, status, now=now + timedelta(hours=25))
    assert plan3.has_work
    assert all(j.channel == "main" for j in plan3.jobs)


def test_main_channel_deduplicated_when_same_sha_as_release() -> None:
    global_cfg, targets, mesa_snap, target_snaps = _base_fixtures()
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    same_sha_snaps = {
        "directx-headers": TargetSnapshot(
            target_id="directx-headers",
            channels={
                "release": ChannelSnapshot("release", "v1.620.2", "same_sha_123", "critical"),
                "preview": ChannelSnapshot("preview", "v1.722.1-preview", "prev_sha_456", "warning"),
                "main": ChannelSnapshot("main", "main", "same_sha_123", "warning"),
            },
        )
    }
    plan = plan_runs(global_cfg, targets, mesa_snap, same_sha_snaps, {}, {}, now=now)
    channels_scheduled = {j.channel for j in plan.jobs}
    assert channels_scheduled == {"release", "preview"}
    assert len(plan.jobs) == 2 * len(mesa_snap.active_branches) * len(targets["directx-headers"].toolchains)


def test_recovery_recheck_only_failing_cell_when_mesa_moves() -> None:
    global_cfg, targets, mesa_snap, target_snaps = _base_fixtures()
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    state, status = _full_passing_state_and_status(targets, mesa_snap, target_snaps, now)

    failing_cid = cell_key("directx-headers", "release", "staging/26.2", "msvc")
    status["cells"][failing_cid]["status"] = "FAIL"

    # Mesa staging/26.2 advances with a fix commit
    advanced_mesa = MesaSnapshot(
        active_branches=mesa_snap.active_branches,
        heads={**mesa_snap.heads, "staging/26.2": "s222_fixed"},
    )
    plan = plan_runs(global_cfg, targets, advanced_mesa, target_snaps, state, status, now=now + timedelta(hours=6))
    assert len(plan.jobs) == 1
    assert plan.jobs[0].cell_id == failing_cid
    assert plan.jobs[0].reason.startswith("recovery-check:")


def test_keepalive_and_runner_provider_switch() -> None:
    global_cfg, targets, mesa_snap, target_snaps = _base_fixtures()
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    state, status = _full_passing_state_and_status(targets, mesa_snap, target_snaps, now)

    plan = plan_runs(
        global_cfg,
        targets,
        mesa_snap,
        target_snaps,
        state,
        status,
        now=now + timedelta(days=31),
    )
    assert not plan.has_work
    assert plan.needs_keepalive

    manual_jobs = build_manual_matrix(
        global_cfg,
        targets["directx-headers"],
        mesa_refs=[("mesa-26.2.7", "deadbeef")],
        dep_ref="v1.620.1",
        dep_sha="a2b1aa9f",
        toolchain_ids=["msvc", "linux-gcc"],
        runner_provider="blacksmith",
    )
    assert len(manual_jobs) == 2
    assert manual_jobs[0].runner.startswith("blacksmith-")
    assert manual_jobs[1].runner.startswith("blacksmith-")
    assert ":" not in manual_jobs[0].artifact_slug


def test_disabled_toolchain_skipped_by_default_but_explicitly_callable() -> None:
    global_cfg, targets, _, _ = _base_fixtures()
    target = targets["directx-headers"]
    default_ids = [tc.id for tc in target.toolchains]
    assert "clang-cl" not in default_ids
    assert len(default_ids) == 6

    all_jobs = build_manual_matrix(
        global_cfg,
        target,
        mesa_refs=[("staging/26.3", "aaa")],
        dep_ref="v1.620.2",
        dep_sha="bbb",
        toolchain_ids=None,
    )
    assert [j.toolchain for j in all_jobs] == default_ids

    explicit_jobs = build_manual_matrix(
        global_cfg,
        target,
        mesa_refs=[("staging/26.3", "aaa")],
        dep_ref="v1.620.2",
        dep_sha="bbb",
        toolchain_ids=["clang-cl"],
    )
    assert [j.toolchain for j in explicit_jobs] == ["clang-cl"]
