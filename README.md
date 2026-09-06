# tgmon

TG 游戏爆料频道监控 → 翻译 → RSS / JSON API / Webhook。

跑在香港小鸡上：**重的活（抓取、翻译、缩略图）全在香港做完，只把轻量文本 +
缩略图送回大陆**。视频一律不落盘、不回传，只给首帧缩略图 + TG 原链接。

## 为什么是这个形状

这台机器的网络特性直接决定了架构（均为实测）：

| 观测 | 影响 |
|---|---|
| 拉国际数据 200 MB/s | 从 TG 抓消息和媒体极快，这是最大优势 |
| 回大陆单连接仅 61 KB/s | 任何「把媒体推给大陆」的设计都会卡死 → 视频只给深链 |
| OpenAI/Anthropic 官方端点硬 403 | Provider 的 `base_url` 必填无默认，必须是中转或 Gemini |
| 磁盘仅剩 16 G、无 swap | 视频不落盘；缩略图 LRU + TTL |
| RTT 233 ms | 服务端渲染 + CSS 内联，没有 SPA bundle |

## 两个进程

```
admin   Web 后台（唯一配置入口）。永不连接 Telethon
worker  独占 user session，抓取→翻译→去重→出站，并消费任务队列
```

为什么拆开：Telethon 的 session 是 SQLite 文件，两个进程同时用同一个 session
会互锁并可能写坏授权状态。所以只有 worker 持有连接；admin 想做任何需要 TG 的事
（发验证码、登录、同步频道列表、测 provider）都往 `task` 表写一条，由 worker 执行
后回填结果。**网页登录流程就是靠这个绕开「Telethon 需要 stdin」的限制。**

两者只通过 SQLite 通信（`app_setting` + `task` 表），WAL 模式并发读写。

## 部署

### 方式一：预构建镜像（推荐，一键）

镜像由 GitHub Actions 自动构建并发布到 `ghcr.io`（含全部依赖与内置
embedding 权重，约 1 GB）。每次 push main 更新 `latest`，打 `v*` tag
另发版本号镜像，同时保留 `sha` 快照便于回滚。

在目标服务器上：

```bash
# 私有仓库先准备 PAT（repo + read:packages 权限），公开仓库跳过
export GH_PAT=<你的GitHub PAT>

git clone https://$GH_PAT@github.com/znjhahaha/tgmon.git /opt/tgmon
cd /opt/tgmon && ./deploy.sh
```

脚本会自动生成 `.env` 和随机后台密码、拉镜像、起容器并做健康检查。
以后更新版本只需 `./deploy.sh update`（拉代码 + 拉镜像 + 滚动重启）。

### 方式二：源码构建（开发调试）

```bash
git clone <你的仓库> /opt/tgmon && cd /opt/tgmon
cp .env.example .env
# 编辑 .env：至少填 TGMON_ADMIN_PASSWORD 和 TGMON_BASE_URL
docker compose up -d --build
```

源码版把 `./tgmon` 只读挂进容器，改完代码 `docker compose restart`
即生效；只有改 `requirements.txt` 才需要重建。生产用 `docker-compose.prod.yml`（纯镜像、不挂代码）。

打开 `https://<你的地址>`，用 `admin` + 你设的密码登录，然后：

1. **账号与凭据** —— 填 `API_ID` / `API_HASH` / 手机号 → 点「发送验证码」→
   填码（**验证码发到 TG 客户端，不是短信**）→ 如有两步验证再填密码
2. **频道管理** —— 点「从我的账号同步」，勾选要监控的频道。
   不用手抄 ID、不用找私密频道链接
3. **Provider** —— 加一个能从香港访问的翻译后端，点「连通性测试」
4. **术语表** —— 按游戏加术语。爆料翻译的成败几乎全在这张表
5. **输出配置** —— 建 RSS feed / 生成 API Key / 配 Webhook

第 1-2 步做完就能看到消息落库。想立刻验证管线，在频道配置里点
「补最近 20 条历史」，不用等新消息。

## 配置在哪

**除 4 项之外，全部在网页后台改，即时生效。**

`.env` 里只有这些（因为它们在 DB 可用之前就要读到）：

| 变量 | 说明 |
|---|---|
| `TGMON_SECRET_KEY` | DB 内敏感字段的加密主密钥。留空则自动生成 `secret.key` |
| `TGMON_ADMIN_PASSWORD` | 后台初始密码，建完账号可清空 |
| `TGMON_BASE_URL` | 对外地址，RSS 绝对链接用 |
| `TGMON_LOG_LEVEL` | 日志级别 |

## 检索、对话与 Wiki

后台 `/kb` 现在是“知识与记忆中心”，分为资料与术语、来源同步、检索与索引、对话记忆、诊断五个页签。消息和资料先经过权限、发布状态与重复过滤，再进入同一个检索接口；崩铁、星铁、HSR 等别名统一为 `崩坏:星穹铁道`。

