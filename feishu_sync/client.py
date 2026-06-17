"""lark-oapi 客户端封装:鉴权、分页、重试、错误归一。

只承载「与飞书的原始通信」,不含同步/转换逻辑。所有 list 接口统一吐出
普通 dict(经 SDK model -> lark.JSON.marshal -> json.loads),便于上层处理与缓存。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

import lark_oapi as lark
from lark_oapi.api.docx.v1 import (
    GetDocumentRequest,
    ListDocumentBlockRequest,
    RawContentDocumentRequest,
)
from lark_oapi.api.wiki.v2 import (
    GetNodeSpaceRequest,
    ListSpaceNodeRequest,
    ListSpaceRequest,
)

logger = logging.getLogger("feishu_sync.client")

# 触发退避重试的飞书错误码(频控 / 服务端临时错误)
_RETRYABLE_CODES = {99991400, 99991661, 1061045}
_MAX_RETRY = 4


class FeishuError(RuntimeError):
    """飞书 API 调用失败(已含 code/msg/log_id)。"""

    def __init__(self, where: str, code: int, msg: str, log_id: str | None = None):
        self.code, self.msg, self.log_id = code, msg, log_id
        super().__init__(f"{where} 失败: code={code} msg={msg!r} log_id={log_id}")


def _obj_to_dict(obj: Any) -> Any:
    """把 SDK model / 数据对象转成纯 dict。"""
    if obj is None:
        return None
    try:
        return json.loads(lark.JSON.marshal(obj))
    except Exception:  # 退化:对象已是基本类型
        return obj


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, log_level: lark.LogLevel = lark.LogLevel.INFO):
        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(log_level)
            .build()
        )

    # ---- 底层:执行 + 重试 + 错误归一 ----
    def _call(self, where: str, fn, request):
        last: FeishuError | None = None
        for attempt in range(_MAX_RETRY):
            resp = fn(request)
            if resp.success():
                return resp
            code = getattr(resp, "code", -1)
            msg = getattr(resp, "msg", "")
            log_id = getattr(getattr(resp, "raw", None), "headers", {}) or None
            last = FeishuError(where, code, msg, getattr(resp, "get_log_id", lambda: None)())
            if code in _RETRYABLE_CODES and attempt < _MAX_RETRY - 1:
                backoff = 2 ** attempt
                logger.warning("%s 触发频控/临时错误(code=%s),%ss 后重试 (%d/%d)",
                               where, code, backoff, attempt + 1, _MAX_RETRY)
                time.sleep(backoff)
                continue
            raise last
        raise last  # pragma: no cover

    # ---- Wiki ----
    def list_spaces(self) -> list[dict]:
        out: list[dict] = []
        page_token: str | None = None
        while True:
            b = ListSpaceRequest.builder().page_size(50)
            if page_token:
                b = b.page_token(page_token)
            resp = self._call("wiki.space.list", self._client.wiki.v2.space.list, b.build())
            data = _obj_to_dict(resp.data) or {}
            out.extend(data.get("items", []) or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
        return out

    def iter_nodes(self, space_id: str, parent_node_token: str | None = None) -> Iterator[dict]:
        """列某父节点下的直接子节点(分页)。递归遍历由上层 wiki.py 负责。"""
        page_token: str | None = None
        while True:
            b = ListSpaceNodeRequest.builder().space_id(space_id).page_size(50)
            if parent_node_token:
                b = b.parent_node_token(parent_node_token)
            if page_token:
                b = b.page_token(page_token)
            resp = self._call("wiki.space_node.list", self._client.wiki.v2.space_node.list, b.build())
            data = _obj_to_dict(resp.data) or {}
            for item in data.get("items", []) or []:
                yield item
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")

    def get_node(self, token: str) -> dict:
        req = GetNodeSpaceRequest.builder().token(token).build()
        resp = self._call("wiki.space.get_node", self._client.wiki.v2.space.get_node, req)
        return (_obj_to_dict(resp.data) or {}).get("node", {})

    # ---- Docx ----
    def get_document(self, document_id: str) -> dict:
        req = GetDocumentRequest.builder().document_id(document_id).build()
        resp = self._call("docx.document.get", self._client.docx.v1.document.get, req)
        return (_obj_to_dict(resp.data) or {}).get("document", {})

    def list_blocks(self, document_id: str) -> list[dict]:
        out: list[dict] = []
        page_token: str | None = None
        while True:
            b = ListDocumentBlockRequest.builder().document_id(document_id).page_size(500)
            if page_token:
                b = b.page_token(page_token)
            resp = self._call("docx.document_block.list", self._client.docx.v1.document_block.list, b.build())
            data = _obj_to_dict(resp.data) or {}
            out.extend(data.get("items", []) or [])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
        return out

    def raw_content(self, document_id: str) -> str:
        """文档纯文本(无格式),用于廉价的变更/内容核对。"""
        req = RawContentDocumentRequest.builder().document_id(document_id).build()
        resp = self._call("docx.document.raw_content", self._client.docx.v1.document.raw_content, req)
        return (_obj_to_dict(resp.data) or {}).get("content", "")

    # ---- Drive media ----
    def download_media(self, file_token: str) -> tuple[bytes, str]:
        """下载媒体(图片/附件),返回 (字节, 文件名)。"""
        from lark_oapi.api.drive.v1 import DownloadMediaRequest
        req = DownloadMediaRequest.builder().file_token(file_token).build()
        resp = self._call("drive.media.download", self._client.drive.v1.media.download, req)
        data = resp.file.read() if resp.file else b""
        return data, (resp.file_name or "")

    # ---- Docx 写入(PUSH) ----
    def convert_markdown(self, markdown: str):
        """官方 markdown → blocks。返回 (blocks, first_level_block_ids) 为 SDK 对象,
        可直接喂给 create_block_descendants(避免 dict↔model 往返)。"""
        from lark_oapi.api.docx.v1 import ConvertDocumentRequest, ConvertDocumentRequestBody
        req = ConvertDocumentRequest.builder().request_body(
            ConvertDocumentRequestBody.builder().content_type("markdown").content(markdown).build()
        ).build()
        resp = self._call("docx.document.convert", self._client.docx.v1.document.convert, req)
        return resp.data.blocks or [], resp.data.first_level_block_ids or []

    def delete_block_children(self, document_id: str, block_id: str, start: int, end: int) -> None:
        """删除某块 [start, end) 区间的子块。end<=start 时直接跳过。"""
        if end <= start:
            return
        from lark_oapi.api.docx.v1 import (
            BatchDeleteDocumentBlockChildrenRequest,
            BatchDeleteDocumentBlockChildrenRequestBody,
        )
        req = (BatchDeleteDocumentBlockChildrenRequest.builder()
               .document_id(document_id).block_id(block_id).document_revision_id(-1)
               .request_body(BatchDeleteDocumentBlockChildrenRequestBody.builder()
                             .start_index(start).end_index(end).build())
               .build())
        self._call("docx.block_children.batch_delete",
                   self._client.docx.v1.document_block_children.batch_delete, req)

    def create_block_descendants(self, document_id: str, block_id: str,
                                 children_id: list, descendants: list, index: int = 0) -> None:
        """在 block_id 下 index 处一次性创建整棵块子树(convert 的产物)。"""
        from lark_oapi.api.docx.v1 import (
            CreateDocumentBlockDescendantRequest,
            CreateDocumentBlockDescendantRequestBody,
        )
        req = (CreateDocumentBlockDescendantRequest.builder()
               .document_id(document_id).block_id(block_id).document_revision_id(-1)
               .request_body(CreateDocumentBlockDescendantRequestBody.builder()
                             .children_id(children_id).index(index).descendants(descendants).build())
               .build())
        self._call("docx.block_descendant.create",
                   self._client.docx.v1.document_block_descendant.create, req)

    def create_block_children(self, document_id: str, block_id: str, children: list, index: int = 0) -> dict:
        """在 block_id 下 index 处创建直接子块(用于建表/填单元格)。返回含 children 的 dict。"""
        from lark_oapi.api.docx.v1 import (
            CreateDocumentBlockChildrenRequest,
            CreateDocumentBlockChildrenRequestBody,
        )
        req = (CreateDocumentBlockChildrenRequest.builder()
               .document_id(document_id).block_id(block_id).document_revision_id(-1)
               .request_body(CreateDocumentBlockChildrenRequestBody.builder()
                             .children(children).index(index).build())
               .build())
        resp = self._call("docx.block_children.create",
                          self._client.docx.v1.document_block_children.create, req)
        return _obj_to_dict(resp.data) or {}

    def create_wiki_node(self, space_id: str, parent_node_token: str | None, title: str) -> dict:
        """在知识库新建一个 docx 节点,返回节点 dict(含 node_token / obj_token)。"""
        from lark_oapi.api.wiki.v2 import CreateSpaceNodeRequest, Node
        builder = Node.builder().obj_type("docx").node_type("origin").title(title)
        if parent_node_token:
            builder = builder.parent_node_token(parent_node_token)
        req = (CreateSpaceNodeRequest.builder().space_id(space_id)
               .request_body(builder.build()).build())
        resp = self._call("wiki.space_node.create", self._client.wiki.v2.space_node.create, req)
        return (_obj_to_dict(resp.data) or {}).get("node", {})

    def delete_drive_file(self, token: str, file_type: str = "docx") -> None:
        """删除云文档(用于回滚新建的文档)。"""
        from lark_oapi.api.drive.v1 import DeleteFileRequest
        req = DeleteFileRequest.builder().file_token(token).type(file_type).build()
        self._call("drive.file.delete", self._client.drive.v1.file.delete, req)

    # 暴露原始 SDK client,供尚未封装的接口直接使用
    @property
    def raw(self) -> lark.Client:
        return self._client
