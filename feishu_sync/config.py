"""配置加载:YAML 文件 + 环境变量(凭证优先取环境变量)。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 项目根:本文件位于 <root>/feishu_sync/config.py
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str


@dataclass
class SpaceMapping:
    """一个飞书知识库 → vault 下一个子目录的映射。"""
    space_id: str
    local_subdir: str
    name: str = ""
    enabled: bool = True


@dataclass
class LocalConfig:
    vault_path: Path
    assets_dir: str = "assets"


@dataclass
class ObsidianConfig:
    enabled: bool = False
    graph_dir: str = "_图谱"


@dataclass
class SyncConfig:
    conflict: str = "latest-wins"
    push_exclude: list[str] = field(default_factory=list)
    delete_protection: bool = True
    create_new_feishu: bool = True   # 本地新增文档(在 spaces 目录内)自动建到飞书


@dataclass
class Config:
    feishu: FeishuConfig
    local: LocalConfig
    sync: SyncConfig
    spaces: list[SpaceMapping] = field(default_factory=list)
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def state_dir(self) -> Path:
        return ROOT / ".state"

    @property
    def assets_path(self) -> Path:
        return self.local.vault_path / self.local.assets_dir

    @property
    def enabled_spaces(self) -> list[SpaceMapping]:
        return [s for s in self.spaces if s.enabled]

    def space_dir(self, sm: SpaceMapping) -> Path:
        """某知识库在本地的根目录。"""
        return self.local.vault_path / sm.local_subdir


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    elif not (os.getenv("FEISHU_APP_ID") and os.getenv("FEISHU_APP_SECRET")):
        raise FileNotFoundError(
            f"未找到配置文件 {cfg_path},且未设置 FEISHU_APP_ID/FEISHU_APP_SECRET 环境变量。\n"
            f"请复制 config.example.yaml 为 config.yaml 并填写。"
        )

    fe = data.get("feishu", {}) or {}
    app_id = os.getenv("FEISHU_APP_ID") or fe.get("app_id", "")
    app_secret = os.getenv("FEISHU_APP_SECRET") or fe.get("app_secret", "")
    if not app_id or not app_secret:
        raise ValueError("缺少飞书 app_id / app_secret(检查 config.yaml 或环境变量)。")

    lo = data.get("local", {}) or {}
    vault = lo.get("vault_path")
    if not vault:
        raise ValueError("缺少 local.vault_path。")

    sy = data.get("sync", {}) or {}
    ob = data.get("obsidian", {}) or {}

    spaces: list[SpaceMapping] = []
    for s in data.get("spaces", []) or []:
        sid = str(s.get("space_id", "")).strip()
        if not sid:
            continue
        spaces.append(SpaceMapping(
            space_id=sid,
            local_subdir=s.get("local_subdir") or s.get("name") or sid,
            name=s.get("name", ""),
            enabled=bool(s.get("enabled", True)),
        ))

    return Config(
        feishu=FeishuConfig(app_id=app_id, app_secret=app_secret),
        local=LocalConfig(
            vault_path=Path(vault).expanduser().resolve(),
            assets_dir=lo.get("assets_dir", "assets"),
        ),
        sync=SyncConfig(
            conflict=sy.get("conflict", "latest-wins"),
            push_exclude=list(sy.get("push_exclude", []) or []),
            delete_protection=bool(sy.get("delete_protection", True)),
            create_new_feishu=bool(sy.get("create_new_feishu", True)),
        ),
        spaces=spaces,
        obsidian=ObsidianConfig(
            enabled=bool(ob.get("enabled", False)),
            graph_dir=ob.get("graph_dir", "_图谱"),
        ),
        raw=data,
    )
