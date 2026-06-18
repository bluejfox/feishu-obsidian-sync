"""把一篇 Markdown 笔记新建到飞书知识库(供 Claude Code 的 /feishu-note 调用)。

流程:解析标题 → 在指定知识库确保父文件夹存在 → 在其下新建 docx → 写入内容
      → 在本地 vault 落一份镜像并写入 manifest 基线 → 打印链接。
标题取 Markdown 第一行 `# 标题`(可用 --title 覆盖)。含表格/图片会被拒绝(飞书反向写入未支持)。

本地留底(默认开启,--no-local 关闭):推送成功后复用 SyncEngine._pull_node 把新建
文档回拉到 vault,使本地文件 == 未来 pull 的产物,并登记 manifest 基线。这样后续
自动同步会把"仅本地改动"判为 push(本地覆盖飞书),而不会用飞书版覆盖你的本地修改。

用法:
  push_note.py --space 个人 --parent "Claude笔记" --file note.md
  echo "# 标题\n正文" | push_note.py --space 个人 --parent "Claude笔记"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feishu_sync.client import FeishuClient  # noqa: E402
from feishu_sync.config import Config, SpaceMapping, load_config  # noqa: E402
from feishu_sync.docx_writer import prepare_markdown, push_document  # noqa: E402


def _resolve_space_id(cfg, name_or_id: str) -> str:
    for sm in cfg.spaces:
        if name_or_id in (sm.name, sm.space_id, sm.local_subdir):
            return sm.space_id
    return name_or_id  # 当作 space_id 直接用


def _ensure_parent(client: FeishuClient, space_id: str, parent_title: str | None) -> str | None:
    """确保父文件夹节点存在,返回其 node_token;parent_title 为空则返回 None(建在根)。"""
    if not parent_title:
        return None
    for node in client.iter_nodes(space_id, None):
        if node.get("title") == parent_title:
            return node.get("node_token")
    node = client.create_wiki_node(space_id, None, parent_title)
    return node.get("node_token")


def _space_mapping(cfg: Config, space_id: str) -> SpaceMapping | None:
    """按 space_id 找到配置里的库映射(用于解析本地子目录)。未配置则返回 None。"""
    for sm in cfg.spaces:
        if sm.space_id == space_id:
            return sm
    return None


def _mirror_local(cfg: Config, client: FeishuClient, sm: SpaceMapping,
                  node: dict, parent_title: str | None, title: str) -> str:
    """把刚新建的飞书文档回拉到本地 vault 并登记 manifest 基线,返回相对 vault 路径。

    复用 SyncEngine._pull_node:写入与正常同步完全一致的带 frontmatter 文件,且把
    两侧 revision/edit_time/hash/mtime 写进 manifest,使下次同步判为 skip;此后
    仅本地改动会被判为 push(本地覆盖飞书),不会被飞书版回拉覆盖。
    """
    from feishu_sync.sync import SyncEngine, SyncReport
    from feishu_sync.wiki import WalkedNode, sanitize_filename

    # 取节点完整信息(含 obj_edit_time / obj_token),构造与 walk_space 一致的相对路径
    node_full = client.get_node(node["node_token"])
    segments = [sanitize_filename(parent_title)] if parent_title else []
    segments.append(sanitize_filename(title))
    rel_path = "/".join(segments) + ".md"

    wn = WalkedNode(node=node_full, rel_path=rel_path, depth=len(segments) - 1)
    engine = SyncEngine(cfg, client)
    engine._pull_node(sm, wn, dry_run=False, only=None, report=SyncReport())
    engine.manifest.save()
    return f"{sm.local_subdir}/{rel_path}"


def _extract_title(markdown: str, override: str | None) -> tuple[str, str]:
    """返回 (标题, 去掉首个 # 标题行后的正文)。"""
    if override:
        return override, markdown
    lines = markdown.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            title = ln[2:].strip()
            body = "\n".join(lines[:i] + lines[i + 1:]).strip() + "\n"
            return title, body
    return "Claude 笔记", markdown


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", default="个人", help="知识库名或 space_id")
    ap.add_argument("--parent", default="Claude笔记", help="父文件夹标题(自动创建)")
    ap.add_argument("--title", default=None, help="覆盖标题(默认取首个 # 行)")
    ap.add_argument("--file", default=None, help="Markdown 文件;省略则读 stdin")
    ap.add_argument("--no-local", action="store_true",
                    help="不在本地 vault 留底、不写 manifest 基线(默认会留底)")
    args = ap.parse_args()

    raw = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    if not raw.strip():
        print("✗ 内容为空", file=sys.stderr)
        return 2

    body, blockers = prepare_markdown(raw)
    if blockers:
        print(f"✗ 笔记含{'/'.join(blockers)},飞书反向写入暂不支持,请改用列表/去掉图片后重试。", file=sys.stderr)
        return 2
    title, body = _extract_title(body, args.title)

    cfg = load_config()
    client = FeishuClient(cfg.feishu.app_id, cfg.feishu.app_secret)
    space_id = _resolve_space_id(cfg, args.space)

    parent_token = _ensure_parent(client, space_id, args.parent)
    node = client.create_wiki_node(space_id, parent_token, title)
    document_id = node.get("obj_token")
    push_document(client, document_id, body, cfg.local.vault_path, cfg.local.assets_dir)

    url = node.get("url", "")
    print(f"✅ 已推送到飞书「{args.space}」/{args.parent}:{title}")
    print(f"   链接: {url}")
    print(f"   document_id: {document_id}")

    # 本地留底 + manifest 基线(默认开启)
    if not args.no_local:
        sm = _space_mapping(cfg, space_id)
        if sm is None:
            print(f"   ⚠ 知识库 {space_id} 未在 config.yaml 配置,跳过本地留底。", file=sys.stderr)
        else:
            rel = _mirror_local(cfg, client, sm, node, args.parent, title)
            print(f"   本地副本: {rel}(已登记 manifest 基线,后续本地修改不会被飞书覆盖)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
