# Mesa Forward-Compatibility Canary CI

Proactive forward-compatibility monitor and canary builder for [Mesa](https://gitlab.freedesktop.org/mesa/mesa).

Official Mesa CI pins external SDKs and headers (e.g., `DirectX-Headers`) and tests primarily MSVC on Windows. This repository watches upstream releases, preview tags, and `main` branches of critical external dependencies, then compiles **Mesa `main` + live `staging/*` branches** across **all supported compiler/CRT variants** — catching breakages and missing backports *before* stable Mesa point releases ship.

> **Upstream etiquette:** This automation **never** files issues, comments, or MRs on `gitlab.freedesktop.org/mesa/mesa`. All alerts, status tables, and GitHub Issues live strictly inside this repository, and Mesa clones use `--depth=1` only when a build is actually triggered.

---

## Live Status Matrix

<!-- STATUS:START -->

_Last updated: **2026-11-23T23:37:17Z**_

### Tracked Upstream Versions

| Dependency | Channel | Severity | Latest Ref | Commit | Last Tested |
| --- | --- | --- | --- | --- | --- |
| **DirectX-Headers** (`microsoft/DirectX-Headers`) | `release` | 🔴 critical | `—` | `—` | — |
| **DirectX-Headers** (`microsoft/DirectX-Headers`) | `preview` | 🟡 warning | `—` | `—` | — |
| **DirectX-Headers** (`microsoft/DirectX-Headers`) | `main` | 🟡 warning | `—` | `—` | — |

### DirectX-Headers — `release` (`untested`) 🔴

| Mesa Branch | HEAD | Win MSVC | MinGW GCC (UCRT64) | MinGW GCC (MINGW64) | MinGW Clang (CLANG64) | Linux GCC (WSL) | Linux Clang (WSL) | Issue |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `main` | `12fafae6` | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | — |
| `staging/26.3` | `6cd33b7f` | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | — |
| `staging/26.2` | `6dce7d63` | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | — |

### DirectX-Headers — `preview` (`untested`) 🟡

| Mesa Branch | HEAD | Win MSVC | MinGW GCC (UCRT64) | MinGW GCC (MINGW64) | MinGW Clang (CLANG64) | Linux GCC (WSL) | Linux Clang (WSL) | Issue |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `main` | `12fafae6` | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | — |
| `staging/26.3` | `6cd33b7f` | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | — |
| `staging/26.2` | `6dce7d63` | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | — |

### DirectX-Headers — `main` (`untested`) 🟡

| Mesa Branch | HEAD | Win MSVC | MinGW GCC (UCRT64) | MinGW GCC (MINGW64) | MinGW Clang (CLANG64) | Linux GCC (WSL) | Linux Clang (WSL) | Issue |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `main` | `12fafae6` | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | — |
| `staging/26.3` | `6cd33b7f` | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | — |
| `staging/26.2` | `6dce7d63` | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ | — |

<!-- STATUS:END -->

---

## Coverage (DirectX-Headers Canary)

### Upstream Channels (Universal Semver — No Hardcoded Version Prefixes)
- **`release`** (🔴 critical): Highest stable semver release tag (automatically filters out prereleases and co-tagged preview aliases; works across `v1.x`, `v2.x`, etc.).
- **`preview`** (🟡 warning): Highest preview/prerelease tag (exercises Mesa's `USE_D3D12_PREVIEW_HEADERS=1` path).
- **`main`** (🟡 warning): Upstream `main` HEAD (filtered by changes to `include/`, `src/`, `meson.build`, with 24h cooldown).

### Build Variants (6 Active + 1 Staged)
| Variant | OS | Compiler / CRT | D3D12 + Video + DZN | VA-API | Media Foundation | Status |
| --- | --- | --- | :---: | :---: | :---: | --- |
| `msvc` | Windows | MSVC `cl.exe` + Windows SDK + `tracewpp` | ✅ | ✅ (auto) | ✅ | Active |
| `msys2-ucrt64` | Windows | MSYS2 MinGW-w64 GCC (UCRT) | ✅ | ✅ (auto) | — (MSVC-only) | Active |
| `msys2-mingw64` | Windows | MSYS2 MinGW-w64 GCC (MSVCRT) | ✅ | ✅ (auto) | — (MSVC-only) | Active |
| `msys2-clang64` | Windows | MSYS2 MinGW-w64 Clang (UCRT + lld) | ✅ | ✅ (auto) | — (MSVC-only) | Active |
| `linux-gcc` | Linux (WSL) | GCC + X11/Wayland | ✅ | ✅ | — (Windows-only) | Active |
| `linux-clang` | Linux (WSL) | Clang + X11/Wayland | ✅ | ✅ | — (Windows-only) | Active |
| `clang-cl` | Windows | LLVM `clang-cl` + MSVC ABI + `tracewpp` | ✅ | ✅ (auto) | ✅ | Disabled (`u_atomic.h` upstream bug) |

---

## Smart Scheduling & Automatic Backport Diagnostics

1. **Zero idle waste:** `.github/workflows/monitor.yml` runs every 6 hours and finishes in ~5 seconds when nothing changed.
2. **Event-driven builds:** Rebuilds trigger only on:
   - new dependency release / preview / relevant `main` change;
   - a newly created live Mesa `staging/XX.Y` branch (discovered from Mesa's `docs/release-calendar.csv` + GitLab Branches API);
   - **recovery detection (`FAIL -> PASS`)**: when an existing cell is failing and that Mesa branch advances, only that failing cell is re-tested to auto-close the Issue.
3. **Automatic Stage 2 Backport Diagnostics:** Whenever a stable branch (`staging/*`) fails, `backport.py` extracts failed Mesa source files from the compiler log, locates un-backported candidate commits in `main`, tests `git cherry-pick --no-commit`, and reports `CLEAN` / `CONFLICT` + upstream MR links directly in the GitHub Issue and status dashboard.
4. **Runner portability:** Switch `runners.provider` in [`config/global.yaml`](config/global.yaml) from `github` to `blacksmith` without modifying workflow files.

---

## Smoke-Test Procedure (Historical Bug `#16936`)

Trigger **Actions → Canary — DirectX-Headers → Run workflow** (`canary-d3d12.yml`):

1. **Baseline Green:** `mesa_refs=mesa-26.2.7`, `dxheaders_ref=v1.619.1` → **GREEN**
2. **Reproduce `#16936` Red + Backport Auto-Diagnosis:** `mesa_refs=mesa-26.2.7`, `dxheaders_ref=v1.620.1`, `run_backport_check=true` → **RED** with `D3D12_VIDEO_ENCODER_PICTURE_CONTROL_CODEC_DATA` const-qualifier error AND automatic identification of `c963b6c8` (`!43715`) in the workflow summary.
3. **Fixed Branch Green:** `mesa_refs=staging/26.2`, `dxheaders_ref=v1.620.1` → **GREEN**

*(Manual runs do not mutate `state/*.json`, `README.md`, or open Issues.)*

---

## Local Development & Tests

```bash
pip install PyYAML pytest ruff
ruff check scripts tests
pytest -v

# Live dry-run check against GitHub + GitLab APIs
python scripts/cli.py check

# Offline backport diagnostics smoke-test against a local Mesa checkout
python scripts/cli.py diagnose-backport \
  --local-mesa-repo /path/to/mesa \
  --failing-ref mesa-26.2.7 \
  --files src/gallium/drivers/d3d12/d3d12_video_enc.cpp,src/gallium/drivers/d3d12/d3d12_video_enc_h264.cpp
```
