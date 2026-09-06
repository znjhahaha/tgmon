"""端到端：三条假消息走完整 pipeline 的语言路由 + 实体标注。

不碰真 TG —— pipeline 的入口只要一个有 text 的对象，用 SimpleNamespace 就够。
验证的是计划里第 ③ 项：全中文跳过、双语丢英文、纯英文正常翻译。
"""
from __future__ import annotations

from tgmon import lang
from tgmon.kb.annotate import annotate

ZH_ONLY = """**未来卡池和女皇相关信息**
7.2卡池
米提亚，兹白，莉奈娅，？
新冰草反应存在，但是只是女皇大人专用"""

BILINGUAL = """**7.x 关于萍姥姥**
萍姥姥不会在7.0大版本内成为自机

[Genshin]
As far as I know, there's no Madame Ping playable this year"""

EN_ONLY = """Kafka rerun is on the 3.1 banner.
She stays off-field for most rotations."""

JA_ONLY = "ver7.2の新キャラ情報が出ました"

CASES = [
    ("全中文（贴吧源）", ZH_ONLY, "zh_first"),
    ("中英双语同义重复", BILINGUAL, "zh_first"),
    ("纯英文", EN_ONLY, "zh_first"),
    ("日文（关键回归用例）", JA_ONLY, "zh_first"),
    ("双语 per_block", BILINGUAL, "per_block"),
]

fail = 0
for name, text, policy in CASES:
    r = lang.route(text, policy=policy, zh_min_chars=30)
    ents = annotate(text, "原神")["entities"]
    print(f"--- {name}  policy={policy}")
    print(f"    lang={r.lang} zh_ratio={r.zh_ratio:.2f} "
          f"blocks={[b.lang for b in r.blocks]}")
    print(f"    需要翻译={r.needs_translation} "
          f"keep={len(r.keep_text)}字 dropped={len(r.dropped_text)}字")
    print(f"    实体={[e['name'] for e in ents] or '—'}")

    if name.startswith("全中文"):
        if r.needs_translation:
            print("    FAIL 全中文不该送翻译"); fail += 1
        if r.keep_text.strip() != text.strip():
            print("    FAIL 中文原文该原样保留"); fail += 1
    if name.startswith("中英双语"):
        if not r.keep_text or not r.dropped_text:
            print("    FAIL 双语该留中文丢英文"); fail += 1
        if r.needs_translation:
            print("    FAIL zh_first 下双语不该翻译"); fail += 1
    if name == "纯英文":
        if not r.needs_translation:
            print("    FAIL 纯英文必须翻译"); fail += 1
    if name.startswith("日文"):
        if r.lang != "ja":
            print(f"    FAIL 日文被判成 {r.lang}"); fail += 1
        if not r.needs_translation:
            print("    FAIL 日文必须翻译"); fail += 1
    if policy == "per_block":
        if not r.needs_translation:
            print("    FAIL per_block 该翻译非中文段"); fail += 1
        if not r.keep_text:
            print("    FAIL per_block 该保留中文段"); fail += 1

print()
print("术语命中（纯英文那条）:")
from tgmon import glossary
hits = glossary.find_hits(EN_ONLY, glossary.terms_for("原神"))
for h in hits:
    print(f"  {h.source:16} -> {h.target}")
if not hits:
    print("  FAIL 行话一个都没命中"); fail += 1

print()
print("FAILURES:", fail)
