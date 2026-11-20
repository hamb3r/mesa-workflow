"""Lightweight HTTP clients for GitHub (dependencies) and GitLab (upstream Mesa)."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from mesa_canary.config import MesaConfig, TargetConfig
from mesa_canary.versioning import (
    ParsedSemver,
    TagRef,
    classify_tags,
    parse_mesa_series,
    version_string_is_prerelease,
)

_USER_AGENT = "mesa-forward-compat-canary/0.1 (+https://github.com)"
_MESON_PROJECT_VER_RE = re.compile(
    r"project\s*\([^)]*?version\s*:\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)


class UpstreamError(RuntimeError):
    """Raised when an upstream API request fails."""


@dataclass(frozen=True)
class ChannelSnapshot:
    channel: str
    ref: str
    sha: str
    severity: str
    changed_paths: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TargetSnapshot:
    target_id: str
    channels: dict[str, ChannelSnapshot]


@dataclass(frozen=True)
class MesaSnapshot:
    active_branches: tuple[str, ...]
    heads: dict[str, str]


def _http_request(
    url: str,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: bytes | None = None,
    timeout: int = 30,
    retries: int = 3,
) -> tuple[bytes, dict[str, str]]:
    req_headers = {"User-Agent": _USER_AGENT}
    if headers:
        req_headers.update(headers)
    last_exc: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.read(), resp_headers
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in {429, 500, 502, 503, 504} and attempt + 1 < retries:
                time.sleep(2**attempt)
                continue
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise UpstreamError(f"HTTP {exc.code} for {url}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
                continue
            raise UpstreamError(f"Network error for {url}: {exc}") from exc
    raise UpstreamError(f"Failed request to {url}: {last_exc}")


def _parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            start = part.find("<")
            end = part.find(">")
            if start != -1 and end != -1 and end > start:
                return part[start + 1 : end]
    return None


class GitHubUpstreamClient:
    """Fetches tags, releases, branch HEADs, and commit path diffs from GitHub."""

    def __init__(self, token: str | None = None, api_base: str = "https://api.github.com") -> None:
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.api_base = api_base.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_json(self, path_or_url: str) -> tuple[Any, dict[str, str]]:
        url = path_or_url if path_or_url.startswith("http") else f"{self.api_base}{path_or_url}"
        raw, headers = _http_request(url, headers=self._headers())
        return json.loads(raw.decode("utf-8")), headers

    def get_paginated(self, path: str, max_pages: int = 10) -> list[Any]:
        items: list[Any] = []
        next_url: str | None = f"{self.api_base}{path}"
        for _ in range(max_pages):
            if not next_url:
                break
            data, headers = self.get_json(next_url)
            if isinstance(data, list):
                items.extend(data)
            else:
                break
            next_url = _parse_next_link(headers.get("link"))
        return items

    def list_tags(self, repo: str) -> list[TagRef]:
        raw_tags = self.get_paginated(f"/repos/{repo}/tags?per_page=100", max_pages=10)
        release_meta: dict[str, tuple[bool, bool]] = {}
        try:
            raw_releases = self.get_paginated(f"/repos/{repo}/releases?per_page=100", max_pages=3)
            for rel in raw_releases:
                tag = rel.get("tag_name")
                if tag:
                    release_meta[str(tag)] = (
                        bool(rel.get("prerelease", False)),
                        bool(rel.get("draft", False)),
                    )
        except UpstreamError:
            # Repositories that only push git tags (or rate-limited release lookups) still work.
            pass

        result: list[TagRef] = []
        for item in raw_tags:
            name = str(item.get("name", ""))
            sha = str((item.get("commit") or {}).get("sha", ""))
            is_pre, is_draft = release_meta.get(name, (False, False))
            if name:
                result.append(
                    TagRef(
                        name=name,
                        sha=sha,
                        is_github_prerelease=is_pre,
                        is_github_draft=is_draft,
                    )
                )
        return result

    def get_branch_sha(self, repo: str, branch: str) -> str:
        quoted = urllib.parse.quote(branch, safe="")
        data, _ = self.get_json(f"/repos/{repo}/branches/{quoted}")
        return str(data["commit"]["sha"])

    def get_changed_paths(self, repo: str, base_sha: str, head_sha: str) -> tuple[str, ...] | None:
        if not base_sha or base_sha == head_sha:
            return ()
        try:
            data, _ = self.get_json(f"/repos/{repo}/compare/{base_sha}...{head_sha}")
        except UpstreamError:
            return None
        files = data.get("files")
        if not isinstance(files, list):
            return None
        return tuple(str(f["filename"]) for f in files if "filename" in f)

    def fetch_raw_file(self, repo: str, ref: str, path: str) -> str | None:
        url = f"https://raw.githubusercontent.com/{repo}/{urllib.parse.quote(ref, safe='')}/{path}"
        try:
            raw, _ = _http_request(url, retries=2)
            return raw.decode("utf-8", errors="replace")
        except UpstreamError:
            return None

    def _verify_meson_tag_is_stable(self, repo: str, cand: ParsedSemver) -> bool:
        """Check meson.build project version on top stable candidate to catch typo tags."""
        content = self.fetch_raw_file(repo, cand.tag, "meson.build")
        if not content:
            return True
        match = _MESON_PROJECT_VER_RE.search(content)
        if not match:
            return True
        declared_ver = match.group(1)
        return not version_string_is_prerelease(declared_ver)

    def fetch_target_snapshot(
        self,
        target: TargetConfig,
        previous_channel_state: dict[str, dict[str, Any]] | None = None,
    ) -> TargetSnapshot:
        repo = target.upstream.repo
        prev = previous_channel_state or {}
        need_tags = any(
            ch.kind in {"semver_stable", "semver_prerelease"}
            for ch in target.upstream.channels.values()
        )
        classified = None
        if need_tags:
            tags = self.list_tags(repo)
            classified = classify_tags(
                tags,
                verify_stable_cb=lambda cand: self._verify_meson_tag_is_stable(repo, cand),
            )

        channels_out: dict[str, ChannelSnapshot] = {}
        for ch_name, ch_cfg in target.upstream.channels.items():
            if ch_cfg.kind == "semver_stable":
                if classified and classified.latest_stable:
                    channels_out[ch_name] = ChannelSnapshot(
                        channel=ch_name,
                        ref=classified.latest_stable.tag,
                        sha=classified.latest_stable.sha,
                        severity=ch_cfg.severity,
                    )
            elif ch_cfg.kind == "semver_prerelease":
                if classified and classified.latest_prerelease:
                    channels_out[ch_name] = ChannelSnapshot(
                        channel=ch_name,
                        ref=classified.latest_prerelease.tag,
                        sha=classified.latest_prerelease.sha,
                        severity=ch_cfg.severity,
                    )
            elif ch_cfg.kind == "branch":
                branch = ch_cfg.branch or "main"
                sha = self.get_branch_sha(repo, branch)
                prev_sha = str((prev.get(ch_name) or {}).get("sha") or "")
                changed_paths: tuple[str, ...] | None = None
                if prev_sha and prev_sha != sha and ch_cfg.relevant_paths:
                    changed_paths = self.get_changed_paths(repo, prev_sha, sha)
                channels_out[ch_name] = ChannelSnapshot(
                    channel=ch_name,
                    ref=branch,
                    sha=sha,
                    severity=ch_cfg.severity,
                    changed_paths=changed_paths,
                )

        return TargetSnapshot(target_id=target.id, channels=channels_out)


class MesaGitLabClient:
    """Lightweight read-only client for gitlab.freedesktop.org/mesa/mesa."""

    def __init__(self, mesa_cfg: MesaConfig) -> None:
        self.cfg = mesa_cfg
        self._encoded_project = urllib.parse.quote(mesa_cfg.project_path, safe="")

    def _api_json(self, suffix: str) -> Any:
        url = f"{self.cfg.gitlab_api}/projects/{self._encoded_project}{suffix}"
        raw, _ = _http_request(url, headers={"Accept": "application/json"})
        return json.loads(raw.decode("utf-8"))

    def fetch_release_calendar_series(self) -> list[str]:
        try:
            raw, _ = _http_request(self.cfg.release_calendar_url, retries=2)
        except UpstreamError:
            return []
        return parse_release_calendar_csv(raw.decode("utf-8", errors="replace"))

    def list_staging_branches(self) -> dict[str, str]:
        data = self._api_json("/repository/branches?search=staging/&per_page=100")
        branches: dict[str, str] = {}
        for item in data:
            name = str(item.get("name", ""))
            if parse_mesa_series(name) is not None and name.startswith("staging/"):
                sha = str((item.get("commit") or {}).get("id", ""))
                if sha:
                    branches[name] = sha
        return branches

    def get_ref_sha(self, ref: str) -> str:
        quoted = urllib.parse.quote(ref, safe="")
        try:
            data = self._api_json(f"/repository/commits/{quoted}")
            return str(data["id"])
        except UpstreamError:
            data = self._api_json(f"/repository/branches/{quoted}")
            return str(data["commit"]["id"])

    def fetch_active_mesa_snapshot(self) -> MesaSnapshot:
        policy = self.cfg.branch_policy
        staging_map = self.list_staging_branches()
        calendar_series = self.fetch_release_calendar_series()

        selected_staging: list[str] = []
        for series in calendar_series:
            branch = f"staging/{series}"
            if branch in staging_map and branch not in selected_staging:
                selected_staging.append(branch)

        if not selected_staging:
            # Fallback to highest semver staging/* branches if calendar is temporarily empty.
            ordered = sorted(
                staging_map.keys(),
                key=lambda b: parse_mesa_series(b) or (0, 0),
                reverse=True,
            )
            selected_staging = ordered[: policy.fallback_staging_count]
        else:
            selected_staging.sort(
                key=lambda b: parse_mesa_series(b) or (0, 0),
                reverse=True,
            )
            selected_staging = selected_staging[: policy.max_staging_branches]

        active: list[str] = []
        heads: dict[str, str] = {}

        if policy.include_main:
            active.append("main")
            heads["main"] = self.get_ref_sha("main")

        for branch in selected_staging:
            active.append(branch)
            heads[branch] = staging_map[branch]

        return MesaSnapshot(active_branches=tuple(active), heads=heads)

    def list_commits_touching_path(
        self,
        ref_name: str,
        file_path: str,
        since_iso: str | None = None,
        per_page: int = 20,
    ) -> list[dict[str, Any]]:
        params = {
            "ref_name": ref_name,
            "path": file_path,
            "per_page": str(per_page),
        }
        if since_iso:
            params["since"] = since_iso
        qs = urllib.parse.urlencode(params)
        data = self._api_json(f"/repository/commits?{qs}")
        return data if isinstance(data, list) else []

    def get_commit(self, sha: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(sha, safe="")
        data = self._api_json(f"/repository/commits/{quoted}")
        return data if isinstance(data, dict) else {}


def parse_release_calendar_csv(csv_text: str) -> list[str]:
    """Parse Mesa docs/release-calendar.csv and return active XX.Y series sorted descending."""
    series_set: set[str] = set()
    reader = csv.reader(io.StringIO(csv_text))
    for row in reader:
        if not row:
            continue
        first = row[0].strip()
        if first and parse_mesa_series(first) is not None and not first.startswith("staging/"):
            series_set.add(first)
    return sorted(series_set, key=lambda s: parse_mesa_series(s) or (0, 0), reverse=True)
