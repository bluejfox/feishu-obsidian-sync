"""读取一篇 docx 文档:meta(title/revision) + 全量块。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .client import FeishuClient


@dataclass
class DocxDocument:
    document_id: str
    revision_id: int = 0
    title: str = ""
    blocks: list[dict] = field(default_factory=list)


def read_document(client: FeishuClient, document_id: str) -> DocxDocument:
    meta = client.get_document(document_id)
    blocks = client.list_blocks(document_id)
    return DocxDocument(
        document_id=document_id,
        revision_id=int(meta.get("revision_id", 0) or 0),
        title=meta.get("title", ""),
        blocks=blocks,
    )
