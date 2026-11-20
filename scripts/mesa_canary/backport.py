"""Automatic backport candidate discovery and cherry-pick dry-run verifier (Stage 2).

When a stable Mesa branch (e.g. staging/26.2 or mesa-26.2.7) fails to compile while
main builds cleanly, this module:
  1. Searches main's history for commits touching the failed Mesa source files.
  2. Filters out commits already present or cherry-picked onto the failing branch.
  3. Ranks candidates by overlap with failed files and recency.
  4. Tests `git cherry-pick --no-commit` cleanly in an isolated temporary repository
     (using lightweight treeless fetches from upstream or a local repo for offline tests).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mesa_canary.config import BackportConfig, MesaConfig
from mesa_canary.upstream import MesaGitLabClient, UpstreamError

_PART_OF_RE = re.compile(r"Part-of:\s*<(?P<url>https?://[^>]+)>", re.IGNORECASE)
_CHERRY_PICKED_RE = re.compile(
    r"\(cherry picked from commit\s+(?P<sha>[0-9a-f]{7,40})\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BackportCandidate:
    sha: str
    short_sha: str
    subject: str
    author: str
    date: str
    mr_url: str | None
    matched_files: tuple[str, ...]
    cherry_pick_status: str  # "CLEAN" | "CONFLICT" | "ALREADY_APPLIED" | "SKIPPED" | "ERROR"
    conflict_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["matched_files"] = list(self.matched_files)
        data["conflict_files"] = list(self.conflict_files)
        return data


def extract_mr_url(commit_message: str) -> str | None:
    match = _PART_OF_RE.search(commit_message or "")
    return match.group("url") if match else None


def extract_cherry_picked_shas(commit_message: str) -> set[str]:
    return {m.group("sha").lower() for m in _CHERRY_PICKED_RE.finditer(commit_message or "")}


def _normalize_subject(subject: str) -> str:
    return re.sub(r"\s+", " ", subject.strip()).lower()


def _run_git(
    args: list[str],
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _test_cherry_pick_in_repo(
    repo_dir: Path,
    base_ref: str,
    candidate_sha: str,
) -> tuple[str, tuple[str, ...]]:
    """Test whether `candidate_sha` cherry-picks cleanly onto `base_ref`."""
    _run_git(["checkout", "--force", "--detach", base_ref], cwd=repo_dir)
    _run_git(["reset", "--hard", base_ref], cwd=repo_dir)
    _run_git(["clean", "-fdx"], cwd=repo_dir)

    cp = _run_git(
        ["cherry-pick", "--no-commit", candidate_sha],
        cwd=repo_dir,
        check=False,
    )
    if cp.returncode == 0:
        # Check if the cherry-pick produced an empty diff (already applied effectively).
        diff_res = _run_git(["diff", "--cached", "--quiet"], cwd=repo_dir, check=False)
        _run_git(["reset", "--hard", base_ref], cwd=repo_dir)
        if diff_res.returncode == 0:
            return "ALREADY_APPLIED", ()
        return "CLEAN", ()

    # Collect conflicted paths before resetting.
    unmerged = _run_git(
        ["diff", "--name-only", "--diff-filter=U"],
        cwd=repo_dir,
        check=False,
    )
    conflicts = tuple(
        sorted(line.strip() for line in unmerged.stdout.splitlines() if line.strip())
    )
    _run_git(["cherry-pick", "--abort"], cwd=repo_dir, check=False)
    _run_git(["reset", "--hard", base_ref], cwd=repo_dir)
    if conflicts or "conflict" in (cp.stdout + cp.stderr).lower():
        return "CONFLICT", conflicts
    if "empty" in (cp.stdout + cp.stderr).lower():
        return "ALREADY_APPLIED", ()
    return "ERROR", ()


def diagnose_backport_local(
    local_mesa_repo: Path,
    failing_ref: str,
    failed_files: list[str],
    *,
    main_ref: str = "main",
    max_candidates: int = 5,
    test_cherry_pick: bool = True,
) -> list[BackportCandidate]:
    """Find and verify backport candidates using a local Mesa git repository."""
    if not failed_files or failing_ref == main_ref:
        return []

    # Resolve commits in main_ref that are not reachable from failing_ref and touch failed_files.
    file_hits: dict[str, list[str]] = {}
    commit_meta: dict[str, tuple[str, str, str, str]] = {}

    # Collect subjects and cherry-picked SHAs already present on failing_ref since merge-base.
    mb_res = _run_git(["merge-base", failing_ref, main_ref], cwd=local_mesa_repo, check=False)
    merge_base = mb_res.stdout.strip()
    if not merge_base:
        return []

    already_subjects: set[str] = set()
    already_orig_shas: set[str] = set()
    stable_log = _run_git(
        ["log", f"{merge_base}..{failing_ref}", "--format=%H%x1f%s%x1f%b%x1e"],
        cwd=local_mesa_repo,
        check=False,
    )
    for rec in stable_log.stdout.split("\x1e"):
        rec = rec.strip("\r\n")
        if not rec:
            continue
        parts = rec.split("\x1f", 2)
        if len(parts) >= 2:
            already_subjects.add(_normalize_subject(parts[1]))
        if len(parts) >= 3:
            already_orig_shas.update(extract_cherry_picked_shas(parts[2]))

    for fpath in failed_files[:15]:
        res = _run_git(
            [
                "log",
                f"{merge_base}..{main_ref}",
                "--no-merges",
                "-n",
                "20",
                "--format=%H%x1f%s%x1f%an%x1f%aI%x1f%b%x1e",
                "--",
                fpath,
            ],
            cwd=local_mesa_repo,
            check=False,
        )
        for rec in res.stdout.split("\x1e"):
            rec = rec.strip("\r\n")
            if not rec:
                continue
            parts = rec.split("\x1f", 4)
            if len(parts) < 4:
                continue
            sha, subject, author, date_str = parts[:4]
            body = parts[4] if len(parts) > 4 else ""
            if _normalize_subject(subject) in already_subjects:
                continue
            if any(sha.lower().startswith(s) or s.startswith(sha.lower()) for s in already_orig_shas):
                continue
            file_hits.setdefault(sha, [])
            if fpath not in file_hits[sha]:
                file_hits[sha].append(fpath)
            commit_meta[sha] = (subject, author, date_str, body)

    ranked_shas = sorted(
        file_hits.keys(),
        key=lambda s: (len(file_hits[s]), commit_meta[s][2]),
        reverse=True,
    )[:max_candidates]

    if not ranked_shas:
        return []

    results: list[BackportCandidate] = []
    if test_cherry_pick:
        with tempfile.TemporaryDirectory(prefix="mesa-bp-") as tmp:
            worktree = Path(tmp) / "wt"
            _run_git(
                ["worktree", "add", "--detach", str(worktree), failing_ref],
                cwd=local_mesa_repo,
            )
            try:
                for sha in ranked_shas:
                    subject, author, date_str, body = commit_meta[sha]
                    cp_status, conflicts = _test_cherry_pick_in_repo(worktree, failing_ref, sha)
                    results.append(
                        BackportCandidate(
                            sha=sha,
                            short_sha=sha[:10],
                            subject=subject,
                            author=author,
                            date=date_str,
                            mr_url=extract_mr_url(body),
                            matched_files=tuple(file_hits[sha]),
                            cherry_pick_status=cp_status,
                            conflict_files=conflicts,
                        )
                    )
            finally:
                _run_git(
                    ["worktree", "remove", "--force", str(worktree)],
                    cwd=local_mesa_repo,
                    check=False,
                )
    else:
        for sha in ranked_shas:
            subject, author, date_str, body = commit_meta[sha]
            results.append(
                BackportCandidate(
                    sha=sha,
                    short_sha=sha[:10],
                    subject=subject,
                    author=author,
                    date=date_str,
                    mr_url=extract_mr_url(body),
                    matched_files=tuple(file_hits[sha]),
                    cherry_pick_status="SKIPPED",
                )
            )
    return results


def diagnose_backport_remote(
    mesa_cfg: MesaConfig,
    bp_cfg: BackportConfig,
    failing_ref: str,
    failed_files: list[str],
    *,
    main_ref: str = "main",
    gitlab_client: MesaGitLabClient | None = None,
) -> list[BackportCandidate]:
    """Find and verify backport candidates via GitLab API + minimal treeless git fetch.

    Respects gitlab.freedesktop.org by querying the REST API first and only doing a
    partial blobless fetch if actual candidate commits are found.
    """
    if not bp_cfg.enabled or not failed_files or failing_ref == main_ref:
        return []

    client = gitlab_client or MesaGitLabClient(mesa_cfg)
    since_iso = (datetime.now(UTC) - timedelta(days=bp_cfg.lookback_days)).isoformat()

    # Step 1: Query commits on both main and failing_ref touching failed_files.
    file_hits: dict[str, list[str]] = {}
    main_meta: dict[str, dict[str, Any]] = {}
    stable_shas: set[str] = set()
    stable_subjects: set[str] = set()
    stable_cherry_picked: set[str] = set()

    for fpath in failed_files[:10]:
        try:
            stable_commits = client.list_commits_touching_path(
                failing_ref, fpath, since_iso=since_iso, per_page=25
            )
            for c in stable_commits:
                c_sha = str(c.get("id", "")).lower()
                if c_sha:
                    stable_shas.add(c_sha)
                title = str(c.get("title", ""))
                if title:
                    stable_subjects.add(_normalize_subject(title))
                msg = str(c.get("message", ""))
                stable_cherry_picked.update(extract_cherry_picked_shas(msg))
        except UpstreamError:
            pass

        try:
            main_commits = client.list_commits_touching_path(
                main_ref, fpath, since_iso=since_iso, per_page=25
            )
        except UpstreamError:
            continue

        for c in main_commits:
            sha = str(c.get("id", ""))
            if not sha or sha.lower() in stable_shas:
                continue
            title = str(c.get("title", ""))
            if title and _normalize_subject(title) in stable_subjects:
                continue
            if any(
                sha.lower().startswith(s) or s.startswith(sha.lower())
                for s in stable_cherry_picked
            ):
                continue
            file_hits.setdefault(sha, [])
            if fpath not in file_hits[sha]:
                file_hits[sha].append(fpath)
            main_meta[sha] = c

    ranked_shas = sorted(
        file_hits.keys(),
        key=lambda s: (len(file_hits[s]), str(main_meta[s].get("committed_date", ""))),
        reverse=True,
    )[: bp_cfg.max_candidates_per_failure]

    if not ranked_shas:
        return []

    # Step 2: Optional cherry-pick dry run using a lightweight blobless shallow fetch.
    cp_results: dict[str, tuple[str, tuple[str, ...]]] = {}
    if bp_cfg.test_cherry_pick:
        with tempfile.TemporaryDirectory(prefix="mesa-bp-remote-") as tmp:
            repo_dir = Path(tmp) / "mesa"
            repo_dir.mkdir()
            try:
                _run_git(["init"], cwd=repo_dir)
                _run_git(["remote", "add", "origin", mesa_cfg.git_url], cwd=repo_dir)
                _run_git(
                    ["fetch", "--filter=blob:none", "--depth=1", "origin", failing_ref],
                    cwd=repo_dir,
                )
                base_sha = _run_git(["rev-parse", "FETCH_HEAD"], cwd=repo_dir).stdout.strip()
                for sha in ranked_shas:
                    # Depth=2 fetches the candidate commit AND its immediate parent for cherry-pick diff.
                    fetch_res = _run_git(
                        ["fetch", "--filter=blob:none", "--depth=2", "origin", sha],
                        cwd=repo_dir,
                        check=False,
                    )
                    if fetch_res.returncode != 0:
                        cp_results[sha] = ("ERROR", ())
                        continue
                    cp_results[sha] = _test_cherry_pick_in_repo(repo_dir, base_sha, sha)
            except subprocess.CalledProcessError:
                pass

    candidates: list[BackportCandidate] = []
    for sha in ranked_shas:
        meta = main_meta[sha]
        msg = str(meta.get("message", ""))
        cp_status, conflicts = cp_results.get(
            sha, ("SKIPPED" if not bp_cfg.test_cherry_pick else "ERROR", ())
        )
        candidates.append(
            BackportCandidate(
                sha=sha,
                short_sha=sha[:10],
                subject=str(meta.get("title", "")),
                author=str(meta.get("author_name", "")),
                date=str(meta.get("committed_date", "")),
                mr_url=extract_mr_url(msg),
                matched_files=tuple(file_hits[sha]),
                cherry_pick_status=cp_status,
                conflict_files=conflicts,
            )
        )
    return candidates
