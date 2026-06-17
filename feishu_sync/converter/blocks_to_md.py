"""飞书 docx 块树 → Markdown。

输入:list_blocks 返回的扁平块列表(每块含 block_id/parent_id/children/block_type
及一个与类型同名的内容键)。用 children 重建树,深度优先渲染。

类型分发用「内容键探测」而非数字枚举(更稳)。图片在 Phase 1 输出占位
`![](feishu-media://<token>)`,由 Phase 2 改写为本地路径;表格在 Phase 3 完善。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .richtext import elements_to_md

# 块 dict 中的元信息键(非内容)
_META_KEYS = {"block_id", "parent_id", "children", "block_type", "comment_ids"}
# 紧凑排版(连续项之间不空行)的列表类型
_LIST_KEYS = {"bullet", "ordered", "todo"}
# heading1..heading9 → 级别
_HEADING = {f"heading{i}": i for i in range(1, 10)}

# 代码块语言:飞书 code.style.language 为整型枚举。先留空(输出无语言的安全代码栏),
# 待拿到真实代码块样例后在此校准映射。
_LANG_MAP: dict[int, str] = {}

ImageResolver = Callable[[str, dict], str]  # (media_token, image_block) -> 完整嵌入字符串


def _content_key(block: dict) -> str | None:
    for k in block:
        if k not in _META_KEYS:
            return k
    return None


class BlocksToMarkdown:
    def __init__(self, blocks: list[dict], image_resolver: ImageResolver | None = None):
        self.by_id: dict[str, dict] = {b["block_id"]: b for b in blocks}
        self.image_resolver = image_resolver
        self.warnings: list[str] = []
        self._root = self._find_root(blocks)

    @staticmethod
    def _find_root(blocks: list[dict]) -> dict | None:
        for b in blocks:
            if b.get("block_type") == 1 or not b.get("parent_id"):
                return b
        return blocks[0] if blocks else None

    # ---- 入口 ----
    def convert(self) -> tuple[str, str]:
        """返回 (title, body_markdown)。"""
        if not self._root:
            return "", ""
        title = ""
        if "page" in self._root:
            title = elements_to_md(self._root["page"].get("elements")).strip()
        lines = self._render_children(self._root.get("children", []), indent=0)
        body = self._cleanup("\n".join(lines))
        return title, body

    # ---- 渲染 ----
    def _render_children(self, child_ids: list[str], indent: int) -> list[str]:
        out: list[str] = []
        ordered_idx = 0
        prev_key: str | None = None
        for cid in child_ids:
            b = self.by_id.get(cid)
            if not b:
                continue
            key = _content_key(b)
            ordered_idx = ordered_idx + 1 if key == "ordered" else 0
            block_lines = self._render_block(b, key, indent, ordered_idx)
            if out:
                tight = key in _LIST_KEYS and prev_key in _LIST_KEYS
                if not tight:
                    out.append("")
            out.extend(block_lines)
            prev_key = key
        return out

    def _inline(self, block: dict, key: str) -> str:
        return elements_to_md((block.get(key) or {}).get("elements"))

    def _render_block(self, b: dict, key: str | None, indent: int, ordered_idx: int) -> list[str]:
        pad = "  " * indent

        if key is None or key == "page":
            return []

        if key in _HEADING:
            level = min(_HEADING[key], 6)
            return [pad + "#" * level + " " + self._inline(b, key)]

        if key == "text":
            txt = self._inline(b, key)
            return [pad + txt] if txt else [""]

        if key == "bullet":
            line = pad + "- " + self._inline(b, key)
            return [line, *self._render_children(b.get("children", []), indent + 1)]

        if key == "ordered":
            line = pad + f"{ordered_idx}. " + self._inline(b, key)
            return [line, *self._render_children(b.get("children", []), indent + 1)]

        if key == "todo":
            done = (b.get("todo", {}).get("style", {}) or {}).get("done")
            mark = "x" if done else " "
            line = pad + f"- [{mark}] " + self._inline(b, key)
            return [line, *self._render_children(b.get("children", []), indent + 1)]

        if key == "code":
            lang = _LANG_MAP.get((b.get("code", {}).get("style", {}) or {}).get("language"), "")
            raw = "".join(
                (el.get("text_run") or {}).get("content", "")
                for el in (b.get("code", {}).get("elements") or [])
            )
            return ["```" + lang, *raw.split("\n"), "```"]

        if key == "quote":
            body = [self._inline(b, key), *self._render_children(b.get("children", []), 0)]
            return [("> " + ln) if ln else ">" for ln in body]

        if key in ("quote_container",):
            body = self._render_children(b.get("children", []), 0)
            return [("> " + ln) if ln else ">" for ln in body]

        if key == "callout":
            body = self._render_children(b.get("children", []), 0)
            return ["> [!note]", *[("> " + ln) if ln else ">" for ln in body]]

        if key == "divider":
            return ["---"]

        if key == "image":
            token = (b.get("image") or {}).get("token", "")
            if self.image_resolver:
                # resolver 返回完整嵌入字符串(如 Obsidian `![[assets/x.png]]`)
                return [pad + self.image_resolver(token, b)]
            return [pad + f"![](feishu-media://{token})"]

        if key == "table":
            return self._render_table(b)

        if key in ("grid", "grid_column", "table_cell"):
            # grid(分栏)无 Markdown 对应,平铺其子内容;table_cell 正常由 table 直接处理,
            # 仅在异常脱离 table 时才走到这里
            return self._render_children(b.get("children", []), indent)

        # 兜底:不丢内容,渲染子节点并告警
        self.warnings.append(f"未知块类型 '{key}',已降级处理")
        body = self._render_children(b.get("children", []), indent)
        inline = self._inline(b, key)
        head = [pad + inline] if inline else []
        return [*head, *body] if (head or body) else []

    def _cell_text(self, cell_id: str) -> str:
        """渲染一个 table_cell 的内容为单行 Markdown(块内换行→<br>,转义竖线)。"""
        cell = self.by_id.get(cell_id)
        if not cell:
            return ""
        parts: list[str] = []
        for cid in cell.get("children", []):
            child = self.by_id.get(cid)
            if not child:
                continue
            ck = _content_key(child)
            inline = self._inline(child, ck) if ck else ""
            if inline:
                parts.append(inline)
        text = "<br>".join(parts)
        return text.replace("|", "\\|").replace("\n", "<br>")

    def _render_table(self, b: dict) -> list[str]:
        tbl = b.get("table", {}) or {}
        prop = tbl.get("property", {}) or {}
        rows = int(prop.get("row_size", 0) or 0)
        cols = int(prop.get("column_size", 0) or 0)
        cells = tbl.get("cells") or b.get("children", [])
        if rows <= 0 or cols <= 0 or not cells:
            self.warnings.append("空表格或缺少行列信息,已跳过")
            return []

        grid: list[list[str]] = []
        for r in range(rows):
            row = []
            for c in range(cols):
                idx = r * cols + c
                row.append(self._cell_text(cells[idx]) if idx < len(cells) else "")
            grid.append(row)

        # 首行作表头(Markdown 表格强制需要表头分隔行)
        lines = ["| " + " | ".join(grid[0]) + " |",
                 "| " + " | ".join(["---"] * cols) + " |"]
        for row in grid[1:]:
            lines.append("| " + " | ".join(row) + " |")
        return lines

    @staticmethod
    def _cleanup(text: str) -> str:
        text = re.sub(r"\n{3,}", "\n\n", text)   # 折叠多余空行
        return text.strip() + "\n"


def blocks_to_markdown(blocks: list[dict], image_resolver: ImageResolver | None = None) -> tuple[str, str]:
    conv = BlocksToMarkdown(blocks, image_resolver)
    title, body = conv.convert()
    return title, body
