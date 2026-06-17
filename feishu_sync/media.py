"""媒体(图片/附件)下载 + 本地落盘 + Obsidian 嵌入路径生成。

PULL 时:image 块的 token → 下载到 vault/assets/<token>.<ext> → 返回 Obsidian
嵌入语法 `![[assets/<file>]]`(Obsidian 按 vault 相对路径解析,比 ./相对路径更稳)。
同一 token 只下载一次(去重缓存)。
"""
from __future__ import annotations

import logging
from pathlib import Path

from .client import FeishuClient

logger = logging.getLogger("feishu_sync.media")

# magic bytes → 扩展名(文件名无扩展时兜底)
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),   # 粗略:RIFF....WEBP
    (b"BM", ".bmp"),
]


def _guess_ext(filename: str, data: bytes) -> str:
    if filename and "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
        if len(ext) <= 6:
            return ext
    for sig, ext in _MAGIC:
        if data.startswith(sig):
            return ext
    return ".png"


class MediaManager:
    def __init__(self, client: FeishuClient, assets_path: Path, assets_rel: str = "assets"):
        self.client = client
        self.assets_path = assets_path
        self.assets_rel = assets_rel.strip("/")
        self._cache: dict[str, str] = {}   # token -> 文件名
        self.downloaded = 0

    def resolve(self, token: str, block: dict) -> str:
        """下载并返回 Obsidian 嵌入字符串 `![[assets/<file>]]`。失败则降级为占位。"""
        if not token:
            return "![](feishu-media://missing)"
        if token in self._cache:
            return self._embed(self._cache[token])
        try:
            data, filename = self.client.download_media(token)
            if not data:
                raise RuntimeError("空内容")
            ext = _guess_ext(filename, data)
            name = f"{token}{ext}"
            self.assets_path.mkdir(parents=True, exist_ok=True)
            (self.assets_path / name).write_bytes(data)
            self._cache[token] = name
            self.downloaded += 1
            logger.info("  ↓ 媒体 %s (%d bytes)", name, len(data))
            return self._embed(name)
        except Exception as e:  # noqa: BLE001
            logger.warning("  媒体下载失败 token=%s: %s(保留占位)", token, e)
            return f"![](feishu-media://{token})"

    def _embed(self, name: str) -> str:
        return f"![[{self.assets_rel}/{name}]]"
