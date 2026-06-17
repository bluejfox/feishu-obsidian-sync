"""Wiki 知识库节点树:递归遍历 + 节点→本地相对路径映射。

映射规则(无损、可逆):
  - 每个 docx 节点 → 一个 `<标题>.md` 文件
  - 含子节点的节点 → 额外建一个同名文件夹存放其子节点
    例:节点「前端」含子节点 → 文件 `前端.md` + 文件夹 `前端/`(放子节点)
  - 路径相对「知识库的本地子目录」;sync.py 负责拼上各库 subdir 前缀

非 docx 节点(sheet/bitable/mindnote/file 等)暂记录但不转换为 md,
由上层决定跳过或降级处理。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .client import FeishuClient

# Windows/macOS 文件名非法字符 + 控制字符
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Obsidian wikilink 保留/敏感字符:# 被当作标题锚点;[ ] | ^ 在 [[...]] 内有特殊含义
_WIKI_HOSTILE = str.maketrans({"#": "", "[": "(", "]": ")", "^": ""})


def sanitize_filename(name: str, fallback: str = "untitled") -> str:
    """生成 Obsidian 友好的文件名(去掉 # / [ ] / ^ 等会破坏 [[双链]] 的字符)。"""
    name = (name or "").strip()
    name = _ILLEGAL.sub("_", name)
    name = name.translate(_WIKI_HOSTILE)
    name = re.sub(r"\s{2,}", " ", name).strip()   # 折叠净化后产生的多余空格
    name = name.rstrip(". ")          # 结尾的点/空格在部分系统非法
    name = name[:120]                  # 控制长度
    return name or fallback


@dataclass
class WalkedNode:
    node: dict                 # 原始节点 dict(node_token/obj_token/obj_type/title/has_child…)
    rel_path: str              # 相对知识库 subdir 的 .md 路径(POSIX),如 "前端/Vue.md"
    depth: int

    @property
    def node_token(self) -> str:
        return self.node.get("node_token", "")

    @property
    def obj_type(self) -> str:
        return self.node.get("obj_type", "")

    @property
    def obj_token(self) -> str:
        return self.node.get("obj_token", "")

    @property
    def title(self) -> str:
        return self.node.get("title", "")


def walk_space(client: FeishuClient, space_id: str) -> list[WalkedNode]:
    """深度优先遍历整个知识库,返回扁平节点列表(含计算好的本地相对路径)。

    同目录重名会被去重(追加 ` (2)`…),且大小写不敏感(适配 macOS 默认文件系统),
    避免静默覆盖。去重在同一父目录的兄弟之间进行。
    """
    out: list[WalkedNode] = []
    _seen: set[str] = set()  # 防御循环引用(快捷方式可能成环)

    def unique_name(used: set[str], base: str) -> str:
        """在 used(casefold)集合内为 base 选一个唯一文件名(不含扩展名)。"""
        cand = base
        i = 2
        while cand.casefold() in used:
            cand = f"{base} ({i})"
            i += 1
        used.add(cand.casefold())
        return cand

    def recurse(parent_token: str | None, folder_segments: list[str], depth: int) -> None:
        used_here: set[str] = set()  # 当前目录已用文件名(去重作用域=同级兄弟)
        for node in client.iter_nodes(space_id, parent_token):
            token = node.get("node_token", "")
            if not token or token in _seen:
                continue
            _seen.add(token)

            base = sanitize_filename(node.get("title", ""), fallback=token)
            fname = unique_name(used_here, base)
            rel_path = "/".join([*folder_segments, fname + ".md"])
            out.append(WalkedNode(node=node, rel_path=rel_path, depth=depth))

            if node.get("has_child"):
                recurse(token, [*folder_segments, fname], depth + 1)

    recurse(None, [], 0)
    return out
