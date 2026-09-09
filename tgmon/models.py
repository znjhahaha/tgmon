"""全部表结构。

约定：
- 时间统一存 UTC naive datetime，展示层再转 Asia/Shanghai
- 状态字段用 str 不用 Enum —— SQLite 加值不用迁移
- 敏感字段（api_hash/api_key/secret）存 crypto.encrypt() 后的密文
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer,
    LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

# 裸 JSON 会把 Python None 存成 **JSON null 字面量**（DB 里是字符串 'null'），
# 不是 SQL NULL。于是 `col.isnot(None)` 对每一行赋过 None 的记录都成立。
#
# 这个坑真咬过：概览页「N 条术语漏译待复查」用 `glossary_miss.isnot(None)`
# 计数，线上显示 39 条，实际有漏译的是 0 条 —— 39 是「翻译过且没漏译」
# （写进去的是 None）的数量。徽章还指向一个根本没有路由的孤儿模板，
# 所以点不进去也就没人发现数字是假的。
#
# none_as_null=True 让 None 落成真正的 SQL NULL，isnot(None) 才有意义。
JSONNull = JSON(none_as_null=True)


def _now() -> datetime:
    return datetime.utcnow()


class AppSetting(Base):
    """全局配置。配置读取优先级 DB → .env → 内置默认，这张表是最高优先级。"""
    __tablename__ = "app_setting"

    key = Column(String(80), primary_key=True)
    value = Column(Text, nullable=True)
    # str / int / float / bool / json —— 决定读出来怎么转型
    type = Column(String(10), nullable=False, default="str")
    group = Column(String(40), nullable=False, default="general")
    is_secret = Column(Boolean, nullable=False, default=False)
    needs_restart = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class AdminUser(Base):
    """后台登录账号。首次启动用 TGMON_ADMIN_PASSWORD 建号。

    role: admin（全部权限）| guest（只读访客：消息浏览 + RSS 清单，
    可播放归档视频；一切管理操作 403，且全站限流）
    """
    __tablename__ = "admin_user"

    id = Column(Integer, primary_key=True)
    username = Column(String(60), nullable=False, unique=True)
    password_hash = Column(String(200), nullable=False)
    role = Column(String(10), nullable=False, default="admin")
    created_at = Column(DateTime, nullable=False, default=_now)
    last_login_at = Column(DateTime, nullable=True)


class AIProvider(Base):
    """翻译后端注册表。按协议而非厂商建模 —— 加新后端只在网页填一行。

    base_url 必填无默认：这台机器上 OpenAI/Anthropic 官方端点是硬 403，
    内置默认地址只会制造「配了但打不通」的困惑。
    """
    __tablename__ = "ai_provider"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False, unique=True)
    protocol = Column(String(20), nullable=False)  # openai | anthropic
    base_url = Column(String(300), nullable=False)
    api_key = Column(Text, nullable=True)          # 密文
    model = Column(String(120), nullable=False)
    extra_headers = Column(JSONNull, nullable=True)    # {"X-Foo": "bar"}
    max_tokens = Column(Integer, nullable=False, default=4096)
    temperature = Column(Float, nullable=False, default=0.3)
    timeout = Column(Integer, nullable=False, default=90)
    priority = Column(Integer, nullable=False, default=100)  # 升序尝试
    concurrency = Column(Integer, nullable=False, default=2)
    rpm_limit = Column(Integer, nullable=False, default=0)   # 0=不限
    enabled = Column(Boolean, nullable=False, default=True)
    # 成本估算用，单位：元 / 百万 token
    price_in = Column(Float, nullable=False, default=0.0)
    price_out = Column(Float, nullable=False, default=0.0)
    # 运行统计
    ok_count = Column(Integer, nullable=False, default=0)
    fail_count = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    last_ok_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


class Channel(Base):
    """被监控的源。source_type 为将来接网站预留，本轮只有 telegram。"""
    __tablename__ = "channel"

    id = Column(Integer, primary_key=True)
    source_type = Column(String(20), nullable=False, default="telegram")
    # TG 频道用 -100 前缀的完整 ID，存字符串避免大整数坑
    tg_id = Column(String(40), nullable=True, index=True)
    username = Column(String(120), nullable=True)
    title = Column(String(300), nullable=False, default="")
    kind = Column(String(20), nullable=True)  # channel / group / megagroup
    is_private = Column(Boolean, nullable=False, default=False)

    enabled = Column(Boolean, nullable=False, default=False)
    # 「识别不出游戏时」的兜底默认值。曾经它是唯一依据，但爆料频道普遍一个频道
    # 混发多款游戏（Seele Leaks 同时发 GI/HSR/ZZZ），填不出单值就只能留空，
    # 结果整个分游戏术语库被绕过。现在真正生效的是每条消息的 game_detected
    game = Column(String(80), nullable=True)
    theme = Column(String(64), nullable=False, default="gaming")
    # 候选游戏白名单。留空 = 不限，全库投票。填了则判定只在这几个里选 ——
    # 一个只发原神和崩铁的频道不该被 ZZZ 的同名术语拉票
    games = Column(JSONNull, nullable=True)
    prompt_override = Column(Text, nullable=True)      # 第三层 prompt
    # 双语消息路由策略（zh_first 默认 / per_block / always）
    bilingual_policy = Column(String(20), nullable=False, default="zh_first")
    # 判重后仍强制推送（你关注的主频道即使后发也要出）
    force_push = Column(Boolean, nullable=False, default=False)
    translate = Column(Boolean, nullable=False, default=True)
    # 媒体开关
    allow_photo = Column(Boolean, nullable=False, default=True)
    allow_video = Column(Boolean, nullable=False, default=True)
    allow_document = Column(Boolean, nullable=False, default=False)
    max_media_mb = Column(Integer, nullable=False, default=0)  # 0=不限（视频不落盘无所谓）
    keep_video = Column(Boolean, nullable=False, default=False)  # 转码归档，默认关
    # 该频道归档视频的源文件上限（MB）。0 = 继承全局 VIDEO_MAX_MB
    video_max_mb = Column(Integer, nullable=False, default=0)

    last_message_at = Column(DateTime, nullable=True)
    message_count = Column(Integer, nullable=False, default=0)
    # catch-up 对账：TG 侧见到的最新消息 ID 与对账时间。和 last_message_at
    # 的区别：那是「已入库最新」，这是「频道实际最新」—— 两者差值即落后条数
    last_tg_id = Column(Integer, nullable=True)
    last_catchup_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("source_type", "tg_id", name="uq_channel_source_tgid"),
    )


class Glossary(Base):
    """旧术语表。已被 GlossaryEntry + GlossaryAlias 取代，保留只为搬迁留后路。

    bootstrap 会把这里的行搬到新表；确认无误后可手工删表。新代码别再读它。
    """
    __tablename__ = "glossary"

    id = Column(Integer, primary_key=True)
    game = Column(String(80), nullable=False, default="", index=True)
    source_term = Column(String(200), nullable=False)
    target_term = Column(String(200), nullable=False)
    note = Column(Text, nullable=True)
    case_sensitive = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=True)
    hit_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_now)

    __table_args__ = (
        UniqueConstraint("game", "source_term", name="uq_glossary_game_term"),
    )


class GlossaryEntry(Base):
    """术语知识库里的一个实体（角色/武器/圣遗物/地区/敌人/行话）。

    和旧 Glossary 的区别是「一个实体多个 surface form」：卡芙卡有 卡妈/妈妈/Kafka
    三个写法，但只有一个规范中文名和一套属性。旧表一行一个写法，四个外号就是四行
    重复的 target，没有共同身份。
    """
    __tablename__ = "glossary_entry"

    id = Column(Integer, primary_key=True)
    game = Column(String(80), nullable=False, default="", index=True)
    # character|weapon|artifact|region|enemy|element|path|jargon|other
    category = Column(String(24), nullable=False, default="other", index=True)
    canonical_zh = Column(String(200), nullable=False)
    note = Column(Text, nullable=True)

    # 自动导入的一律 pending 等人工过审。一条错术语会静默污染之后每一条翻译，
    # 而且很难发现 —— 所以宁可让审核麻烦一点
    status = Column(String(12), nullable=False, default="active", index=True)
    origin = Column(String(120), nullable=False, default="manual")
    origin_ref = Column(String(120), nullable=True)   # 源里的 slug / id，便于回溯

    # 结构化属性：rarity / element / weapon / region / path 等。
    # 注入 prompt 时带上它能消歧义（Kafka 是角色不是作家）
    attrs = Column(JSONNull, nullable=True)

    enabled = Column(Boolean, nullable=False, default=True)
    hit_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_now)
    reviewed_at = Column(DateTime, nullable=True)

    aliases = relationship("GlossaryAlias", back_populates="entry",
                           cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("game", "canonical_zh", name="uq_kb_game_canonical"),
        Index("ix_kb_game_status", "game", "status"),
    )


class GlossaryAlias(Base):
    """实体的一个写法。多对一。

    alias_kind 必须区分，这是实测纠正过的一个误判：原神 genshin-db 的 aliases 是
    官方称号和命座名（白垩之子 / 白垩之子座），崩铁 nickname.json 才是社区外号
    （卡妈 / 看板娘）。爆料帖里出现社区外号的概率远高于官方称号，混在一起会让
    匹配优先级失真。
    """
    __tablename__ = "glossary_alias"

    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey("glossary_entry.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    surface = Column(String(200), nullable=False)
    lang = Column(String(8), nullable=False, default="en")   # en|ja|zh
    # primary|community|official_title|constellation|abbrev
    alias_kind = Column(String(20), nullable=False, default="primary")
    match_mode = Column(String(12), nullable=False, default="word")  # word|substring|regex
    case_sensitive = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=True)
    hit_count = Column(Integer, nullable=False, default=0)

    entry = relationship("GlossaryEntry", back_populates="aliases")

    __table_args__ = (
        UniqueConstraint("entry_id", "surface", "lang", name="uq_kb_alias"),
        Index("ix_kb_alias_lookup", "lang", "enabled"),
    )


class PromptTemplate(Base):
    """三层 prompt：global < game < channel。高层覆盖低层。"""
    __tablename__ = "prompt_template"

    id = Column(Integer, primary_key=True)
    scope = Column(String(20), nullable=False)   # global | game
    scope_key = Column(String(80), nullable=False, default="")  # game 名；global 留空
    body = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("scope", "scope_key", name="uq_prompt_scope"),
    )


class MonitorMessage(Base):
    """核心表。一条源消息（相册合并后算一条）。"""
    __tablename__ = "monitor_message"

    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channel.id", ondelete="CASCADE"), nullable=False)
    # 去重第 1 层：(channel_id, tg_message_id) 唯一 —— 防重启重放，零成本
    tg_message_id = Column(String(40), nullable=False)
    grouped_id = Column(String(40), nullable=True)  # 相册

    text_raw = Column(Text, nullable=False, default="")
    theme = Column(String(64), nullable=False, default="gaming")
    text_zh = Column(Text, nullable=True)
    # pending / ok / failed / skipped(纯图无文字) / disabled(该频道关了翻译)
    # / skipped_zh(原文已是中文，零 AI 调用)
    translate_status = Column(String(20), nullable=False, default="pending")
    translate_error = Column(Text, nullable=True)
    provider_name = Column(String(80), nullable=True)
    model = Column(String(120), nullable=True)
    tokens_in = Column(Integer, nullable=False, default=0)
    tokens_out = Column(Integer, nullable=False, default=0)
    cost = Column(Float, nullable=False, default=0.0)
    from_cache = Column(Boolean, nullable=False, default=False)
    # 术语命中与漏译，供后台复查
    glossary_hits = Column(JSONNull, nullable=True)
    glossary_miss = Column(JSONNull, nullable=True)
    # 漏译复查队列用。不清空 glossary_miss 本身 —— 那是模型行为的证据，
    # 只打一个「人已经看过了」的时间戳把它移出队列
    miss_reviewed_at = Column(DateTime, nullable=True)

    # 语言路由结果，便于后台排查策略效果
    lang_detected = Column(String(8), nullable=True)         # zh / en / ja / ko / none
    text_dropped = Column(Text, nullable=True)               # 被丢弃的非中文段，可回查
    entities = Column(JSONNull, nullable=True)                   # 标注的实体 canonical_zh 列表
    # 实际生效的游戏归属（显式标记优先，其次术语投票），覆盖 channel.game
    game_detected = Column(String(80), nullable=True)
    # 判定过程：{"scores": {游戏: 分}, "reasons": [...]}。
    # 判错时能直接看出是哪个信号带偏的，不用重跑一遍
    game_scores = Column(JSONNull, nullable=True)
    topics = Column(JSONNull, nullable=True)                     # 内容类型标签，可多个
    version_tag = Column(String(40), nullable=True)          # 7.1 / 4.6 / 3.2.12

    # 剧透。ranges 是相对 text_raw 的 Python 字符区间 [[start, length], ...]。
    # 走模型翻译的消息只有 has_spoiler，没有 ranges —— 译文段落被重写后无法对齐
    spoiler_ranges = Column(JSONNull, nullable=True)
    has_spoiler = Column(Boolean, nullable=False, default=False)

    # 去重第 2 层：归一化正文的 SHA-256 + SimHash（近似匹配）
    text_hash = Column(String(64), nullable=True, index=True)
    simhash = Column(String(20), nullable=True)
    # 去重第 3 层结果与判重指向
    duplicate_of = Column(Integer, ForeignKey("monitor_message.id"), nullable=True)
    dup_reason = Column(String(30), nullable=True)  # text_exact/simhash/phash
    dup_overridden = Column(Boolean, nullable=False, default=False)  # 人工翻案

    deeplink = Column(String(300), nullable=True)
    sender_name = Column(String(200), nullable=True)
    has_media = Column(Boolean, nullable=False, default=False)
    published_at = Column(DateTime, nullable=False, default=_now, index=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    pushed_at = Column(DateTime, nullable=True)

    channel = relationship("Channel")
    media = relationship("MessageMedia", back_populates="message",
                         cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("channel_id", "tg_message_id", name="uq_msg_channel_tgid"),
        Index("ix_msg_published_dup", "published_at", "duplicate_of"),
    )


class MessageMedia(Base):
    """只存缩略图。视频从不落盘，靠 deeplink 回 TG 看。"""
    __tablename__ = "message_media"

    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("monitor_message.id", ondelete="CASCADE"),
                        nullable=False)
    kind = Column(String(20), nullable=False)  # photo | video | document
    source_tg_id = Column(String(80), nullable=True, index=True)
    source_identity = Column(String(160), nullable=True)
    status = Column(String(24), nullable=False, default="ready")
    error = Column(Text, nullable=True)
    sha256 = Column(String(64), nullable=True, index=True)
    original_path = Column(String(300), nullable=True)
    original_bytes = Column(Integer, nullable=False, default=0)
    # media/ 下的相对路径
    thumb_path = Column(String(300), nullable=True)
    thumb_bytes = Column(Integer, nullable=False, default=0)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    duration = Column(Integer, nullable=True)      # 视频时长（秒）
    orig_bytes = Column(Integer, nullable=False, default=0)  # 原始媒体大小
    mime = Column(String(80), nullable=True)
    # 去重第 3 层：感知哈希，抓「同图配不同文案」与重新编码
    phash = Column(String(20), nullable=True, index=True)
    dhash = Column(String(20), nullable=True)
    # 归档转码后的视频。默认不产生 —— 要 VIDEO_ARCHIVE_ENABLED 且频道开 keep_video。
    # 路径相对 VIDEO_DIR（不是 MEDIA_DIR），那个目录不挂给 Caddy，只走鉴权路由
    video_path = Column(String(300), nullable=True)
    video_bytes = Column(Integer, nullable=False, default=0)   # 转码后大小
    # ok / skipped_size / skipped_duration / skipped_disabled / failed
    video_status = Column(String(20), nullable=True)
    has_spoiler = Column(Boolean, nullable=False, default=False)
    last_access_at = Column(DateTime, nullable=False, default=_now)
    created_at = Column(DateTime, nullable=False, default=_now)

    message = relationship("MonitorMessage", back_populates="media")


class TranslationCache(Base):
    """hash(原文+prompt+model) → 译文。同一条被多频道转发时不重复调用。"""
    __tablename__ = "translation_cache"

    key = Column(String(64), primary_key=True)
    text_zh = Column(Text, nullable=False)
    provider_name = Column(String(80), nullable=True)
    model = Column(String(120), nullable=True)
    hit_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_now)
    last_hit_at = Column(DateTime, nullable=True)


class Task(Base):
    """admin → worker 的任务队列。

    存在的理由：Telethon 的 session 是 SQLite，两个进程同时用同一个 session
    会互锁并可能损坏授权状态。所以只有 worker 碰 session，admin 想做任何需要
    TG 连接的事（发验证码、登录、同步频道列表）都写一条任务，由 worker 执行。
    """
    __tablename__ = "task"

    id = Column(Integer, primary_key=True)
    # send_code / sign_in / sign_in_2fa / logout / sync_dialogs
    # test_provider / retranslate / repush / restart_worker / backfill
    # archive_video
    kind = Column(String(40), nullable=False)
    payload = Column(JSONNull, nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    result = Column(JSONNull, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class ProcessingJob(Base):
    __tablename__ = "processing_job"

    id = Column(Integer, primary_key=True)
    queue = Column(String(24), nullable=False, index=True)
    scope_key = Column(String(160), nullable=True, index=True)
    key = Column(String(160), nullable=False, unique=True)
    payload = Column(JSONNull, nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    available_at = Column(DateTime, nullable=False, default=_now, index=True)
    lease_until = Column(DateTime, nullable=True)
    owner = Column(String(64), nullable=True)
    error = Column(Text, nullable=True)
    result = Column(JSONNull, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now)


class SourceEvent(Base):
    __tablename__ = "source_event"

    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channel.id", ondelete="CASCADE"), nullable=False)
    source_id = Column(String(80), nullable=False)
    revision = Column(String(64), nullable=False)
    grouped_id = Column(String(80), nullable=True)
    raw = Column(LargeBinary, nullable=True)
    snapshot = Column(JSONNull, nullable=False)
    message_id = Column(Integer, nullable=True)
    received_at = Column(DateTime, nullable=False, default=_now)
    __table_args__ = (UniqueConstraint("channel_id", "source_id", "revision",
                                       name="uq_source_event_revision"),)


class SourceCursor(Base):
    __tablename__ = "source_cursor"

    channel_id = Column(Integer, ForeignKey("channel.id", ondelete="CASCADE"), primary_key=True)
    state = Column(JSONNull, nullable=True)
    offset_id = Column(Integer, nullable=False, default=0)
    high_id = Column(Integer, nullable=False, default=0)
    complete = Column(Boolean, nullable=False, default=False)
    cutoff = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_now)
    error = Column(Text, nullable=True)


class SchemaMigration(Base):
    __tablename__ = "schema_migration"
    version = Column(Integer, primary_key=True)
    applied_at = Column(DateTime, nullable=False, default=_now)


class ContentRevision(Base):
    __tablename__ = "content_revision"
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, nullable=False, index=True)
    digest = Column(String(64), nullable=False)
    snapshot = Column(JSONNull, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now)
    __table_args__ = (UniqueConstraint("message_id", "digest", name="uq_content_revision"),)


class ContentRelation(Base):
    __tablename__ = "content_relation"
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, nullable=False, index=True)
    related_id = Column(Integer, nullable=False)
    reason = Column(String(30), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now)
    __table_args__ = (UniqueConstraint("message_id", "related_id", "reason", name="uq_content_relation"),)


class WorkerState(Base):
    """worker 心跳与状态，单行表（id=1）。概览页读它判断在线。"""
    __tablename__ = "worker_state"

    id = Column(Integer, primary_key=True, default=1)
    heartbeat_at = Column(DateTime, nullable=True)
    # booting / need_credentials / need_login / online / error
    status = Column(String(30), nullable=False, default="booting")
    tg_user = Column(String(200), nullable=True)
    detail = Column(Text, nullable=True)
    # 当前正在干什么的人话描述（对账 xxx / 任务 backfill / 转码 xxx.mp4）。
    # 空串 = 空闲。给概览页「不知道系统在不在干活」一个直接答案
    current_action = Column(String(200), nullable=True, default="")
    queue_depth = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime, nullable=True)


class ApiKey(Base):
    __tablename__ = "api_key"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)
    # 只存 sha256，创建时一次性显示明文
    key_hash = Column(String(64), nullable=False, unique=True)
    prefix = Column(String(12), nullable=False)  # 供界面辨识
    enabled = Column(Boolean, nullable=False, default=True)
    rate_per_min = Column(Integer, nullable=False, default=120)
    last_used_at = Column(DateTime, nullable=True)
    use_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_now)


class Webhook(Base):
    __tablename__ = "webhook"

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)
    url = Column(String(400), nullable=False)
    secret = Column(Text, nullable=True)  # 密文，HMAC-SHA256 签名用
    enabled = Column(Boolean, nullable=False, default=True)
    # 留空=全部频道；否则 channel id 列表
    channel_ids = Column(JSONNull, nullable=True)
    include_duplicates = Column(Boolean, nullable=False, default=False)
    max_retry = Column(Integer, nullable=False, default=4)
    created_at = Column(DateTime, nullable=False, default=_now)


class WebhookDelivery(Base):
    __tablename__ = "webhook_delivery"

    id = Column(Integer, primary_key=True)
    webhook_id = Column(Integer, ForeignKey("webhook.id", ondelete="CASCADE"), nullable=False)
    message_id = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    http_status = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    next_retry_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    delivered_at = Column(DateTime, nullable=True)


class QqBot(Base):
    """QQ 官方机器人凭据，支持多个机器人同时接入。

    每行一个机器人（q.qq.com 创建后拿 AppID / AppSecret）。token 获取、
    回调验签、消息发送都按行隔离。群（QqGroup）按「拉它进群的机器人」
    归属——QQ 平台的 group_openid 本身就是按机器人发的：同一物理群里
    不同机器人拿到的 openid 不同，多机器人数据天然不冲突。

    历史兼容：迁移（bootstrap）会把旧配置 QQ_APP_ID / QQ_APP_SECRET
    导入为首行；bot_id 为 NULL 的群在「恰好只有一个启用机器人」时
    仍然推送（单机器人旧部署零改动）。
    """
    __tablename__ = "qq_bot"

    id = Column(Integer, primary_key=True)
    app_id = Column(String(40), nullable=False, unique=True, index=True)
    nickname = Column(String(60), nullable=False, default="")
    # 密文存储（crypto 主密钥加密，与 settings 敏感项同机制）
    app_secret_enc = Column(String(400), nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    # 该机器人的 botpy 桥心跳（每个机器人一个桥容器，各自上报）
    bridge_last_seen = Column(DateTime, nullable=True)
    added_at = Column(DateTime, nullable=False, default=_now)


class QqBotCapability(Base):
    """Per-bot delivery capabilities learned from platform responses.

    QQ accounts can differ in whether proactive group delivery is permitted.
    Keeping this state outside ``qq_bot`` lets existing installations migrate
    by simply creating one small table and leaves credential rows untouched.
    """
    __tablename__ = "qq_bot_capability"

    bot_id = Column(Integer, ForeignKey("qq_bot.id", ondelete="CASCADE"), primary_key=True)
    proactive_enabled = Column(Boolean, nullable=False, default=True)
    proactive_error = Column(Text, nullable=True)
    proactive_disabled_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class QqGroup(Base):
    """QQ 官方机器人的目标群。

    group_openid 不是群号 —— 是机器人被拉进群时腾讯回调 GROUP_ADD_ROBOT
    事件里给的临时 ID，发消息只能用它。进群回调自动建行，无需手工录入。
    """
    __tablename__ = "qq_group"

    id = Column(Integer, primary_key=True)
    group_openid = Column(String(64), nullable=False, unique=True, index=True)
    # 回调事件不给群名，只能人工在后台标注，方便辨认
    nickname = Column(String(120), nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=True)
    # 被腾讯判「机器人不在群」时置位，页面提示重新拉群
    dropped_at = Column(DateTime, nullable=True)
    added_at = Column(DateTime, nullable=False, default=_now)
    last_sent_at = Column(DateTime, nullable=True)
    sent_today = Column(Integer, nullable=False, default=0)
    sent_date = Column(String(10), nullable=True)  # YYYY-MM-DD（本地时区），当日计数用
    # /latest 与 /new 的「已读游标」：该群成员上次拉取到的最新消息 id。
    # /new 只返回比它新的消息；拉取后推进。NULL = 从未拉取过
    last_seen_msg_id = Column(Integer, nullable=True)
    games = Column(JSONNull, nullable=True)
    themes = Column(JSONNull, nullable=True)
    cursors = Column(JSONNull, nullable=True)
    # 归属机器人（qq_bot.id）。NULL = 迁移前的存量群，仅在恰好一个
    # 启用机器人时推送；机器人进群/被 @ 事件会带上归属自动落库
    bot_id = Column(Integer, nullable=True, index=True)


class QqDelivery(Base):
    """QQ 群推送的投递记录。重试节奏与 webhook_delivery 一致（30s/120s/600s/3600s）。"""
    __tablename__ = "qq_delivery"

    id = Column(Integer, primary_key=True)
    group_openid = Column(String(64), nullable=False, index=True)
    bot_id = Column(Integer, nullable=True, index=True)
    source = Column(String(40), nullable=False, default="legacy")
    trigger = Column(String(80), nullable=False, default="push")
    message_id = Column(Integer, nullable=True, index=True)  # NULL = 测试消息
    bundle_id = Column(Integer, nullable=True)
    dedup_key = Column(String(80), nullable=True, unique=True)
    parts = Column(JSONNull, nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    next_retry_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    delivered_at = Column(DateTime, nullable=True)


class QqInbound(Base):
    """QQ 侧入站消息留痕（botpy WS 桥或 webhook 收到的群 @ / C2C）。

    管理页「最近收到的消息」的数据源，也是「群友都吩咐过什么」的审计
    记录 —— 排查「机器人没反应」时，先看这里有没有行：有行说明事件
    到了、是处理或回复环节的问题；没行说明事件根本没进来（桥挂了或
    平台没推）。
    """
    __tablename__ = "qq_inbound"

    id = Column(Integer, primary_key=True)
    # GROUP_AT_MESSAGE_CREATE / C2C_MESSAGE_CREATE
    event_type = Column(String(40), nullable=False, index=True)
    # C2C 私聊时为空串
    group_openid = Column(String(64), nullable=False, default="")
    # 群消息 = member_openid；C2C = user_openid
    member_openid = Column(String(64), nullable=False, default="")
    content = Column(Text, nullable=False, default="")
    msg_id = Column(String(120), nullable=True)
    reply = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)


class RetrievalIndex(Base):
    """Unified lexical/semantic index record for messages and future KB/memory data."""
    __tablename__ = "retrieval_index"

    id = Column(Integer, primary_key=True)
    ref_type = Column(String(24), nullable=False, default="message")
    ref_id = Column(Integer, nullable=False)
    text = Column(Text, nullable=False, default="")
    title = Column(String(300), nullable=False, default="")
    channel = Column(String(300), nullable=False, default="")
    channel_id = Column(Integer, nullable=True, index=True)
    game = Column(String(80), nullable=True)
    version = Column(String(40), nullable=True)
    published_at = Column(DateTime, nullable=True, index=True)
    deeplink = Column(String(400), nullable=True)
    embedding = Column(LargeBinary, nullable=True)  # float32 bytes
    embedding_model = Column(String(120), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("ref_type", "ref_id", name="uq_retrieval_ref"),
        Index("ix_retrieval_game_channel", "game", "channel"),
    )


class Conversation(Base):
    """Short and long term QQ conversation state, isolated by scope."""
    __tablename__ = "conversation"

    id = Column(Integer, primary_key=True)
    scope_type = Column(String(12), nullable=False)  # group | user
    scope_id = Column(String(120), nullable=False)
    summary = Column(Text, nullable=True)
    facts = Column(JSONNull, nullable=True)
    context_state = Column(JSONNull, nullable=True)
    revision = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)
    expires_at = Column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("scope_type", "scope_id", name="uq_conversation_scope"),)


class ConversationTurn(Base):
    __tablename__ = "conversation_turn"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False, default="")
    tool = Column(String(40), nullable=True)
    citations = Column(JSONNull, nullable=True)
    actor_id = Column(String(80), nullable=True)
    # 记忆 v2：说话人的可读名（昵称）。落库时快照，注入时免二次查表
    speaker = Column(String(80), nullable=True)
    event_key = Column(String(160), nullable=True, unique=True)
    source_event_id = Column(String(160), nullable=True)
    reply_to = Column(String(160), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now, index=True)

    conversation = relationship("Conversation")


class MemberProfile(Base):
    """QQ 成员档案：昵称 + 个人事实（记忆 v2）。

    member_openid 是 QQ 官方给的哈希，模型读不懂也分不清人。昵称由用户
    自称（"叫我科比"）或 /nickname 命令写入；个人事实从 /remember 与
    agent 对话直接落库，不再依赖摘要管线提取。
    """
    __tablename__ = "member_profile"

    id = Column(Integer, primary_key=True)
    member_openid = Column(String(80), nullable=False, unique=True, index=True)
    nickname = Column(String(60), nullable=True)
    scope_data = Column(JSONNull, nullable=True)
    confirmation = Column(String(20), nullable=False, default="pending")
    facts = Column(JSONNull, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class KnowledgeSource(Base):
    __tablename__ = "knowledge_source"

    id = Column(Integer, primary_key=True)
    game = Column(String(80), nullable=False, default="")
    url = Column(String(400), nullable=False)
    categories = Column(JSONNull, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    trusted = Column(Boolean, nullable=False, default=False)
    kind = Column(String(24), nullable=False, default="wiki")
    last_sync_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)


class KnowledgePage(Base):
    __tablename__ = "knowledge_page"

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("knowledge_source.id", ondelete="CASCADE"), nullable=False, index=True)
    wiki_page_id = Column(String(80), nullable=False)
    revision_id = Column(String(80), nullable=True)
    title = Column(String(300), nullable=False)
    content = Column(Text, nullable=False, default="")
    content_hash = Column(String(64), nullable=False, default="")
    status = Column(String(12), nullable=False, default="pending")
    document_status = Column(String(12), nullable=False, default="pending")
    review_reason = Column(String(80), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    category = Column(String(40), nullable=False, default="other")
    entity = Column(String(200), nullable=True)
    attrs = Column(JSONNull, nullable=True)
    aliases = Column(JSONNull, nullable=True)
    first_seen_at = Column(DateTime, nullable=False, default=_now)
    last_seen_at = Column(DateTime, nullable=False, default=_now)
    missing_count = Column(Integer, nullable=False, default=0)
    enabled = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("source_id", "wiki_page_id", name="uq_knowledge_page"),
    )


class RssFeed(Base):
    """一个 feed = 一组筛选条件。按频道 / 按 game / 按关键词组合。"""
    __tablename__ = "rss_feed"

    id = Column(Integer, primary_key=True)
    slug = Column(String(60), nullable=False, unique=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    channel_ids = Column(JSONNull, nullable=True)   # 留空=全部
    games = Column(JSONNull, nullable=True)
    keywords = Column(JSONNull, nullable=True)      # 任一命中
    include_duplicates = Column(Boolean, nullable=False, default=False)
    include_original = Column(Boolean, nullable=False, default=True)  # 译文后附原文
    max_items = Column(Integer, nullable=False, default=50)
    public = Column(Boolean, nullable=False, default=True)  # false 则需 API Key
    created_at = Column(DateTime, nullable=False, default=_now)


class ShareToken(Base):
    """消息分享授权（单条或多条合并）。管理员生成短链，持有 token 者
    免登录看授权的消息。

    校验与显示分离（2026-09 用户反馈：刷新后链接 URL 找不回）：
    - token_hash：SHA-256，校验入口（库泄露也遍历不出有效链接）
    - token_enc：token 的可逆加密值（secret.key），仅用于管理侧还原
      URL 显示/复制。旧链接（升级前生成）只有 hash，显示为不可恢复
    - message_ids：合并分享的消息 id 数组；null = 单条（用 message_id，
      兼容存量）。message_id 对合并链接存第一条的 id 做锚点
      （列是 NOT NULL，访客页优先读 message_ids）
    撤销 = revoked 置位；过期 = expires_at 到点，路由侧二选一校验。
    """
    __tablename__ = "share_token"

    id = Column(Integer, primary_key=True)
    # token_urlsafe(12) 的 SHA-256 十六进制（64 字符），唯一索引
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    message_id = Column(Integer,
                        ForeignKey("monitor_message.id", ondelete="CASCADE"),
                        nullable=False)
    # JSON 数组（消息 id 列表），合并分享；null = 单条
    message_ids = Column(JSONNull, nullable=True)
    snapshot_items = Column(JSONNull, nullable=True)
    # 可逆加密的 token（Fernet），管理侧还原 URL 用
    token_enc = Column(Text, nullable=True)
    # 一图流分享的人工/AI 一句话概括（2026-09）。空 = 未填
    summary = Column(Text, nullable=True)
    # 消息 id 集合的内容指纹（2026-09）：同内容分享去重复用，命中
    # bundle_cards 的渲染缓存，重复查询不再重新渲染长图
    content_key = Column(String(64), nullable=True, index=True)
    created_by = Column(String(60), nullable=False, default="")
    expires_at = Column(DateTime, nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)
    views = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_now)


class QqEvent(Base):
    __tablename__ = "qq_event"
    event_key = Column(String(80), primary_key=True)
    status = Column(String(20), nullable=False, default="processing")
    result = Column(JSONNull, nullable=True)
    parts = Column(JSONNull, nullable=True)
    reply_deadline = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)


class QqPending(Base):
    __tablename__ = "qq_pending"
    id = Column(Integer, primary_key=True)
    group_openid = Column(String(80), nullable=False, index=True)
    message_id = Column(Integer, nullable=False)
    game = Column(String(80), nullable=False, default="")
    status = Column(String(20), nullable=False, default="waiting", index=True)
    delivery_id = Column(Integer, nullable=True)
    ready_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    __table_args__ = (UniqueConstraint("group_openid", "message_id", name="uq_qq_pending_message"),)


class AiUsage(Base):
    """按天累计，用于预算上限与概览页花费估算。"""
    __tablename__ = "ai_usage"

    id = Column(Integer, primary_key=True)
    day = Column(String(10), nullable=False)  # YYYY-MM-DD（本地时区）
    provider_name = Column(String(80), nullable=False, default="")
    calls = Column(Integer, nullable=False, default=0)
    tokens_in = Column(Integer, nullable=False, default=0)
    tokens_out = Column(Integer, nullable=False, default=0)
    cost = Column(Float, nullable=False, default=0.0)

    __table_args__ = (
        UniqueConstraint("day", "provider_name", name="uq_usage_day_provider"),
    )


class SystemEvent(Base):
    """最近错误 / 重要事件。概览页显示，避免只能看 docker logs。"""
    __tablename__ = "system_event"

    id = Column(Integer, primary_key=True)
    level = Column(String(10), nullable=False, default="info")
    source = Column(String(40), nullable=False, default="")
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_now, index=True)
