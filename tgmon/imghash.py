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


def hashes_for(path) -> tuple[str | None, str | None]:
    try:
        with Image.open(path) as img:
            img.load()
            return phash(img), dhash(img)
    except Exception as e:
        logger.debug("打开图片失败 %s: %s", path, e)
        return None, None
