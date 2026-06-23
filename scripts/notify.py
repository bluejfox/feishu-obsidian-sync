"""把 Claude Code 的 Notification 事件推送到飞书 App(机器人私信本人)。

两种用法:

1) 一次性解析 open_id 并写入 config.yaml 的 notify 段(交互配置):
     notify.py resolve --email you@example.com
     notify.py resolve --mobile 13800000000
   成功后会把 open_id 写进 config.yaml 的 `notify.open_id`。

2) 钩子模式(供 ~/.claude/settings.json 的 Notification 钩子调用):
   从 stdin 读取 Claude Code 注入的事件 JSON(含 message 字段),取出 message
   发给 config.yaml 里配置好的 notify.open_id。
     echo '{"message":"..."}' | notify.py

设计原则:钩子模式下**绝不抛错打断 Claude**——任何异常都吞掉并以退出码 0 收场,
仅把错误写到 stderr(被钩子的 2>/dev/null 丢弃)。所需权限:im:message、
contact:user.id:readonly(仅 resolve 用)。凭据只读 config.yaml / 环境变量。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from feishu_sync.client import FeishuClient  # noqa: E402
from feishu_sync.config import DEFAULT_CONFIG_PATH, load_config  # noqa: E402


def _client() -> FeishuClient:
    """通知用的飞书客户端。

    通知可使用独立的机器人应用:优先取 config.yaml `notify.app_id`/
    `notify.app_secret`;两者缺一即回退到同步共用的 `feishu.*` 主凭据。
    注意 open_id 按应用隔离,换应用后须用新应用重新 resolve。
    """
    cfg = load_config()
    no = cfg.raw.get("notify") or {}
    app_id = no.get("app_id") or cfg.feishu.app_id
    app_secret = no.get("app_secret") or cfg.feishu.app_secret
    return FeishuClient(app_id, app_secret)


def _notify_target() -> str:
    """从 config.yaml 的 notify 段取 open_id。"""
    cfg = load_config()
    open_id = (cfg.raw.get("notify") or {}).get("open_id", "")
    if not open_id:
        raise RuntimeError("config.yaml 缺少 notify.open_id,请先运行 notify.py resolve")
    return open_id


def cmd_resolve(args: argparse.Namespace) -> int:
    """解析 open_id 并写回 config.yaml(保留原文件其余内容)。"""
    if not args.email and not args.mobile:
        print("需提供 --email 或 --mobile", file=sys.stderr)
        return 2
    open_id = _client().resolve_open_id(email=args.email, mobile=args.mobile)
    print(f"open_id = {open_id}")

    import yaml
    path = DEFAULT_CONFIG_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("notify", {})["open_id"] = open_id
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"已写入 {path} 的 notify.open_id")
    return 0


def cmd_hook(kind: str) -> int:
    """钩子模式:读 stdin 事件,按事件类型(kind)发不同文案的飞书消息。
    任何异常都不打断 Claude。
    - kind=notify:权限确认/提问(Notification 事件),正文用事件 message,前缀「待你操作」。
    - kind=stop  :任务结束(Stop 事件),固定文案;Stop 事件 stdin 通常无 message。
    """
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if kind == "stop":
            text = "✅ Claude Code · 任务结束\n本轮已完成,可回来查看结果。"
        else:
            message = event.get("message") or "需要你确认或输入。"
            text = f"⏳ Claude Code · 待你操作\n{message}"
        _client().send_text(_notify_target(), text)
    except Exception as e:  # 钩子绝不因推送失败而打断主流程
        print(f"飞书通知发送失败: {e}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="飞书通知推送 / open_id 解析")
    parser.add_argument("--kind", choices=["notify", "stop"], default="notify",
                        help="钩子事件类型:notify=待操作(默认) / stop=任务结束")
    sub = parser.add_subparsers(dest="cmd")
    r = sub.add_parser("resolve", help="用邮箱/手机号解析 open_id 并写入 config.yaml")
    r.add_argument("--email")
    r.add_argument("--mobile")
    args = parser.parse_args()

    if args.cmd == "resolve":
        return cmd_resolve(args)
    return cmd_hook(args.kind)  # 无子命令 = 钩子模式


if __name__ == "__main__":
    raise SystemExit(main())
