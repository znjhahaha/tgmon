from tgmon.kb.wiki import parse_infobox, parse_page


def test_wiki_infobox_extracts_npc_and_attributes():
    text = "{{Infobox Character\n| name = 波尔卡·卡卡目\n| aliases = Polka Kakamond, Lord of Silence\n| path = [[Erudition]]\n| faction = Genius Society\n}}"
    info = parse_infobox(text)
    assert info["path"] == "Erudition"
    page = parse_page({"pageid": 42, "title": "Polka Kakamond", "revid": 3, "content": text}, "npc")
    assert page["canonical_zh"] == "波尔卡·卡卡目"
    assert "Polka Kakamond" in page["aliases"]
    assert page["category"] == "npc"


def test_wiki_localized_name_template_wins_over_english_title():
    page = parse_page({"title": "Polka Kakamond",
                       "content": "{{Other Languages|en=Polka Kakamond|zh_cn=波尔卡·卡卡目}}"}, "npc")
    assert page["canonical_zh"] == "波尔卡·卡卡目"
    assert "Polka Kakamond" in page["aliases"]


def test_wiki_english_fallback_is_rejected_candidate():
    page = parse_page({"title": "Zenless Limit",
                       "content": "{{Infobox Lore|name=Zenless Limit}}"}, "lore")
    assert page["canonical_zh"] == ""
    assert page["quality_reason"] == "missing_chinese_name"


def test_wiki_chinese_title_is_a_valid_candidate():
    page = parse_page({"title": "绝区零世界观", "content": "{{Infobox Lore|name=Zenless Lore}}"}, "lore")
    assert page["canonical_zh"] == "绝区零世界观"
    assert page["quality_reason"] is None


def test_fandom_zhs_language_field_provides_simplified_chinese_name():
    page = parse_page({"title": "Sons of Calydon", "content":
        "{{Other Languages|default_hidden=1|en=Sons of Calydon|zhs=卡吕冬之子|zht=卡呂冬之子}}"}, "faction")
    assert page["canonical_zh"] == "卡吕冬之子"
    assert page["quality_reason"] is None
