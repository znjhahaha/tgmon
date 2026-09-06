"""③ 知识库 —— 游戏实体、术语、别名的查看与管理。

不再是扁平的「source→target」双语对照表，而是结构化知识库：
一个实体多个 surface form（卡芙卡有 Kafka/卡妈/妈妈 三个写法但只有一个规范名）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from sqlalchemy import Text, func

from ...db import session_scope
from ...kb.wiki_review import (STATUSES, REASONS, candidate_problem, page_aliases,
                               review_entry, review_page)
from ...models import (GlossaryAlias, GlossaryEntry, MonitorMessage,
                        KnowledgeSource, KnowledgePage)
from ...util import alias_match_mode, has_cjk, opt_int, unreliable_alias_reason
from ..deps import (
    format_task_view, get_task, render, require_admin, require_worker,
    submit_task, templates,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kb")
from . import knowledge_center
router.include_router(knowledge_center.router)


PAGE_SIZE = 100
MISS_PAGE_SIZE = 40
WIKI_PAGE_SIZE = 30


def _wiki_query(s, game="", category="", status="pending"):
    q = s.query(KnowledgePage, KnowledgeSource).join(KnowledgeSource).filter(KnowledgePage.enabled.is_(True))
    if game:
        q = q.filter(KnowledgeSource.game == game)
    if category:
        q = q.filter(KnowledgePage.category == category)
    return q.filter(KnowledgePage.status == status)


def _wiki_context(game="", category="", status="pending", page=1, notice="", error=False):
    status = status if status in STATUSES else "pending"
    with session_scope() as s:
        q = _wiki_query(s, game, category, status)
        total = q.count()
        pages = max(1, (total + WIKI_PAGE_SIZE - 1) // WIKI_PAGE_SIZE)
        page = min(pages, max(1, opt_int(page) or 1))
        rows = [{"page": p, "source": src, "aliases": page_aliases(p),
                 "problem": candidate_problem(p.entity, page_aliases(p)),
                 "reason": REASONS.get(p.review_reason, p.review_reason or "")}
                for p, src in q.order_by(KnowledgePage.id.desc())
                .offset((page - 1) * WIKI_PAGE_SIZE).limit(WIKI_PAGE_SIZE)]
        counts = dict(s.query(KnowledgePage.status, func.count(KnowledgePage.id))
                      .filter(KnowledgePage.enabled.is_(True)).group_by(KnowledgePage.status).all())
        games = sorted({g for g, in s.query(KnowledgeSource.game).distinct() if g})
        categories = sorted({c for c, in s.query(KnowledgePage.category).distinct() if c})
    return {"wiki_rows": rows, "wiki_counts": counts, "wiki_games": games,
            "wiki_categories": categories, "wiki_game": game, "wiki_category": category,
            "wiki_status": status, "wiki_page": page, "wiki_pages": pages,
            "wiki_total": total, "wiki_notice": notice, "wiki_error": error}


def _wiki_response(game="", category="", status="pending", page=1, notice="", error=False):
    return HTMLResponse(templates.get_template("fragments/wiki_review.html").render(
        _wiki_context(game, category, status, page, notice, error)))


@router.get("/wiki/review")
async def wiki_review(wiki_game: str = "", wiki_category: str = "", wiki_status: str = "pending",
                      wiki_page: str = "1", user: str = Depends(require_admin)):
    return _wiki_response(wiki_game, wiki_category, wiki_status, wiki_page)


@router.post("/wiki/bulk-status")
async def wiki_bulk_status(status: str = Form(...), scope: str = Form("selected"),
                           page_ids: list[int] = Form(default=[]), invalid_only: str = Form(""),
                           wiki_game: str = Form(""), wiki_category: str = Form(""),
                           wiki_status: str = Form("pending"), wiki_page: str = Form("1"),
                           user: str = Depends(require_admin)):
    args = (wiki_game, wiki_category, wiki_status, wiki_page)
    if status not in STATUSES or wiki_status not in STATUSES or scope not in ("selected", "filtered"):
        return _wiki_response(*args, notice="无效审核条件", error=True)
    if scope == "selected" and not page_ids:
        return _wiki_response(*args, notice="请先选择页面", error=True)
    done = skipped = 0
    with session_scope() as s:
        q = _wiki_query(s, wiki_game, wiki_category, wiki_status)
        if scope == "selected":
            q = q.filter(KnowledgePage.id.in_(page_ids))
        for p, src in q.all():
            problem = candidate_problem(p.entity, page_aliases(p))
            if invalid_only and not problem:
                continue
            if status in ("active", "pending") and problem:
                skipped += 1
                continue
            review_page(s, p, status)
            done += 1
    if done:
        from ...glossary import touch_version
        touch_version()
    notice = f"已处理 {done} 个页面"
    if skipped:
        notice += f"，{skipped} 个缺少有效中文译名，未启用"
    return _wiki_response(*args, notice=notice)


def _miss_query(s):
    """待复查的漏译。

    三个条件缺一不可：非 SQL NULL、非 JSON 'null' 字面量、非空数组。
    只写 isnot(None) 会把「翻译过但没漏译」的消息全捞出来 —— 概览页那个
    「39 条待复查」就是这么来的，实际一条都没有。
    """
    return (s.query(MonitorMessage)
            .filter(MonitorMessage.glossary_miss.isnot(None),
                    MonitorMessage.glossary_miss.cast(Text).notin_(("null", "[]")),
                    MonitorMessage.miss_reviewed_at.is_(None))
            .order_by(MonitorMessage.published_at.desc()))


def _miss_rows(s, limit: int = MISS_PAGE_SIZE) -> list[dict]:
    rows = []
    for m in _miss_query(s).limit(limit).all():
        rows.append({
            "id": m.id,
            "miss": list(m.glossary_miss or []),
            "hits": list(m.glossary_hits or []),
            "game": m.game_detected,
            "text_raw": m.text_raw or "",
            "text_zh": m.text_zh or "",
            "published_at": m.published_at,
        })
    return rows


@router.get("")
async def page(request: Request, game: str = "", category: str = "",
               status: str = "", page: str = "1",
               tab: str = "terms",
               user: str = Depends(require_admin)):
    """知识库浏览 + 批量导入入口。

    分页而不是一次 500 条：1600 条实体的整表 HTML 是 822 KB（gzip 35 KB），
    回大陆只有 61 KB/s，光这一页就要等半秒以上，是其他页面的八倍。
    """
    tab = tab if tab in dict(knowledge_center.TABS) else "terms"
    page = max(1, opt_int(page) or 1)
    with session_scope() as s:
        games = [r[0] for r in s.query(GlossaryEntry.game).distinct().all()
                 if r[0]]
        cats = [r[0] for r in s.query(GlossaryEntry.category).distinct().all()]

        q = s.query(GlossaryEntry)
        if game:
            q = q.filter(GlossaryEntry.game == game)
        if category:
            q = q.filter(GlossaryEntry.category == category)
        if status:
            q = q.filter(GlossaryEntry.status == status)

        matched = q.count()
        entries = q.order_by(
            GlossaryEntry.game.asc(),
            GlossaryEntry.category.asc(),
            GlossaryEntry.canonical_zh.asc()
        ).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

        # 别名一次查完再按 entry_id 归组。之前是每行一条 SELECT，
        # 一页 500 行就是 500 次往返
        ids = [e.id for e in entries]
        by_entry: dict[int, list] = {i: [] for i in ids}
        if ids:
            for a in (s.query(GlossaryAlias)
                      .filter(GlossaryAlias.entry_id.in_(ids))
                      .order_by(GlossaryAlias.alias_kind.asc(),
                                GlossaryAlias.surface.asc()).all()):
                by_entry[a.entry_id].append(a)
        rows = [{"entry": e, "aliases": by_entry.get(e.id, [])}
                for e in entries]

        counts = dict(s.query(GlossaryEntry.status,
                              func.count(GlossaryEntry.id))
                      .group_by(GlossaryEntry.status).all())
        total = sum(counts.values())
        miss_rows = _miss_rows(s)
        miss_total = _miss_query(s).count()
        wiki_sources = s.query(KnowledgeSource).order_by(KnowledgeSource.game.asc()).all()

        # 三大游戏信息卡片聚合统计
        game_meta = [
            {"key": "genshin", "name": "原神", "coverage": "官方 TextMap + Gachabase 补充"},
            {"key": "hsr", "name": "崩坏:星穹铁道", "coverage": "官方 TextMap + 命途属性 + Gachabase 补充"},
            {"key": "zzz", "name": "绝区零", "coverage": "官方角色 + Gachabase 音擎/邦布/驱动盘"},
        ]
        game_cards = []
        for gm in game_meta:
            name = gm["name"]
            g_total = s.query(GlossaryEntry).filter(GlossaryEntry.game == name).count()
            g_active = s.query(GlossaryEntry).filter(GlossaryEntry.game == name, GlossaryEntry.status == "active").count()
            g_pending = s.query(GlossaryEntry).filter(GlossaryEntry.game == name, GlossaryEntry.status == "pending").count()
            game_cards.append({
                "key": gm["key"],
                "name": name,
                "coverage": gm["coverage"],
                "total": g_total,
                "active": g_active,
                "pending": g_pending,
            })

    pages = max(1, (matched + PAGE_SIZE - 1) // PAGE_SIZE)
    return render(request, "kb.html", {
        "nav_active": "kb",
        "tab": tab, **knowledge_center.context(tab),
        "games": games, "categories": cats,
        "game": game, "category": category, "status": status,
        "rows": rows, "counts": counts, "total": total,
        "game_cards": game_cards,
        "shown": len(rows), "matched": matched,
        "page": page, "pages": pages,
        "miss": miss_rows, "miss_total": miss_total,
        "wiki_sources": wiki_sources,
        **_wiki_context(),
    })


@router.post("/wiki/source")
async def add_wiki_source(game: str = Form(""), url: str = Form(...),
                          categories: str = Form(""),
                          user: str = Depends(require_admin)):
    url = url.strip()
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        return HTMLResponse("<div class='flash err'>Wiki 地址不正确</div>")
    from ...kb.wiki import normalize_api_url
    url = normalize_api_url(url)
    cats = [x.strip() for x in categories.split(",") if x.strip()]
    with session_scope() as s:
        s.add(KnowledgeSource(game=game.strip(), url=url, categories=cats or None))
    return HTMLResponse("<div class='flash ok'>Wiki 来源已保存，默认条目待审核</div>")


@router.post("/wiki/source/{source_id}/sync")
async def sync_wiki_source(source_id: int, user: str = Depends(require_admin)):
    require_worker()
    with session_scope() as s:
        if s.get(KnowledgeSource, source_id) is None:
            return HTMLResponse("<div class='flash err'>来源不存在</div>")
    tid = submit_task("wiki_sync", {"source_id": source_id})
    t = get_task(tid) or {"id": tid}
    v = format_task_view(t)
    v["detail"] = "Wiki 分类抓取已下发，新增条目默认待审核"
    return HTMLResponse(templates.get_template("fragments/task_progress.html").render({"t": v}))


@router.post("/wiki/source/{source_id}/toggle")
async def toggle_wiki_source(source_id: int, user: str = Depends(require_admin)):
    with session_scope() as s:
        src = s.get(KnowledgeSource, source_id)
        if src is None:
            return HTMLResponse("<span class='err'>来源不存在</span>")
        src.enabled = not src.enabled
        label = "已启用" if src.enabled else "已停用"
    from ...glossary import touch_version
    touch_version()
    return HTMLResponse(f"<span class='tag {'ok' if src.enabled else ''}'>{label}</span>")


_GAME_ZH = {"genshin": "原神", "hsr": "崩铁", "zzz": "绝区零"}
_GAME_FULL = {"genshin": "原神", "hsr": "崩坏:星穹铁道", "zzz": "绝区零"}


@router.post("/sync/all")
async def sync_all_kb(request: Request, user: str = Depends(require_admin)):
    """智能一键全量同步三家游戏（官方源 + Gachabase 补充）。"""
    require_worker()
    tid = submit_task("kb_smart_sync", {"game": "all"})
    t = get_task(tid) or {"id": tid}
    v = format_task_view(t)
    v["detail"] = "全量智能同步已下发，正在多阶段抓取并校验..."
    return HTMLResponse(templates.get_template("fragments/task_progress.html").render({"t": v}))


@router.post("/sync/{game}")
async def sync_single_game_kb(game: str, request: Request,
                              user: str = Depends(require_admin)):
    """智能同步单个游戏（官方源 + Gachabase 补充）。"""
    require_worker()
    if game not in ("genshin", "hsr", "zzz"):
        return HTMLResponse("<div class='flash err'>未知游戏</div>")
    tid = submit_task("kb_smart_sync", {"game": game})
    t = get_task(tid) or {"id": tid}
    v = format_task_view(t)
    name = _GAME_ZH.get(game, game)
    v["detail"] = f"正在智能同步 {name} 官方与补充数据..."
    return HTMLResponse(templates.get_template("fragments/task_progress.html").render({"t": v}))


@router.post("/approve-all")
async def approve_all_pending(request: Request, game: str = Form(""),
                              user: str = Depends(require_admin)):
    """一键批量通过全部待审条目（支持按游戏或全部）。"""
    with session_scope() as s:
        q = s.query(GlossaryEntry).filter(GlossaryEntry.status == "pending")
        if game:
            q = q.filter(GlossaryEntry.game == game)
        n = sum(review_entry(s, e, "active") for e in q.all())
    logger.info("批量通过 %d 条待审条目 (game=%s)", n, game or "all")
    from ...glossary import touch_version
    touch_version()
    scope = f"{game} 的 " if game else ""
    return HTMLResponse(f"<div class='flash ok'>已启用 {scope}{n} 条有效待审条目</div>", headers={"HX-Refresh": "true"})


@router.post("/import/{game}")
async def import_game(game: str, request: Request,
                      user: str = Depends(require_admin)):
    """高级选项：单导官方数据。"""
    if game not in _GAME_ZH:
        return HTMLResponse("<div class='flash err'>未知游戏</div>")
    require_worker()
    payload: dict = {"game": game}
    cats = [c.strip() for c in
            (request.query_params.get("categories") or "").split(",")
            if c.strip()]
    if cats:
        payload["categories"] = cats
    tid = submit_task("kb_import", payload)
    t = get_task(tid) or {"id": tid}
    v = format_task_view(t)
    v["detail"] = f"正在导入 {_GAME_ZH[game]} 官方数据..."
    return HTMLResponse(templates.get_template("fragments/task_progress.html").render({"t": v}))


@router.post("/import-gachabase/{game}")
async def import_gachabase_route(game: str, request: Request,
                                 user: str = Depends(require_admin)):
    """高级选项：单导 gachabase 补充数据。"""
    if game not in _GAME_FULL:
        return HTMLResponse("<div class='flash err'>未知游戏</div>")
    require_worker()
    tid = submit_task("kb_import_gachabase", {"game": _GAME_FULL[game]})
    t = get_task(tid) or {"id": tid}
    v = format_task_view(t)
    v["detail"] = f"正在抓取 {_GAME_ZH[game]} gachabase 补充数据..."
    return HTMLResponse(templates.get_template("fragments/task_progress.html").render({"t": v}))


@router.post("/changelog-poll")
async def changelog_poll(request: Request, game: str = Form(""),
                         user: str = Depends(require_admin)):
    """立刻检测一次数据版本变更。"""
    full = _GAME_FULL.get(game, "") if game else ""
    if game and not full:
        return HTMLResponse("<div class='flash err'>未知游戏</div>")
    require_worker()
    tid = submit_task("changelog_poll", {"game": full})
    t = get_task(tid) or {"id": tid}
    v = format_task_view(t)
    who = _GAME_ZH.get(game, "三个游戏")
    v["detail"] = f"正在检查 {who} 的数据版本更新（gachabase）..."
    return HTMLResponse(templates.get_template("fragments/task_progress.html").render({"t": v}))
    who = _GAME_ZH.get(game, "三个游戏")
    return HTMLResponse(
        f"<div class='flash ok'>{who}的 changelog 抓取已下发。"
        f"新 revision 会作为消息落进「gachabase 数据更新」频道，"
        f"去<a href='/messages'>消息浏览</a>看</div>")


@router.post("/entry/{entry_id}/status")
async def update_status(entry_id: int, status: str = Form(...),
                       user: str = Depends(require_admin)):
    """审核通过 / 拒绝 / 禁用。允许改回 pending —— 点错了要能退回去。"""
    if status not in ("active", "rejected", "disabled", "pending"):
        return HTMLResponse("<div class='flash err'>无效状态</div>")
    with session_scope() as s:
        e = s.get(GlossaryEntry, entry_id)
        if e is None:
            return HTMLResponse("<div class='flash err'>条目不存在</div>")
        page_ids = [x.id for x in s.query(KnowledgePage).filter(
            KnowledgePage.entity == e.canonical_zh).all()]
        if not review_entry(s, e, status):
            return HTMLResponse("<div class='flash err'>缺少有效中文译名，未启用</div>")
    from ...glossary import touch_version
    touch_version()
    from ...kb.service import sync_page_index
    for page_id in page_ids:
        sync_page_index(page_id)
    return HTMLResponse(f"<div class='flash ok'>已更新为 {status}</div>", headers={"HX-Refresh": "true"})


@router.post("/wiki/page/{page_id}/status")
async def wiki_page_status(page_id: int, status: str = Form(...),
                           canonical_zh: str | None = Form(None),
                           wiki_game: str = Form(""), wiki_category: str = Form(""),
                           wiki_status: str = Form("pending"), wiki_page: str = Form("1"),
                           user: str = Depends(require_admin)):
    """Review a Wiki page and apply its staged entity aliases on approval."""
    args = (wiki_game, wiki_category, wiki_status, wiki_page)
    try:
        with session_scope() as s:
            page = s.get(KnowledgePage, page_id)
            if page is None:
                raise ValueError("Wiki 页面不存在")
            review_page(s, page, status, canonical_zh)
    except ValueError as exc:
        return _wiki_response(*args, notice=str(exc), error=True)
    from ...glossary import touch_version
    touch_version()
    from ...kb.service import sync_page_index
    sync_page_index(page_id)
    return _wiki_response(*args, notice="已处理 1 个页面")


@router.post("/miss/{message_id}/clear")
async def miss_clear(message_id: int, user: str = Depends(require_admin)):
    """标记这条漏译已复查。

    不清空 `glossary_miss` —— 那是模型行为的证据，删了就再也查不到
    「哪些术语模型爱不听话」。只打一个时间戳把它移出队列。
    """
    with session_scope() as s:
        m = s.get(MonitorMessage, message_id)
        if m is None:
            return HTMLResponse("<tr><td colspan='6' class='dim'>消息不存在</td></tr>")
        m.miss_reviewed_at = datetime.utcnow()
    return HTMLResponse("")          # hx-swap=outerHTML，空串即把整行删掉


@router.post("/miss/clear-all")
async def miss_clear_all(user: str = Depends(require_admin)):
    with session_scope() as s:
        n = 0
        for m in _miss_query(s).all():
            m.miss_reviewed_at = datetime.utcnow()
            n += 1
    return HTMLResponse(
        f"<div class='flash ok'>已把 {n} 条标记为已复查，刷新页面生效</div>")


@router.post("/quick-add")
async def quick_add(surface: str = Form(...), canonical_zh: str = Form(...),
                    game: str = Form(""), user: str = Depends(require_admin)):
    """从漏译队列一键录入。

    这是唯一能让知识库持续变准的闭环：复查漏译时顺手补一条，
    比另开一个页面手打要现实得多。手工录入直接 active —— 是人当场判断的结果。
    """
    surface, canonical_zh = surface.strip(), canonical_zh.strip()
    game = game.strip()
    if not surface or not canonical_zh:
        return HTMLResponse("<span class='err'>原文和译法都不能为空</span>")

    # 单字汉字这类写法两种匹配模式都不可靠，录进去只会在别的词里误命中。
    # 这里直接拦下并说明原因 —— 比默默存一条永远不生效（或到处乱命中）的行好
    bad = unreliable_alias_reason(surface)
    if bad:
        return HTMLResponse(f"<span class='err'>「{surface}」不能作为术语：{bad}</span>")

    lang = "zh" if has_cjk(surface) else "en"
    with session_scope() as s:
        e = (s.query(GlossaryEntry)
             .filter(GlossaryEntry.game == game,
                     GlossaryEntry.canonical_zh == canonical_zh).first())
        if e is None:
            e = GlossaryEntry(game=game, category="jargon",
                              canonical_zh=canonical_zh, status="active",
                              origin="miss", origin_ref=surface,
                              reviewed_at=datetime.utcnow())
            s.add(e)
            s.flush()
        dup = (s.query(GlossaryAlias)
               .filter(GlossaryAlias.entry_id == e.id,
                       GlossaryAlias.surface == surface,
                       GlossaryAlias.lang == lang).first())
        if dup is not None:
            return HTMLResponse(
                f"<span class='warn'>「{surface}」已经在知识库里了</span>")
        s.add(GlossaryAlias(entry_id=e.id, surface=surface, lang=lang,
                            alias_kind="primary",
                            match_mode=alias_match_mode(surface)))
    from ...glossary import touch_version
    touch_version()
    return HTMLResponse(
        f"<span class='ok'>已录入：{surface} → {canonical_zh}，立即生效</span>")


@router.post("/quick-add-pending")
async def quick_add_pending(surface: str = Form(...), canonical_zh: str = Form(""),
                            game: str = Form(""), category: str = Form("other"),
                            user: str = Depends(require_admin)):
    """Create a reviewable term from a translation miss without guessing a name."""
    surface = surface.strip()
    canonical_zh = canonical_zh.strip() or surface
    if not surface:
        return HTMLResponse("<span class='err'>原文不能为空</span>")
    with session_scope() as s:
        existing = s.query(GlossaryEntry).filter_by(game=game.strip(), canonical_zh=canonical_zh).first()
        if existing:
            return HTMLResponse("<span class='dim'>条目已存在，请到知识库审核或补充别名</span>")
        e = GlossaryEntry(game=game.strip(), category=category[:24], canonical_zh=canonical_zh,
                          status="pending", origin="miss", origin_ref=surface[:120])
        s.add(e); s.flush()
        s.add(GlossaryAlias(entry_id=e.id, surface=surface, lang="zh" if has_cjk(surface) else "en",
                            alias_kind="primary", match_mode=alias_match_mode(surface),
                            enabled=unreliable_alias_reason(surface) is None))
    return HTMLResponse("<span class='ok'>已创建待审核条目</span>")


@router.post("/bulk-status")
async def bulk_status(game: str = Form(""), category: str = Form(""),
                     from_status: str = Form("pending"),
                     to_status: str = Form("active"),
                     user: str = Depends(require_admin)):
    """按当前筛选条件批量改状态。

    导入一次 456 条，逐条点是不可能的 —— 审核必须能按类别整批过，
    否则「导入结果一律待审」这条规矩会因为太麻烦而被绕过。
    """
    if to_status not in ("active", "rejected", "disabled", "pending"):
        return HTMLResponse("<div class='flash err'>无效状态</div>")
    with session_scope() as s:
        q = s.query(GlossaryEntry).filter(GlossaryEntry.status == from_status)
        if game:
            q = q.filter(GlossaryEntry.game == game)
        if category:
            q = q.filter(GlossaryEntry.category == category)
        n = sum(review_entry(s, e, to_status) for e in q.all())
    from ...glossary import touch_version
    touch_version()
    return HTMLResponse(
        f"<div class='flash ok'>已把 {n} 条从 {from_status} 改为 {to_status}"
        f"</div>", headers={"HX-Refresh": "true"})


@router.post("/entry/{entry_id}/delete")
async def delete_entry(entry_id: int, user: str = Depends(require_admin)):
    """删除条目（连同它的所有别名）。"""
    with session_scope() as s:
        e = s.get(GlossaryEntry, entry_id)
        if e is None:
            return HTMLResponse("<div class='flash err'>条目不存在</div>")
        # 外键级联删除别名
        s.delete(e)
    from ...glossary import touch_version
    touch_version()
    return HTMLResponse("<div class='flash ok'>已删除</div>")


@router.post("/changelog-poll")
async def changelog_poll_route(user: str = Depends(require_admin)):
    """手动抓一次 changelog（三个游戏）。

    定时轮询已经挂在 worker 的 maintenance_loop 里，每 20 分钟跑一次。
    这个按钮的用途是「刚开启想立即看到内容」或「站点改版后测正则还灵不灵」。
    """
    from ...models import Task
    with session_scope() as s:
        s.add(Task(kind="changelog_poll", payload={}))
    return HTMLResponse(
        "<div class='flash ok'>已提交，几秒后刷新频道页或消息列表</div>")
