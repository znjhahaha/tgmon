"""一图流分享卡片渲染（PIL）。

背景（2026-09 群反馈）：分享链接贴到 QQ 群里越积越多，分不清哪条是
哪条。改为生成一张概括卡片 —— 游戏标签 + 频道 + 日期 + 概括（分享时
AI 自动生成，覆盖全部内容、不限 30 字）+ 缩略图网格 + 分享短链二维码
—— 管理员在管理页下载后手动转发进群。图片是像素，不受「主动消息
禁止 URL」的平台限制，二维码/链接直接印在图里。合并分享（多选）也
出卡片：概括覆盖全部消息、缩略图跨消息取前 4 张、头部带合集角标。

设计口径：1080 宽、深色底（与管理页一致）、高度按内容自适应。
字体：仓库内置 Noto Sans SC（OFL 协议，tgmon/assets/fonts/）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

W = 1080
PAD = 48
BG = (17, 19, 25)
FG = (232, 234, 238)
DIM = (148, 154, 165)
ACCENT = (86, 156, 255)
LINE = (48, 52, 64)
SUMMARY_COLOR = (250, 210, 130)

_FONTS = Path(__file__).parent / "assets" / "fonts"
_FONT_R = _FONTS / "NotoSansSC-Regular.otf"
_FONT_B = _FONTS / "NotoSansSC-Bold.otf"

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _font_cache:
        path = _FONT_B if bold else _FONT_R
        if not path.exists():
            raise RuntimeError(
                f"缺字体文件 {path.name}（一图流卡片需要内置 Noto Sans SC）")
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int,
          max_lines: int = 3) -> list[str]:
    """CJK 逐字贪心断行（中文没有空格可依）。超过 max_lines 截断加省略号。"""
    lines: list[str] = []
    cur = ""
    for ch in text.replace("\n", " "):
        if font.getlength(cur + ch) > max_w and cur:
            lines.append(cur)
            cur = ch
            if len(lines) >= max_lines:
                break
        else:
            cur += ch
    if len(lines) < max_lines and cur:
        lines.append(cur)
    if len(lines) == max_lines:
        # 判断是否被截断：原文还有剩余且最后一行顶满 → 加省略号
        consumed = sum(len(x) for x in lines)
        if consumed < len(text.replace("\n", " ")):
            last = lines[-1]
            while last and font.getlength(last + "…") > max_w:
                last = last[:-1]
            lines[-1] = last + "…"
    return lines or [""]


def _cover(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """等比缩放填充格子后居中裁切（cover 模式）。"""
    ratio = max(box_w / img.width, box_h / img.height)
    nw, nh = max(1, round(img.width * ratio)), max(1, round(img.height * ratio))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - box_w) // 2, (nh - box_h) // 2
    return img.crop((left, top, left + box_w, top + box_h))


def _contain(img: Image.Image, box_w: int, box_h: int,
             background: tuple[int, int, int] = BG) -> Image.Image:
    """等比缩放完整放入格子，不裁掉截图内容。"""
    if img.width <= 0 or img.height <= 0:
        return Image.new("RGB", (box_w, box_h), background)
    ratio = min(box_w / img.width, box_h / img.height)
    nw, nh = max(1, round(img.width * ratio)), max(1, round(img.height * ratio))
    scaled = img.resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGB", (box_w, box_h), background)
    out.paste(scaled, ((box_w - nw) // 2, (box_h - nh) // 2))
    return out


def _load_thumb(thumb_path: str | None, media_dir: Path) -> Image.Image | None:
    """缩略图（webp）读取，失败返回 None（跳过该格）。"""
    if not thumb_path:
        return None
    p = media_dir / thumb_path
    try:
        if not p.is_file():
            return None
        with Image.open(p) as im:
            return im.convert("RGB")
    except Exception as e:
        logger.warning("卡片读图失败 %s: %s", p, e)
        return None


def _qr_image(url: str, size: int) -> Image.Image | None:
    """短链二维码。qrcode 未安装时返回 None（卡片降级只印链接文字）。"""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1, box_size=8,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color=(17, 19, 25),
                            back_color=(238, 240, 244)).convert("RGB")
        return img.resize((size, size), Image.NEAREST)
    except Exception as e:
        logger.warning("二维码生成失败（降级为纯链接）: %s", e)
        return None


def _wrap_full(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """按实际像素宽度完整断行，不截断长摘要或正文。"""
    if not text:
        return []
    out: list[str] = []
    for paragraph in str(text).splitlines() or [""]:
        cur = ""
        for ch in paragraph or " ":
            if cur and font.getlength(cur + ch) > max_w:
                out.append(cur)
                cur = ch
            else:
                cur += ch
        out.append(cur.rstrip() or " ")
    return out


def _jpeg_bytes(img: Image.Image) -> bytes:
    import io
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def render_long_cards(items: list[dict], summary: str = "", url: str = "",
                      media_dir: Path | None = None,
                      max_height: int = 5000) -> list[bytes]:
    """Render bounded pages without splitting text lines or shrinking whole cards."""
    from .paths import MEDIA_DIR
    media_dir = media_dir or MEDIA_DIR
    height = max(1200, min(8000, int(max_height or 5000)))
    inner = W - 2 * PAD
    body, heading, meta = _font(31), _font(38, bold=True), _font(25)
    footer_lines = _wrap_full(url, meta, inner - 160)
    footer_h = max(180, len(footer_lines) * 34 + 36)
    bottom = height - footer_h - PAD
    pages = []
    canvas = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(canvas)
    y = PAD

    def finish():
        nonlocal canvas, draw, y
        end = min(height, max(400, y + footer_h + PAD))
        fy = end - footer_h
        draw.line([(PAD, fy - 16), (W - PAD, fy - 16)], fill=LINE, width=2)
        for index, line in enumerate(footer_lines):
            draw.text((PAD, fy + index * 34), line, font=meta, fill=ACCENT)
        draw.text((PAD, end - 42), f"tgmon  /  {len(pages) + 1}", font=meta, fill=DIM)
        qr = _qr_image(url, 132) if url else None
        if qr:
            canvas.paste(qr, (W - PAD - 132, fy))
        pages.append(_jpeg_bytes(canvas.crop((0, 0, W, end))))
        canvas = Image.new("RGB", (W, height), BG)
        draw = ImageDraw.Draw(canvas)
        y = PAD

    def space(amount):
        nonlocal y
        if y + amount > bottom and y > PAD:
            finish()

    def paragraph(value, font, color, leading):
        nonlocal y
        for line in _wrap_full(value, font, inner):
            space(leading)
            draw.text((PAD, y), line, font=font, fill=color)
            y += leading

    if summary:
        paragraph(summary, heading, SUMMARY_COLOR, 52)
        y += 24
    for item in items or [{}]:
        space(200)
        paragraph(item.get("game") or "待识别", heading, ACCENT, 52)
        paragraph(item.get("channel") or "", meta, FG, 36)
        paragraph("  ".join(str(x) for x in (
            item.get("date_str"), item.get("version")) if x), meta, DIM, 36)
        y += 18
        paragraph(item.get("text") or "", body, FG, 44)
        raw = item.get("raw") or ""
        if raw and raw != item.get("text"):
            y += 18
            paragraph("原文", meta, DIM, 36)
            paragraph(raw, meta, DIM, 36)
        for path in item.get("thumb_paths") or []:
            image = _load_thumb(path, media_dir)
            if image is None:
                continue
            scaled_h = max(1, round(image.height * inner / image.width))
            # Keep full width and tile very tall source images across pages.
            scaled = image.resize((inner, scaled_h), Image.Resampling.LANCZOS)
            offset = 0
            y += 18
            while offset < scaled_h:
                space(min(300, scaled_h - offset))
                available = max(1, bottom - y)
                take = min(available, scaled_h - offset)
                canvas.paste(scaled.crop((0, offset, inner, offset + take)), (PAD, y))
                y += take
                offset += take
                if offset < scaled_h:
                    finish()
            y += 18
        y += 34
    finish()
    return pages


def render_card(*, game: str = "", channel: str = "", date_str: str = "",
                version: str = "", summary: str = "", url: str = "",
                badge: str = "", thumb_paths: list[str] | None = None,
                media_dir: Path | None = None, text: str = "",
                raw: str = "") -> bytes:
    """兼容旧调用方的单卡片接口；长内容由 render_long_cards 负责分页。"""
    pages = render_long_cards([{
        "game": game, "channel": channel, "date_str": date_str,
        "version": version, "text": text, "raw": raw,
        "thumb_paths": thumb_paths or [], "badge": badge,
    }], summary=summary, url=url, media_dir=media_dir)
    return pages[0]
