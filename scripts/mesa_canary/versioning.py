"""Universal semantic-version tag parsing and stable/prerelease classification.

Avoids hardcoding major/minor version prefixes (e.g., 1.6 vs 1.7) so that future
major releases like v2.0.0 or v3.1.0 are handled automatically.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# Matches v1.620.2, 2.0.0, v1.722.1-preview, v2.0.0-rc.1, v1.716.0_beta2, etc.
_SEMVER_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:\.(?P<build>\d+))?"
    r"(?:[-._](?P<prerelease>[A-Za-z][A-Za-z0-9._-]*))?$"
)

# Common Mesa branch series like "26.2" or "staging/26.3".
_SERIES_RE = re.compile(r"^(?:staging/)?(?P<major>\d+)\.(?P<minor>\d+)$")

_PRERELEASE_KEYWORDS = ("preview", "rc", "alpha", "beta", "pre", "dev", "nightly", "snapshot")


@dataclass(frozen=True)
class TagRef:
    name: str
    sha: str
    is_github_prerelease: bool = False
    is_github_draft: bool = False


@dataclass(frozen=True)
class ParsedSemver:
    tag: str
    sha: str
    major: int
    minor: int
    patch: int
    build: int
    prerelease: str | None

    @property
    def is_prerelease_name(self) -> bool:
        return self.prerelease is not None

    def sort_key(self) -> tuple[int, int, int, int, int, tuple[tuple[int, int | str], ...]]:
        """Total ordering key where stable > prerelease for the same core version."""
        if not self.prerelease:
            return (self.major, self.minor, self.patch, self.build, 1, ())
        tokens: list[tuple[int, int | str]] = []
        for part in re.split(r"([0-9]+|[A-Za-z]+)", self.prerelease.lower()):
            if not part or part in ".-_":
                continue
            if part.isdigit():
                tokens.append((1, int(part)))
            else:
                tokens.append((0, part))
        return (self.major, self.minor, self.patch, self.build, 0, tuple(tokens))


@dataclass(frozen=True)
class ClassifiedTags:
    latest_stable: ParsedSemver | None
    latest_prerelease: ParsedSemver | None
    stable_tags: tuple[ParsedSemver, ...]
    prerelease_tags: tuple[ParsedSemver, ...]


def parse_semver_tag(tag_name: str, sha: str = "") -> ParsedSemver | None:
    match = _SEMVER_RE.match(tag_name.strip())
    if not match:
        return None
    return ParsedSemver(
        tag=tag_name.strip(),
        sha=sha,
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        build=int(match.group("build") or 0),
        prerelease=match.group("prerelease"),
    )


def parse_mesa_series(branch_or_series: str) -> tuple[int, int] | None:
    match = _SERIES_RE.match(branch_or_series.strip())
    if not match:
        return None
    return (int(match.group("major")), int(match.group("minor")))


def version_string_is_prerelease(version_str: str) -> bool:
    low = version_str.strip().lower()
    return any(f"-{kw}" in low or f"_{kw}" in low or f".{kw}" in low for kw in _PRERELEASE_KEYWORDS)


def classify_tags(
    tags: Iterable[TagRef],
    verify_stable_cb: Callable[[ParsedSemver], bool] | None = None,
) -> ClassifiedTags:
    """Classify tags universally into stable vs prerelease streams.

    Handles real-world anomalies such as typo tags without a suffix pointing to the
    exact same commit SHA as a `-preview` tag (e.g. DirectX-Headers `v1.721.2`
    co-tagged with `v1.721.2-preview`), without hardcoding version numbers.

    If `verify_stable_cb` is provided, it is invoked lazily on top stable candidates
    (highest semver first) and returns False if the tag's own project metadata declares
    it to be a prerelease.
    """
    tag_list = [t for t in tags if not t.is_github_draft]
    prerelease_shas: set[str] = set()
    explicit_prereleases: list[ParsedSemver] = []
    stable_candidates: list[ParsedSemver] = []

    for ref in tag_list:
        parsed = parse_semver_tag(ref.name, ref.sha)
        if parsed is None:
            continue
        if parsed.is_prerelease_name or ref.is_github_prerelease:
            if ref.sha:
                prerelease_shas.add(ref.sha)
            explicit_prereleases.append(parsed)
        else:
            stable_candidates.append(parsed)

    # Filter out any stable-looking tag that shares its exact commit SHA with a prerelease tag.
    filtered_stable: list[ParsedSemver] = []
    for cand in stable_candidates:
        if cand.sha and cand.sha in prerelease_shas:
            continue
        filtered_stable.append(cand)

    filtered_stable.sort(key=lambda item: item.sort_key(), reverse=True)
    explicit_prereleases.sort(key=lambda item: item.sort_key(), reverse=True)

    verified_stable: list[ParsedSemver] = []
    if verify_stable_cb is None:
        verified_stable = filtered_stable
    else:
        # Lazily verify from highest down; demote any candidate whose own metadata says prerelease.
        for idx, cand in enumerate(filtered_stable):
            if verify_stable_cb(cand):
                verified_stable = filtered_stable[idx:]
                break
            explicit_prereleases.append(cand)
        explicit_prereleases.sort(key=lambda item: item.sort_key(), reverse=True)

    return ClassifiedTags(
        latest_stable=verified_stable[0] if verified_stable else None,
        latest_prerelease=explicit_prereleases[0] if explicit_prereleases else None,
        stable_tags=tuple(verified_stable),
        prerelease_tags=tuple(explicit_prereleases),
    )
