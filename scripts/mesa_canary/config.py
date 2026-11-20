"""Configuration models and YAML loader for mesa-canary."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class MesaBranchPolicy:
    include_main: bool = True
    fallback_staging_count: int = 2
    max_staging_branches: int = 3


@dataclass(frozen=True)
class MesaConfig:
    gitlab_api: str
    project_path: str
    git_url: str
    release_calendar_url: str
    branch_policy: MesaBranchPolicy


@dataclass(frozen=True)
class RunnersConfig:
    provider: str
    mapping: dict[str, dict[str, str]]

    def resolve(self, os_name: str, provider_override: str | None = None) -> str:
        prov = provider_override or self.provider
        if prov not in self.mapping:
            raise ValueError(f"Unknown runner provider '{prov}'. Available: {sorted(self.mapping)}")
        os_map = self.mapping[prov]
        if os_name not in os_map:
            raise ValueError(
                f"Runner provider '{prov}' has no mapping for OS '{os_name}'. "
                f"Available: {sorted(os_map)}"
            )
        return os_map[os_name]


@dataclass(frozen=True)
class BackportConfig:
    enabled: bool = True
    max_candidates_per_failure: int = 5
    lookback_days: int = 270
    test_cherry_pick: bool = True


@dataclass(frozen=True)
class IssuesConfig:
    enabled: bool = True
    base_label: str = "mesa-canary"


@dataclass(frozen=True)
class GlobalConfig:
    mesa: MesaConfig
    runners: RunnersConfig
    keepalive_days: int = 30
    backport: BackportConfig = field(default_factory=BackportConfig)
    issues: IssuesConfig = field(default_factory=IssuesConfig)


@dataclass(frozen=True)
class ChannelConfig:
    name: str
    kind: str  # "semver_stable" | "semver_prerelease" | "branch"
    severity: str = "warning"  # "critical" | "warning"
    description: str = ""
    branch: str | None = None
    relevant_paths: tuple[str, ...] = ()
    min_interval_hours: int = 0


@dataclass(frozen=True)
class UpstreamConfig:
    type: str  # "github"
    repo: str
    git_url: str
    channels: dict[str, ChannelConfig]


@dataclass(frozen=True)
class SubprojectConfig:
    name: str
    wrap_path: str
    default_directory: str
    meson_dependency_names: tuple[str, ...]


@dataclass(frozen=True)
class ToolchainConfig:
    id: str
    label: str
    os: str  # "windows" | "linux"
    kind: str  # "msvc" | "clang-cl" | "msys2" | "linux"
    c_compiler: str
    cpp_compiler: str
    use_sccache: bool = True
    needs_tracewpp: bool = False
    gallium_va: str = "auto_windows"  # "enabled" | "disabled" | "auto_windows"
    msys2_sys: str | None = None
    msys2_prefix: str | None = None
    meson_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetConfig:
    id: str
    name: str
    workflow: str
    enabled: bool
    upstream: UpstreamConfig
    subproject: SubprojectConfig
    meson_common_args: tuple[str, ...]
    toolchains: tuple[ToolchainConfig, ...]

    def get_toolchain(self, toolchain_id: str) -> ToolchainConfig:
        for tc in self.toolchains:
            if tc.id == toolchain_id:
                return tc
        available = [tc.id for tc in self.toolchains]
        raise KeyError(f"Unknown toolchain '{toolchain_id}' for target '{self.id}'. Available: {available}")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {path}")
    return data


def load_global_config(repo_root: Path) -> GlobalConfig:
    raw = _load_yaml(repo_root / "config" / "global.yaml")
    mesa_raw = raw["mesa"]
    bp_raw = mesa_raw.get("branch_policy", {})
    mesa = MesaConfig(
        gitlab_api=str(mesa_raw["gitlab_api"]).rstrip("/"),
        project_path=str(mesa_raw["project_path"]),
        git_url=str(mesa_raw["git_url"]),
        release_calendar_url=str(mesa_raw["release_calendar_url"]),
        branch_policy=MesaBranchPolicy(
            include_main=bool(bp_raw.get("include_main", True)),
            fallback_staging_count=int(bp_raw.get("fallback_staging_count", 2)),
            max_staging_branches=int(bp_raw.get("max_staging_branches", 3)),
        ),
    )
    runners_raw = raw["runners"]
    runners = RunnersConfig(
        provider=str(runners_raw.get("provider", "github")),
        mapping={
            str(prov): {str(k): str(v) for k, v in os_map.items()}
            for prov, os_map in runners_raw["map"].items()
        },
    )
    bp_diag = raw.get("backport", {})
    issues_raw = raw.get("issues", {})
    return GlobalConfig(
        mesa=mesa,
        runners=runners,
        keepalive_days=int(raw.get("keepalive_days", 30)),
        backport=BackportConfig(
            enabled=bool(bp_diag.get("enabled", True)),
            max_candidates_per_failure=int(bp_diag.get("max_candidates_per_failure", 5)),
            lookback_days=int(bp_diag.get("lookback_days", 270)),
            test_cherry_pick=bool(bp_diag.get("test_cherry_pick", True)),
        ),
        issues=IssuesConfig(
            enabled=bool(issues_raw.get("enabled", True)),
            base_label=str(issues_raw.get("base_label", "mesa-canary")),
        ),
    )


def load_target_config(path: Path) -> TargetConfig:
    raw = _load_yaml(path)
    up_raw = raw["upstream"]
    channels: dict[str, ChannelConfig] = {}
    for ch_name, ch_raw in up_raw["channels"].items():
        kind = str(ch_raw["kind"])
        if kind not in {"semver_stable", "semver_prerelease", "branch"}:
            raise ValueError(f"Unsupported channel kind '{kind}' in {path}")
        channels[str(ch_name)] = ChannelConfig(
            name=str(ch_name),
            kind=kind,
            severity=str(ch_raw.get("severity", "warning")),
            description=str(ch_raw.get("description", "")),
            branch=str(ch_raw["branch"]) if ch_raw.get("branch") else None,
            relevant_paths=tuple(str(p) for p in ch_raw.get("relevant_paths", [])),
            min_interval_hours=int(ch_raw.get("min_interval_hours", 0)),
        )
    upstream = UpstreamConfig(
        type=str(up_raw["type"]),
        repo=str(up_raw["repo"]),
        git_url=str(up_raw["git_url"]),
        channels=channels,
    )
    sp_raw = raw["subproject"]
    subproject = SubprojectConfig(
        name=str(sp_raw["name"]),
        wrap_path=str(sp_raw["wrap_path"]),
        default_directory=str(sp_raw["default_directory"]),
        meson_dependency_names=tuple(str(n) for n in sp_raw.get("meson_dependency_names", [sp_raw["name"]])),
    )
    meson_raw = raw.get("meson", {})
    toolchains = tuple(
        ToolchainConfig(
            id=str(tc["id"]),
            label=str(tc.get("label", tc["id"])),
            os=str(tc["os"]),
            kind=str(tc["kind"]),
            c_compiler=str(tc["c_compiler"]),
            cpp_compiler=str(tc["cpp_compiler"]),
            use_sccache=bool(tc.get("use_sccache", True)),
            needs_tracewpp=bool(tc.get("needs_tracewpp", False)),
            gallium_va=str(tc.get("gallium_va", "auto_windows")),
            msys2_sys=str(tc["msys2_sys"]) if tc.get("msys2_sys") else None,
            msys2_prefix=str(tc["msys2_prefix"]) if tc.get("msys2_prefix") else None,
            meson_args=tuple(str(a) for a in tc.get("meson_args", [])),
        )
        for tc in raw["toolchains"]
    )
    return TargetConfig(
        id=str(raw["id"]),
        name=str(raw.get("name", raw["id"])),
        workflow=str(raw["workflow"]),
        enabled=bool(raw.get("enabled", True)),
        upstream=upstream,
        subproject=subproject,
        meson_common_args=tuple(str(a) for a in meson_raw.get("common_args", [])),
        toolchains=toolchains,
    )


def load_all_targets(repo_root: Path) -> dict[str, TargetConfig]:
    targets_dir = repo_root / "config" / "targets"
    targets: dict[str, TargetConfig] = {}
    for yaml_path in sorted(targets_dir.glob("*.yaml")):
        cfg = load_target_config(yaml_path)
        if cfg.enabled:
            targets[cfg.id] = cfg
    return targets
