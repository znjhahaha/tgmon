# 术语知识库重设计 - 实施状态

## 2026-09-06：统一知识、记忆与 QQ 发布链路

- 统一消息查询使用规范游戏别名、有效归属、文本指纹和重复消息媒体；网站、RSS、API、QQ 查询与推送共用结果。
- QQ 会话按群话题、群内成员和私聊隔离，事件、回复分段、60 秒游戏批次、订阅、分享快照和发送核实状态均持久化。
- 最新爆料默认发送 48 小时快照长图和链接；明确文字请求、翻译和问答继续发送文字。长图按可读文本和原图比例分页。
- Wiki 资料发布、来源可信度、分类判断依据、索引重建、记忆管理和发送异常集中在 `/kb` 的五个页签。
- 迁移前自动生成 SQLite 备份，277 项本地测试通过；发布前仍需线上预检、真实翻译后端校验和桌面/手机页面检查。

## 2026-09-05 续作：本地向量与 QQ

- 默认开启本地 BGE-small-zh embedding（512 维，CPU）；Docker 镜像内置权重，运行时不调用 embedding API。
- 文档更新清除旧向量，旧模型向量重建，长文分块，缺模型时保留关键词检索。
- QQ 新增 `/translate`、`/tr`、`翻译：`，支持指定游戏和 `#消息id`，使用已审核知识库、现有翻译后端和预算。
- 同条爆料的全部图片合为一张完整长图，正文与图同发；覆盖拉取命令、主动推送和重试，按一条消息计配额。
- 图片默认直传 QQ；线上已验证完整长图上传成功，避开 GitHub URL 拉取超时，URL 中转保留为备用。
- 已通过本地完整回归：233 tests；编译与 Python 3.11 语法检查通过；真实模型断网检索通过。
- 部署脚本在 `scripts/stage_release.sh` 与 `scripts/apply_release.sh`，先验证数据库副本，再备份并切换服务。

> 状态说明（2026-09-05）：本文后面的历史章节记录早期术语库工作；本次分享、RAG、QQ 记忆和 Wiki 升级已完成并以代码与测试为准。Wiki 默认来源覆盖原神、崩坏：星穹铁道、绝区零三个 Fandom MediaWiki API，首次同步条目统一待审核。

## 本轮升级已完成

- 分享：两遍布局长图、完整正文与媒体、消息边界分页、JPEG/ZIP 下载、公开 token 媒体与长图、跨分页多选、筛选结果最多 50 条、完整文案复制。
- 检索：`RetrievalIndex` + FTS5/LIKE 回退、可选 float32 embedding、混合排序、游戏/频道/时间/实体过滤、相关消息与时间线、索引幂等和 TTL 回收。
- QQ：群/个人隔离记忆、滚动摘要、`/ask`/`/related`/`/timeline`/`/remember`/`/forget`，Agent 工具白名单与证据引用。
- Wiki：通用 MediaWiki/Fandom 适配器、分类分页、revision 幂等、失败保留旧数据、连续消失禁用、待审核页面与批量审核接入翻译。
- 验证：`python -m compileall -q tgmon` 和 `python -m pytest -q` 均通过（190 tests）。

## ✅ 已完成

### 核心功能

1. **语言检测与路由** (`tgmon/lang.py`)
   - 逐行语言判定（假名优先 → 谚文 → 汉字/词数比较）
   - 分块合并同语言相邻行
   - 三种策略：`zh_first` / `per_block` / `always`
   - 24 个测试用例全部通过
   - **已集成到 pipeline.py** - 翻译前先路由

2. **知识库数据模型** (`tgmon/models.py`)
   - `GlossaryEntry` - 实体表（1609 条）
   - `GlossaryAlias` - 别名表（2546 条）
   - 五种别名类型：`primary` / `community` / `official_title` / `constellation` / `abbrev`
   - 三种匹配模式：`word` / `substring` / `regex`
   - 状态机：`pending` → `active` / `rejected`

