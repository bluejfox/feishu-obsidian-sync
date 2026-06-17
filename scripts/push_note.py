"""把一篇 Markdown 笔记新建到飞书知识库(供 Claude Code 的 /feishu-note 调用)。

流程:解析标题 → 在指定知识库确保父文件夹存在 → 在其下新建 docx → 写入内容 → 打印链接。
标题取 Markdown 第一行 `# 标题`(可用 --title 覆盖)。含表格/图片会被拒绝(飞书反向写入未支持)。

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
from feishu_sync.config import load_config  # noqa: E402
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
