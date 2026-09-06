"""游戏判定 / 内容类型 / 版本号测试。样本全部取自线上真实消息。

判定错游戏的后果不是「标签难看」而是**整个分游戏术语库被套错**：
msg 16 的 `Vodyanitsa` 在 game=原神 下译「沃雅妮莎」（对），
在无 game 下译「沃佳尼察」（错）。所以这里的断言是翻译质量的前置条件。

不碰数据库：术语命中（hits）直接构造 Term 传进去，这正是 detect() 收 hits
参数的形状。DB 里的术语表会变，测试不该跟着变。
"""
from __future__ import annotations

import pytest

from tgmon import classify, settings
from tgmon.glossary import Term

# ---- 真实样本（线上 monitor_message 原文）----

MSG12 = "Punishment time! Let's find out which bad kids didn't pull for Elation."
MSG16 = "Vodyanitsa sings"
MSG19 = "「GI 7.1」\nV4  1.96GB"
MSG20 = ("[Zenless Zone Zero] v3.2.12 Rev. 18560506 Hotfix Update\n\n"
         "Changelog: https://zzz.gachabase.net/changelog/creator#v3.2.12-rev-18560506\n\n"
         "[W-Engines] Bloodmarrow Coffer\n|- Chinese: Changes (Full)")
MSG21 = ("「HSR 4.6」\n\n污染情况文字版：\n"
         "混沌1: 呼雷，亚婆离，绘世(污染3)\n末日:卡夫卡(污染3)，金血忆灵，呼雷")

HSR = "崩坏:星穹铁道"


def term(source, target, game, category="character", kind="primary"):
    """构造一条术语命中。只填 detect() 真正读的字段。"""
    return Term(entry_id=abs(hash(source)) % 10**6, alias_id=1,
                source=source, target=target, game=game,
                category=category, alias_kind=kind)


# ---------------------------------------------------------------- 显式标记


@pytest.mark.parametrize("text,game,version", [
    (MSG19, "原神", "7.1"),
    (MSG21, HSR, "4.6"),
    (MSG20, "绝区零", "3.2.12"),
])
def test_explicit_marker_wins(text, game, version):
    """有显式标记就不看票数 —— 线上 44 条里 17 条有，是最可靠的信号。"""
    det = classify.detect(text, channel=None, hits=[])
    assert det.game == game
    assert det.version == version
    assert det.reasons and "显式标记" in det.reasons[0]


def test_marker_beats_contradicting_votes():
    """标记与术语票矛盾时以标记为准，但票数要留在 game_scores 里可复查。

    msg 21 是崩铁的深渊配队表，正文里的「卡夫卡」如果被原神的同名条目
    命中，也不能把 HSR 的标记顶掉。
    """
    hits = [term("卡夫卡", "卡夫卡", "原神")] * 3
    det = classify.detect(MSG21, channel=None, hits=hits)
    assert det.game == HSR
    assert det.as_json()["scores"].get("原神")      # 票照样记下来了


def test_version_picks_the_one_next_to_the_marker():
    """一条消息里有多个数字，只有标记旁边那个是版本号。

    msg 19 正文的 `V4  1.96GB` 里 1.96 是文件大小，不是版本。
    """
    assert classify.detect(MSG19, hits=[]).version == "7.1"


# ---------------------------------------------------------------- 术语投票


def test_vote_decides_when_no_marker():
    """msg 12 没有任何标记，`Elation` 是唯一信号 —— 这条就是 #3 的原始病例。

    「欢愉/Elation」在库里归类是 jargon 但带 game=崩铁，所以必须计票；
    以前 game 作用域的 jargon 被整体排除，这条只能靠模型蒙。
    """
    det = classify.detect(MSG12, channel=None,
                          hits=[term("Elation", "欢愉", HSR, "jargon")])
    assert det.game == HSR


def test_generic_jargon_does_not_vote():
    """game="" 的通用行话（卡池 / banner）在哪款游戏里都出现，不能计票。"""
    det = classify.detect("new banner soon", channel=None,
                          hits=[term("banner", "卡池", "", "jargon")])
    assert det.game is None
    assert not det.scores


