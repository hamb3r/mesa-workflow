#!/usr/bin/env python3
"""Command-line interface for Mesa Forward-Compatibility Canary CI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Allow running directly as `python scripts/cli.py` without editable install.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from mesa_canary.backport import diagnose_backport_local, diagnose_backport_remote
from mesa_canary.build_d3d12 import run_build
from mesa_canary.config import load_all_targets, load_global_config
from mesa_canary.log_parser import parse_build_log
from mesa_canary.planner import build_manual_matrix, plan_runs
from mesa_canary.reporter import collect_result_artifacts, load_json, process_report
from mesa_canary.upstream import GitHubUpstreamClient, MesaGitLabClient


def _repo_root(arg_root: str | None) -> Path:
    if arg_root:
        return Path(arg_root).resolve()
    return Path(__file__).resolve().parent.parent


def _write_github_output(outputs: dict[str, str]) -> None:
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if not gh_out:
        return
    with Path(gh_out).open("a", encoding="utf-8") as fh:
        for key, value in outputs.items():
            if "\n" in value:
                delim = "MESA_CANARY_EOF"
                fh.write(f"{key}<<{delim}\n{value}\n{delim}\n")
            else:
                fh.write(f"{key}={value}\n")


def _cmd_check(args: argparse.Namespace) -> int:
    root = _repo_root(args.repo_root)
    global_cfg = load_global_config(root)
    targets = load_all_targets(root)
    state = load_json(root / "state" / "state.json", {})
    status = load_json(root / "state" / "status.json", {})

    mesa_client = MesaGitLabClient(global_cfg.mesa)
    gh_client = GitHubUpstreamClient()

    mesa_snap = mesa_client.fetch_active_mesa_snapshot()
    target_snaps = {}
    for t_id, t_cfg in targets.items():
        prev_ch = ((state.get("targets") or {}).get(t_id) or {}).get("channels") or {}
        target_snaps[t_id] = gh_client.fetch_target_snapshot(t_cfg, prev_ch)

    channel_filter = (
        {c.strip() for c in args.channels.split(",") if c.strip() and c.strip() != "all"}
        if args.channels
        else None
    )
    tc_filter = (
        {t.strip() for t in args.toolchains.split(",") if t.strip() and t.strip() != "all"}
        if args.toolchains
        else None
    )

    plan = plan_runs(
        global_cfg,
        targets,
        mesa_snap,
        target_snaps,
        state,
        status,
        force_all=args.force_all,
        channel_filter=channel_filter,
        toolchain_filter=tc_filter,
        runner_provider=args.runner_provider,
    )

    snapshot_payload: dict[str, Any] = {
        "mesa": {
            "active_branches": list(mesa_snap.active_branches),
            "heads": mesa_snap.heads,
        },
        "targets": {
            t_id: {
                ch_name: {
                    "ref": ch.ref,
                    "sha": ch.sha,
                    "severity": ch.severity,
                    "changed_paths": list(ch.changed_paths) if ch.changed_paths is not None else None,
                }
                for ch_name, ch in snap.channels.items()
            }
            for t_id, snap in target_snaps.items()
        },
    }

    if args.snapshot_out:
        out_path = Path(args.snapshot_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(snapshot_payload, indent=2) + "\n", encoding="utf-8")

    d3d12_jobs = plan.jobs_for_workflow("canary-d3d12.yml")

    _write_github_output(
        {
            "has_work": "true" if plan.has_work else "false",
            "needs_keepalive": "true" if plan.needs_keepalive else "false",
            "total_jobs": str(len(plan.jobs)),
            "d3d12_has_work": "true" if d3d12_jobs else "false",
            "d3d12_matrix": json.dumps({"include": d3d12_jobs}, separators=(",", ":")),
        }
    )

    summary = {
        "mesa_branches": list(mesa_snap.active_branches),
        "has_work": plan.has_work,
        "needs_keepalive": plan.needs_keepalive,
        "total_jobs": len(plan.jobs),
        "reasons": list(plan.reasons_summary),
        "targets": snapshot_payload["targets"],
        "jobs": [j.to_dict() for j in plan.jobs],
    }
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_plan_manual(args: argparse.Namespace) -> int:
    """Resolve refs and emit matrix JSON for manual workflow_dispatch runs (e.g. smoke-test #16936)."""
    root = _repo_root(args.repo_root)
    global_cfg = load_global_config(root)
    targets = load_all_targets(root)
    target = targets[args.target]

    mesa_client = MesaGitLabClient(global_cfg.mesa)
    gh_client = GitHubUpstreamClient()

    mesa_refs_raw = [r.strip() for r in args.mesa_refs.split(",") if r.strip()]
    mesa_refs: list[tuple[str, str]] = []
    for ref in mesa_refs_raw:
        try:
            sha = mesa_client.get_ref_sha(ref)
        except Exception:
            sha = ""
        mesa_refs.append((ref, sha))

    # Resolve dep_ref SHA if possible
    dep_ref = args.dep_ref.strip()
    dep_sha = ""
    try:
        data, _ = gh_client.get_json(f"/repos/{target.upstream.repo}/commits/{dep_ref}")
        dep_sha = str(data.get("sha", ""))
    except Exception:
        pass

    tc_list = (
        [t.strip() for t in args.toolchains.split(",") if t.strip() and t.strip() != "all"]
        if args.toolchains
        else None
    )
    if tc_list == []:
        tc_list = None

    jobs = build_manual_matrix(
        global_cfg,
        target,
        mesa_refs=mesa_refs,
        dep_ref=dep_ref,
        dep_sha=dep_sha,
        channel=args.channel,
        severity=args.severity,
        toolchain_ids=tc_list,
        runner_provider=args.runner_provider,
    )
    matrix_obj = {"include": [j.to_dict() for j in jobs]}
    _write_github_output(
        {
            "has_work": "true" if jobs else "false",
            "matrix": json.dumps(matrix_obj, separators=(",", ":")),
        }
    )
    print(json.dumps(matrix_obj, indent=2))
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    root = _repo_root(args.repo_root)
    global_cfg = load_global_config(root)
    targets = load_all_targets(root)
    target = targets[args.target]

    result = run_build(
        global_cfg,
        target,
        mesa_ref=args.mesa_ref,
        dep_ref=args.dep_ref,
        toolchain_id=args.toolchain,
        channel=args.channel,
        severity=args.severity,
        work_dir=Path(args.work_dir).resolve(),
        output_dir=Path(args.output_dir).resolve(),
        packagecache_dir=Path(args.packagecache_dir).resolve() if args.packagecache_dir else None,
        run_url=args.run_url or "",
        local_mesa_url=args.local_mesa_url,
        local_dep_url=args.local_dep_url,
    )

    # Write concise step summary in GitHub Actions UI.
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        icon = "🟢 PASS" if result["status"] == "PASS" else "🔴 FAIL"
        lines = [
            f"### {icon} — `{result['target_name']}` `{result['dep_ref']}` × Mesa `{result['mesa_branch']}` (`{result['toolchain']}`)",
            "",
            f"- **Mesa:** `{result['mesa_branch']}` @ `{result['mesa_sha'][:12]}`",
            f"- **Dependency:** `{result['dep_ref']}` @ `{result['dep_sha'][:12]}` (resolved: `{result['dep_resolved_version']}`)",
            f"- **Duration:** {result['duration_sec']}s",
        ]
        if result["status"] == "FAIL":
            lines.append(f"- **Stage:** `{result['error_stage']}`")
            lines.append(f"- **Error:** `{result['error_summary']}`")
            if result["failed_files"]:
                lines.append(
                    "- **Failed files:** " + ", ".join(f"`{f}`" for f in result["failed_files"][:10])
                )
            if result["error_excerpt"]:
                lines.extend(["", "```text", result["error_excerpt"][:2500], "```"])
        with Path(summary_file).open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    print(json.dumps({k: v for k, v in result.items() if k != "error_excerpt"}, indent=2))
    if args.fail_on_build_error and result["status"] != "PASS":
        return 1
    return 0


def _cmd_parse_log(args: argparse.Namespace) -> int:
    text = Path(args.log_file).read_text(encoding="utf-8", errors="replace")
    parsed = parse_build_log(text, source_root=args.source_root)
    print(
        json.dumps(
            {
                "summary": parsed.summary,
                "failed_files": list(parsed.failed_files),
                "error_count": len(parsed.error_lines),
                "excerpt": parsed.excerpt,
            },
            indent=2,
        )
    )
    return 0


def _cmd_diagnose_backport(args: argparse.Namespace) -> int:
    root = _repo_root(args.repo_root)
    global_cfg = load_global_config(root)
    files = [f.strip() for f in args.files.split(",") if f.strip()]
    if args.local_mesa_repo:
        cands = diagnose_backport_local(
            Path(args.local_mesa_repo).resolve(),
            failing_ref=args.failing_ref,
            failed_files=files,
            main_ref=args.main_ref,
            max_candidates=args.max_candidates,
            test_cherry_pick=not args.no_cherry_pick,
        )
    else:
        cands = diagnose_backport_remote(
            global_cfg.mesa,
            global_cfg.backport,
            failing_ref=args.failing_ref,
            failed_files=files,
            main_ref=args.main_ref,
        )
    print(json.dumps([c.to_dict() for c in cands], indent=2))
    return 0


def _cmd_summarize_manual(args: argparse.Namespace) -> int:
    """Render consolidated Markdown summary (and optional backport diagnosis) for manual runs."""
    root = _repo_root(args.repo_root)
    global_cfg = load_global_config(root)
    results = collect_result_artifacts(Path(args.artifacts_dir).resolve())

    lines = [
        "## Mesa Canary Manual / Smoke-Test Summary",
        "",
        "| Mesa Ref | Dependency Ref | Resolved Ver | Toolchain | Status | Stage | Duration | Error |",
        "| --- | --- | --- | --- | --- | --- | ---: | --- |",
    ]
    failed_groups: dict[str, list[str]] = {}
    for r in sorted(results, key=lambda x: (str(x.get("mesa_branch")), str(x.get("toolchain")))):
        st = "🟢 PASS" if r.get("status") == "PASS" else "🔴 FAIL"
        err = (str(r.get("error_summary") or "—")).replace("|", "\\|")[:100]
        lines.append(
            f"| `{r.get('mesa_branch')}` | `{r.get('dep_ref')}` | `{r.get('dep_resolved_version') or '—'}` | "
            f"`{r.get('toolchain')}` | {st} | `{r.get('error_stage') or '—'}` | {r.get('duration_sec', 0)}s | {err} |"
        )
        if r.get("status") == "FAIL" and r.get("mesa_branch") != "main":
            branch = str(r.get("mesa_branch"))
            failed_groups.setdefault(branch, [])
            for f in r.get("failed_files") or []:
                if f not in failed_groups[branch]:
                    failed_groups[branch].append(f)

    if args.diagnose_backport and failed_groups:
        lines.extend(["", "### Stage 2 Backport Auto-Diagnostics"])
        for branch, files in sorted(failed_groups.items()):
            if not files:
                continue
            if args.local_mesa_repo:
                cands = diagnose_backport_local(
                    Path(args.local_mesa_repo).resolve(),
                    failing_ref=branch,
                    failed_files=files,
                    max_candidates=global_cfg.backport.max_candidates_per_failure,
                    test_cherry_pick=global_cfg.backport.test_cherry_pick,
                )
            else:
                cands = diagnose_backport_remote(
                    global_cfg.mesa,
                    global_cfg.backport,
                    failing_ref=branch,
                    failed_files=files,
                )
            lines.append(f"\n#### Failing ref `{branch}` (files: {', '.join(f'`{f}`' for f in files[:6])})")
            if not cands:
                lines.append("- No candidate fix commits found in `main`.")
                continue
            for c in cands:
                mr = f" ([MR]({c.mr_url}))" if c.mr_url else ""
                lines.append(
                    f"- `{c.short_sha}`{mr} — **{c.cherry_pick_status}** — {c.subject}"
                )

    output = "\n".join(lines) + "\n"
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with Path(summary_file).open("a", encoding="utf-8") as fh:
            fh.write(output)
    print(output)
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    root = _repo_root(args.repo_root)
    global_cfg = load_global_config(root)
    targets = load_all_targets(root)
    outcome = process_report(
        root,
        global_cfg,
        targets,
        artifacts_dir=Path(args.artifacts_dir).resolve(),
        snapshot_path=Path(args.snapshot).resolve() if args.snapshot else None,
        repo_slug=args.repo_slug,
        sync_issues=not args.no_issues,
        local_mesa_repo=Path(args.local_mesa_repo).resolve() if args.local_mesa_repo else None,
    )
    print(json.dumps(outcome, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mesa-canary", description="Mesa Forward-Compatibility Canary CI CLI")
    parser.add_argument("--repo-root", default=None, help="Path to repository root")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Check upstream dependencies and Mesa branches, output build matrix")
    p_check.add_argument("--force-all", action="store_true", help="Force rebuild of all active cells")
    p_check.add_argument("--channels", default="all", help="Comma-separated channel filter (or 'all')")
    p_check.add_argument("--toolchains", default="all", help="Comma-separated toolchain filter (or 'all')")
    p_check.add_argument("--runner-provider", default=None, help="Override runner provider (github/blacksmith)")
    p_check.add_argument("--snapshot-out", default=None, help="Path to save upstream snapshot JSON")
    p_check.set_defaults(func=_cmd_check)

    p_man = sub.add_parser("plan-manual", help="Build matrix for manual workflow_dispatch")
    p_man.add_argument("--target", default="directx-headers")
    p_man.add_argument("--mesa-refs", required=True, help="Comma-separated Mesa branches/tags/SHAs")
    p_man.add_argument("--dep-ref", required=True, help="Dependency tag, branch, or SHA")
    p_man.add_argument("--toolchains", default="all", help="Comma-separated toolchain IDs or 'all'")
    p_man.add_argument("--channel", default="manual")
    p_man.add_argument("--severity", default="critical")
    p_man.add_argument("--runner-provider", default=None)
    p_man.set_defaults(func=_cmd_plan_manual)

    p_build = sub.add_parser("build", help="Execute a single canary build cell")
    p_build.add_argument("--target", default="directx-headers")
    p_build.add_argument("--mesa-ref", required=True)
    p_build.add_argument("--dep-ref", required=True)
    p_build.add_argument("--toolchain", required=True)
    p_build.add_argument("--channel", default="manual")
    p_build.add_argument("--severity", default="critical")
    p_build.add_argument("--work-dir", required=True)
    p_build.add_argument("--output-dir", required=True)
    p_build.add_argument("--packagecache-dir", default=None)
    p_build.add_argument("--run-url", default="")
    p_build.add_argument("--local-mesa-url", default=None)
    p_build.add_argument("--local-dep-url", default=None)
    p_build.add_argument(
        "--fail-on-build-error",
        action="store_true",
        help="Return exit code 1 if build failed (recommended for standalone manual smoke tests)",
    )
    p_build.set_defaults(func=_cmd_build)

    p_log = sub.add_parser("parse-log", help="Parse compiler/meson errors from a build log")
    p_log.add_argument("log_file")
    p_log.add_argument("--source-root", default=None)
    p_log.set_defaults(func=_cmd_parse_log)

    p_bp = sub.add_parser("diagnose-backport", help="Find and cherry-pick test candidate fixes in Mesa main")
    p_bp.add_argument("--failing-ref", required=True, help="Failing stable branch or tag (e.g. mesa-26.2.7)")
    p_bp.add_argument("--files", required=True, help="Comma-separated Mesa-relative source files")
    p_bp.add_argument("--main-ref", default="main")
    p_bp.add_argument("--max-candidates", type=int, default=5)
    p_bp.add_argument("--local-mesa-repo", default=None, help="Optional local Mesa git repo for offline diagnosis")
    p_bp.add_argument("--no-cherry-pick", action="store_true")
    p_bp.set_defaults(func=_cmd_diagnose_backport)

    p_sum = sub.add_parser("summarize-manual", help="Summarize manual/smoke-test artifacts")
    p_sum.add_argument("--artifacts-dir", required=True)
    p_sum.add_argument("--diagnose-backport", action="store_true")
    p_sum.add_argument("--local-mesa-repo", default=None)
    p_sum.set_defaults(func=_cmd_summarize_manual)

    p_rep = sub.add_parser("report", help="Aggregate artifacts, diagnose backports, update state/README/Issues")
    p_rep.add_argument("--artifacts-dir", required=True)
    p_rep.add_argument("--snapshot", default=None)
    p_rep.add_argument("--repo-slug", default=None, help="GitHub owner/repo (defaults to GITHUB_REPOSITORY)")
    p_rep.add_argument("--no-issues", action="store_true", help="Skip GitHub Issues API calls")
    p_rep.add_argument("--local-mesa-repo", default=None)
    p_rep.set_defaults(func=_cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
