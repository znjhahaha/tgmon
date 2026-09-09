"""图片感知哈希。去重第 3 层，纯本地计算不调 AI。

作用是抓「同图配不同文案」与「重新编码 / 加水印 / 轻微裁剪的同图」—— 这类重复
文本指纹抓不到。只在已生成的缩略图上算，开销可以忽略。

自己实现 DCT 而不用 imagehash：它依赖 scipy（约 40 MB），这台机器只剩 16 G 盘。
"""
from __future__ import annotations

import logging

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_PHASH_IMG = 32   # DCT 输入边长
_PHASH_LOW = 8    # 取左上角低频块边长
_DCT_MATRIX: np.ndarray | None = None


def _dct_matrix(n: int) -> np.ndarray:
    """DCT-II 变换矩阵。只算一次然后缓存。"""
    global _DCT_MATRIX
    if _DCT_MATRIX is None or _DCT_MATRIX.shape[0] != n:
        k = np.arange(n).reshape(-1, 1)
        i = np.arange(n).reshape(1, -1)
        _DCT_MATRIX = np.cos(np.pi * (i + 0.5) * k / n)
    return _DCT_MATRIX


def _bits_to_hex(bits: np.ndarray) -> str:
    val = 0
    for b in bits.flatten():
        val = (val << 1) | int(bool(b))
    return f"{val:016x}"


def phash(img: Image.Image) -> str | None:
    """感知哈希：低频 DCT 系数与中位数比较。对缩放、轻微压缩稳定。"""
    try:
        g = img.convert("L").resize((_PHASH_IMG, _PHASH_IMG), Image.LANCZOS)
        a = np.asarray(g, dtype=np.float64)
        d = _dct_matrix(_PHASH_IMG)
        coef = d @ a @ d.T
        low = coef[:_PHASH_LOW, :_PHASH_LOW]
        # 排除 DC 分量（整体亮度），否则调亮度就换指纹
        rest = low.flatten()[1:]
        med = np.median(rest)
        bits = (low.flatten() > med)
        bits[0] = False
        return _bits_to_hex(bits[:64])
    except Exception as e:
        logger.debug("phash 计算失败: %s", e)
        return None


def dhash(img: Image.Image) -> str | None:
    """差值哈希：相邻像素梯度方向。比 phash 更抗整体色调变化。"""
    try:
        g = img.convert("L").resize((9, 8), Image.LANCZOS)
        a = np.asarray(g, dtype=np.int16)
        bits = a[:, 1:] > a[:, :-1]
        return _bits_to_hex(bits)
    except Exception as e:
        logger.debug("dhash 计算失败: %s", e)
        return None


def _trim_letterbox(img: Image.Image) -> Image.Image:
    """裁掉纯黑/纯白遮幅边（漫画分镜、宽屏适配的上下黑条）。每边最多 45%。

    黑边占到画面一半以上时，phash 的低频与 dhash 的梯度都被黑边主导：
    两张内容完全不同的遮幅图，dhash 距离可以小到 4（2026-09 案例：遮幅
    截图互撞误判）。裁掉边再算，指纹才反映内容区。保守起见只认接近纯黑
    /纯白且几乎无噪声的行列，避免误裁正常图的暗部天空。
    """
    try:
        a = np.asarray(img.convert("L"), dtype=np.uint8)
        if a.ndim != 2 or a.size == 0:
            return img
        h, w = a.shape

        def is_pad(line: np.ndarray) -> bool:
            return line.std() < 6.0 and (line.mean() < 24 or line.mean() > 232)

        def scan(get, n, limit):
            i = 0
            while i < limit and is_pad(get(i)):
                i += 1
            return i

        top = scan(lambda i: a[i, :], h, int(h * 0.45))
        bottom = scan(lambda i: a[h - 1 - i, :], h, int(h * 0.45))
        left = scan(lambda i: a[:, i], w, int(w * 0.45))
        right = scan(lambda i: a[:, w - 1 - i], w, int(w * 0.45))
        box = (left, top, w - right, h - bottom)
        if (top + bottom >= h or left + right >= w
                or box == (0, 0, w, h)):
            return img
        return img.crop(box)
    except Exception as e:
        logger.debug("letterbox 裁剪失败: %s", e)
        return img


def hashes_for(path) -> tuple[str | None, str | None]:
    try:
        with Image.open(path) as img:
            img.load()
            img = _trim_letterbox(img)
            ph, dh = phash(img), dhash(img)
            # 近纯色图（视频黑首帧 / 纯色背景）的哈希无区分度，存 None：
            # 存全零会让任意两个黑首帧媒体在判重时距离 0-1，全部误判
            from .dedup import informative
            return (ph if informative(ph) else None,
                    dh if informative(dh) else None)
    except Exception as e:
        logger.debug("打开图片失败 %s: %s", path, e)
        return None, None
