# PYLinTech/Postman-Skill

命令行 IMAP/SMTP 邮件客户端，**只用 Python 标准库**——没有 `pip install`，没有虚拟环境，没有构建步骤，Python 3.8+ 直接能跑。

它同时是两种东西：用 `SKILL.md` 注册进 agent 之后是一个邮件 Skill，脱离 agent 也能当普通命令行工具用。

- skill 标识 `postman`（`SKILL.md` 的 `name`），入口 `scripts/mail.py`，12 个子命令
- 仓库地址与项目全名以 **`PYLinTech/Postman-Skill`** 为准，本文档一律使用全名

> **为什么仓库叫 `PYLinTech/Postman-Skill` 而 skill 叫 `postman`**：仓库名要带
> 归属前缀以在 GitHub 上不撞名，而 skill 标识必须匹配 `^[a-z0-9-]+$`——斜杠不合法。
> 两者指向同一个东西，文档里出现的 `PYLinTech/Postman-Skill`、`postman` 和
> `scripts/mail.py` 分别对应「项目」「skill 标识」「命令入口」三个不同层面。

## 它做什么

- **收信搜索**：原生 IMAP 检索条件，或 Gmail 的 `X-GM-RAW` 语法；按未读、星标、带附件、日期区间过滤，`--limit` / `--offset` 分页。全程用 `BODY.PEEK`，**搜索不会把邮件标成已读**。
- **读信**：RFC 2047 标题解码（中文主题直接可读，不再是 `=?utf-8?B?...?=`）；没有 `text/plain` 部件时自动把 HTML 转成可读文本（这是很多同类客户端直接返回空的地方）；附件列出文件名与大小。正文默认截断到 20000 字符，截断时显式告知。
- **发信**：纯文本 / HTML / 两者并存（`multipart/alternative`，收件端表现最稳）；支持附件；Bcc 不出现在可见头里；`--in-reply-to` 串联会话；`--dry-run` 只组装不投递。
- **整理**：存草稿、改标记、移动文件夹、删除（有 Trash 就移进去，没有才彻底删）、下载附件。
- **多账号**：一个配置文件放多个账户，`--account <名称>` 切换。

支持 QQ、网易（163 / 126 / yeah）、Gmail、Outlook、Yahoo、搜狐、新浪、阿里邮箱，以及任何自定义 IMAP/SMTP 服务——服务器地址从邮箱域名自动推导，不需要手工填。

## 目录

```
PYLinTech/Postman-Skill/
├── SKILL.md                     # Agent Skill 定义：描述何时该用它
├── README.md
├── LICENSE
├── resources/
│   └── config.example.json      # 凭据配置模板
└── scripts/
    ├── mail.py                  # CLI 入口，12 个子命令
    ├── selftest.py              # 自检：74 项离线检查 + 可选联网探测
    └── mailkit/                 # 实现，扁平子模块，无顶层再导出
        ├── __init__.py          # 仅一行 docstring，存在理由见下
        ├── config.py            # 凭据解析、服务商推导、传输方式选择
        ├── mime.py              # MIME 组装与解析、RFC 2047、HTML 转文本
        ├── imap_client.py       # IMAP 会话、文件夹特殊用途、UID 操作
        ├── smtp_client.py       # SMTP 投递
        └── commands.py          # 子命令实现
```

> `mailkit/__init__.py` 只有一行 docstring，不是残留：它让这个包成为**常规包**。
> 没有它就会退化成命名空间包（PEP 420），而命名空间包在 `sys.path` 上遇到同名
> 常规包时会被后者覆盖——即使本目录排在前面。一个碰巧叫 `mailkit` 的第三方包
> 就足以让整个 skill 起不来。

## 安装

把仓库拷进你的 agent skills 目录，没有其它步骤。

```bash
git clone git@github.com:PYLinTech/Postman-Skill.git
```

## 配置凭据

**几乎所有服务商都不接受账号密码，必须用「应用专用密码」**，且要先在账号设置里开启 IMAP/SMTP。QQ 和网易尤其如此——直接填普通密码一定失败。

| 来源 | 方式 |
|---|---|
| 配置文件 | `--config PATH`，或 `MAIL_CONFIG`；模板见 `resources/config.example.json` |
| 环境变量 | `MAIL_ADDRESS`、`MAIL_PASSWORD`，可选 `MAIL_PROVIDER`、`MAIL_SENDER_NAME` |

解析优先级：命令行参数 > 配置文件 > 环境变量。**工具没有默认配置文件路径**——文件名由你决定。

多账户用 `accounts` 映射，配 `--account` 切换：

```json
{
  "accounts": {
    "work":     { "address": "you@company.com", "password": "...",
                  "imap_host": "imap.company.com", "smtp_host": "smtp.company.com" },
    "personal": { "address": "you@163.com", "password": "..." }
  }
}
```

```bash
cp resources/config.example.json ~/mail.json
chmod 600 ~/mail.json     # 里面是明文密码
python scripts/mail.py --config ~/mail.json --account work check
```

`--account` 指定了不存在的账户会**直接报错并列出可用名称**，不会静默回落到别的账户——那会导致连错邮箱。

## 用法

```bash
M=./scripts/mail.py

python $M check                          # 验证凭据、列文件夹、报未读数
python $M selftest                       # 验证这份代码本身，不需要网络
python $M selftest --live                # 追加只读 IMAP 探测
python $M search --unread --limit 10
python $M read --uid 12345
python $M send --to a@b.com --subject 'Hi' --text 'Hello'
python $M send --to a@b.com --subject 'Q3' --html-file report.html --attach q3.pdf
python $M download --uid 12345 --dir ~/Downloads
```

