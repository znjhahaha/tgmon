"""文本归一化 + 指纹。去重第 2 层。

爆料圈同一条情报会被多个频道反复转发，且转发时常改几个字、换标点、加自己的
推广尾巴。所以精确哈希之外还要 SimHash 做近似匹配。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# 频道尾巴 / 推广行：整行命中就删
_TAIL_PATTERNS = [
    re.compile(r"^\s*(?:订阅|关注|投稿|加入|频道|交流群|广告|合作)\s*[:：]?.*$", re.M),
    re.compile(r"^.*\b(?:t\.me|telegram\.me|https?://t\.me)\S*.*$", re.M | re.I),
    re.compile(r"^\s*@[\w_]{4,}\s*$", re.M),
    re.compile(r"^\s*[-—_=~*·]{3,}\s*$", re.M),
    re.compile(r"^\s*(?:via|source|来源|转自)\s*[:：].*$", re.M | re.I),
]

# 标点统一：全角 → 半角，各种引号破折号 → 统一形态
_PUNCT_MAP = str.maketrans({
    "，": ",", "。": ".", "！": "!", "？": "?", "；": ";", "：": ":",
    "（": "(", "）": ")", "【": "[", "】": "]", "「": '"', "」": '"',
    "“": '"', "”": '"', "‘": "'", "’": "'", "－": "-", "—": "-",
    "–": "-", "～": "~", "、": ",", "％": "%", "＃": "#", "＠": "@",
})

_URL_RE = re.compile(r"https?://\S+")


def _strip_symbols(text: str) -> str:
    """去 emoji 与装饰符号。Unicode 类别 So/Sk/Cs 基本就是它们。"""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cs", "Cf", "Co"):
            continue
        if cat == "Sm" and ch not in "+=<>":
            continue
        out.append(ch)
    return "".join(out)


def normalize(text: str | None) -> str:
    """归一化正文，用于算指纹。有损，不用于展示。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    for pat in _TAIL_PATTERNS:
        t = pat.sub("", t)
    t = _URL_RE.sub("", t)
    t = _strip_symbols(t)
    t = t.translate(_PUNCT_MAP)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip().lower()


def text_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _tokens(normalized: str) -> list[str]:
    """CJK 按字，拉丁按词。再做 bigram shingle 保留局部语序。"""
    raw = re.findall(r"[a-z0-9]+|[぀-鿿가-힯]", normalized)
    if len(raw) < 2:
        return raw
    return [f"{raw[i]}_{raw[i + 1]}" for i in range(len(raw) - 1)]


def simhash(normalized: str, bits: int = 64) -> str:
    """64 位 SimHash，返回十六进制。空文本返回空串。"""
    toks = _tokens(normalized)
    if not toks:
        return ""
    vec = [0] * bits
    for tok in toks:
        h = int.from_bytes(hashlib.md5(tok.encode("utf-8")).digest()[:8], "big")
        for i in range(bits):
            vec[i] += 1 if (h >> i) & 1 else -1
    val = 0
    for i in range(bits):
        if vec[i] > 0:
            val |= 1 << i
    return f"{val:016x}"


def hamming_hex(a: str | None, b: str | None) -> int:
    """两个十六进制指纹的汉明距离。任一为空返回一个大数。"""
    if not a or not b:
        return 999
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 999
