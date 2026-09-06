"""语言路由测试。样本全部取自真实爆料频道（t.me/s/Genshinleakflow）。

日文那条是关键回归用例：日文大量用汉字，如果判定顺序写错就会被当成中文
直接跳过翻译，而且不会报错，只会静默漏译。
"""
from __future__ import annotations

from tgmon import lang

# ---- 真实样本 ----

ALL_ZH = """**未来卡池和女皇相关信息**
7.2卡池
米提亚，兹白，莉奈娅，？
新冰草反应存在，但是只是女皇大人专用
米提亚对女皇大人不只是提供稳定后台雷还有精通辅"""

BILINGUAL_DUP = """**7.x 关于萍姥姥**
萍姥姥不会在7.0大版本内成为自机

[Genshin]
As far as I know, there's no Madame Ping playable this year

Thank you DK2 for the info."""

LINE_INTERLEAVED = """7.2 地图扩展大致范围参考
目前是2，如图所示
忽略大小，只是大致范围
Q: Is it option 1 or option 2?
(Ignore size, just general idea)
A: 2, for now"""

ALL_EN = """New related assets
still unfinished

For some reason snezhnaya "mark" wasn't added"""

JAPANESE = """ver7.2の新キャラ情報
リネアはもうすぐ実装される"""

NUMERIC_ZH = """0命异化星超导仅前台生效，中频挂雷，Q叠层，14％基础区
1命40点队伍精通，加速进入后台
2命在前台自身90％爆伤提升"""

# 中文行里引用日文名 —— 不该判成日文
ZH_WITH_KANA_NAME = "新角色リネア的情报，7.2实装"
MIXED_LINE = "斯科特的 mechatron workshop will rebuild bodies"


class TestLangOfLine:
    def test_mixed_line_is_not_silently_skipped(self):
        assert lang.lang_of_line(MIXED_LINE) == lang.LANG_MIXED

    def test_pure_chinese(self):
        assert lang.lang_of_line("米提亚，兹白，莉奈娅") == lang.LANG_ZH

    def test_pure_english(self):
        assert lang.lang_of_line("Thank you DK2 for the info.") == lang.LANG_EN

    def test_japanese_not_chinese(self):
        """日文用汉字，必须先查假名。判错会静默漏译。"""
        assert lang.lang_of_line("ver7.2の新キャラ情報") == lang.LANG_JA
        assert lang.lang_of_line("リネアはもうすぐ実装される") == lang.LANG_JA

    def test_chinese_quoting_kana_name(self):
        """中文里引用一个日文名，整行仍是中文。"""
        assert lang.lang_of_line(ZH_WITH_KANA_NAME) == lang.LANG_ZH

    def test_korean(self):
        assert lang.lang_of_line("7.2 신규 캐릭터 정보") == lang.LANG_KO

    def test_symbols_only(self):
        for s in ("", "   ", "7.2", "---", "**", "[]"):
            assert lang.lang_of_line(s) == lang.LANG_NONE

    def test_mixed_line_weighted(self):
        # 汉字少、英文长 → 判英文
        assert lang.lang_of_line("A: 2, for now 是的") == lang.LANG_EN
        # 汉字多、夹个版本号 → 判中文
        assert lang.lang_of_line("7.2卡池米提亚兹白莉奈娅") == lang.LANG_ZH


class TestSplitBlocks:
    def test_all_zh_single_block(self):
        bs = lang.split_blocks(ALL_ZH)
        assert [b.lang for b in bs] == [lang.LANG_ZH]

    def test_bilingual_splits(self):
        bs = lang.split_blocks(BILINGUAL_DUP)
        assert [b.lang for b in bs] == [lang.LANG_ZH, lang.LANG_EN]
        assert "萍姥姥" in bs[0].text
        assert "Madame Ping" in bs[1].text

    def test_line_interleaved_splits(self):
        bs = lang.split_blocks(LINE_INTERLEAVED)
        assert [b.lang for b in bs] == [lang.LANG_ZH, lang.LANG_EN]

    def test_blank_lines_preserved(self):
        """空行有排版意义，不能在分块时丢掉。"""
        bs = lang.split_blocks(ALL_EN)
        assert "\n".join(b.text for b in bs) == ALL_EN


class TestRouteZhFirst:
    def test_mixed_line_routes_complete_text(self):
        r = lang.route(MIXED_LINE)
        assert r.needs_translation
        assert r.translate_text == MIXED_LINE

    def test_all_chinese_no_translation(self):
        r = lang.route(ALL_ZH)
        assert r.lang == lang.LANG_ZH
        assert not r.needs_translation
        assert r.keep_text == ALL_ZH
        assert r.dropped_text == ""
        assert r.zh_ratio == 1.0

    def test_bilingual_keeps_zh_drops_en(self):
        r = lang.route(BILINGUAL_DUP)
        assert not r.needs_translation
        assert "萍姥姥" in r.keep_text
        assert "Madame Ping" not in r.keep_text
        assert "Madame Ping" in r.dropped_text

    def test_line_interleaved_keeps_zh(self):
        r = lang.route(LINE_INTERLEAVED)
        assert not r.needs_translation
        assert "地图扩展" in r.keep_text
        assert "option 1" in r.dropped_text

    def test_all_english_translates(self):
        r = lang.route(ALL_EN)
        assert r.lang == lang.LANG_EN
        assert r.needs_translation
        assert r.translate_text == ALL_EN
        assert r.keep_text == ""

    def test_japanese_translates(self):
        """日文要翻译，不能被当成中文跳过。"""
        r = lang.route(JAPANESE)
        assert r.lang == lang.LANG_JA
        assert r.needs_translation

    def test_numeric_heavy_chinese_no_translation(self):
        r = lang.route(NUMERIC_ZH)
        assert not r.needs_translation
        assert r.keep_text == NUMERIC_ZH

    def test_short_zh_in_english_still_translates(self):
        """英文段里夹一句短中文，不该被当成译文而丢掉整段英文。

        走默认阈值而不是写死一个数 —— 之前这里钉了 30，默认值改成 10 之后
        测试照样绿，等于这条用例不再守护线上实际用的阈值。
        """
        text = "补充\n\n" + "New assets were added to the beta build this week. " * 3
        r = lang.route(text)
        assert r.needs_translation
        assert "New assets" in r.translate_text


class TestRouteOtherPolicies:
    def test_per_block_translates_non_zh(self):
        r = lang.route(BILINGUAL_DUP, policy="per_block")
        assert r.needs_translation
        assert "Madame Ping" in r.translate_text
        assert "萍姥姥" in r.keep_text
        assert r.dropped_text == ""

    def test_always_translates_everything(self):
        r = lang.route(ALL_ZH, policy="always")
        assert r.needs_translation
        assert r.translate_text == ALL_ZH

    def test_empty_text(self):
        r = lang.route("")
        assert not r.needs_translation
        assert r.lang == lang.LANG_NONE


class TestStitch:
    def test_zh_first_then_translation(self):
        r = lang.route(BILINGUAL_DUP, policy="per_block")
        out = lang.stitch(r, "据我所知今年不会有萍姥姥")
        assert out.index("萍姥姥不会") < out.index("据我所知")

    def test_translation_only(self):
        r = lang.route(ALL_EN)
        assert lang.stitch(r, "译文") == "译文"

    def test_keep_only(self):
        r = lang.route(ALL_ZH)
        assert lang.stitch(r, "") == ALL_ZH
