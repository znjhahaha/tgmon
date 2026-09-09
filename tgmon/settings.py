"""配置读取：DB(app_setting) → .env → 内置默认。

首次启动把 .env 当种子导入 DB，之后以 DB 为准 —— 网页改完即时生效，不必登
服务器改文件。少数字段（API_ID/API_HASH/PHONE）改完要重新登录，界面单独标注。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from .crypto import decrypt, encrypt
from .db import session_scope
from .models import AppSetting

logger = logging.getLogger(__name__)

# key -> (default, type, group, is_secret, needs_restart, 说明)
DEFAULTS: dict[str, tuple[Any, str, str, bool, bool, str]] = {
    # ---- 账号与凭据 ----
    "API_ID": ("", "str", "account", False, True, "my.telegram.org 拿到的 api_id"),
    "API_HASH": ("", "str", "account", True, True, "api_hash，32 位十六进制"),
    "PHONE_NUMBER": ("", "str", "account", True, True, "+8613800138000"),
    "BOT_TOKEN": ("", "str", "account", True, True, "可选。仅用于把错误告警发到 TG"),
    "ALERT_CHAT_ID": ("", "str", "account", False, False, "可选。告警发给谁（你的 user_id）"),

    # ---- 翻译 ----
    "TRANSLATE_ENABLED": (True, "bool", "translate", False, False, "总开关"),
    "TRANSLATE_CONCURRENCY": (2, "int", "translate", False, False, "全局并发翻译数"),
    "AI_CHAT_DEADLINE": (20, "int", "translate", False, False, "聊天请求总截止时间（秒）"),
    "AI_TRANSLATE_DEADLINE": (90, "int", "translate", False, False, "翻译请求总截止时间（秒）"),
    "AI_SUMMARY_DEADLINE": (30, "int", "translate", False, False, "摘要请求总截止时间（秒）"),
    "AI_PURPOSE_PROVIDERS": ({}, "json", "translate", False, False, "按用途排列优先后端名称"),
    "TRANSLATE_CACHE_ENABLED": (True, "bool", "translate", False, False, "同文缓存"),
    "GLOSSARY_CHECK_ENABLED": (True, "bool", "translate", False, False, "译后术语校验"),
    "APPEND_ORIGINAL": (True, "bool", "translate", False, False, "译文后附原文"),
    "LANG_ROUTE_ENABLED": (True, "bool", "translate", False, False, "语言路由（跳过中文原文）"),
    # 10 而不是 30。门槛量的是「中文块 strip 后的字符数」，不是整条消息的字数
    # —— 这两个数差很远，「萍姥姥不会在 7.0 大版本内成为自机」整行 27 字，
    # 但作为块只有 19。实测：真双语消息的中文块 13~19 字，而 #tag、【原神】、
    # 「来源：NGA」这类不该被当成译文的行只有 4~6 字。10 卡在这两簇中间。
    # 调高到 20 会让三条真双语消息的英文段照样送翻译，产出和中文段重复的译文
    "BILINGUAL_ZH_MIN_CHARS": (10, "int", "translate", False, False, "双语消息中文段最小长度"),
    "ENTITY_TAG_ENABLED": (True, "bool", "translate", False, False, "实体标注"),
    "GAME_DETECT_ENABLED": (True, "bool", "translate", False, False,
                            "按消息识别游戏（多游戏频道必开）"),
    # 分数量的是术语投票的加权和，不是命中条数。character 3 分、光锥/音擎/武器/
    # 游戏行话 2 分、enemy/region 0.5 分（这两类词太泛，容易制造假票），
    # 术语长度 ≥6 再 ×1.3。2.5 这条线的实际效果：
    #   - 一个角色名（3.0）够 —— 「Vodyanitsa sings」判得出原神
    #   - 一个长而独特的行话（Elation 2.0×1.3=2.6）够 —— 这是 msg 12 唯一的信号
    #   - 一个短行话（减抗 2.0）不够 —— 这类词其实跨游戏通用，只是被归到了原神名下
    # 显式标记（「GI 7.1」这类）不受此阈值约束，命中即定案
    "GAME_DETECT_MIN_SCORE": (2.5, "float", "translate", False, False,
                              "游戏判定最低票数，低于此值算「不确定」"),
    "TOPIC_TAG_ENABLED": (True, "bool", "translate", False, False, "内容类型标签"),

    # ---- 混合检索 / RAG ----
    "RETRIEVAL_ENABLED": (True, "bool", "retrieval", False, False, "消息检索索引"),
    "EMBEDDING_ENABLED": (True, "bool", "retrieval", False, False, "启用内置本地 embedding 语义检索"),
    "EMBEDDING_BATCH_SIZE": (32, "int", "retrieval", False, False, "embedding 批量大小"),

    # ---- QQ 记忆 ----
    "MEMORY_ENABLED": (True, "bool", "qq", False, False, "QQ 对话记忆"),
    "MEMORY_TTL_DAYS": (30, "int", "qq", False, False, "个人短期记忆保留天数"),
    "MEMORY_RECENT_TURNS": (8, "int", "qq", False, False, "注入最近对话轮数"),
    "MEMORY_TOKEN_BUDGET": (2048, "int", "qq", False, False, "多轮上下文 token 预算"),
    "MEMORY_SUMMARY_TRIGGER": (12, "int", "qq", False, False, "触发滚动摘要的轮数"),
    "WIKI_SYNC_ENABLED": (True, "bool", "kb", False, False, "Wiki 知识库同步"),
    "WIKI_FETCH_INTERVAL_HOURS": (24, "int", "kb", False, False, "Wiki 同步间隔"),

    # ---- changelog 监控 ----
    "CHANGELOG_ENABLED": (True, "bool", "changelog", False, False, "内鬼站数据更新监控"),
    "CHANGELOG_FIRST_RUN_KEEP": (1, "int", "changelog", False, False,
                                 "首次抓取只保留最新 N 条，0=全量"),

    # ---- 预算 ----
    "AI_DAILY_CALL_LIMIT": (2000, "int", "budget", False, False, "每日调用上限，0=不限"),
    "AI_DAILY_COST_LIMIT": (0.0, "float", "budget", False, False, "每日花费上限（元），0=不限"),
    "AI_OVER_LIMIT_ACTION": ("store_only", "str", "budget", False, False,
                             "超限行为：store_only 只存原文 / stop 停止入库"),

    # ---- 去重 ----
    "DEDUP_ENABLED": (True, "bool", "dedup", False, False, "总开关"),
    "DEDUP_WINDOW_DAYS": (7, "int", "dedup", False, False, "近似匹配回看窗口"),
    "SIMHASH_DISTANCE": (3, "int", "dedup", False, False, "SimHash 汉明距离阈值"),
    "PHASH_DISTANCE": (5, "int", "dedup", False, False, "图片 pHash 汉明距离阈值"),
    "DEDUP_MIN_TEXT_LEN": (20, "int", "dedup", False, False, "短文本不参与文本判重"),

    # ---- 频道对账补拉（catch-up）----
    # 实时监听断线/重启窗口的消息只能靠这个补回来。每轮逐频道比对
    # TG 最新消息 ID 与库里的，落后就在 MAX_GAP 内自动补
    "CATCHUP_ENABLED": (True, "bool", "catchup", False, False, "自动对账补拉总开关"),
    "CATCHUP_INTERVAL": (300, "int", "catchup", False, False, "对账间隔（秒）"),
    "CATCHUP_MAX_GAP": (200, "int", "catchup", False, False,
                        "单频道单轮最多补拉条数（防过载保险，窗口外由时间下限挡住）"),
    # 对齐窗口：只保证「最近 N 天」的消息齐全，更早的缺口不回头补。
    # 语义从按条数改为按时间 —— 爆料频道一天可能几十条，条数上限
    # 挡不住时间上的洞
    "ALIGN_WINDOW_DAYS": (3, "int", "catchup", False, False,
                          "对齐窗口：只补最近 N 天内的缺口"),

    # ---- 媒体 ----
    "THUMB_WIDTH": (640, "int", "media", False, False, "视频缩略图宽"),
    "PHOTO_WIDTH": (1280, "int", "media", False, False, "图片下采样宽"),
    "WEBP_QUALITY": (80, "int", "media", False, False, "WebP 质量"),
    "MEDIA_TTL_DAYS": (7, "int", "media", False, False, "媒体保留天数"),
    "MEDIA_FREE_RESERVE_MB": (256, "int", "media", False, False, "媒体处理预留磁盘空间 MB"),
    "MEDIA_MAX_MB": (2048, "int", "media", False, False, "媒体总量上限（MB），LRU 淘汰"),
    "FFMPEG_FALLBACK": (True, "bool", "media", False, False, "无 thumb 时流式抽首帧"),
    "VIDEO_MAX_MB": (50, "int", "media", False, False, "归档视频源文件大小上限（MB）"),
    "BACKFILL_LIMIT": (20, "int", "media", False, False, "补历史时的默认拉取条数上限"),
    # 消息整体 TTL：超期的消息连同图片/视频/缩略图一起删。
    # 语义按 published_at（消息发布时间）算 —— 对齐窗口 3 天 < TTL 5 天，
    # 补进来的消息不会一入库就过期。副作用：dedup 回看窗口（7 天）
    # 会被截断，第 6 天的转载不再判重 —— 「全删」意图下可接受
    "MESSAGE_TTL_DAYS": (0, "int", "media", False, False,
                         "消息保留天数（超期连同媒体全删，0=不删）"),

    # ---- 视频归档 ----
    # 默认关。开了就会真的下载原片并全片转码 —— 盘只有 13G 可用、内存 3.9G，
    # 这是本项目最烧资源的一条路径，必须由人显式打开
    "VIDEO_ARCHIVE_ENABLED": (True, "bool", "video", False, False, "视频归档总开关"),
    "VIDEO_MAX_SECONDS": (0, "int", "video", False, False, "归档时长限制，0=不限"),
    "VIDEO_HEIGHT": (720, "int", "video", False, False, "转码目标高度"),
    "VIDEO_CRF": (28, "int", "video", False, False, "x264 CRF，越大越小越糊"),
    "VIDEO_TTL_DAYS": (7, "int", "video", False, False, "归档视频保留天数"),
    "VIDEO_MAX_TOTAL_MB": (3000, "int", "video", False, False, "归档视频总量上限（MB）"),

    # ---- 出站 ----
    "BASE_URL": ("", "str", "output", False, False, "对外地址，RSS 绝对链接用"),
    "RSS_DEFAULT_MAX_ITEMS": (50, "int", "output", False, False, ""),
    "API_RATE_PER_MIN": (120, "int", "output", False, False, "API 默认限速"),
    "WEBHOOK_ENABLED": (True, "bool", "output", False, False, ""),
    # RSS 里剧透段打黑底黑字 + 标题加 [剧透] 前缀。只关这个开关不影响入库，
    # spoiler_ranges 照常记录，API 消费方自己决定怎么呈现
    "SPOILER_MASK_OUTPUT": (True, "bool", "output", False, False, "出站内容给剧透段打码"),

    # ---- QQ 官方机器人 ----
    # 群推送出站。AppID/AppSecret 在 q.qq.com 创建机器人后获得
    "QQ_ENABLED": (False, "bool", "qq", False, False, "QQ 群推送总开关"),
    "QQ_REPLY_WINDOW_SECONDS": (300, "int", "qq", False, False, "当前账户被动回复有效期秒数"),
    "QQ_APP_ID": ("", "str", "qq", False, False, "q.qq.com 的机器人 AppID"),
    # 「机器人密钥」= Bot Secret。API 鉴权与 Webhook 回调验签**共用**它，
    # 不要再为验签单开配置项 —— 填成「机器人令牌」会让回调校验静默失败
    "QQ_APP_SECRET": ("", "str", "qq", True, False, "机器人密钥 AppSecret（API 鉴权 + 回调验签，密文存储）"),
    # 留空 = 全部频道；否则 channel id 列表（JSON）
    "QQ_CHANNEL_IDS": ([], "json", "qq", False, False, "只推这些频道，留空=全部"),
    "QQ_INCLUDE_DUPS": (False, "bool", "qq", False, False, "重复消息也推"),
    # Operator budget only. Account permissions and platform limits come from API feedback.
    "QQ_DAILY_LIMIT": (0, "int", "qq", False, False, "每群每日推送预算，0 不设本地上限"),
    # 每个机器人收到无权限错误后由发送链路单独熔断，不影响被动回复。
    # ---- botpy WS 事件桥 ----
    # botpy 容器转发事件到 admin /qqbot/bridge 的鉴权令牌。空 = 桥未启用。
    # 生成：管理页「重新生成桥令牌」按钮，或服务器上 secrets.token_hex(16)
    "QQ_BRIDGE_TOKEN": ("", "str", "qq", True, False,
                        "botpy WS 事件桥的鉴权令牌（桥容器 env 的 BRIDGE_TOKEN）"),
    # 桥心跳时间戳（ISO 格式，UTC）。管理页「桥在线」判定依据，自动维护
    "QQ_BRIDGE_LAST_SEEN": ("", "str", "qq", False, False,
                        "桥最后心跳时间（自动维护，勿手改）"),
    # ---- 通用主题与扩展 ----
    "THEME_DEFAULT": ("gaming", "str", "general", False, False, "默认主题包"),
    "PLUGIN_ENABLED": (True, "bool", "general", False, False, "允许本地插件宿主"),
    "PLUGIN_ROOT": ("", "str", "general", False, False, "本地插件目录"),
    "PLUGIN_WORKER_STATUS": ({}, "json", "general", False, False, "worker 插件宿主状态"),
    "MCP_ENABLED": (False, "bool", "general", False, False, "启用 MCP 外部工具"),
    "MCP_SERVERS": ([], "json", "general", True, False, "MCP stdio / HTTP 服务配置"),
    "MCP_BOT_TOOLS": ({}, "json", "general", False, False, "按机器人 AppID 授权外部工具"),
    # ---- QQ 群内 AI ----
    # 群/C2C 的 AI 对答总开关（细粒度的触发策略见 QQ_AI_FALLBACK）
    "QQ_AI_ENABLED": (True, "bool", "qq", False, False, "群内 AI 对话开关"),
    # AI 人设（系统提示词）。空 = 用 commands.py 里的默认人设
    "QQ_AI_SYSTEM": ("", "str", "qq", False, False,
                      "AI 人设系统提示词（空=默认）"),
    "QQ_AGENT_SYSTEM": ("", "str", "qq", False, False,
                        "Agent 路由与证据回答提示词（空=默认）"),
    # 开 = @ 机器人的非命令文本直接当 AI 对话；关 = 仅 /ai 命令触发
    "QQ_AI_FALLBACK": (True, "bool", "qq", False, False,
                        "非命令 @ 消息也当 AI 对话"),
    # AI agent 化：非命令消息先经意图路由（AI 判断要不要查库/执行命令），
    # 关闭则退回纯聊天模式
    "QQ_AI_AGENT_ENABLED": (True, "bool", "qq", False, False,
                             "AI 意图路由（agent 模式）开关"),
    # /latest 默认拉几条；明确请求最多 5 条，内容会合并为长图和链接
    "QQ_LATEST_DEFAULT_N": (3, "int", "qq", False, False,
                             "/latest 默认条数（1-5）"),
    # ---- QQ 图片中转（GitHub） ----
    # 背景：平台富媒体上传是腾讯机房来拉 URL，拉不动境外小站（本站 850027
    # 超时），但 GitHub raw 实测可达。图片经 GitHub 公开仓库中转。
    # 仓库格式 "owner/name"，须为公开仓库（raw URL 需匿名可读）
    "GITHUB_RELAY_REPO": ("", "str", "qq", False, False,
                          "图片中转 GitHub 仓库（owner/name，公开仓库）"),
    # fine-grained PAT，只需该仓库 Contents 读写权限
    "GITHUB_RELAY_TOKEN": ("", "str", "qq", True, False,
                           "图片中转 GitHub Token（Contents 读写）"),

    # ---- 磁盘水位 ----
    # 媒体目录（含归档视频）总量超过高水位 → 按 LRU 删视频，删到低水位止
    "DISK_WATERMARK_HIGH_GB": (10, "int", "media", False, False,
                                "媒体总量高水位（GB），超过触发清理"),
    "DISK_WATERMARK_LOW_GB": (3, "int", "media", False, False,
                               "媒体总量清理目标（GB），删到此值以下"),

    # ---- 内鬼站 changelog ----
    "CHANGELOG_ENABLED": (True, "bool", "changelog", False, False,
                          "监控 gachabase 数据版本变更"),
    # 首跑只发最新 N 条，其余标记为已见但不入消息列表。站点首页一次给出
    # 二十多条历史 revision，全发等于开箱即刷屏
    "CHANGELOG_FIRST_RUN_KEEP": (2, "int", "changelog", False, False,
                                 "新频道首次抓取只发最新几条"),

    # ---- 系统 ----
    "LOG_LEVEL": ("INFO", "str", "system", False, True, ""),
    "WORKER_RESTART_TOKEN": ("", "str", "system", False, False, "改这个值触发 worker 重启"),
    "WORKER_PAUSED": (False, "bool", "system", False, False,
                      "worker 暂停连接 TG（登录流程中短暂使用）"),
    "CHANGELOG_ENABLED": (True, "bool", "system", False, False,
                          "gachabase changelog 监控总开关"),
    "CHANGELOG_FIRST_RUN_KEEP": (3, "int", "system", False, False,
                                 "首跑只保留最新 N 条 revision，0=全部"),
}

_CACHE: dict[str, Any] = {}
_CACHE_AT = 0.0
_CACHE_TTL = 3.0  # 秒。两个进程各自缓存，改配置后最多 3s 生效
_LOCK = threading.Lock()


def _coerce(raw: Any, type_: str) -> Any:
    if raw is None:
        return None
    if type_ == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if type_ == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return 0
    if type_ == "float":
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return 0.0
    if type_ == "json":
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None
    return str(raw)


def _load_all() -> dict[str, Any]:
    out: dict[str, Any] = {}
    with session_scope() as s:
        rows = {r.key: r for r in s.query(AppSetting).all()}
    for key, (default, type_, _g, is_secret, _nr, _d) in DEFAULTS.items():
        row = rows.get(key)
        if row is not None and row.value not in (None, ""):
            val = decrypt(row.value) if is_secret else row.value
            out[key] = _coerce(val, type_)
            continue
        env = os.getenv(f"TGMON_{key}") or os.getenv(key)
        if env not in (None, ""):
            out[key] = _coerce(env, type_)
            continue
        out[key] = default
    # DB 里有但不在 DEFAULTS 的键也带上，方便临时加配置
    for key, row in rows.items():
        if key not in out:
            val = decrypt(row.value) if row.is_secret else row.value
            out[key] = _coerce(val, row.type)
    return out


def get(key: str, default: Any = None) -> Any:
    global _CACHE, _CACHE_AT
    with _LOCK:
        if time.time() - _CACHE_AT > _CACHE_TTL:
            try:
                _CACHE = _load_all()
                _CACHE_AT = time.time()
            except Exception as e:  # DB 还没建好时不要炸
                logger.warning("读取配置失败，回退默认值: %s", e)
                if not _CACHE:
                    _CACHE = {k: v[0] for k, v in DEFAULTS.items()}
        if key in _CACHE:
            return _CACHE[key]
    if key in DEFAULTS:
        return DEFAULTS[key][0]
    return default


def invalidate() -> None:
    global _CACHE_AT
    with _LOCK:
        _CACHE_AT = 0.0


def set_many(values: dict[str, Any]) -> None:
    """写配置。敏感字段自动加密；空字符串对敏感字段表示「不改」。"""
    with session_scope() as s:
        for key, val in values.items():
            meta = DEFAULTS.get(key)
            type_ = meta[1] if meta else "str"
            group = meta[2] if meta else "general"
            is_secret = meta[3] if meta else False
            needs_restart = meta[4] if meta else False

            if is_secret and (val is None or val == ""):
                continue  # 留空=保留原值，这是「只写不读」的表单语义

            if type_ == "json" and not isinstance(val, str):
                stored = json.dumps(val, ensure_ascii=False)
            elif type_ == "bool":
                stored = "true" if _coerce(val, "bool") else "false"
            else:
                stored = "" if val is None else str(val)

            if is_secret:
                stored = encrypt(stored)

            row = s.get(AppSetting, key)
            if row is None:
                row = AppSetting(key=key, type=type_, group=group,
                                 is_secret=is_secret, needs_restart=needs_restart)
                s.add(row)
            row.value = stored
    invalidate()


def seed_from_env() -> None:
    """首次启动把 .env 里出现的值写进 DB，之后以 DB 为准。"""
    with session_scope() as s:
        existing = {r.key for r in s.query(AppSetting.key).all()}
    to_write = {}
    for key, (_d, _t, _g, _s, _nr, _desc) in DEFAULTS.items():
        if key in existing:
            continue
        env = os.getenv(f"TGMON_{key}") or os.getenv(key)
        if env not in (None, ""):
            to_write[key] = env
    if to_write:
        logger.info("从 .env 种子导入 %d 项配置到 DB", len(to_write))
        set_many(to_write)
