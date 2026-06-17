"""同步编排。Phase 1 实现 PULL(飞书 → 本地,只读飞书)。

PULL 流程(逐知识库):
  walk_space 得节点树 → 对每个 docx 节点用 obj_edit_time 判断是否需拉取
  → 拉块 → blocks_to_markdown → 带 frontmatter 写本地 → 更新 manifest

frontmatter 记录飞书映射元信息(node_token/document_id/space_id/url),既便于
push 阶段定位,也让本地文件自带溯源信息(比纯路径映射更抗重命名)。
"""
from __future__ import annotations

import fnmatch
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

from .client import FeishuClient
from .config import Config, SpaceMapping
from .converter.blocks_to_md import blocks_to_markdown
from .docx_reader import read_document
from .docx_writer import prepare_markdown, push_document
from .media import MediaManager
from .state import DocRecord, Manifest, decide_action, file_hash, sha256_text
from .wiki import WalkedNode, walk_space

logger = logging.getLogger("feishu_sync.sync")


@dataclass
class SyncReport:
    pulled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)   # 非 docx 节点
    orphaned: list[str] = field(default_factory=list)       # 飞书侧已不存在
    pruned: list[str] = field(default_factory=list)          # 被 --prune 删除的本地孤儿
    pushed: list[str] = field(default_factory=list)          # 推送到飞书的文档
    conflicts: list[str] = field(default_factory=list)       # 冲突(含裁决方向)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"拉取 {len(self.pulled)} · 推送 {len(self.pushed)} · 跳过 {len(self.skipped)} · "
                f"冲突 {len(self.conflicts)} · 非docx {len(self.unsupported)} · "
                f"孤立 {len(self.orphaned)} · 清理 {len(self.pruned)} · 告警 {len(self.warnings)}")


