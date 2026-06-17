"""把本地 Markdown 写回飞书 docx(PUSH),支持表格。

非表格内容:官方 convert(markdown→blocks)+ block_descendant.create 一次写入。
表格:飞书表格不能随 descendants 提交单元格内容,需特殊流程——
  ① 用 block_children.create 提交"只含 property(行列数)的裸表格块",飞书自动生成空单元格;
  ② 从响应里取真实 cell block_id(行优先顺序);
  ③ 逐格用 block_children.create 在 cell 下写入文本块。
整篇按"文本段 / 表格"分段、按顺序追加到页面末尾。

安全约束:
  - 剥离 frontmatter;含**图片**(![[...]])的文档跳过 push(图片反向上传未实现)。
  - 单元格内文本做轻量去标记(**/`/* 等),飞书表格单元格按纯文本写入。
"""
from __future__ import annotations

import re

import frontmatter

from .client import FeishuClient

_WIKILINK_ALIAS = re.compile(r"\[\[[^\]\|]+\|([^\]]+)\]\]")
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_IMG_EMBED = re.compile(r"!\[\[[^\]]+\]\]")
_IMG_MD = re.compile(r"!\[[^\]]*\]\((feishu-media://[^)]+)\)")
# 一整块 Markdown 表格:表头行 + |---| 分隔行 + 若干数据行
_TABLE_BLOCK = re.compile(
    r"(?m)^[ \t]*\|.+\|[ \t]*\n[ \t]*\|[ \t:|\-]+\|[ \t]*\n(?:[ \t]*\|.*\|[ \t]*\n?)*"
)


def prepare_markdown(file_text: str) -> tuple[str, list[str]]:
    """返回 (用于上传的 markdown 正文, blockers)。blockers 非空则不应 push。"""
    post = frontmatter.loads(file_text)
    body = post.content
    blockers: list[str] = []
    if _IMG_EMBED.search(body) or _IMG_MD.search(body):
        blockers.append("图片")
    body = _WIKILINK_ALIAS.sub(r"\1", body)
    body = _WIKILINK.sub(r"\1", body)
    return body.strip() + "\n", blockers


# ---- 表格解析与块构造 ----
def _clean_cell(text: str) -> str:
    text = text.replace("\\|", "|").replace("<br>", " ")
    text = re.sub(r"\*\*|`|(?<!\*)\*(?!\*)", "", text)  # 去掉 **bold** / *italic* / `code` 标记
    return text.strip()


def _parse_md_table(md: str) -> list[list[str]]:
    """Markdown 表格 → 行优先 list[list[str]](已去分隔行)。"""
    rows: list[list[str]] = []
    lines = [l for l in md.splitlines() if l.strip().startswith("|")]
    for i, l in enumerate(lines):
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        if i == 1 and all(set(c) <= set("-: ") for c in cells):  # 分隔行
            continue
        rows.append([_clean_cell(c) for c in cells])
    return rows


def _split_tables(markdown: str):
    """把整篇切成按序的 [("text", 段), ("table", 段), ...]。"""
    segments, pos = [], 0
    for m in _TABLE_BLOCK.finditer(markdown):
        if m.start() > pos:
            segments.append(("text", markdown[pos:m.start()]))
        segments.append(("table", m.group()))
        pos = m.end()
    if pos < len(markdown):
        segments.append(("text", markdown[pos:]))
    return segments


def _bare_table_block(nrow: int, ncol: int):
    from lark_oapi.api.docx.v1 import Block, Table, TableProperty
    prop = TableProperty.builder().row_size(nrow).column_size(ncol).header_row(True).build()
    return Block.builder().block_id("t").block_type(31).table(Table.builder().property(prop).build()).build()


def _text_block(text: str):
    from lark_oapi.api.docx.v1 import Block, Text, TextElement, TextRun
    tr = TextRun.builder().content(text).build()
    el = TextElement.builder().text_run(tr).build()
    return Block.builder().block_id("c").block_type(2).text(Text.builder().elements([el]).build()).build()


def _table_cell_ids(resp: dict, client: FeishuClient, document_id: str) -> list[str]:
    for b in resp.get("children", []) or []:
        if b.get("block_type") == 31:
            cells = (b.get("table") or {}).get("cells")
            if cells:
                return cells
    # 兜底:重列,取最后一个表格的 cells
    tabs = [b for b in client.list_blocks(document_id) if b.get("block_type") == 31]
    return (tabs[-1].get("table") or {}).get("cells", []) if tabs else []


def push_document(client: FeishuClient, document_id: str, markdown: str) -> int:
    """清空并用 markdown 重建 docx 内容(支持表格)。返回写入的顶层段数。"""
    blocks = client.list_blocks(document_id)
    root = next((b for b in blocks if b.get("block_type") == 1 or not b.get("parent_id")), None)
    child_count = len(root.get("children", []) if root else [])
    client.delete_block_children(document_id, document_id, 0, child_count)

    idx = 0
    for kind, seg in _split_tables(markdown):
        if kind == "text":
            if not seg.strip():
                continue
            new_blocks, first_ids = client.convert_markdown(seg)
            if new_blocks:
                client.create_block_descendants(document_id, document_id, first_ids, new_blocks, idx)
                idx += len(first_ids)
        else:  # table
            rows = _parse_md_table(seg)
            if not rows:
                continue
            ncol = max(len(r) for r in rows)
            resp = client.create_block_children(document_id, document_id, [_bare_table_block(len(rows), ncol)], idx)
            cell_ids = _table_cell_ids(resp, client, document_id)
            for i, cid in enumerate(cell_ids):
                r, c = divmod(i, ncol)
                text = rows[r][c] if (r < len(rows) and c < len(rows[r])) else ""
                if text:
                    client.create_block_children(document_id, cid, [_text_block(text)], 0)
            idx += 1
    return idx
