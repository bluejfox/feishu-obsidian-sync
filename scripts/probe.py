"""Phase 0 只读能力探测。

用真实凭证验证整条只读链路是否可用,并落一份样例 blocks JSON 供转换器开发:
  token 鉴权 -> 列知识库 spaces -> 递归节点树 -> 取一篇 docx 的 blocks -> 统计块类型

用法:
  cd feishu-sync
  .venv/bin/python scripts/probe.py            # 用 config.yaml / 环境变量
  .venv/bin/python scripts/probe.py <space_id> # 指定知识库
不写入任何飞书数据,也不改动本地 vault。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feishu_sync.client import FeishuClient  # noqa: E402
from feishu_sync.config import ROOT, load_config  # noqa: E402


def walk_tree(client: FeishuClient, space_id: str, parent=None, depth=0, acc=None, max_nodes=500):
    """深度优先遍历节点树,返回扁平 list[(depth, node)]。"""
    if acc is None:
        acc = []
    for node in client.iter_nodes(space_id, parent):
        acc.append((depth, node))
        if len(acc) >= max_nodes:
            return acc
        if node.get("has_child"):
            walk_tree(client, space_id, node.get("node_token"), depth + 1, acc, max_nodes)
    return acc


def main() -> int:
    cfg = load_config()
    client = FeishuClient(cfg.feishu.app_id, cfg.feishu.app_secret)

    configured = {s.space_id: s for s in cfg.spaces}

    print("① 鉴权 + 列知识库 spaces …")
    spaces = client.list_spaces()
    if not spaces:
        print("  ⚠ 未列到任何知识库。请确认应用已开通 wiki 权限,且应用被添加为知识库成员。")
        return 1
    print("  应用可访问的全部知识库(把要同步的填进 config.yaml 的 spaces 列表):")
    for s in spaces:
        sid = s.get("space_id")
        mark = "  ✅已配置" if sid in configured else ""
        print(f"  - {s.get('name')}  space_id={sid}{mark}")

    # 采样目标:命令行参数 > 已配置的第一个 > 全部里的第一个
    space_id = (sys.argv[1] if len(sys.argv) > 1 else "")
    if not space_id:
        space_id = (cfg.enabled_spaces[0].space_id if cfg.enabled_spaces else spaces[0]["space_id"])
    print(f"\n② 遍历节点树 (space_id={space_id}) …")
    tree = walk_tree(client, space_id)
    print(f"  共 {len(tree)} 个节点:")
    for depth, n in tree[:60]:
        print(f"  {'  ' * depth}- [{n.get('obj_type')}] {n.get('title')}  node={n.get('node_token')}")
    if len(tree) > 60:
        print(f"  …(省略 {len(tree) - 60} 个)")

    docx_nodes = [n for _, n in tree if n.get("obj_type") == "docx"]
    if not docx_nodes:
        print("\n⚠ 未发现 docx 类型节点,无法采样 blocks。")
        return 0

    sample = docx_nodes[0]
    document_id = sample.get("obj_token")
    print(f"\n③ 采样文档: {sample.get('title')}  document_id={document_id}")
    meta = client.get_document(document_id)
    print(f"  meta: title={meta.get('title')} revision_id={meta.get('revision_id')}")

    blocks = client.list_blocks(document_id)
    types = Counter(b.get("block_type") for b in blocks)
    print(f"  共 {len(blocks)} 个块;block_type 分布: {dict(types)}")

    out = ROOT / "scripts" / "sample_blocks.json"
    out.write_text(json.dumps({"meta": meta, "blocks": blocks}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 探测成功。样例已写入 {out}")
    print("   (该文件已被 .gitignore 忽略,仅供本地转换器开发参考)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
