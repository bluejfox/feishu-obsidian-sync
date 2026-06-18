# feishu-sync

飞书知识库(Wiki) ⇄ Obsidian 双向同步 CLI,并生成 Obsidian 知识图谱。

- 同步两个知识库:**个人**、**工作**,各映射到 vault 下同名子目录。
- 拉取(飞书→本地)、推送(本地→飞书)、双向同步(冲突按修改时间裁决 + 备份)。
- 每小时定时自动同步(launchd),有界滚动日志。
- 知识图谱在 `RayValut/_图谱/`(由 Claude 精读生成,不回推飞书)。

---

## 方案原理

**一句话:把飞书里的每篇文档,在电脑本地存一份对应的 Markdown 文件;谁改了就把改动同步给另一边,让两边内容始终一致。**

可以把它想成「云端文档」和「本地文件」互为镜子,中间有个小账本帮忙记账:

1. **一一对应**:飞书里的每篇文档,对应本地一个 `.md` 文件;飞书里的文件夹,对应本地一个同名文件夹。每个本地文件开头藏了几行信息,记着「我对应飞书哪篇文档」,所以即使你改了文件名也不会认错。

2. **记账本**:工具内部存了一个小账本,记录「上一次同步完成时,两边各自长什么样」。下次同步时,拿现在的状态和账本对一对,就知道这段时间谁动过。

3. **谁改了同步谁**:
   - 只有本地改了 → 把本地的传上飞书;
   - 只有飞书改了 → 把飞书的拉下来;
   - **两边都改了 → 算"冲突"**,默认按"谁改得更晚就用谁的",并且**把要被覆盖的那一份先备份起来**,绝不会让你的内容凭空消失。

4. **格式照搬**:飞书的标题、列表、代码、**表格、图片**等,都会在两边之间如实转换,不只是纯文字。

5. **自动跑**:每小时自动同步一次(开机后台运行,不用管);另外在 Claude Code 里输入 `/note`,就能把当前对话整理成笔记一键发到飞书,同时本地也留一份。

```
   飞书文档  ◀──── 谁改了同步谁 ────▶  本地 .md 文件
       │                                    │
       └────────▶  对比账本(上次状态)◀──────┘
              只本地改→上传 / 只飞书改→下载 / 都改→冲突(留备份)
```

---

## 安装方式

**前置**:macOS、Python ≥ 3.10、一个具备 Wiki 读写权限的飞书自建应用(拿到 `app_id` / `app_secret`)。

```bash
# 1) 获取代码
git clone https://github.com/bluejfox/feishu-obsidian-sync.git feishu-sync
cd feishu-sync

# 2) 创建虚拟环境并安装(可编辑安装,带 CLI 入口)
python3 -m venv .venv
.venv/bin/pip install -e .

# 3) 配置:复制模板,填 app_id/app_secret、vault_path、spaces
cp config.example.yaml config.yaml
#   凭证也可改用环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET(优先级更高)

# 4) 列出应用可访问的知识库,回填 config.yaml 的 spaces
.venv/bin/python -m feishu_sync.cli init

# 5) 预览(只读)→ 首次全量拉取
.venv/bin/python -m feishu_sync.cli status
.venv/bin/python -m feishu_sync.cli pull
```

**可选 · 开启每小时定时同步**:`bash scripts/timer.sh enable`(详见下文「定时器」)。

**可选 · 安装 Claude Code `/note` 命令**:把仓库内备份的命令文件复制到全局命令目录即可全局可用。

```bash
cp claude/commands/note.md ~/.claude/commands/note.md
```

> `claude/commands/note.md` 是 `~/.claude/commands/note.md` 的**版本化备份**;两者需保持同步(改其一即同步另一)。

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
  标题取首行 `# 标题`;父文件夹不存在会自动创建;支持表格(原生表格),仅含图片时按 push 限制处理。

> 推送成功后会**在本地 vault 立即留一份副本并登记 manifest 基线**(`个人/Claude笔记/`),因此之后改本地这份会被判为 push(本地→飞书),不会被定时同步的飞书版回拉覆盖。加 `--no-local` 可关闭留底。

## 已知限制

- **push 全功能**:支持文本/标题/列表/代码/引用/待办、**表格**(建飞书原生表格)、**图片**(`![[assets/x]]` 自动上传到飞书)。本地图片文件缺失时,仅跳过该图并告警。
- 超大表格(几百单元格)或多图 push 较慢(逐格/逐图写入),属正常。
- **本地新增文档自动建到飞书**:在 `个人/`、`工作/` 目录内新增的 `.md`(不在 `push_exclude`)会在 sync/push 时自动在飞书对应文件夹下建文档;父文件夹按本地路径映射,找不到则建到库根。可用 `sync.create_new_feishu: false` 关闭。
- **push 是整篇替换式重写**:飞书侧评论会丢失、revision 增加(内容忠实)。

---

## 目录结构

```
feishu-sync/
├── config.yaml               # 凭证 / vault 路径 / spaces / 冲突策略(gitignore)
├── config.example.yaml       # 配置模板
├── feishu_sync/              # 源码:client/wiki/docx_*/converter/media/state/sync/cli
├── scripts/
│   ├── push_note.py          # /note 底层:整理好的 md → 飞书新建 + 本地留底
│   ├── timer.sh              # 定时器开关
│   └── com.ray.feishu-sync.plist  # launchd 配置
├── claude/commands/note.md   # Claude Code /note 命令的版本化备份(与 ~/.claude/commands/note.md 同步)
├── logs/                     # 滚动日志(gitignore)
└── .state/                   # 同步清单 manifest.json + 冲突备份(gitignore)
```

冲突备份:`.state/backups/<时间戳>/`(双向同步覆盖前自动备份被覆盖方)。