消息入库后会写入 SQLite FTS5 检索索引；本地 embedding 默认开启，内置
`BAAI/bge-small-zh-v1.5`（512 维、CPU），无需外部 embedding API 或密钥。
Docker 构建时下载约 90 MB 权重并打包进镜像，运行时只读取本地模型；非 Docker
安装先执行 `python -m tgmon.embeddings --download`。模型不可用时回退关键词检索。
长文按块计算向量，内容修改和模型升级会重新索引，旧 API 向量不会混用。
QQ 中支持 `/search`、`/ask`、`/related`、
`/timeline`、`/remember` 和 `/forget`，问答会附 `【消息#id】` 证据引用。

QQ 翻译可用 `/translate [崩铁] 原文`、`/tr 原文`、`翻译：原文`，或
`/translate #消息id` 翻译已收录消息的原文。游戏名可省略，由已审核术语识别；
使用现有翻译后端、预算和缓存，pending/rejected 条目不参与翻译。

QQ 的 `/latest`、`/new`、`/game`、`/peek` 和主动推送均保留同条爆料的全部图片。
由于群富媒体接口一次只接受一个文件，图组按原顺序合为一张 JPEG 长图，完整等比
缩放、不裁切，正文附在同一条媒体消息上；纯图推送没有额外正文。长图高度最多
16000 像素，极长图组会整体缩小。缺图或上传失败会明确降级或进入重试，不会当成
完整相册已发送。群被动回复仍遵守每次 @ 最多 5 条的限制。
图片优先通过 QQ `file_data` 直接上传，无需 GitHub 图床；直接上传失败时才走
已配置的 GitHub/本站 URL 回退。上传使用 `srv_send_msg=false`，由发送层统一发消息。

知识库页首次启动会登记原神、崩坏：星穹铁道、绝区零的 Fandom MediaWiki API
来源，按角色、NPC、派系和设定分类同步。Wiki 新页面和 revision 变更默认待审，
审核通过后才进入翻译术语表。

读取优先级：**DB → `.env` → 内置默认**。首次启动把 `.env` 当种子导入 DB。

改完需要重新登录的只有 `API_ID` / `API_HASH` / `PHONE_NUMBER`（等于换应用或换
账号）。其余全部热生效：prompt、术语表、去重阈值、限流、保留策略、频道开关、
provider 配置。

## 输出

```
GET  /rss/<slug>              RSS，加 ?media=none 出纯文本版
GET  /feeds                   所有订阅地址清单
GET  /api/messages            分页 + 按频道/时间/关键词/重复状态过滤
GET  /api/messages/<id>       单条详情
GET  /api/channels            频道列表与统计
GET  /api/stats               总量与 worker 状态
POST /api/webhook/test        手动触发一次推送
```

API 走 `X-API-Key` 请求头 + 速率限制。Webhook 带 HMAC-SHA256 签名
（`X-Tgmon-Signature: sha256=<hex>`）与退避重试。

## 三层去重

1. **消息 ID** —— `(channel_id, tg_message_id)` 唯一索引。零成本，防重启重放
2. **文本指纹** —— 归一化（去 emoji、折叠空白、剥频道尾巴与推广行、统一标点）
   后 SHA-256 精确匹配；再用 SimHash 抓改了几个字的转发
3. **图片感知哈希** —— pHash 与 dHash 取距离较小者。纯本地计算，不调 AI

第 3 层的实测边界（见后台「去重」页的表格）：重新编码 / 强压缩 / 半透明水印 /
不透明色块水印 / 调亮都能抓到（距离 0-2）；**裁剪与加边框抓不到**（距离 11-29），
这是感知哈希的固有局限，靠第 2 层兜。

命中重复**不丢弃**，只标记 `duplicate_of` 指向首条，后台可展开看「这条在哪些频道
出现过」，可人工翻案。

## 换机器

这台机 2026-09-30 到期。整个目录进 git，换机时 `git clone` +
`docker compose up -d --build` 十分钟恢复。

但要另外搬两样**不在 git 里**的东西：

- `secret.key` —— 否则 DB 里已存的 `api_hash` / `api_key` 解不开
- `sessions/` —— 否则要重新验证码登录

后台「系统」页可下载配置备份（配置 + 术语表 + prompt + feed 定义）。

## 账号风险

用用户账号自动化读取违反 TG 的 ToS，加入大量爆料频道更容易触发风控。
**强烈建议用专门的小号**，不要用主账号。`sessions/` 要备份。控制加频道的节奏。
这是本方案最大的不可逆风险。

## 目录

```
tgmon/
  db.py settings.py crypto.py models.py paths.py util.py   基础设施
  providers/        协议维度的 AI 后端注册表 + 故障转移链
  translate.py      缓存 → 预算闸门 → 术语注入 → 故障转移 → 译后校验
  glossary.py       术语表：注入与漏译检测
  prompts.py        三层 prompt 解析
  dedup.py          文本归一化 + SHA-256 + SimHash
  imghash.py        pHash / dHash（自实现 DCT，不引 scipy）
  media.py          缩略图提取，视频不落盘
  pipeline.py       抓取管线
  outputs.py        webhook 推送 + 统一序列化
  worker/           Telethon 生命周期、任务队列、定时维护
  admin/            FastAPI 后台（10 个页面）+ JSON API + RSS
```
