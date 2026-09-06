from tgmon import glossary


def term(source, target, game="崩坏:星穹铁道", category="character", eid=1):
    return glossary.Term(eid, eid, source, target, game=game, category=category)


def test_untranslated_approved_name_is_a_miss():
    assert glossary.check_translation("Kafka 的速度为 120。", [term("Kafka", "卡芙卡")]) == ["Kafka"]


def test_correction_preserves_urls_code_and_numbers():
    text = "Kafka 的速度 120；Kafkaesque https://example.com/Kafka `Kafka` #Kafka @Kafka"
    actual, corrected = glossary.correct_translation(text, [term("Kafka", "卡芙卡")])
    assert actual == "卡芙卡 的速度 120；Kafkaesque https://example.com/Kafka `Kafka` #Kafka @Kafka"
    assert corrected == ["Kafka"]


def test_correction_does_not_guess_wrong_chinese_name():
    assert glossary.correct_translation("卡夫卡的速度为120", [term("Kafka", "卡芙卡")]) == ("卡夫卡的速度为120", [])


def test_conflicting_targets_are_not_replaced():
    hits = [term("Trailblazer", "开拓者"), term("Trailblazer", "另一角色", eid=2)]
    assert glossary.correct_translation("Trailblazer", hits) == ("Trailblazer", [])


def test_longer_term_wins_without_changing_other_short_matches():
    hits = [term("Hunt", "巡猎", category="path"), term("The Hunt", "巡猎", category="path", eid=2)]
    text, _ = glossary.correct_translation("The Hunt / Hunt / Hunting", hits)
    assert text == "巡猎 / 巡猎 / Hunting"


def test_translation_terms_drop_other_game_and_noop_names():
    pool = [term("Kafka", "卡芙卡"), term("Kafka", "原神角色", game="原神", eid=2),
            term("Unknown", "Unknown", eid=3)]
    hits = glossary.translation_hits("Kafka Unknown", pool, "崩铁")
    assert [(t.source, t.target) for t in hits] == [("Kafka", "卡芙卡")]


def test_translation_terms_retain_multiple_aliases_of_same_entity():
    pool = [term("Dan Heng", "丹恒"), term("DH", "丹恒")]
    assert len(glossary.translation_hits("Dan Heng (DH)", pool, "崩铁")) == 2


def test_nested_language_template_preserves_complete_chinese_name():
    from tgmon.kb.wiki import parse_page
    page = parse_page({"title": "Polka Kakamond", "content":
        "{{Other Languages|zh_cn={{Lang|zh|波尔卡·卡卡目}}<ref>reference</ref>|en=Polka Kakamond}}"}, "npc")
    assert page["canonical_zh"] == "波尔卡·卡卡目"
