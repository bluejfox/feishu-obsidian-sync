"""同步状态清单(manifest):记录每篇文档的「飞书 ↔ 本地」映射与同步基线。

以 node_token 为主键(Wiki 节点稳定标识,可跨重命名追踪)。变更检测依据:
  - 飞书侧:当前 revision_id / edit_time 是否 > 基线
  - 本地侧:当前内容 hash / mtime 是否 ≠ 基线
manifest 落盘 <root>/.state/manifest.json(已 gitignore)。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1


@dataclass
class DocRecord:
    node_token: str                 # Wiki 节点主键
    space_id: str
    document_id: str = ""           # docx obj_token
    obj_type: str = "docx"
    title: str = ""
    local_path: str = ""            # 相对 vault_path 的 POSIX 路径
    feishu_revision: int = 0
    feishu_edit_time: int = 0       # 秒级时间戳
    local_hash: str = ""            # 上次同步时本地正文 sha256
    local_mtime: float = 0.0
    last_synced: float = 0.0
    status: str = "active"          # active | orphaned-feishu | orphaned-local


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.space_ids: list[str] = []
        self.graph_built_at: float = 0.0   # 知识图谱上次生成的时间戳(0=从未标记)
        self._docs: dict[str, DocRecord] = {}

    # ---- 持久化 ----
    @classmethod
    def load(cls, path: Path) -> "Manifest":
        m = cls(path)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            m.space_ids = data.get("space_ids", [])
            m.graph_built_at = float(data.get("graph_built_at", 0.0) or 0.0)
            for d in data.get("docs", []):
                # 容忍未知字段:仅取 DocRecord 已知字段
                known = {k: d[k] for k in DocRecord.__annotations__ if k in d}
                rec = DocRecord(**known)
                m._docs[rec.node_token] = rec
        return m

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "space_ids": self.space_ids,
            "graph_built_at": self.graph_built_at,
            "docs": [asdict(r) for r in self._docs.values()],
        }
        # 原子写
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ---- 访问 ----
    def get(self, node_token: str) -> DocRecord | None:
        return self._docs.get(node_token)

    def by_local_path(self, local_path: str) -> DocRecord | None:
        for r in self._docs.values():
            if r.local_path == local_path:
                return r
        return None

    def all(self) -> Iterable[DocRecord]:
        return list(self._docs.values())

    def upsert(self, rec: DocRecord) -> None:
        self._docs[rec.node_token] = rec

    def remove(self, node_token: str) -> None:
        self._docs.pop(node_token, None)

    def stale_docs(self) -> list[DocRecord]:
        """图谱生成后发生过同步变更的文档(用于过时提醒)。
        graph_built_at 为 0(从未标记)时返回空,避免首次误报。"""
        if not self.graph_built_at:
            return []
        return [r for r in self._docs.values()
                if r.status == "active" and r.last_synced > self.graph_built_at]

    # ---- 变更检测 ----
    @staticmethod
    def feishu_changed(rec: DocRecord, cur_revision: int, cur_edit_time: int) -> bool:
        if cur_revision and cur_revision != rec.feishu_revision:
            return True
        return bool(cur_edit_time and cur_edit_time > rec.feishu_edit_time)

    @staticmethod
    def local_changed(rec: DocRecord, cur_hash: str, cur_mtime: float) -> bool:
        if not cur_hash:                      # 文件已不存在
            return rec.local_hash != ""
        if cur_hash != rec.local_hash:
            return True
        return cur_mtime > rec.local_mtime + 1  # 容忍 1s 抖动


def decide_action(
    rec: DocRecord | None,
    feishu_present: bool, feishu_changed: bool, feishu_edit_time: int,
    local_present: bool, local_changed: bool, local_mtime: float,
    conflict: str = "latest-wins",
) -> str:
    """返回同步动作:pull / push / skip / conflict-pull / conflict-push /
    new-local / new-feishu / orphan-feishu / orphan-local。

    纯函数,便于单测。实际 I/O 由 sync.py 执行。"""
    if rec is None:
        if feishu_present and not local_present:
            return "new-local"
        if local_present and not feishu_present:
            return "new-feishu"
        # 两侧都在但无基线:按内容首次登记,默认以飞书为准拉取
        return "pull"

    if feishu_present and not local_present:
        return "orphan-local"      # 本地被删
    if local_present and not feishu_present:
        return "orphan-feishu"     # 飞书被删

    if feishu_changed and local_changed:
        if conflict == "feishu-wins":
            return "conflict-pull"
        if conflict == "local-wins":
            return "conflict-push"
        if conflict == "manual":
            return "conflict"
        # latest-wins:比较时间戳
        return "conflict-pull" if feishu_edit_time >= local_mtime else "conflict-push"
    if feishu_changed:
        return "pull"
    if local_changed:
        return "push"
    return "skip"
