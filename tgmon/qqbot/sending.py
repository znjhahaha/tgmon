"""One durable sender for requested story cards and proactive batches."""
from __future__ import annotations

import asyncio
from datetime import datetime

import httpx

from .. import settings
from ..db import session_scope
from ..models import QqGroup
from . import client, events, media


def _quota(target: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    with session_scope() as s:
        row = s.query(QqGroup).filter_by(group_openid=target, enabled=True).first()
        if row is None:
            return False
        if row.sent_date != today:
            row.sent_date, row.sent_today = today, 0
        limit = int(settings.get("QQ_DAILY_LIMIT") or 950)
        return s.query(QqGroup).filter(QqGroup.id == row.id, QqGroup.sent_today < limit).update(
            {QqGroup.sent_today: QqGroup.sent_today + 1}, synchronize_session=False) == 1


async def _upload(target: str, reply, private: bool):
    if private:
        jpeg = await asyncio.to_thread(media._jpeg_bytes, reply.thumb_path)
        if not jpeg:
            return None
        return await client.upload_c2c_file_data(target, jpeg)
    if reply.thumb_paths:
        return await media.upload_album(target, reply.thumb_paths, raise_fatal=True)
    return await media.upload_image(target, reply.thumb_path, raise_fatal=True)


async def send_parts(target: str, replies, *, key: str, private=False,
                     msg_id: str | None = None) -> dict:
    replies = replies[:5]
    if not events.begin_delivery(key, len(replies)):
        return events.delivery_parts(key)
    send_text = client.send_c2c_text if private else client.send_group_text
    send_media = client.send_c2c_media if private else client.send_group_media

    def fail_unattempted(start: int, reason: str) -> None:
        current = events.delivery_parts(key)
        for pending in range(start + 1, len(replies) + 1):
            if current.get(str(pending), {}).get("status") == "pending":
                events.finish_part(key, pending, "failed", reason)
                current[str(pending)] = {"status": "failed"}

    try:
        prior = events.delivery_parts(key)
        upload_indices = [i for i, r in enumerate(replies, 1) if r.kind == "image"
                          and prior.get(str(i), {}).get("status") not in ("done", "sending", "unknown")]
        uploaded = dict(zip(upload_indices, await asyncio.gather(
            *(_upload(target, replies[i - 1], private) for i in upload_indices), return_exceptions=True)))
        for index, reply in enumerate(replies, 1):
            if not events.begin_part(key, index):
                continue
            if not msg_id and not _quota(target):
                events.finish_part(key, index, "failed", "每日配额不足或群已停用")
                continue
            args = {"msg_id": msg_id, "msg_seq": index} if msg_id else {}
            sent = False
            sending = False
            try:
                if reply.kind == "image":
                    fi = uploaded.get(index)
                    if isinstance(fi, Exception):
                        # Uploads do not deliver messages, so a transport failure here is retryable.
                        if isinstance(fi, client.QqApiError):
                            raise fi
                        fi = None
                    if not fi:
                        if not reply.text:
                            events.finish_part(key, index, "failed", "图片上传失败")
                            continue
                        sending = True
                        await send_text(target, reply.text, **args)
                    else:
                        try:
                            sending = True
                            await send_media(target, fi, **({"content": reply.text} if reply.text else {}), **args)
                        except client.QqApiError as exc:
                            if exc.code != client.ERR_URL_FORBIDDEN:
                                raise
                            await send_media(target, fi, **args)
                            events.finish_part(key, index, "done", "平台拒绝文字链接，长图包含短链和二维码")
                            sent = True
                elif reply.kind == "video" and not private:
                    fi = await media.upload_video(target, reply.mid)
                    if not fi:
                        events.finish_part(key, index, "failed", "视频上传失败")
                        continue
                    sending = True
                    await send_media(target, fi, **args)
                elif reply.text:
                    sending = True
                    await send_text(target, reply.text, **args)
                if not sent:
                    events.finish_part(key, index, "done")
            except client.QqApiError as exc:
                events.finish_part(key, index, "failed", f"code={exc.code} {exc.message}")
                if exc.code in client.ERR_NOT_IN_GROUP:
                    with session_scope() as s:
                        s.query(QqGroup).filter_by(group_openid=target).update({"enabled": False, "dropped_at": datetime.utcnow()})
                    fail_unattempted(index, "群已移除，后续分片未发送")
                    break
            except (httpx.TransportError, asyncio.TimeoutError) as exc:
                events.finish_part(key, index, "unknown" if sending else "failed",
                                   f"{'发送结果待核实' if sending else '上传失败'}: {type(exc).__name__}")
                fail_unattempted(index, "前序发送中断，后续分片未发送")
                break
            except Exception as exc:
                events.finish_part(key, index, "failed", f"{type(exc).__name__}: {exc}")
    finally:
        parts = events.end_delivery(key)
    if parts and all(p.get("status") == "done" for p in parts.values()) and msg_id:
        from .commands import mark_read
        mark_read(target if not private else "", list(dict.fromkeys(mid for r in replies for mid in r.story_ids)))
    return parts