3. **数据导入** (`tgmon/kb/importer.py`)
   - 崩铁：Mar-7th/StarRailRes (463 条，含社区外号)
   - 原神：theBowja/genshin-db (1085 条，含官方称号/命座)
   - 绝区零：Dimbreath/ZenlessData (43 条，仅角色)
   - 通用行话：手写种子表 (91 条，待用户审核)
   - **已修复**：中文规范名现在会自动添加为 primary alias

4. **实体标注** (`tgmon/kb/annotate.py`)
   - 多别名匹配（中英文+社区外号）
   - 按优先级去重（community > abbrev > primary > official_title > constellation）
   - 输出结构化属性（稀有度/元素/命途/武器类型等）
   - **已集成到 pipeline.py** - 入库时自动标注

5. **翻译系统集成** (`tgmon/glossary.py`)
   - 术语表注入 prompt（只用非中文别名）
   - 实体上下文注入（带属性消歧义）
   - 长度优先排序（避免短词抢长词）
   - 命中数统计
   - 漏译校验（复用原有逻辑）

6. **数据库迁移** (`tgmon/bootstrap.py`)
   - 幂等补列机制（无 alembic）
   - 老 `glossary` 表迁移（→ entry+alias）
   - 行话种子导入
   - **已在 VPS 上执行成功**

7. **Web UI** (`tgmon/admin/routes/glossary_ui.py` + `kb.html`)
   - 游戏/类别/状态筛选
   - 批量导入按钮（三个游戏）
   - 状态下拉切换（pending → active）
   - 别名 pills 显示（社区外号标记）
   - 删除功能

8. **备份导出** (`tgmon/admin/routes/system.py`)
   - 嵌套 JSON 结构（entry + aliases）
   - 包含审核状态（手工劳动成果）

### 验证结果

```
✅ 语言路由
  - 全中文：正确跳过翻译 (zh_ratio=1.00)
  - 双语重复：中文段保留，英文段丢弃
  - 纯英文：正常翻译

✅ 实体标注
  输入："卡芙卡和布洛妮娅在新版本卡池中复刻"
  输出：
    - 卡芙卡 (5★ 虚无 雷)
    - 布洛妮娅 (5★ 存护 风)

✅ 知识库统计
  - 总计：1609 实体，2546 别名
  - 激活：178 条（91 行话 + 87 测试角色）
  - 待审：1431 条（游戏数据）
  - 原神：1085 条
  - 崩铁：463 条（含 127 个社区外号）
  - 绝区零：43 条
```

### 新增配置项 (`tgmon/settings.py`)

```python
LANG_ROUTE_ENABLED = True              # 语言路由总开关
BILINGUAL_ZH_MIN_CHARS = 30            # 实质中文段最小字数
ENTITY_TAG_ENABLED = True              # 实体标注开关
KB_AUTO_APPROVE = False                # 导入后自动激活（默认关闭）
```

### 新增数据库字段

```sql
-- channel 表
ALTER TABLE channel ADD COLUMN bilingual_policy VARCHAR(20) NOT NULL DEFAULT 'zh_first';

-- monitor_message 表
ALTER TABLE monitor_message ADD COLUMN lang_detected VARCHAR(8);
ALTER TABLE monitor_message ADD COLUMN text_dropped TEXT;
ALTER TABLE monitor_message ADD COLUMN entities TEXT;  -- JSON
```

## 🚧 待完成

### 高优先级

1. **频道策略 UI**
   - `channels.html` 添加 `bilingual_policy` 下拉
   - 三选一：`zh_first` / `per_block` / `always`
   - 默认 `zh_first` 适用于大多数中文聚合频道

2. **消息详情页增强**
   - 显示 `lang_detected` 标签
   - 显示实体标签（可点击过滤）
   - `text_dropped` 可展开查看

3. **RSS/API 过滤**
   - `/rss/{slug}?entity=女皇` - 按实体订阅
   - `/api/messages?entity=卡芙卡` - API 过滤
   - 后台消息列表添加实体筛选器

4. **漏译闭环**
   - 漏译队列添加「加进术语表」按钮
   - 一键创建 pending entry
   - 这是让术语表持续变准的唯一路径

### 中优先级

