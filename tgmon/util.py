"""小工具。时区、格式化、日志。"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# 展示用时区。DB 一律存 UTC naive
LOCAL_TZ = timezone(timedelta(hours=8))


def utcnow() -> datetime:
    return datetime.utcnow()


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)


def local_day(dt: datetime | None = None) -> str:
    """本地时区的 YYYY-MM-DD，用于按天统计。"""
    d = to_local(dt or utcnow())
    return d.strftime("%Y-%m-%d")


def fmt_local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    d = to_local(dt)
    return d.strftime(fmt) if d else "—"


def ago(dt: datetime | None) -> str:
    if dt is None:
        return "从未"
    delta = utcnow() - dt
    sec = int(delta.total_seconds())
    if sec < 0:
        return "刚刚"
    if sec < 60:
        return f"{sec} 秒前"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    if sec < 86400:
        return f"{sec // 3600} 小时前"
    return f"{sec // 86400} 天前"


def human_bytes(n: int | float | None) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def has_cjk(s: str) -> bool:
    """含汉字。决定术语该用词边界还是子串匹配 —— CJK 没有词边界。"""
    return any("一" <= c <= "鿿" or "㐀" <= c <= "䶿" for c in s)


def alias_match_mode(surface: str) -> str:
    """按字面选匹配方式，而不是按它来自哪个语言的字段。

    踩过：崩铁 nickname.json 里混着纯 ASCII 的外号（xcw = 声优小仓唯的拼音首字母），
    当成中文用子串匹配会在别的词里误命中。
    """
    return "substring" if has_cjk(surface) else "word"


def unreliable_alias_reason(surface: str) -> str | None:
    """这个写法能不能可靠匹配？不能就返回原因，能就返回 None。

    单字汉字别名（魈、琴、刃、本、照）两种匹配方式都不对：

    - substring：命中「钢琴」「版本」「照旧」里的那个字。实测 ZZZ 的「本」
      （Ben Bigger 被截断的产物）让「7.4版本的五星角色」判成了绝区零
    - word（\\b 边界）：中文没有空格，「魈的技能」里 魈 后面跟着 的（也是词字符），
      边界不成立，等于永远匹配不到

    所以这类别名一律停用。实体本身不受影响 —— 魈 还有 Xiao、本 还有 Ben，
    英文写法走词边界是可靠的。
    """
    s = (surface or "").strip()
    if not s:
        return "空"
    if len(s) == 1 and has_cjk(s):
        return "单字汉字，子串匹配会在其他词里误命中，词边界在中文里又不成立"
    return None


def opt_int(raw) -> int | None:
    """HTTP 查询参数 → 可选整数。

    为什么不用 `page: int | None = None`：FastAPI 对 `?page=`（空串）会直接
    422 —— 表单里「全部」选项提交的就是空串，线上五种页面全中过。
    统一 str 接收再走这里：空串/None/坏值 → None（= 默认值）。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def truncate(text: str | None, n: int = 120) -> str:
    if not text:
        return ""
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def utf16_to_char_offset(text: str, offset: int) -> int:
    """Telegram entity 的 UTF-16 code unit 偏移 → Python 字符（码点）索引。

    TG 的 message entity offset/length 全部按 UTF-16 code unit 计：emoji 是
    代理对占 2 个 unit，Python 字符串按码点索引。含 emoji 的消息不换算的话，
    剧透区间会整体向右漂移 —— 漂多少取决于 emoji 在区间前出现了几个。
    BMP 内字符（汉字、假名）两者一致，纯 CJK 文本不换算也对，但没人保证
    爆料帖里没 emoji。
    """
    if offset <= 0:
        return 0
    units = 0
    for i, ch in enumerate(text):
        if units >= offset:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def spoiler_segments(text: str | None,
                     ranges: list | None) -> list[tuple[str, bool]]:
    """按剧透区间把文本切成 [(片段, 是否剧透), ...]。

    ranges 是 [[start, length], ...]（Python 字符区间，相对 text_raw）。
    区间越界 / 交叠时保守处理：排序后顺序切，越界部分截断到文本长度。
    空文本或无区间返回空列表，调用方据此回退到整体显示。
    """
    if not text or not ranges:
        return []
    segs: list[tuple[str, bool]] = []
    pos = 0
    for rng in sorted(ranges, key=lambda r: r[0] if isinstance(r, (list, tuple)) and r else 0):
        if not isinstance(rng, (list, tuple)) or len(rng) < 2:
            continue
        try:
            start = max(0, min(int(rng[0]), len(text)))
            end = max(start, min(start + max(0, int(rng[1])), len(text)))
        except (TypeError, ValueError):
            continue
        # 交叠 / 被前一个吞掉的区间：从已消费位置截取可见部分
        if end <= pos:
            continue
        start = max(start, pos)
        if start > pos:
            segs.append((text[pos:start], False))
        segs.append((text[start:end], True))
        pos = end
    if pos < len(text):
        segs.append((text[pos:], False))
    return segs or [(text, False)]


def setup_logging(name: str) -> None:
    level = (os.getenv("TGMON_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format=f"%(asctime)s [{name}] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # 这些库在 INFO 下太吵
    for noisy in ("telethon", "httpx", "httpcore", "openai", "anthropic",
                  "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(level: str, source: str, message: str) -> None:
    """写 system_event 表，供概览页「最近错误」显示。"""
    from .db import session_scope
    from .models import SystemEvent
    try:
        with session_scope() as s:
            s.add(SystemEvent(level=level, source=source, message=message[:4000]))
    except Exception:
        pass
