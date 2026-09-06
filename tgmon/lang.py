"""语言检测与分块 —— 决定「要不要翻译」，在决定「怎么翻译」之前。

这个模块存在的理由：实测爆料频道后发现，绝大多数原文本来就是中文（源头是贴吧/NGA），
把它送去翻译不只是白花钱，是把圈内人写的好中文喂给模型改写成谁都不说的话。
还有一类是爆料人自己发的中英双语，两段说同一件事，整条送翻译会输出两段重复中文。

手写而不用 langdetect/fasttext：这台机器 3.9 GB 内存、16 GB 盘，而我们只需要区分
中/英/日/韩四类，逐行判比整段判更准（双语消息就是逐行混排的）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 假名。放在最前面判 —— 日文大量用汉字，先看汉字比例会把日文误判成中文，
# 然后被当成「不用翻译」直接跳过，这是最难发现的一类错。
KANA = re.compile(r"[぀-ゟ゠-ヿ]")
HANGUL = re.compile(r"[가-힯ᄀ-ᇿ㄰-㆏]")
# CJK 统一汉字 + 扩展 A。中日共用，所以单看这个区分不出语言
HAN = re.compile(r"[一-鿿㐀-䶿]")
# 拉丁词。按词数而不是字母数和汉字比 —— 英文大量用 for/now/is 这类短功能词，
# 数字母会系统性低估英文的分量
LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'’]*")

LANG_ZH = "zh"
LANG_EN = "en"
LANG_JA = "ja"
LANG_KO = "ko"
LANG_MIXED = "mixed"
LANG_NONE = "none"      # 纯符号/数字/空行，跟随上下文


@dataclass
class Block:
    """一段连续同语言的文本。保留原始换行，翻译后要能原样拼回去。"""
    lang: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.text.strip())


@dataclass
class Route:
    """路由结论。pipeline 只看这个结构，不重复判语言。"""
    lang: str                                  # 整条消息的主语言
    policy: str                                # 实际生效的策略
    translate_text: str = ""                   # 要送去翻译的部分（空 = 不用翻译）
    keep_text: str = ""                         # 原样保留的中文部分
    dropped_text: str = ""                      # 被丢弃的部分，存库可回查
    blocks: list[Block] = field(default_factory=list)
    zh_ratio: float = 0.0

    @property
    def needs_translation(self) -> bool:
        return bool(self.translate_text.strip())


def lang_of_line(s: str) -> str:
    """判单行语言。顺序：假名 → 谚文 → 汉字/拉丁比例。"""
    if not s or not s.strip():
        return LANG_NONE

    kana = len(KANA.findall(s))
    han = len(HAN.findall(s))
    words = len(LATIN_WORD.findall(s))

    # 中文行里引用一个日文名（如「リネア」）不该整行判成日文，
    # 所以要求假名足够多且相对汉字占比够高，而不是「出现即日文」
    if kana >= 3 and kana * 2 >= han:
        return LANG_JA
    if kana and han == 0 and words == 0:
        return LANG_JA
    if HANGUL.search(s):
        return LANG_KO

    # Mixed prose must not be mistaken for an already complete translation.
    if han >= 4 and words >= 2:
        latin = [word.casefold() for word in LATIN_WORD.findall(s)]
        common = {"the", "is", "are", "was", "were", "with", "for", "of", "to",
                  "and", "that", "this", "has", "have", "will", "can", "from",
                  "after", "before", "when", "into", "on", "in"}
        if common.intersection(latin) or sum(len(word) >= 4 for word in latin) >= 2:
            return LANG_MIXED

    if han == 0 and words == 0:
        return LANG_NONE
    if han == 0:
        return LANG_EN
    if words == 0:
        return LANG_ZH
    # 一个汉字≈一个词，直接比词数。用字母数会低估英文
    return LANG_ZH if han >= words else LANG_EN


def split_blocks(text: str) -> list[Block]:
    """逐行判语言，合并相邻同语言行。

    LANG_NONE 的行（空行、纯符号、`[Genshin]` 这种标记）跟随前一块，
    这样排版结构不会被切碎 —— 原文的空行是有意义的。
    """
    if not text:
        return []

    groups: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        lg = lang_of_line(line)
        if groups and (groups[-1][0] == lg or lg == LANG_NONE):
            groups[-1][1].append(line)
        elif groups and groups[-1][0] == LANG_NONE and lg != LANG_NONE:
            # 开头的空行/标记块，被后面第一个有语言的块吸收
            groups[-1] = (lg, groups[-1][1] + [line])
        else:
            groups.append((lg, [line]))

    return [Block(lang=lg, text="\n".join(lines)) for lg, lines in groups]


def dominant_lang(blocks: list[Block]) -> str:
    """按字符数选主语言。none 块不计票 —— 它们没有语言信息。"""
    if not blocks:
        return LANG_NONE
    tally: dict[str, int] = {}
    for b in blocks:
        if b.lang == LANG_NONE:
            continue
        tally[b.lang] = tally.get(b.lang, 0) + b.chars
    if not tally:
        return LANG_NONE
    return max(tally.items(), key=lambda kv: kv[1])[0]


def route(text: str, policy: str = "zh_first", zh_min_chars: int = 10) -> Route:
    """决定这条消息怎么处理。

    policy:
      zh_first  —— 有实质中文段就认定是双语重复，非中文段丢弃（默认）
      per_block —— 逐块翻译非中文段，中文段照抄
      always    —— 整条送翻译，等于关掉语言路由

    zh_min_chars 是「实质中文段」的门槛，量的是中文块 strip 后的字符数（含空格
    和数字），不是整条消息的字数 —— 两者能差 8 字以上。太低会把 #tag、【原神】、
    「来源：NGA」这类 4~6 字的行当成译文从而丢掉整段英文；太高会漏掉真双语消息
    （实测中文块只有 13~19 字）。默认 10 卡在这两簇中间，见 settings.py。
    """
    blocks = split_blocks(text)
    if not blocks:
        return Route(lang=LANG_NONE, policy=policy)

    total = sum(b.chars for b in blocks if b.lang != LANG_NONE)
    zh_chars = sum(b.chars for b in blocks if b.lang == LANG_ZH)
    ratio = zh_chars / total if total else 0.0
    main = dominant_lang(blocks)

    r = Route(lang=main, policy=policy, blocks=blocks, zh_ratio=ratio)

    if policy == "always":
        r.translate_text = text
        return r

    zh_blocks = [b for b in blocks if b.lang == LANG_ZH]
    other = [b for b in blocks if b.lang not in (LANG_ZH, LANG_NONE)]

    if any(b.lang == LANG_MIXED for b in blocks) and policy == "zh_first":
        r.translate_text = text
        return r

    # 全中文（或中文 + 纯符号）：一个字都不用翻
    if not other:
        r.keep_text = text
        return r

    # 没有中文：全文翻译，走原有路径
    if not zh_blocks:
        r.translate_text = text
        return r

    # 混合。有实质中文段 → 判定为双语重复
    if policy == "zh_first" and zh_chars >= zh_min_chars:
        r.keep_text = "\n\n".join(b.text.strip("\n") for b in zh_blocks).strip()
        r.dropped_text = "\n\n".join(b.text.strip("\n") for b in other).strip()
        return r

    # per_block，或中文段太短不足以认定是译文 → 只翻非中文段
    r.keep_text = "\n\n".join(b.text.strip("\n") for b in zh_blocks).strip()
    r.translate_text = "\n\n".join(b.text.strip("\n") for b in other).strip()
    return r


def stitch(route_: Route, translated: str) -> str:
    """把译文和原样保留的中文拼回去，顺序按原文块序。

    只在 per_block 有实际拼接工作。zh_first 不调用这个（没有译文），
    纯外文也不调用（译文就是全部）。
    """
    if not route_.keep_text:
        return translated
    if not translated:
        return route_.keep_text
    # 中文块在前还是译文在前，看原文里第一个有语言的块是什么
    for b in route_.blocks:
        if b.lang == LANG_NONE:
            continue
        if b.lang == LANG_ZH:
            return f"{route_.keep_text}\n\n{translated}"
        break
    return f"{translated}\n\n{route_.keep_text}"