5. **Worker 任务迁移**
   - `kb_import` handler（目前导入在 admin 进程，违反设计原则）
   - 三个游戏的导入改为提交 task

6. **缓存键修正**
   - `translate.py` 的 `cache_key()` 加入 `bilingual_policy`
   - 否则改策略后会命中旧译文

7. **UI 细节**
   - 待审队列按 category + rarity 分组
   - 批量通过操作（「全选 5★ 角色」）
   - 导入进度提示

### 低优先级

8. **文档**
   - 用户手册（如何审核待审条目）
   - 术语匹配优先级说明
   - 双语策略选择指南

9. **监控**
   - 跳过翻译的消息数统计
   - 实体标注命中率
   - 术语表命中分布

## 📊 性能收益预估

基于真实爆料频道采样（`t.me/s/Genshinleakflow`）：

- **约 60% 消息是中文原文** → 零 AI 调用
- **约 20% 是双语重复** → 省一半翻译成本
- **剩余 20% 纯英文** → 翻译质量提升（术语表 + 实体上下文）

**预计节省 70% 翻译调用，同时提升译文准确性。**

## ⚠️ 已知局限

1. **ZZZ 覆盖有限**
   - 只有 43 个角色（`Avatar_Name` 筛选结果）
   - 武器类名称散布在活动道具中，未导入
   - 需要时可手工补充或等官方数据结构稳定

2. **未实装角色缺失**
   - 米提亚、兹白、莉奈娅等只在爆料圈流传的名字不在官方数据
   - 只能通过漏译队列手工添加

3. **`zh_first` 策略假设**
   - 假定「有中文段 = 双语重复」
   - 极少数情况英文段可能有独立信息
   - 兜底：`text_dropped` 存库可回查，且可按频道改策略

## 🔄 部署清单

```bash
# 1. 上传新代码（已完成）
scp -r tgmon/kb/ znj:/opt/tgmon/tgmon/
scp tgmon/lang.py znj:/opt/tgmon/tgmon/
scp tgmon/pipeline.py znj:/opt/tgmon/tgmon/
scp tgmon/models.py znj:/opt/tgmon/tgmon/
scp tgmon/bootstrap.py znj:/opt/tgmon/tgmon/
scp tgmon/glossary.py znj:/opt/tgmon/tgmon/
scp tgmon/translate.py znj:/opt/tgmon/tgmon/
scp tgmon/settings.py znj:/opt/tgmon/tgmon/
scp tgmon/admin/routes/glossary_ui.py znj:/opt/tgmon/tgmon/admin/routes/
scp tgmon/admin/templates/kb.html znj:/opt/tgmon/tgmon/admin/templates/
scp tgmon/admin/templates/base.html znj:/opt/tgmon/tgmon/admin/templates/

# 2. 重启容器（已完成）
ssh znj 'cd /opt/tgmon && docker compose restart admin worker'

# 3. 补充缺失的中文 primary alias（已完成）
# 178 个实体已补齐

# 4. 待审条目审核（需用户操作）
# 登录后台 → 知识库 → 筛选 pending → 按游戏/类别批量通过
```

## 📝 用户待办

### 立即

1. **审核行话种子表** (`tgmon/kb/seed/jargon.csv`)
   - 91 条通用/原神/崩铁/绝区零术语
   - 特别关注：
     - `精通` vs `元素精通`（设了 case_sensitive）
     - `EM` → `精通`（缩写）
     - 各种元素反应（超载/感电/超导等）
   - 确认无误后这些会自动激活

2. **激活测试实体**
   - 后台 → 知识库 → 筛选 pending
   - 建议先激活：5★ 角色（高频出现）
   - 批量操作：选中 → 状态改 active

### 后续

3. **观察漏译队列**
   - 后台 → 消息 → 漏译记录
   - 高频漏译词 → 点「加进术语表」

4. **调整频道策略**
   - 如发现某频道英文段常有独立信息
   - 频道设置 → `bilingual_policy` 改为 `per_block`

---

**生成时间**: 2026-09-01  
**实施者**: Claude Code  
**审核状态**: 待用户确认行话表与实体激活策略
