"""Compare QQ direct upload with URL fetching; never send a message."""
import asyncio
import base64
import io
import json
import time

import httpx
from PIL import Image
from sqlalchemy import func

from tgmon.db import session_scope
from tgmon.models import MessageMedia, QqGroup
from tgmon.paths import MEDIA_DIR
from tgmon.qqbot import client, media


async def main():
    with session_scope() as s:
        group = s.query(QqGroup).filter(QqGroup.enabled.is_(True)).first()
        assert group
        openid = group.group_openid
        rows = (s.query(MessageMedia.message_id, func.count()).filter(
            MessageMedia.kind == "photo", MessageMedia.thumb_path.isnot(None))
            .group_by(MessageMedia.message_id).having(func.count() > 1)
            .order_by(func.count().desc()).all())
        for mid, _ in rows:
            photos = [row[0] for row in s.query(MessageMedia.thumb_path).filter_by(
                message_id=mid, kind="photo").order_by(MessageMedia.id).all() if row[0]]
            if all((MEDIA_DIR / p).is_file() for p in photos):
                break
    composite = media.compose_album(photos)
    jpeg = (MEDIA_DIR / composite).read_bytes()
    with Image.open(io.BytesIO(jpeg)) as image:
        dimensions = image.size
    print(json.dumps({"album": composite, "images": len(photos), "bytes": len(jpeg),
                      "dimensions": dimensions}), flush=True)
    tiny = io.BytesIO()
    Image.new("RGB", (64, 64), (30, 70, 120)).save(tiny, "JPEG")
    token = await client._get_token()
    async with httpx.AsyncClient(timeout=45) as connection:
        for label, data in (("tiny", tiny.getvalue()), ("complete_album", jpeg)):
            started = time.perf_counter()
            response = await connection.post(
                f"{client.API_BASE}/v2/groups/{openid}/files",
                headers={"Authorization": f"QQBot {token}"},
                json={"file_type": 1, "file_data": base64.b64encode(data).decode("ascii"),
                      "srv_send_msg": False})
            body = response.json()
            print(json.dumps({"test": label, "status": response.status_code,
                              "code": body.get("code"), "message": body.get("message"),
                              "accepted": bool(body.get("file_info")),
                              "seconds": round(time.perf_counter() - started, 3)}), flush=True)
            if not body.get("file_info"):
                break


asyncio.run(main())
