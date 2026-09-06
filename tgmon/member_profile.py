"""QQ 成员档案：昵称与个人事实的直存管理（记忆 v2）。

旧链路的问题（2026-09 记忆重构的动机）：个人事实藏在 conversation.facts
的 sha256 键下，注入给模型的是一团 JSON，模型无法把「自称科比」关联到
「当前说话的这个人」；成员在 QQ 官方 API 里只有一串 openid 哈希，整条
链路从未建立可读身份。

本模块以 member_openid 为主键显式管理昵称与事实：
- 昵称：用户自称（agent 的 nickname 动作）、/nickname 命令、管理面板
- 事实：/remember、agent 的 remember 动作、摘要管线自动提取
读写都是显式操作，注入侧（memory.build_chat_context）负责转成模型
能直接使用的结构化文本。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from . import settings
from .db import session_scope
from .models import MemberProfile

logger = logging.getLogger(__name__)

_MAX_FACTS = 50


def display_name(member_openid: str) -> str:
    """成员的可读名：昵称优先，无则退回 成员#前6位。查库失败也不抛。"""
    if not member_openid:
        return "成员"
    try:
        with session_scope() as s:
            row = s.query(MemberProfile).filter_by(member_openid=member_openid).first()
            if row and (row.nickname or "").strip():
                return row.nickname.strip()
    except Exception:
        logger.debug("读取成员昵称失败", exc_info=True)
    return f"成员#{member_openid[:6]}"


def get_profile(member_openid: str) -> dict:
    """读档案。返回 {"nickname": str, "facts": [str, ...]}。"""
    if not member_openid:
        return {"nickname": "", "facts": []}
    with session_scope() as s:
        row = s.query(MemberProfile).filter_by(member_openid=member_openid).first()
        if row is None:
            return {"nickname": "", "facts": []}
        facts = [(v.get("text") if isinstance(v, dict) else v) or k
                 for k, v in (row.facts or {}).items()]
        return {"nickname": (row.nickname or "").strip(),
                "facts": [f for f in facts if f]}


def nickname_map(member_openids: list[str]) -> dict[str, str]:
    """批量查昵称：{member_openid: nickname}，只含有昵称的成员。"""
    ids = [x for x in {m for m in member_openids if m} if x]
    if not ids:
        return {}
    with session_scope() as s:
        rows = (s.query(MemberProfile)
                .filter(MemberProfile.member_openid.in_(ids)).all())
        return {r.member_openid: r.nickname.strip()
                for r in rows if (r.nickname or "").strip()}


def set_nickname(member_openid: str, nickname: str) -> bool:
    name = (nickname or "").strip()[:60]
    if not member_openid or not name:
        return False
    with session_scope() as s:
        row = _get_or_create(s, member_openid)
        row.nickname = name
    return True


def add_fact(member_openid: str, fact: str, *, source: str = "explicit") -> bool:
    """存一条个人事实。同文幂等（只刷新时间），超上限丢最旧。"""
    text = (fact or "").strip()[:500]
    if not text or not member_openid or not settings.get("MEMORY_ENABLED"):
        return False
    now = datetime.utcnow().isoformat()
    with session_scope() as s:
        row = _get_or_create(s, member_openid)
        facts = dict(row.facts or {})
        for k, v in facts.items():
            old = (v.get("text") if isinstance(v, dict) else v) or ""
            if old == text:
                facts[k] = {"text": text, "source": source, "updated_at": now}
                row.facts = facts
                return True
        key = hashlib.sha256(text.encode()).hexdigest()[:16]
        facts[key] = {"text": text, "source": source, "updated_at": now}
        if len(facts) > _MAX_FACTS:
            ordered = sorted(facts.items(),
                             key=lambda kv: (kv[1] or {}).get("updated_at", ""))
            for k, _ in ordered[:len(facts) - _MAX_FACTS]:
                facts.pop(k)
        row.facts = facts
    return True


def forget_fact(member_openid: str, query: str | None = None) -> int:
    """删事实；query 为空删全部。返回删除条数。"""
    if not member_openid:
        return 0
    removed = 0
    with session_scope() as s:
        row = s.query(MemberProfile).filter_by(member_openid=member_openid).first()
        if row is None:
            return 0
        facts = dict(row.facts or {})
        for k, v in list(facts.items()):
            value = str((v.get("text") if isinstance(v, dict) else v) or k)
            if not query or query.casefold() in value.casefold():
                facts.pop(k)
                removed += 1
        row.facts = facts
    return removed


def _get_or_create(s, member_openid: str) -> MemberProfile:
    row = s.query(MemberProfile).filter_by(member_openid=member_openid).first()
    if row is None:
        row = MemberProfile(member_openid=member_openid, facts={})
        s.add(row)
        s.flush()
    return row
