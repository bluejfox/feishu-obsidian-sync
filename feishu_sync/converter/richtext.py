"""行内富文本:飞书 elements ⇄ Markdown 行内字符串。

飞书一个块的正文是 elements 数组,每个 element 可能是:
  - text_run:普通文本 + text_element_style(bold/italic/strikethrough/inline_code/link)
  - mention_doc:指向其他飞书文档的链接
  - mention_user:@用户
  - equation:行内公式
本模块只负责「行内」级别;块级结构由 blocks_to_md 处理。
"""
from __future__ import annotations

from urllib.parse import unquote
from typing import Any

# 反向(md -> elements)所需:行内样式标记
_BOLD = "**"
_ITALIC = "*"
_STRIKE = "~~"
_CODE = "`"


def _wrap(text: str, style: dict[str, Any]) -> str:
    """按样式给一段纯文本加 Markdown 行内标记。空白串不加。"""
    if not text:
        return text
    link = (style.get("link") or {}).get("url")
    inline_code = style.get("inline_code")

    if inline_code:
        # 行内代码内部不再叠加其它样式(Markdown 限制)
        out = f"{_CODE}{text}{_CODE}"
    else:
        out = text
        if style.get("bold"):
            out = f"{_BOLD}{out}{_BOLD}"
        if style.get("italic"):
            out = f"{_ITALIC}{out}{_ITALIC}"
        if style.get("strikethrough"):
            out = f"{_STRIKE}{out}{_STRIKE}"

    if link:
        out = f"[{out}]({unquote(link)})"
    return out


def element_to_md(el: dict[str, Any]) -> str:
    """单个 element → Markdown 行内字符串。"""
    if "text_run" in el:
        tr = el["text_run"]
        return _wrap(tr.get("content", ""), tr.get("text_element_style", {}) or {})

    if "mention_doc" in el:
        md = el["mention_doc"]
        title = md.get("title", "") or "文档"
        url = md.get("url", "")
        return f"[{title}]({unquote(url)})" if url else title

    if "mention_user" in el:
        mu = el["mention_user"]
        # 仅有 user_id 时无法解析昵称,保留占位
        name = mu.get("name") or mu.get("user_id", "")
        return f"@{name}" if name else ""

    if "equation" in el:
        content = el["equation"].get("content", "").strip()
        return f"${content}$" if content else ""

    # 其它(reminder/file/inline_block 等)暂取 content 兜底
    for v in el.values():
        if isinstance(v, dict) and "content" in v:
            return v.get("content", "")
    return ""


def elements_to_md(elements: list[dict[str, Any]] | None) -> str:
    """整段 elements → Markdown 行内字符串。"""
    if not elements:
        return ""
    return "".join(element_to_md(el) for el in elements)
