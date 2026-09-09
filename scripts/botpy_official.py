# -*- coding: utf-8 -*-
"""botpy 官方 SDK 事件桥 —— WebSocket 网关模式，纯转发。

职责单一：连 QQ 平台 WS 网关（wss://api.sgroup.qq.com）收事件，
构造与平台 webhook 回调同构的 {"op":0,"id":...,"t":...,"d":{...},
"bot_appid":...} 转发给 tgmon admin 的 POST /qqbot/bridge
（docker 内网 + 令牌鉴权）。

不做业务、不直接回复 —— 命令/AI/进退群处理和被动回复统一在
admin 侧（tgmon/qqbot/commands.py + qqbot_cb.py）发出。

每 60s 发一次 {"op":14} 心跳，admin 侧据此维护该机器人的
「桥在线」状态（qq_bot.bridge_last_seen）。

botpy 仅支持 WebSocket 接入（源码无 webhook/op13 实现）。
本服务不监听任何端口，无需 Caddy 反代，纯出站连接。

多机器人：每个机器人各起一个本容器（同一份脚本），APPID / SECRET
各填各的，BRIDGE_TOKEN 共用。事件带 bot_appid，admin 按它路由到
对应机器人的凭据。单机器人部署同样适用。

运行（服务器，Docker；第 N 个机器人改容器名与 env）：
  docker run -d --name botpy-official \
    --restart unless-stopped \
    --network tgmon_default \
    --env-file /opt/tgmon/botpy.env \
    -v /opt/tgmon/scripts/botpy_official.py:/app/botpy_official.py:ro \
    python:3.11-slim \
    sh -c "pip install -q qq-botpy && cd /app && exec python -u botpy_official.py"

环境变量（botpy.env，每个机器人一份）：
  APPID        机器人 appid（管理页「机器人」列表里的 AppID）
  SECRET       AppSecret（管理端「机器人密钥」）
  ADMIN_URL    admin 服务地址（如 http://tgmon-admin:8000）
  BRIDGE_TOKEN 桥令牌（管理页「重新生成」给出的值，所有机器人共用）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import traceback

import aiohttp
import botpy
from botpy import logging
from botpy.connection import ConnectionState as _ConnState
from botpy.message import C2CMessage, GroupMessage

_log = logging.get_logger()

APPID = os.environ.get("APPID", "")
SECRET = os.environ.get("SECRET", "")
ADMIN_URL = os.environ.get("ADMIN_URL", "http://tgmon-admin:8000")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")

HEARTBEAT_INTERVAL = 60  # 秒

_session: aiohttp.ClientSession | None = None
_heartbeat_started = False


def _evt(t: str, d: dict, eid: str = "") -> dict:
    """构造与平台 webhook 回调同构的事件，并带上自己的 AppID。"""
    return {"op": 0, "id": eid, "t": t, "d": d, "bot_appid": APPID}


async def _forward(event: dict) -> None:
    """转发事件到 admin /qqbot/bridge。网络类失败重试 1 次，4xx 不重试。"""
    global _session
    if not BRIDGE_TOKEN:
        _log.error("[BRIDGE] 未配置 BRIDGE_TOKEN，事件被丢弃")
        return
    if _session is None or _session.closed:
        # 35s：对齐 admin 侧处理守卫 25s + 下载/重试余量。此前 20s 在
        # 并发高峰（长图渲染 + 多人 @）时先于 admin 超时，桥侧重试反而
        # 放大压力（2026-09 超时复盘）。
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=35))
    headers = {"Content-Type": "application/json",
               "X-Bridge-Token": BRIDGE_TOKEN}
    for attempt in (1, 2):
        try:
            async with _session.post(f"{ADMIN_URL}/qqbot/bridge",
                                     json=event, headers=headers) as r:
                if r.status == 200:
                    return
                body = await r.text()
                # 4xx（令牌错/坏 JSON/未知机器人）重试无意义，记日志即止
                _log.error("[BRIDGE] admin 返回 %s: %s",
                           r.status, body[:150])
                return
        except Exception as e:
            _log.warning("[BRIDGE] 转发异常(第 %s 次): %s: %s",
                         attempt, type(e).__name__, e)
            if attempt == 1:
                await asyncio.sleep(2)
    _log.error("[BRIDGE] 事件转发最终失败: %s",
               json.dumps(event, ensure_ascii=False)[:200])


# ---------------- 新版平台群消息事件（group_message_create） ----------------
# 2026-09 实测：q.qq.com 新建的机器人收到的群消息推送是全量、小写事件名
# group_message_create（而非老公域机器人的 GROUP_AT_MESSAGE_CREATE），
# qq-botpy 的 parser 不认识会直接丢弃。这里把解析器注册进去：拿原始
# 报文判断是否 @ 了本机器人，是则按老 GROUP_AT 事件格式转发（admin 侧
# 零改动），不是则只留一行日志——不转发普通群消息（否则机器人会对
# 每条闲聊都回话）。

ROBOT_ID = ""                      # on_ready 时补上（mention 标签可能用平台用户 id）
_MENTION_TAG = re.compile(r"<@!?([\w]+)>")
_raw_log_left = 10                 # 启动后前 10 条全量消息留完整报文，方便排查


def _is_at_bot(d: dict) -> tuple[bool, str]:
    """判断全量群消息是否 @ 了本机器人。返回 (是否命中, 去掉 @ 标记的正文)。

    实测报文（2026-09）：@ 某人时 content 形如 "<@OPENID> 文本"，且
    d.mentions[] 里对应条目带 is_you 字段——@ 到机器人时为 true，
    这是最可靠的判定；content 标签 / mentions 含 appid 或平台用户 id
    作为兼容回退。
    """
    content = str(d.get("content") or "")
    ids = {APPID, ROBOT_ID} - {""}
    hit = False
    mentions = d.get("mentions")
    if isinstance(mentions, list):
        for m in mentions:
            if not isinstance(m, dict):
                continue
            if m.get("is_you"):
                hit = True
                mid = str(m.get("id") or "")
                if mid:
                    ids.add(mid)     # 把机器人自己的 mention id 纳入剥离集合
        if not hit:
            try:
                m_str = json.dumps(mentions, ensure_ascii=False)
                hit = any(i in m_str for i in ids)
            except (TypeError, ValueError):
                pass

    def _strip(m):
        nonlocal hit
        if m.group(1) in ids:
            hit = True
            return ""
        return m.group(0)

    text = _MENTION_TAG.sub(_strip, content).strip()
    return hit, text


def _parse_group_message_create(self, payload):
    """注入 ConnectionState 的解析器（sync —— gateway 不 await parser）。

    实测报文（2026-09）：t=GROUP_MESSAGE_CREATE（大写，botpy 转小写后路由到
    这里）；d 里 group_id 与 group_openid 并存，author 带 member_openid。
    """
    global _raw_log_left
    d = payload.get("d") or {}
    try:
        hit, text = _is_at_bot(d)
        if _raw_log_left > 0 or hit:
            _log.info("[BRIDGE] 新版群消息 raw=%s",
                      json.dumps(payload, ensure_ascii=False)[:1200])
            _raw_log_left = max(0, _raw_log_left - 1)
        if not hit:
            return
        gid = str(d.get("group_openid") or d.get("group_id") or "")
        mid = str(d.get("id") or payload.get("id") or "")
        asyncio.get_running_loop().create_task(_forward(_evt(
            "GROUP_AT_MESSAGE_CREATE",
            {"id": mid,
             "group_openid": gid,
             "content": text,
             "author": {"member_openid":
                        str((d.get("author") or {}).get("member_openid") or "")},
             "timestamp": d.get("timestamp")},
            mid)))
        _log.info("[BRIDGE] 新版群消息命中 @：group=%s content=%r",
                  gid[:10], text[:60])
    except Exception:
        _log.error("[BRIDGE] 新版群消息处理异常: %s",
                   traceback.format_exc()[-500:])


_ConnState.parse_group_message_create = _parse_group_message_create


class BridgeClient(botpy.Client):
    """事件桥客户端：handler 只做留痕 + 转发。"""

    async def on_ready(self):
        global ROBOT_ID
        ROBOT_ID = str(getattr(self.robot, "id", "") or "")
        _log.info("[BRIDGE] on_ready robot=%s id=%s —— 事件开始转发到 %s",
                  getattr(self.robot, "name", "?"), ROBOT_ID or "?", ADMIN_URL)
        global _heartbeat_started
        if not _heartbeat_started:
            _heartbeat_started = True
            asyncio.get_running_loop().create_task(_heartbeat_loop())

    async def on_group_at_message_create(self, message: GroupMessage):
        _log.info("[BRIDGE] 群@消息: group=%s content=%r",
                  message.group_openid, (message.content or "")[:60])
        await _forward(_evt("GROUP_AT_MESSAGE_CREATE", {
            "id": message.id,
            "group_openid": message.group_openid,
            "content": message.content,
            "author": {"member_openid":
                       getattr(message.author, "member_openid", "")},
            "timestamp": message.timestamp,
        }, getattr(message, "event_id", "") or message.id))

    async def on_c2c_message_create(self, message: C2CMessage):
        _log.info("[BRIDGE] C2C消息: content=%r",
                  (message.content or "")[:60])
        await _forward(_evt("C2C_MESSAGE_CREATE", {
            "id": message.id,
            "content": message.content,
            "author": {"user_openid":
                       getattr(message.author, "user_openid", "")},
            "timestamp": message.timestamp,
        }, getattr(message, "event_id", "") or message.id))

    async def on_group_add_robot(self, event):
        _log.info("[BRIDGE] 机器人进群: group=%s", event.group_openid)
        await _forward(_evt("GROUP_ADD_ROBOT", {
            "group_openid": event.group_openid,
            "op_member_openid": getattr(event, "op_member_openid", ""),
            "timestamp": getattr(event, "timestamp", None),
        }, getattr(event, "event_id", "") or ""))

    async def on_group_del_robot(self, event):
        _log.info("[BRIDGE] 机器人被移出群: group=%s", event.group_openid)
        await _forward(_evt("GROUP_DEL_ROBOT", {
            "group_openid": event.group_openid,
            "op_member_openid": getattr(event, "op_member_openid", ""),
            "timestamp": getattr(event, "timestamp", None),
        }, getattr(event, "event_id", "") or ""))

    async def on_error(self, event_method, *args, **kwargs):
        _log.error("[BRIDGE] on_error event=%s", event_method)
        _log.error("%s", traceback.format_exc()[-600:])


async def _heartbeat_loop():
    """每 60s 向 admin 报一次平安（op=14，带 AppID 按机器人记心跳）。"""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            await _forward({"op": 14, "t": "BRIDGE_PING", "d": {},
                            "bot_appid": APPID})
        except Exception as e:
            _log.warning("[BRIDGE] 心跳异常: %s", e)


def main() -> int:
    if not APPID or not SECRET:
        print("缺少 APPID / SECRET 环境变量", file=sys.stderr)
        return 2
    if not BRIDGE_TOKEN:
        print("缺少 BRIDGE_TOKEN 环境变量（管理页生成后写入 botpy.env）",
              file=sys.stderr)
        return 2
    _log.info("[BRIDGE] 启动 botpy 事件桥 admin=%s appid=%s intents=public_messages",
              ADMIN_URL, APPID)
    intents = botpy.Intents(public_messages=True)
    client = BridgeClient(
        intents=intents,
        log_format="%(asctime)s [%(levelname)s] %(message)s")
    # run() 阻塞：token 获取 -> WS 建连 -> identify -> 事件循环
    client.run(appid=APPID, secret=SECRET)
    return 0


if __name__ == "__main__":
    sys.exit(main())
