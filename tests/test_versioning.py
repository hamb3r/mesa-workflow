"""Unit tests for universal semver parsing, tag classification, and Mesa calendar parsing."""

from __future__ import annotations

from mesa_canary.upstream import parse_release_calendar_csv
from mesa_canary.versioning import TagRef, classify_tags, parse_mesa_series, parse_semver_tag


def test_universal_semver_future_major_versions() -> None:
    tags = [
        TagRef("v1.619.1", "aaa111"),
        TagRef("v1.620.2", "bbb222"),
        TagRef("v2.0.0", "ccc333"),
        TagRef("v2.0.0-preview", "ddd444"),
        TagRef("v2.1.0-rc.2", "eee555"),
        TagRef("v2.1.0-rc.10", "fff666"),
        TagRef("v10.0.1", "999999"),
    ]
    res = classify_tags(tags)
    assert res.latest_stable is not None
    assert res.latest_stable.tag == "v10.0.1"
    assert res.latest_prerelease is not None
    assert res.latest_prerelease.tag == "v2.1.0-rc.10"


def test_directx_headers_real_world_cotagged_preview_typo() -> None:
    """Microsoft tagged both v1.721.2 and v1.721.2-preview on the same commit SHA."""
    tags = [
        TagRef("v1.619.1", "9e393d6d"),
        TagRef("v1.620.1", "a2b1aa9f"),
        TagRef("v1.620.2", "61c77189"),
        TagRef("v1.721.2", "6064ae31"),
        TagRef("v1.721.2-preview", "6064ae31"),
        TagRef("v1.722.1-preview", "a97becc4"),
    ]
    res = classify_tags(tags)
    assert res.latest_stable is not None
    assert res.latest_stable.tag == "v1.620.2"
    assert res.latest_prerelease is not None
    assert res.latest_prerelease.tag == "v1.722.1-preview"


def test_verify_callback_demotes_unmarked_preview_tag() -> None:
    tags = [
        TagRef("v1.620.2", "61c77189"),
        TagRef("v1.799.0", "deadbeef"),  # Suppose no -preview tag pushed, but meson.build says preview
    ]
    res = classify_tags(tags, verify_stable_cb=lambda c: c.tag != "v1.799.0")
    assert res.latest_stable is not None
    assert res.latest_stable.tag == "v1.620.2"
    assert res.latest_prerelease is not None
    assert res.latest_prerelease.tag == "v1.799.0"


def test_github_prerelease_and_draft_flags_respected() -> None:
    tags = [
        TagRef("v2.0.0", "aaa", is_github_draft=True),
        TagRef("v1.9.0", "bbb", is_github_prerelease=True),
        TagRef("v1.8.5", "ccc"),
    ]
    res = classify_tags(tags)
    assert res.latest_stable is not None
    assert res.latest_stable.tag == "v1.8.5"
    assert res.latest_prerelease is not None
    assert res.latest_prerelease.tag == "v1.9.0"


def test_semver_ordering_numeric_not_lexicographic() -> None:
    a = parse_semver_tag("v1.9.0")
    b = parse_semver_tag("v1.10.0")
    assert a and b and b.sort_key() > a.sort_key()


def test_parse_release_calendar_csv() -> None:
    csv_text = (
        "26.2,2026-11-25,26.2.8,Eric Engestrom,\n"
        "26.3,2026-10-14,26.3.0-rc1,Dylan Baker\n"
        ",2026-10-21,26.3.0-rc2,Dylan Baker\n"
    )
    assert parse_release_calendar_csv(csv_text) == ["26.3", "26.2"]
    assert parse_mesa_series("staging/26.3") == (26, 3)
    assert parse_mesa_series("main") is None
