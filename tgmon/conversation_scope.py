"""Request-local conversation identity shared by memory and tools."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationScope:
    platform: str = "qq"
    bot: str = ""
    group: str = ""
    member: str = ""
    event_id: str = ""


_current: ContextVar[ConversationScope | None] = ContextVar("tgmon_scope", default=None)
reply_deadline: ContextVar = ContextVar("tgmon_reply_deadline", default=None)


def current() -> ConversationScope | None:
    return _current.get()


def bot_identity(bot: dict | None) -> str:
    return str((bot or {}).get("app_id") or (bot or {}).get("id") or "")


@contextmanager
def activate(bot: dict | None, group: str, member: str, event_id: str = ""):
    token = _current.set(ConversationScope(bot=bot_identity(bot), group=group,
                                         member=member, event_id=event_id))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def profile_key(member: str) -> str:
    scope = current()
    if scope is None:
        return member
    value = f"{scope.platform}\0{scope.bot}\0{scope.group}\0{member}"
    return "v3:" + hashlib.sha256(value.encode()).hexdigest()
