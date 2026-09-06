"""util.py 的纯函数单测：UTF-16 偏移换算（任务4 剧透区间的正确性根基）。"""
from __future__ import annotations

from tgmon.util import opt_int, spoiler_segments, utf16_to_char_offset


# ---------------- opt_int ----------------

def test_opt_int_empty_and_none():
    """表单「全部」选项提交空串 —— 这是线上 422 的直接原因。"""
    assert opt_int("") is None
    assert opt_int("   ") is None
    assert opt_int(None) is None


def test_opt_int_valid():
    assert opt_int("3") == 3
    assert opt_int(" 7 ") == 7
    assert opt_int("-1001723628120") == -1001723628120
    assert opt_int(42) == 42        # 已是 int 也接


def test_opt_int_garbage():
    """坏值不抛异常 —— 宁可用默认值也不能 422。"""
    assert opt_int("abc") is None
    assert opt_int("1.5") is None
    assert opt_int("7abc") is None


# ---------------- utf16_to_char_offset ----------------

def test_ascii_offsets_are_identity():
    """BMP 内的 ASCII：UTF-16 unit 数 == 字符数，索引一致。"""
    text = "abcdef"
    for i in range(len(text) + 1):
        assert utf16_to_char_offset(text, i) == i


def test_cjk_offsets_are_identity():
    """汉字也是 BMP，unit 数 == 字符数。"""
    text = "卡池复刻"
    for i in range(len(text) + 1):
        assert utf16_to_char_offset(text, i) == i


def test_emoji_surrogate_pair():
    """emoji 占 2 个 UTF-16 unit、1 个 Python 字符 —— 必须换算。

    "a😀b" 的 unit 序列: a=1, 😀=2, b=1
    unit 偏移 1 → 字符 1（😀 起点）；3 → 字符 2（b 起点）
    落在代理对中间的偏移（2）向上取整到下一个完整字符 —— TG 保证
    entity 边界对齐字符，这只是防御性语义
    """
    text = "a😀b"
    assert utf16_to_char_offset(text, 0) == 0
    assert utf16_to_char_offset(text, 1) == 1    # 😀 之前
    assert utf16_to_char_offset(text, 2) == 2    # 😀 中间 → 取整到 b
    assert utf16_to_char_offset(text, 3) == 2    # 😀 之后 = b
    assert utf16_to_char_offset(text, 4) == 3    # 末尾
    assert utf16_to_char_offset(text, 99) == 3   # 越界 → 文本末尾


def test_emoji_before_spoiler_range():
    """经典场景：emoji 在剧透段之前，不换算区间会向右漂。

    Telegram 给 "🎉🎉剧透内容" 的 spoiler entity offset=4 length=4
    （🎉 各占 2 unit）。字符索引应该是 2..6。
    """
    text = "🎉🎉剧透内容"
    start = utf16_to_char_offset(text, 4)
    end = utf16_to_char_offset(text, 4 + 4)
    assert text[start:end] == "剧透内容"


def test_spoiler_inside_emoji():
    """区间起点落在代理对中间：取整到下一个完整字符，不切开。"""
    text = "x🚀y"
    # unit 偏移 2 → 🚀 的第二个 unit → 字符 2（y）
    assert utf16_to_char_offset(text, 2) == 2
    assert utf16_to_char_offset(text, 3) == 2


# ---------------- spoiler_segments ----------------

def test_segments_basic():
    text = "前面 [中间这段是剧透] 后面"
    # 「中间这段是剧透」7 个字，从索引 4 开始（前=0 面=1 空格=2 [=3）
    segs = spoiler_segments(text, [[4, 7]])
    assert segs == [("前面 [", False), ("中间这段是剧透", True), ("] 后面", False)]


def test_segments_with_emoji():
    """含 emoji 的文本 + 已换算的字符区间。"""
    text = "🎉🎉剧透内容"
    segs = spoiler_segments(text, [[2, 4]])
    assert segs == [("🎉🎉", False), ("剧透内容", True)]


def test_segments_empty():
    assert spoiler_segments("", [[0, 1]]) == []
    assert spoiler_segments("abc", None) == []
    assert spoiler_segments(None, [[0, 1]]) == []


def test_segments_out_of_range_clamped():
    """区间超出文本长度：截断，不越界不丢尾部。"""
    segs = spoiler_segments("abc", [[1, 99]])
    assert segs == [("a", False), ("bc", True)]


def test_segments_overlapping_sorted():
    """交叠区间：第一个 [1,3) 覆盖 bcd，第二个 [2,4+2) 覆盖 cdef，
    交叠部分 ef 仍是剧透，不重复不乱序。"""
    segs = spoiler_segments("abcdefgh", [[1, 3], [2, 4]])
    assert segs == [("a", False), ("bcd", True), ("ef", True), ("gh", False)]


def test_segments_contained_swallowed():
    """第二个区间被第一个完全包含：直接吞掉，不产生空段。"""
    segs = spoiler_segments("abcdefgh", [[1, 6], [2, 2]])
    assert segs == [("a", False), ("bcdefg", True), ("h", False)]