class SyncEngine:
    def __init__(self, cfg: Config, client: FeishuClient | None = None):
        self.cfg = cfg
        self.client = client or FeishuClient(cfg.feishu.app_id, cfg.feishu.app_secret)
        self.manifest = Manifest.load(cfg.state_dir / "manifest.json")
        self.media = MediaManager(self.client, cfg.assets_path, cfg.local.assets_dir)

    # ---- PULL ----
    def pull(self, dry_run: bool = False, only: str | None = None,
             force: bool = False, prune: bool = False) -> SyncReport:
        report = SyncReport()
        seen_tokens: set[str] = set()
        self._force = force

        spaces = self.cfg.enabled_spaces
        if not spaces:
            raise ValueError("config.yaml 的 spaces 列表为空,无可同步的知识库。先跑 `feishu-sync init`。")

        for sm in spaces:
            self.manifest.space_ids = sorted(set(self.manifest.space_ids) | {sm.space_id})
            logger.info("遍历知识库 %s (%s)…", sm.name or sm.space_id, sm.space_id)
            nodes = walk_space(self.client, sm.space_id)
            for wn in nodes:
                seen_tokens.add(wn.node_token)
                self._pull_node(sm, wn, dry_run, only, report)

        # 孤立检测:manifest 里属于已同步知识库、但本轮未见到的节点
        space_set = {s.space_id for s in spaces}
        for rec in self.manifest.all():
            if rec.space_id in space_set and rec.node_token not in seen_tokens and rec.status == "active":
                rec.status = "orphaned-feishu"
                report.orphaned.append(rec.local_path)
                logger.warning("飞书侧已不存在: %s(标记 orphaned,未删除本地)", rec.local_path)

        if prune:
            self._prune_orphans(spaces, seen_tokens, dry_run, report)

        self._warn_if_graph_stale(report)
        if not dry_run:
            self.manifest.save()
        return report

    def _prune_orphans(self, spaces, seen_tokens, dry_run: bool, report: SyncReport) -> None:
        """删除「已同步知识库目录」内、不属于本轮任何节点的 .md 文件(孤儿)。

        安全边界:仅在各 space 的 local_subdir 目录内操作;绝不触碰 vault 根目录
        的其它笔记、assets/ 或 _图谱/。用本轮 manifest 记录的有效路径作为白名单。
        """
        valid_paths = {
            (self.cfg.local.vault_path / rec.local_path).resolve()
            for rec in self.manifest.all()
            if rec.node_token in seen_tokens
        }
        for sm in spaces:
            space_root = self.cfg.space_dir(sm)
            if not space_root.exists():
                continue
            for md in space_root.rglob("*.md"):
                if md.resolve() not in valid_paths:
                    rel = md.relative_to(self.cfg.local.vault_path).as_posix()
                    if dry_run:
                        report.pruned.append(rel + "  (dry-run)")
                    else:
                        md.unlink()
                        report.pruned.append(rel)
                        logger.info("✗ 删除孤儿文件 %s", rel)
            # 清理空目录
            if not dry_run:
                for d in sorted(space_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()

    def _pull_node(self, sm: SpaceMapping, wn: WalkedNode, dry_run: bool,
                   only: str | None, report: SyncReport) -> None:
        rel_to_vault = f"{sm.local_subdir}/{wn.rel_path}"

        if only and not fnmatch.fnmatch(rel_to_vault, only):
            return

        if wn.obj_type != "docx":
            report.unsupported.append(f"[{wn.obj_type}] {rel_to_vault}")
            return

        edit_time = int(wn.node.get("obj_edit_time", 0) or 0)
        abs_path = self.cfg.local.vault_path / rel_to_vault
        rec = self.manifest.get(wn.node_token)

        # 判断是否需要拉取:无记录 / 飞书更新过 / 本地文件缺失
        need = (
            getattr(self, "_force", False)
            or rec is None
            or Manifest.feishu_changed(rec, 0, edit_time)
            or not abs_path.exists()
        )
        if not need:
            report.skipped.append(rel_to_vault)
            return

        if dry_run:
            report.pulled.append(rel_to_vault + "  (dry-run)")
            return

        # 拉取 + 转换 + 写盘
        doc = read_document(self.client, wn.obj_token)
        _, body = blocks_to_markdown(doc.blocks, image_resolver=self.media.resolve)
        post = frontmatter.Post(body, **{
            "title": doc.title or wn.title,
            "feishu": {
                "node_token": wn.node_token,
                "document_id": wn.obj_token,
                "space_id": sm.space_id,
                "url": wn.node.get("url", ""),
            },
        })
        content = frontmatter.dumps(post) + "\n"

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

        self.manifest.upsert(DocRecord(
            node_token=wn.node_token,
            space_id=sm.space_id,
            document_id=wn.obj_token,
            obj_type=wn.obj_type,
            title=doc.title or wn.title,
            local_path=rel_to_vault,
            feishu_revision=doc.revision_id,
            feishu_edit_time=edit_time,
            local_hash=sha256_text(content),
            local_mtime=abs_path.stat().st_mtime,
            last_synced=abs_path.stat().st_mtime,
            status="active",
        ))
        report.pulled.append(rel_to_vault)
        logger.info("✓ pulled %s", rel_to_vault)

    # ---- 公共辅助 ----
    def _warn_if_graph_stale(self, report: SyncReport) -> None:
        """图谱生成后有文档变更时,加一条过时提醒(选项A:提醒后手动重建)。"""
        stale = self.manifest.stale_docs()
        if stale:
            msg = (f"⚠ 知识图谱可能过时:{len(stale)} 篇文档自上次生成图谱后有变更,"
                   f"建议重新生成 _图谱/(让 Claude 重读并更新)")
            report.warnings.append(msg)
            logger.warning(msg)

    def _excluded(self, rel: str) -> bool:
        """该相对路径是否在 push_exclude 中(不参与推送)。"""
        return any(fnmatch.fnmatch(rel, pat) for pat in self.cfg.sync.push_exclude)

    def _backup(self, rel: str, content: bytes) -> None:
        """把将被覆盖的一侧内容存入 .state/backups/<时间戳>/。"""
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = self.cfg.state_dir / "backups" / ts / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    def _refresh_baseline_after_push(self, rec: DocRecord, abs_path: Path, cur_hash: str) -> None:
        """push 后刷新基线,使两侧下次都「看起来未变」,避免回声 ping-pong。"""
        meta = self.client.get_document(rec.document_id)
        node = self.client.get_node(rec.node_token)
        rec.feishu_revision = int(meta.get("revision_id", 0) or 0)
        rec.feishu_edit_time = int(node.get("obj_edit_time", 0) or 0)
        rec.local_hash = cur_hash
        rec.local_mtime = abs_path.stat().st_mtime
        rec.last_synced = time.time()
        self.manifest.upsert(rec)

    def _push_record(self, rec: DocRecord, dry_run: bool, report: SyncReport) -> str:
        """推送一条已映射记录的本地内容到飞书。返回结果标签。"""
        abs_path = self.cfg.local.vault_path / rec.local_path
        if not abs_path.exists():
            return "missing-local"
        markdown, blockers = prepare_markdown(abs_path.read_text(encoding="utf-8"))
        if blockers:
            report.warnings.append(f"含{'/'.join(blockers)},跳过 push(避免丢内容/破坏往返): {rec.local_path}")
            return "skip-blocked"
        if dry_run:
            report.pushed.append(rec.local_path + "  (dry-run)")
            return "dry-run"
        push_document(self.client, rec.document_id, markdown,
                      self.cfg.local.vault_path, self.cfg.local.assets_dir)
        self._refresh_baseline_after_push(rec, abs_path, file_hash(abs_path))
        report.pushed.append(rec.local_path)
        logger.info("↑ pushed %s", rec.local_path)
        return "pushed"

    # ---- PUSH(本地 → 飞书) ----
    def push(self, dry_run: bool = False, only: str | None = None) -> SyncReport:
        report = SyncReport()
        for sm in self.cfg.enabled_spaces:
            for rec in self.manifest.all():
                if rec.space_id != sm.space_id or rec.status != "active" or rec.obj_type != "docx":
                    continue
                rel = rec.local_path
                if only and not fnmatch.fnmatch(rel, only):
                    continue
                if self._excluded(rel):
                    report.skipped.append(rel)
                    continue
                abs_path = self.cfg.local.vault_path / rel
                if not abs_path.exists():
                    rec.status = "orphaned-local"
                    report.orphaned.append(rel)
                    continue
                cur_hash = file_hash(abs_path)
                cur_mtime = abs_path.stat().st_mtime
                if not Manifest.local_changed(rec, cur_hash, cur_mtime):
                    report.skipped.append(rel)
                    continue
                self._push_record(rec, dry_run, report)
            self._scan_new_local(sm, dry_run, only, report)   # 本地新增 → 飞书
        if not dry_run:
            self.manifest.save()
        return report

    # ---- 本地新增 → 飞书新建 ----
    def _resolve_parent_token(self, sm: SpaceMapping, rel: str) -> str | None:
        """按本地文件夹路径找飞书父节点 token;库根返回 None;父文件夹未映射也返回 None(建到根)。"""
        from pathlib import PurePosixPath
        parts = PurePosixPath(rel).parts
        inner = parts[1:] if parts and parts[0] == sm.local_subdir else parts
        if len(inner) <= 1:
            return None
        parent_doc_rel = sm.local_subdir + "/" + "/".join(inner[:-1]) + ".md"
        rec = self.manifest.by_local_path(parent_doc_rel)
        return rec.node_token if rec else None

    def _create_feishu_doc(self, sm: SpaceMapping, rel: str, abs_path: Path,
                           dry_run: bool, report: SyncReport) -> None:
        markdown, blockers = prepare_markdown(abs_path.read_text(encoding="utf-8"))
        if blockers:
            report.warnings.append(f"含{'/'.join(blockers)},跳过新建飞书: {rel}")
            return
        if dry_run:
            report.pushed.append(rel + "  (新建飞书·dry-run)")
            return
        title = abs_path.stem
        parent_token = self._resolve_parent_token(sm, rel)
        node = self.client.create_wiki_node(sm.space_id, parent_token, title)
        document_id = node.get("obj_token")
        push_document(self.client, document_id, markdown,
                      self.cfg.local.vault_path, self.cfg.local.assets_dir)
        meta = self.client.get_document(document_id)
        edit_time = int(self.client.get_node(node["node_token"]).get("obj_edit_time", 0) or 0)
        self.manifest.upsert(DocRecord(
            node_token=node["node_token"], space_id=sm.space_id, document_id=document_id,
            obj_type="docx", title=title, local_path=rel,
            feishu_revision=int(meta.get("revision_id", 0) or 0), feishu_edit_time=edit_time,
            local_hash=file_hash(abs_path), local_mtime=abs_path.stat().st_mtime,
            last_synced=time.time(), status="active",
        ))
        report.pushed.append(rel + "  (新建飞书)")
        logger.info("＋ 新建飞书文档 %s", rel)

    def _scan_new_local(self, sm: SpaceMapping, dry_run: bool, only: str | None, report: SyncReport) -> None:
        """扫描 space 目录内、不在 manifest 的 .md(本地新增),建到飞书。"""
        if not self.cfg.sync.create_new_feishu:
            return
        space_root = self.cfg.space_dir(sm)
        if not space_root.exists():
            return
        mapped = {r.local_path for r in self.manifest.all()}
        for md in sorted(space_root.rglob("*.md")):
            rel = md.relative_to(self.cfg.local.vault_path).as_posix()
            if rel in mapped or self._excluded(rel):
                continue
            if only and not fnmatch.fnmatch(rel, only):
                continue
            self._create_feishu_doc(sm, rel, md, dry_run, report)

    # ---- 双向同步 ----
    def sync(self, dry_run: bool = False, only: str | None = None) -> SyncReport:
        report = SyncReport()
        conflict_policy = self.cfg.sync.conflict
        spaces = self.cfg.enabled_spaces
        if not spaces:
            raise ValueError("config.yaml 的 spaces 列表为空。")

        for sm in spaces:
            self.manifest.space_ids = sorted(set(self.manifest.space_ids) | {sm.space_id})
            nodes = {wn.node_token: wn for wn in walk_space(self.client, sm.space_id)}
            rec_tokens = {r.node_token for r in self.manifest.all() if r.space_id == sm.space_id}
            for token in sorted(set(nodes) | rec_tokens):
                self._sync_one(sm, token, nodes.get(token), self.manifest.get(token),
                               conflict_policy, dry_run, only, report)
            self._scan_new_local(sm, dry_run, only, report)   # 本地新增 → 飞书

        self._warn_if_graph_stale(report)
        if not dry_run:
            self.manifest.save()
        return report

    def _sync_one(self, sm, token, wn, rec, conflict_policy, dry_run, only, report) -> None:
        # 非 docx 飞书节点:记录跳过
        if wn is not None and wn.obj_type != "docx":
            report.unsupported.append(f"[{wn.obj_type}] {sm.local_subdir}/{wn.rel_path}")
            return

        rel = rec.local_path if rec else (f"{sm.local_subdir}/{wn.rel_path}" if wn else None)
        if rel is None or (only and not fnmatch.fnmatch(rel, only)):
            return

        abs_path = self.cfg.local.vault_path / rel
        feishu_present = wn is not None
        feishu_edit = int(wn.node.get("obj_edit_time", 0) or 0) if wn else 0
        local_present = abs_path.exists()
        cur_hash = file_hash(abs_path) if local_present else ""
        cur_mtime = abs_path.stat().st_mtime if local_present else 0.0

        feishu_changed = (Manifest.feishu_changed(rec, 0, feishu_edit) if rec else feishu_present)
        local_changed = (Manifest.local_changed(rec, cur_hash, cur_mtime) if rec else local_present)

        action = decide_action(rec, feishu_present, feishu_changed, feishu_edit,
                               local_present, local_changed, cur_mtime, conflict_policy)

        # 推送类动作若命中排除规则,则降级为跳过(不回推派生/图谱文件)
        if action in ("push", "conflict-push", "new-feishu") and self._excluded(rel):
            report.skipped.append(rel)
            return

        if action == "skip":
            report.skipped.append(rel)
        elif action in ("pull", "new-local"):
            self._pull_node(sm, wn, dry_run, None, report)
        elif action == "push":
            self._push_record(rec, dry_run, report)
        elif action == "conflict-pull":
            self._do_conflict(sm, wn, rec, "pull", rel, abs_path, dry_run, report)
        elif action == "conflict-push":
            self._do_conflict(sm, wn, rec, "push", rel, abs_path, dry_run, report)
        elif action == "conflict":
            self._write_conflict_copy(wn, rel, abs_path, dry_run, report)
        elif action == "new-feishu":
            report.warnings.append(f"本地新增文档,自动建飞书节点暂未启用: {rel}")
        elif action == "orphan-feishu":
            if rec:
                rec.status = "orphaned-feishu"
            report.orphaned.append(rel + " (飞书侧消失)")
        elif action == "orphan-local":
            if rec:
                rec.status = "orphaned-local"
            report.orphaned.append(rel + " (本地消失)")

    def _do_conflict(self, sm, wn, rec, winner, rel, abs_path, dry_run, report) -> None:
        """latest-wins/显式策略下的冲突:备份失败方后按 winner 执行。"""
        tag = f"{rel} → {'飞书覆盖本地' if winner == 'pull' else '本地覆盖飞书'}"
        report.conflicts.append(tag)
        if dry_run:
            (report.pulled if winner == "pull" else report.pushed).append(rel + "  (conflict-dry-run)")
            return
        if winner == "pull":
            if abs_path.exists():                       # 备份将被覆盖的本地
                self._backup(rel, abs_path.read_bytes())
            self._pull_node(sm, wn, dry_run, None, report)
        else:
            if rec:                                     # 备份将被覆盖的飞书(拉一份当前飞书内容)
                doc = read_document(self.client, rec.document_id)
                _, body = blocks_to_markdown(doc.blocks)
                self._backup(rel + ".feishu.md", body.encode("utf-8"))
                self._push_record(rec, dry_run, report)
        logger.warning("⚔ 冲突: %s", tag)

    def _write_conflict_copy(self, wn, rel, abs_path, dry_run, report) -> None:
        """manual 策略:不覆盖任何一方,把飞书版写成 .conflict 副本供人工合并。"""
        report.conflicts.append(rel + " (manual,生成 .conflict 副本)")
        if dry_run or wn is None:
            return
        doc = read_document(self.client, wn.obj_token)
        _, body = blocks_to_markdown(doc.blocks, image_resolver=self.media.resolve)
        copy = abs_path.with_suffix(".conflict.md")
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(body, encoding="utf-8")
        logger.warning("⚔ 冲突(manual): 已写 %s", copy.name)
