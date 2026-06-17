"""feishu-sync 命令行入口。"""
from __future__ import annotations

import datetime
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

import click

from .client import FeishuClient
from .config import ROOT, load_config
from .sync import SyncEngine

# 滚动日志:单文件上限 1MB,保留 5 个历史 → 最多约 6MB,自动覆盖最旧
_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUPS = 5


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    # 重复调用时先清掉旧 handler(单进程通常只调一次)
    for h in list(root.handlers):
        root.removeHandler(h)

    # 有界滚动文件:始终写,作为权威日志(定时运行也只进这里,不会无限增长)
    logdir = ROOT / "logs"
    logdir.mkdir(exist_ok=True)
    fh = RotatingFileHandler(logdir / "feishu-sync.log", maxBytes=_LOG_MAX_BYTES,
                             backupCount=_LOG_BACKUPS, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # 仅交互式(TTY)时再加控制台输出;launchd 重定向的非 TTY 不加,
    # 避免 stdout/stderr 重定向文件被无限追加。
    if sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


@click.group()
@click.option("-c", "--config", "config_path", default=None, help="config.yaml 路径")
@click.option("-v", "--verbose", is_flag=True, help="详细日志")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, verbose: bool) -> None:
    """飞书知识库 ⇄ Obsidian 双向同步。"""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


def _load(ctx: click.Context):
    try:
        return load_config(ctx.obj.get("config_path"))
    except Exception as e:  # noqa: BLE001
        click.secho(f"配置错误: {e}", fg="red", err=True)
        sys.exit(1)


@cli.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """列出应用可访问的全部知识库,供回填 config.yaml 的 spaces。"""
    cfg = _load(ctx)
    client = FeishuClient(cfg.feishu.app_id, cfg.feishu.app_secret)
    spaces = client.list_spaces()
    if not spaces:
        click.secho("未列到任何知识库。确认应用已开通 wiki 权限并被加入知识库成员。", fg="yellow")
        return
    configured = {s.space_id for s in cfg.spaces}
    click.echo("应用可访问的知识库:")
    for s in spaces:
        sid = s.get("space_id")
        mark = click.style("  ✅已配置", fg="green") if sid in configured else ""
        click.echo(f"  - {s.get('name')}  space_id={sid}{mark}")
    click.echo("\n把要同步的填入 config.yaml 的 spaces(每个指定 local_subdir)。")


@cli.command()
@click.option("--only", default=None, help="仅处理匹配该 glob 的路径(相对 vault)")
@click.pass_context
def status(ctx: click.Context, only: str | None) -> None:
    """dry-run:列出本次 pull 将执行的动作,不改动任何数据。"""
    cfg = _load(ctx)
    engine = SyncEngine(cfg)
    report = engine.pull(dry_run=True, only=only)
    _print_report(report, title="PULL 预览(dry-run)")


@cli.command()
@click.option("--only", default=None, help="仅处理匹配该 glob 的路径(相对 vault)")
@click.option("--dry-run", is_flag=True, help="只预览不写盘")
@click.option("--force", is_flag=True, help="忽略变更检测,强制重拉全部")
@click.option("--prune", is_flag=True, help="删除同步目录内不再属于飞书的本地孤儿 .md")
@click.pass_context
def pull(ctx: click.Context, only: str | None, dry_run: bool, force: bool, prune: bool) -> None:
    """飞书 → 本地。"""
    cfg = _load(ctx)
    engine = SyncEngine(cfg)
    report = engine.pull(dry_run=dry_run, only=only, force=force, prune=prune)
    _print_report(report, title="PULL 结果" + ("(dry-run)" if dry_run else ""))


@cli.command()
@click.option("--only", default=None, help="仅处理匹配该 glob 的路径(相对 vault)")
@click.option("--dry-run", is_flag=True, help="只预览不写入飞书")
@click.pass_context
def push(ctx: click.Context, only: str | None, dry_run: bool) -> None:
    """本地 → 飞书(整篇替换式重写已映射文档)。"""
    cfg = _load(ctx)
    engine = SyncEngine(cfg)
    report = engine.push(dry_run=dry_run, only=only)
    _print_report(report, title="PUSH 结果" + ("(dry-run)" if dry_run else ""))


@cli.command()
@click.option("--only", default=None, help="仅处理匹配该 glob 的路径(相对 vault)")
@click.option("--dry-run", is_flag=True, help="只预览,不写本地也不写飞书")
@click.pass_context
def sync(ctx: click.Context, only: str | None, dry_run: bool) -> None:
    """双向同步(按时间戳裁决冲突,被覆盖方留备份)。"""
    cfg = _load(ctx)
    engine = SyncEngine(cfg)
    report = engine.sync(dry_run=dry_run, only=only)
    _print_report(report, title="SYNC 结果" + ("(dry-run)" if dry_run else ""))


@cli.command()
@click.option("--mark-built", is_flag=True, help="标记图谱已重新生成(清零过时提醒)")
@click.pass_context
def graph(ctx: click.Context, mark_built: bool) -> None:
    """知识图谱过时状态;--mark-built 在重新生成图谱后调用以刷新基线。"""
    from .state import Manifest
    cfg = _load(ctx)
    m = Manifest.load(cfg.state_dir / "manifest.json")
    if mark_built:
        m.graph_built_at = time.time()
        m.save()
        click.secho(f"✅ 已标记图谱生成时间为 {datetime.datetime.now():%Y-%m-%d %H:%M:%S},过时提醒清零。", fg="green")
        return
    built = m.graph_built_at
    built_str = (datetime.datetime.fromtimestamp(built).strftime("%Y-%m-%d %H:%M:%S")
                 if built else "(从未标记)")
    click.echo(f"图谱上次生成: {built_str}")
    stale = m.stale_docs()
    if not built:
        click.secho("尚未标记生成时间。重新生成图谱后运行 'feishu-sync graph --mark-built'。", fg="yellow")
    elif stale:
        click.secho(f"⚠ 过时:{len(stale)} 篇文档自生成后有变更:", fg="yellow")
        for r in stale[:20]:
            click.echo(f"  - {r.local_path}")
        if len(stale) > 20:
            click.echo(f"  …(共 {len(stale)} 篇)")
    else:
        click.secho("✅ 图谱是最新的(生成后无文档变更)。", fg="green")


def _print_report(report, title: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 写入有界滚动日志:每次运行一行带时间戳的结果(定时运行靠这个留痕)
    logging.getLogger("feishu_sync.run").info("%s — %s", title, report.summary())
    click.secho(f"\n===== [{ts}] {title} =====", bold=True)
    click.echo(report.summary())
    for p in report.pulled:
        click.secho(f"  ↓ {p}", fg="green")
    for p in report.pushed:
        click.secho(f"  ↑ {p}", fg="blue")
    for c in report.conflicts:
        click.secho(f"  ⚔ {c}", fg="magenta")
    for w in report.warnings:
        click.secho(f"  ⚠ {w}", fg="yellow")
    for u in report.unsupported:
        click.secho(f"  ~ 跳过(非docx) {u}", fg="cyan")
    for o in report.orphaned:
        click.secho(f"  ⚠ 孤立 {o}", fg="yellow")
    for p in report.pruned:
        click.secho(f"  ✗ 清理 {p}", fg="red")
    if report.skipped:
        click.echo(f"  (未变更跳过 {len(report.skipped)} 篇)")


if __name__ == "__main__":
    cli()