长 HTML 用 `json` 子命令传，避免 shell 引号地狱；任何子命令都可加 `--json` 取结构化输出，也都有 `--help` 可以查完整参数：

```bash
python $M json '{"command":"send","to":["a@b.com"],"html":"<p>hi</p>"}'
```

### 命令表

| 命令 | 说明 |
|---|---|
| `check` | 验证凭据、列文件夹、报未读数 |
| `folders` | 列出文件夹及其特殊用途标记 |
| `search` | 检索邮件，**不改动已读状态** |
| `read` | 读完整正文、HTML、附件列表 |
| `send` | 经 SMTP 投递 |
| `draft` | 存入草稿箱 |
| `flag` | 改标记 |
| `move` | 移动邮件 |
| `delete` | 删除（有 Trash 就先进 Trash） |
| `download` | 保存附件 |
| `selftest` | 验证本构建，`--live` 追加只读探测 |
| `json` | 用一个 JSON 对象执行命令 |

全局选项 `--config` / `--account` / `--json` 对所有子命令生效。常用参数：

| 子命令 | 参数 |
|---|---|
| `search` | `-f/--folder`、`-q/--query`（默认 `ALL`）、`--gmail-raw`、`--since` / `--before`（YYYY-MM-DD）、`--unread`、`--flagged`、`--has-attachment`、`-n/--limit`（默认 20）、`--offset` |
| `read` | `--uid`、`--latest N`、`-f/--folder`、`--mark-read`、`--html`、`--max-chars N`（默认 20000，`0` 关闭截断） |
| `send` / `draft` | `--to`（可重复或逗号分隔）、`--cc`、`--bcc`、`--subject`、`--text` / `--text-file`、`--html` / `--html-file`、`--attach`（可重复）、`--reply-to`、`--in-reply-to`、`--reference`；`send` 另有 `--dry-run`，`draft` 的 `-f/--folder` 默认 `Drafts` |
| `flag` | `--uid`（可重复）、`-f/--folder`、`--flags`（可重复）、`--mode {add,remove}` |
| `move` | `--uid`（可重复）、`-f/--folder`（源）、`--to-folder` |
| `delete` | `--uid`（可重复）、`-f/--folder`、`--permanent` |
| `download` | `--uid`、`-f/--folder`、`--dir`（默认当前目录）、`--name`（可重复） |

文件夹接受别名（`inbox`、`sent`、`drafts`、`trash`、`junk`、`archive`）和本地化名称（如 `已发送`）。`--dir` 默认是当前目录——在仓库根目录跑 `download` 会把附件落在工作树里，建议显式给一个仓库外的目录。

## 几个设计取舍

- **全程用 UID，不用序号**。别的客户端动一下邮箱，序号就漂了；UID 不会。`search` 返回 UID，后续所有命令都接 UID。
- **搜索用 `BODY.PEEK`**。`search` 的本意是「找出有哪些」，顺手改已读状态是副作用而非结果。
- **已读状态默认不动**。列信和读信都不改标记，除非显式 `--mark-read`。
- **文件夹名按账户自身的特殊用途标记解析**。`inbox` / `sent` / `drafts` / `trash` / `junk` / `archive` 落到对应的 `\Sent`、`\Drafts` 上，所以德语或中文界面的邮箱不用配置也能用。
- **原始 HTML 默认不返回**。里面的 CSS、内联 base64 图片对读取内容是纯噪声，而抽取出的文本已经覆盖了信息；要原始部分加 `--html`。
- **正文体积设上限**。不设限时，一封营销邮件的 JSON 输出能放大到正文体积的两倍多，直接灌爆上下文。
- **密码永不回显**，`check` 里显示为 `***`。
- **零第三方依赖**。整个仓库只用标准库，代价是要自己处理 MIME 编码、HTML 转文本和服务商差异，收益是拷进去就能跑，不会因为依赖冲突和宿主的其它工具链互相污染。

## 验证一份构建

```bash
python scripts/selftest.py          # 74 项离线检查，不需要网络和凭据
python scripts/selftest.py --live   # 追加只读 IMAP 探测（connect / SELECT / LIST / SEARCH）
```

离线部分覆盖：RFC 2047 标题解码、HTML 转文本、MIME 组装与往返、服务器解析、标记语义、正文截断、多账户选择、文件名安全与重名避让。

命令行为异常时先跑它——能把「这份代码坏了」和「服务器拒绝了我们」分开。

## 已知边界

- 仅 IMAP，不支持 POP3。
- 发信只做 `multipart/alternative` 与 `text/*`，不支持日历邀请，也不支持把邮件作为 `message/rfc822` 附件再发。
- `move` 用 COPY + EXPUNGE，**EXPUNGE 不是 UID 范围的**：该文件夹里其它已标记删除的邮件会一起被清掉。这是 IMAP 的固有行为，不是 bug。
- 权限模型不是沙箱。运行它的进程能碰到的东西，它就能碰到。
- 正文截断默认 20000 字符；需要全文请显式 `--max-chars 0`。
- 声明支持 Python 3.8+，但开发与验证均在 3.9 及以上进行，3.8 未实机跑过。

## 协议

MIT，见 [LICENSE](LICENSE)。
