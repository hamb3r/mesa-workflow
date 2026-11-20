"""Compiler and Meson build-log error extractor.

Extracts concise error diagnostics and normalized Mesa-relative source file paths
from MSVC, clang-cl, MinGW GCC, MinGW Clang, Linux GCC/Clang, Linker, and Meson logs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# MSVC: C:\work\mesa\src\gallium\drivers\d3d12\d3d12_video_enc.cpp(2028): error C2664: ...
# clang-cl: ..\src\gallium\drivers\d3d12\d3d12_video_enc.cpp(2028,17): error: ...
_MSVC_ERROR_RE = re.compile(
    r"^(?P<file>[^\r\n(]+?)\((?P<line>\d+)(?:,(?P<col>\d+))?\)\s*:\s*"
    r"(?P<kind>(?:fatal\s+)?error(?:\s+[A-Z]+\d+)?)\s*:\s*(?P<msg>.+)$",
    re.IGNORECASE,
)

# GCC / Clang / MinGW: ../src/gallium/drivers/d3d12/d3d12_video_enc.cpp:2028:17: error: ...
_GCC_ERROR_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^\r\n:]+?):(?P<line>\d+)(?::(?P<col>\d+))?\s*:\s*"
    r"(?P<kind>(?:fatal\s+)?error)\s*:\s*(?P<msg>.+)$",
    re.IGNORECASE,
)

# Meson configure error: meson.build:712:18: ERROR: Dependency "DirectX-Headers" not found
_MESON_ERROR_RE = re.compile(
    r"^(?:(?P<file>[^\r\n:]+?):(?P<line>\d+):(?P<col>\d+):\s*)?ERROR:\s*(?P<msg>.+)$"
)

# Linker / Ninja / WPP errors
_LINKER_ERROR_RE = re.compile(
    r"(?:\bLNK\d{4}\b|undefined reference to|ld\.lld:\s*error:|lld-link:\s*error:|"
    r"collect2:\s*error:|tracewpp.*error|FAILED:\s*.+)",
    re.IGNORECASE,
)

# System/external prefixes that must never be mistaken for Mesa source files.
_NON_MESA_PATH_MARKERS = (
    "/usr/include/",
    "/usr/local/include/",
    "/usr/lib/",
    "/mingw64/",
    "/ucrt64/",
    "/clang64/",
    "/msys64/",
    "windows kits/",
    "program files",
    "/subprojects/",
)


@dataclass(frozen=True)
class ParsedLog:
    error_lines: tuple[str, ...]
    failed_files: tuple[str, ...]
    summary: str
    excerpt: str


def normalize_mesa_path(raw_path: str, source_root: str | None = None) -> str | None:
    """Normalize a compiler-reported file path to a Mesa-relative POSIX path.

    Returns None for system headers, build-dir generated files outside Mesa's source tree,
    or third-party subproject headers (so backport lookup focuses on Mesa source files).
    """
    cleaned = raw_path.strip().strip('"').strip("'").replace("\\", "/")
    if not cleaned:
        return None

    low_full = cleaned.lower()
    if any(bad in low_full for bad in _NON_MESA_PATH_MARKERS):
        return None

    # Strip exact source_root prefix if provided.
    stripped_from_root = False
    if source_root:
        norm_root = source_root.replace("\\", "/").rstrip("/") + "/"
        if cleaned.lower().startswith(norm_root.lower()):
            cleaned = cleaned[len(norm_root) :]
            stripped_from_root = True

    # Collapse leading "./" and "../" segments.
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    while cleaned.startswith("../"):
        cleaned = cleaned[3:]

    # Normalize internal "a/b/../c" segments.
    parts: list[str] = []
    is_abs = cleaned.startswith("/") or bool(re.match(r"^[A-Za-z]:/", cleaned))
    for part in cleaned.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts and not (len(parts) == 1 and parts[0].endswith(":")):
                parts.pop()
            continue
        parts.append(part)
    if not parts:
        return None
    normalized = ("/" if cleaned.startswith("/") else "") + "/".join(parts)
    low_norm = normalized.lower()

    if low_norm.startswith("subprojects/") or "/subprojects/" in low_norm:
        return None

    # Anchor on Mesa source directories cleanly (handles "../mesa/src/...", "/tmp/mesa/src/...", etc.).
    if not stripped_from_root:
        if "/src/" in low_norm:
            idx = low_norm.rfind("/src/")
            normalized = normalized[idx + 1 :]
        elif "/mesa/include/" in low_norm:
            idx = low_norm.rfind("/mesa/include/")
            normalized = normalized[idx + len("/mesa/") :]
        elif low_norm.endswith("/meson.build"):
            normalized = "meson.build"
        elif low_norm.endswith("/meson.options"):
            normalized = "meson.options"
        elif is_abs:
            return None

    if not any(
        normalized == m or normalized.startswith(m)
        for m in ("src/", "include/", "meson.build", "meson.options")
    ):
        return None
    return normalized


def parse_build_log(
    log_text: str,
    source_root: str | None = None,
    max_error_lines: int = 40,
    max_excerpt_chars: int = 3500,
) -> ParsedLog:
    """Extract high-signal compiler/meson diagnostics and Mesa source files from raw build output."""
    lines = log_text.splitlines()
    matched_lines: list[str] = []
    seen_errors: set[str] = set()
    failed_files: list[str] = []
    seen_files: set[str] = set()
    excerpt_blocks: list[str] = []

    def record_file(raw_path: str | None) -> None:
        if not raw_path:
            return
        norm = normalize_mesa_path(raw_path, source_root=source_root)
        if norm and norm not in seen_files:
            seen_files.add(norm)
            failed_files.append(norm)

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        m = _MSVC_ERROR_RE.match(stripped) or _GCC_ERROR_RE.match(stripped)
        if m:
            record_file(m.group("file"))
            if stripped not in seen_errors:
                seen_errors.add(stripped)
                matched_lines.append(stripped)
                # Include up to 2 trailing note/context lines (caret lines or MSVC 'note:').
                block = [stripped]
                for follow in lines[idx + 1 : idx + 3]:
                    fstrip = follow.rstrip()
                    if not fstrip:
                        break
                    if (
                        "note:" in fstrip.lower()
                        or "^" in fstrip
                        or fstrip.startswith(" ")
                        or "see declaration of" in fstrip.lower()
                    ):
                        block.append(fstrip)
                    else:
                        break
                excerpt_blocks.append("\n".join(block))
            continue

        mm = _MESON_ERROR_RE.match(stripped)
        if mm:
            record_file(mm.group("file"))
            if stripped not in seen_errors:
                seen_errors.add(stripped)
                matched_lines.append(stripped)
                excerpt_blocks.append(stripped)
            continue

        if _LINKER_ERROR_RE.search(stripped) and stripped not in seen_errors:
            seen_errors.add(stripped)
            matched_lines.append(stripped)
            excerpt_blocks.append(stripped)

    if not matched_lines:
        # Fall back to last 25 non-empty lines so no failure is ever blank.
        tail = [ln.rstrip() for ln in lines if ln.strip()][-25:]
        excerpt = "\n".join(tail)[-max_excerpt_chars:]
        summary = tail[-1][:240] if tail else "Build failed (no log output captured)"
        return ParsedLog(
            error_lines=tuple(tail[-max_error_lines:]),
            failed_files=tuple(failed_files),
            summary=summary,
            excerpt=excerpt,
        )

    trimmed_errors = matched_lines[:max_error_lines]
    # Prefer the first real compiler/meson error (not a generic "FAILED: target.obj") for summary.
    primary = next(
        (ln for ln in trimmed_errors if not ln.startswith("FAILED:")),
        trimmed_errors[0],
    )
    summary = primary[:240]

    excerpt_parts: list[str] = []
    total_len = 0
    for block in excerpt_blocks:
        if total_len + len(block) + 1 > max_excerpt_chars:
            break
        excerpt_parts.append(block)
        total_len += len(block) + 1
    excerpt = "\n".join(excerpt_parts)
    if len(matched_lines) > max_error_lines or total_len >= max_excerpt_chars - 50:
        excerpt += f"\n... ({len(matched_lines)} total error lines captured)"

    return ParsedLog(
        error_lines=tuple(trimmed_errors),
        failed_files=tuple(failed_files),
        summary=summary,
        excerpt=excerpt,
    )