def test_element_terms_do_not_vote():
    """Fire / Ice 这种词在任何游戏文本里都出现，权重被设成 0。"""
    det = classify.detect("Fire and Ice", channel=None, hits=[
        term("Fire", "火", HSR, "element"),
        term("Ice", "冰", HSR, "element"),
    ])
    assert det.game is None


def test_close_race_refuses_to_guess():
    """两款游戏咬得很近多半是跨游戏对比帖，判谁都不对 —— 宁可返回 None。

    猜错游戏比不猜更糟：会把另一款游戏的译名套上去。
    """
    det = classify.detect("compare them", channel=None, hits=[
        term("胡桃", "胡桃", "原神"),
        term("卡芙卡", "卡芙卡", HSR),
    ])
    assert det.game is None
    assert any("差距不足" in r for r in det.reasons)


def test_below_threshold_refuses_to_guess():
    """弱信号不判定。阈值取运行时配置，不写死字面量 ——

    以前把阈值写死在测试里，改了线上配置测试照样绿，等于没测。
    """
    weak = float(settings.get("GAME_DETECT_MIN_SCORE"))
    # region 权重 0.5，凑不到阈值
    hits = [term("深渊", "深渊", "原神", "region")]
    det = classify.detect("something about 深渊", channel=None, hits=hits)
    assert sum(det.scores.values()) < weak
    assert det.game is None


# ---------------------------------------------------------------- 频道兜底


class Chan:
    def __init__(self, game=None, games=None):
        self.game = game
        self.games = games


def test_entity_evidence_precedes_channel_hint():
    """明确实体归属优先于频道配置。"""
    det = classify.detect("x", channel=Chan(games=["原神"]),
                          hits=[term("卡芙卡", "卡芙卡", HSR)])
    assert det.game == HSR and det.method == "entity"


def test_single_candidate_is_the_fallback():
    det = classify.detect("没有任何信号", channel=Chan(games=["崩铁"]), hits=[])
    assert det.game == HSR             # 「崩铁」要被 normalize 成规范名


def test_multi_game_channel_without_signal_gives_up():
    """Seele Leaks 这类频道：候选多个又没信号 → None，让 prompt 走保留原文分支。"""
    det = classify.detect("没有任何信号", hits=[],
                          channel=Chan(games=["原神", "崩铁", "绝区零"]))
    assert det.game is None


def test_msg16_falls_back_to_channel_without_hits():
    """判不出来时才轮到频道兜底。

    注意 msg 16 在**线上**并不走这条路 —— 全库匹配能命中 `Vodyanitsa`
    （原神 character，3.9 分）直接投票判定。这里 hits 传空是为了单独测
    兜底分支：真判不出来时，宁可用频道的单值 game 也不要猜。
    """
    assert classify.detect(MSG16, hits=[]).game is None
    assert classify.detect(MSG16, channel=Chan(game="原神"), hits=[]).game == "原神"


def test_msg16_decided_by_vote_in_production():
    """线上的真实路径：`Vodyanitsa` 一个 character 命中就足以定案。

    这是 #3 的直接证据 —— 判定拿到「原神」后 build_table 才会注入
    `Vodyanitsa → 沃雅妮莎`，译文从「沃佳尼察」变回「沃雅妮莎」。
    """
    det = classify.detect(MSG16, channel=Chan(), hits=[
        term("Vodyanitsa", "沃雅妮莎", "原神", "character")])
    assert det.game == "原神"


# ---------------------------------------------------------------- 内容类型


@pytest.mark.parametrize("text,expected", [
    (MSG12, "卡池"),          # didn't pull for
    (MSG20, "版本更新"),       # Hotfix Update
    (MSG19, "资源解包"),       # V4 / GB
])
def test_topics(text, expected):
    assert expected in classify.detect_topics(text)


def test_topics_are_multi_label():
    """一条消息可以既是卡池又是角色。"""
    got = classify.detect_topics("7.2卡池 新角色 米提亚")
    assert {"卡池", "角色"} <= set(got)


def test_gachabase_domain_is_not_a_gacha_topic():
    """`zzz.gachabase.net` 出现在几乎每条数据更新里，不能因此标成卡池。"""
    assert "卡池" not in classify.detect_topics(
        "https://zzz.gachabase.net/changelog/creator")
