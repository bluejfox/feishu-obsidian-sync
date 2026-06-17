# feishu-sync

飞书知识库(Wiki) ⇄ Obsidian 双向同步 CLI,并生成 Obsidian 知识图谱。

- 同步两个知识库:**个人**、**工作**,各映射到 vault 下同名子目录。
- 拉取(飞书→本地)、推送(本地→飞书)、双向同步(冲突按修改时间裁决 + 备份)。
- 每小时定时自动同步(launchd),有界滚动日志。
- 知识图谱在 `RayValut/_图谱/`(由 Claude 精读生成,不回推飞书)。

---

## 快速开始

```bash
cd /Users/ray/Documents/storage/feishu-sync

# 1) 配置(首次):复制模板,填 app_id/app_secret、vault_path、spaces
cp config.example.yaml config.yaml

# 2) 列出应用可访问的全部知识库(回填 config.yaml 的 spaces)
.venv/bin/python -m feishu_sync.cli init

# 3) 预览将拉取什么(只读,安全)
.venv/bin/python -m feishu_sync.cli status

# 4) 首次全量拉取
.venv/bin/python -m feishu_sync.cli pull
```

> 所有命令都用项目内置虚拟环境的 Python:`.venv/bin/python -m feishu_sync.cli <命令>`。

---

## CLI 命令

通用选项:`-c/--config <路径>` 指定配置文件;`-v/--verbose` 详细日志。

| 命令 | 作用 | 常用选项 |
|---|---|---|
| `init` | 列出应用可访问的全部飞书知识库 | — |
| `status` | 预览本次 **pull** 将执行的动作(dry-run,不改动) | `--only <glob>` |
| `pull` | 飞书 → 本地 | `--dry-run` `--force` `--prune` `--only <glob>` |
| `push` | 本地 → 飞书(整篇替换式重写已映射文档) | `--dry-run` `--only <glob>` |
| `sync` | 双向同步(冲突按时间戳裁决,被覆盖方留备份) | `--dry-run` `--only <glob>` |
| `graph` | 查看知识图谱过时状态 | `--mark-built` |

### 选项说明
- `--dry-run`:只预览,不写本地、不写飞书。
- `--force`:忽略变更检测,强制重拉全部(pull)。
- `--prune`:删除同步目录内不再属于飞书的本地孤儿 `.md`(仅限各知识库子目录,不碰根目录与 `_图谱/`)。
- `--only <glob>`:仅处理匹配该 glob 的相对路径,如 `--only '个人/01 技术/**'`。
- `graph --mark-built`:重新生成知识图谱后调用,刷新"过时"基线、清零提醒。

### 示例
```bash
.venv/bin/python -m feishu_sync.cli sync --dry-run            # 预览双向同步
.venv/bin/python -m feishu_sync.cli sync                      # 执行双向同步
.venv/bin/python -m feishu_sync.cli pull --force              # 强制重拉全部
.venv/bin/python -m feishu_sync.cli pull --prune              # 拉取并清理本地孤儿
.venv/bin/python -m feishu_sync.cli push --only '工作/**' --dry-run   # 预览推送某目录
.venv/bin/python -m feishu_sync.cli graph                     # 看图谱是否过时
```

---

## 定时器(每小时自动同步)

用 `scripts/timer.sh` 管理 launchd 定时任务(macOS 原生):

```bash
bash scripts/timer.sh enable     # 开启(每小时一次,且开启时立即先同步一次)
bash scripts/timer.sh disable    # 关闭(配置保留,可随时再开)
bash scripts/timer.sh status     # 查看是否在运行
bash scripts/timer.sh run-now    # 不等定时,立刻手动触发一次
bash scripts/timer.sh logs       # 查看最近同步日志
```

- 频率:编辑 `scripts/com.ray.feishu-sync.plist` 里的 `StartInterval`(秒,3600=1 小时),再 `enable` 生效。
- 日志:有界滚动 `logs/feishu-sync.log`(单文件 1MB × 保留 5 份);崩溃兜底 `logs/launchd.err.log`。

---

## 知识图谱维护

知识图谱在 `RayValut/_图谱/`,由 **Claude 精读全文人工生成**(概念级双链),**定时同步不会自动重建**。

- 当文档自图谱生成后有变更,`sync`/`pull` 会在日志提醒「⚠ 图谱可能过时」。
- 查看状态:`.venv/bin/python -m feishu_sync.cli graph`
- 更新流程:让 Claude「更新图谱」重读变更并重生成 → 运行 `graph --mark-built` 刷新基线。

---

## Claude Code 集成:`/note`(全局)

把当前对话里有价值的内容,让 Claude 整理成笔记并一键推送到飞书「个人」库的「Claude笔记」文件夹。

- **全局可用**:命令文件在 `~/.claude/commands/note.md`,任何项目/会话都能用。
- **用法**:在 Claude Code 里输入 `/note [整理提示词]`。
  - 带提示词:按你的指令决定整理什么/侧重/风格,如 `/note 总结排查根因和修复方案`、`/note 整理成给团队评审的方案`。
  - 不带:整理最近一段有价值的回复/结论。
- **固定风格与结构**:以资深架构师/全栈/AI 视角、IBM Consulting 风格输出,结构为 背景→目的→现状→根因→方案→验证→风险(不适用的小节省略)。
- **原理**:当前会话的 Claude 直接做整理(零额外 API 成本)→ 写临时文件 → 调 `scripts/push_note.py` 在飞书新建文档 → 返回链接。
- **底层脚本**(也可单独用):
  ```bash
  echo "# 标题\n正文" | .venv/bin/python scripts/push_note.py --space 个人 --parent "Claude笔记"
  .venv/bin/python scripts/push_note.py --space 个人 --parent "Claude笔记" --file note.md
  ```
  标题取首行 `# 标题`;父文件夹不存在会自动创建;含表格/图片会被拒绝(同 push 限制)。

> 推到飞书的笔记是新文档,不在本地 manifest 里;下次 `pull` 会把它拉到本地 `个人/Claude笔记/`。

## 已知限制

- **push 支持表格**(自动建飞书原生表格);但**不支持图片**:含图片(`![[...]]`)的文档在 push/sync 时会被跳过并告警(图片反向上传未实现)。**pull 方向表格和图片完全正常。**
- 超大表格(几百单元格)push 较慢(逐格写入),属正常。
- **push 是整篇替换式重写**:飞书侧评论会丢失、revision 增加(内容忠实)。
- **本地新增文档**暂不自动在飞书建节点(仅告警)。

---

## 目录结构

```
feishu-sync/
├── config.yaml               # 凭证 / vault 路径 / spaces / 冲突策略(gitignore)
├── config.example.yaml       # 配置模板
├── feishu_sync/              # 源码:client/wiki/docx_*/converter/media/state/sync/cli
├── scripts/
│   ├── timer.sh              # 定时器开关
│   └── com.ray.feishu-sync.plist  # launchd 配置
├── logs/                     # 滚动日志(gitignore)
└── .state/                   # 同步清单 manifest.json + 冲突备份(gitignore)
```

冲突备份:`.state/backups/<时间戳>/`(双向同步覆盖前自动备份被覆盖方)。
