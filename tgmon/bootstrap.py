"""建表 / 种子数据 / 建后台账号。admin 与 worker 启动时都调用，幂等。"""
from __future__ import annotations

import csv
import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path

from .crypto import hash_password
from .db import engine, session_scope
from .models import (
    AdminUser, Base, GlossaryAlias, GlossaryEntry, Glossary, PromptTemplate,
    WorkerState,
)
from .prompts import DEFAULT_GLOBAL_PROMPT
from .settings import seed_from_env
from .util import (
    alias_match_mode, has_cjk, log_event, unreliable_alias_reason,
)

logger = logging.getLogger(__name__)


def init_all() -> None:
    from filelock import FileLock
    from .paths import DB_PATH
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(DB_PATH.parent / "migration.lock"), timeout=180):
        _init_all()


def _init_all() -> None:
    from .unified_migration import backup_before_upgrade
    backup_before_upgrade()
    Base.metadata.create_all(engine)
    _ensure_columns()
    _ensure_retrieval_fts()
    _fix_json_null_literals()
    seed_from_env()
    _seed_prompt()
    _upgrade_global_prompt()
    _seed_worker_row()
    _seed_admin()
    _migrate_glossary()
    _seed_jargon()
    _seed_verified_terms()
    _seed_wiki_sources()
    _disable_unreliable_aliases()
    from .kb.wiki_review import repair_invalid_wiki
    repaired = repair_invalid_wiki()
    if any(repaired.values()):
        logger.info("Wiki 无效候选已拒绝: %s", repaired)
    from .unified_migration import migrate
    migrate()
    _migrate_qq_bots()
    from .general_migration import migrate as migrate_general
    migrate_general()


def _migrate_qq_bots() -> None:
    """多机器人迁移（幂等）：旧配置 QQ_APP_ID / QQ_APP_SECRET 导入 qq_bot 首行。

    存量部署只有一对凭据 —— 导入后所有 bot_id 为 NULL 的群都归属它，
    单机器人行为零变化。表已有行或旧配置为空则跳过。
    """
    from .crypto import decrypt, encrypt
    from .models import AppSetting, QqBot, QqGroup

    with session_scope() as s:
        if s.query(QqBot.id).first() is not None:
            return
        row = s.query(AppSetting).filter_by(key="QQ_APP_ID").first()
        secret_row = s.query(AppSetting).filter_by(key="QQ_APP_SECRET").first()
        app_id = (row.value or "").strip() if row is not None else ""
        secret = ""
        if secret_row is not None and secret_row.value:
            try:
                secret = decrypt(secret_row.value)
            except Exception:
                secret = secret_row.value
        if not app_id or not secret:
            return
        bot = QqBot(app_id=app_id, nickname="默认机器人",
                    app_secret_enc=encrypt(secret))
        s.add(bot)
        s.flush()
        n = (s.query(QqGroup)
             .filter(QqGroup.bot_id.is_(None))
             .update({"bot_id": bot.id}, synchronize_session=False))
        logger.info("QQ 凭据已迁移到多机器人表（app_id=%s，归属群 %d 个）",
                    app_id, n)


def _ensure_retrieval_fts() -> None:
    """Create the optional FTS5 table; old SQLite builds simply use LIKE."""
    from sqlalchemy import text as sql_text
    with engine.begin() as conn:
        conn.execute(sql_text(
            "CREATE TRIGGER IF NOT EXISTS trg_message_index_delete "
            "AFTER DELETE ON monitor_message BEGIN "
            "DELETE FROM retrieval_index WHERE ref_type='message' AND ref_id=OLD.id; END"))
        conn.execute(sql_text(
            "CREATE TRIGGER IF NOT EXISTS trg_message_index_update "
            "AFTER UPDATE OF text_raw,text_zh,entities,topics,game_detected,version_tag,channel_id "
            "ON monitor_message BEGIN DELETE FROM retrieval_index "
            "WHERE ref_type='message' AND ref_id=NEW.id; END"))
        try:
            conn.execute(sql_text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_fts USING fts5(" \
                "ref_type UNINDEXED, ref_id UNINDEXED, text, title, channel, game)"))
            conn.execute(sql_text(
                "CREATE TRIGGER IF NOT EXISTS trg_retrieval_delete "
                "AFTER DELETE ON retrieval_index BEGIN "
                "DELETE FROM retrieval_fts WHERE ref_type=OLD.ref_type AND ref_id=CAST(OLD.ref_id AS TEXT); END"))
        except Exception as e:
            logger.info("SQLite 不支持 FTS5，检索将回退 LIKE: %s", e)


# 新增列。create_all 只建新表，不会给已有表加列，而项目里没有 alembic ——
# SQLite 的 ALTER TABLE ADD COLUMN 足够，为这点改动引入迁移框架不值得。
# 格式：表名 -> [(列名, SQL 类型与默认值)]
_NEW_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "source_cursor": [("state", "TEXT")],
    "member_profile": [("scope_data", "TEXT"),
                       ("confirmation", "VARCHAR(20) NOT NULL DEFAULT 'pending'")],
    "processing_job": [("scope_key", "VARCHAR(160)")],
    "knowledge_source": [
        ("trusted", "BOOLEAN NOT NULL DEFAULT 0"),
        ("kind", "VARCHAR(24) NOT NULL DEFAULT 'wiki'"),
    ],
    "conversation": [
        ("context_state", "TEXT"), ("revision", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "conversation_turn": [
        ("source_event_id", "VARCHAR(160)"), ("reply_to", "VARCHAR(160)"),
        ("actor_id", "VARCHAR(80)"), ("event_key", "VARCHAR(160)"),
        # 记忆 v2：说话人昵称快照
        ("speaker", "VARCHAR(80)"),
    ],
    "channel": [
        ("theme", "VARCHAR(64) NOT NULL DEFAULT 'gaming'"),
        ("bilingual_policy", "VARCHAR(20) NOT NULL DEFAULT 'zh_first'"),
        ("games", "TEXT"),             # JSON 在 SQLite 里就是 TEXT
        ("video_max_mb", "INTEGER NOT NULL DEFAULT 0"),
        ("last_tg_id", "INTEGER"),
        ("last_catchup_at", "DATETIME"),
    ],
    "monitor_message": [
        ("theme", "VARCHAR(64) NOT NULL DEFAULT 'gaming'"),
        ("lang_detected", "VARCHAR(8)"),
        ("text_dropped", "TEXT"),
        ("entities", "TEXT"),          # JSON 在 SQLite 里就是 TEXT
        ("game_detected", "VARCHAR(80)"),
        ("game_scores", "TEXT"),
        ("topics", "TEXT"),
        ("version_tag", "VARCHAR(40)"),
        ("spoiler_ranges", "TEXT"),
        ("has_spoiler", "BOOLEAN NOT NULL DEFAULT 0"),
        ("miss_reviewed_at", "DATETIME"),
    ],
    "message_media": [
        ("video_bytes", "INTEGER NOT NULL DEFAULT 0"),
        ("video_status", "VARCHAR(20)"),
        ("has_spoiler", "BOOLEAN NOT NULL DEFAULT 0"),
        ("source_tg_id", "VARCHAR(80)"), ("source_identity", "VARCHAR(160)"),
        ("status", "VARCHAR(24) NOT NULL DEFAULT 'ready'"), ("error", "TEXT"),
        ("sha256", "VARCHAR(64)"), ("original_path", "VARCHAR(300)"),
        ("original_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "admin_user": [
        ("role", "VARCHAR(10) NOT NULL DEFAULT 'admin'"),
    ],
    "worker_state": [
        ("current_action", "VARCHAR(200) DEFAULT ''"),
    ],
    "qq_group": [
        ("themes", "TEXT"),
        ("games", "TEXT"), ("cursors", "TEXT"),
        # /new 未读增量的群维度已读游标（2026-09 QQ 命令升级）
        ("last_seen_msg_id", "INTEGER"),
        # 多机器人：群归属（qq_bot.id）。NULL = 迁移前存量，仅单机器人时推送
        ("bot_id", "INTEGER"),
    ],
    "share_token": [
        ("snapshot_items", "TEXT"),
        # 2026-09 分享升级：token 可逆加密存储（刷新后 URL 可再显示）
        # + 合并分享（一个链接看多条消息）
        ("token_enc", "TEXT"),
        ("message_ids", "TEXT"),       # JSON 数组，null = 单条
        # 2026-09 一图流分享：一句话概括（人工输入 / AI 生成）
        ("summary", "TEXT"),
        # 2026-09 分享去重：消息 id 集合内容指纹，同内容复用缓存
        ("content_key", "VARCHAR(64)"),
    ],
    "knowledge_page": [
        ("document_status", "VARCHAR(12) NOT NULL DEFAULT 'pending'"),
        ("aliases", "TEXT"),
        ("missing_count", "INTEGER NOT NULL DEFAULT 0"),
        ("review_reason", "VARCHAR(80)"),
        ("reviewed_at", "DATETIME"),
    ],
    "retrieval_index": [
        ("channel_id", "INTEGER"),
        ("version", "VARCHAR(40)"),
        ("embedding_model", "VARCHAR(120)"),
    ],
    "qq_delivery": [
        ("bot_id", "INTEGER"), ("source", "VARCHAR(40) NOT NULL DEFAULT 'legacy'"),
        ("trigger", "VARCHAR(80) NOT NULL DEFAULT 'push'"),
        ("bundle_id", "INTEGER"), ("dedup_key", "VARCHAR(80)"), ("parts", "TEXT"),
    ],
    "qq_event": [("reply_deadline", "DATETIME")],
}


def _ensure_columns() -> None:
    """幂等补列。已存在的列跳过，所以可以反复调用。"""
    from sqlalchemy import text as sql_text

    with engine.begin() as conn:
        for table, cols in _NEW_COLUMNS.items():
            existing = {
                r[1] for r in conn.execute(sql_text(f"PRAGMA table_info({table})"))
            }
            if not existing:
                continue          # 表还不存在，create_all 会带着新列一起建
            for name, ddl in cols:
                if name in existing:
                    continue
                conn.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                logger.info("已给 %s 加列 %s", table, name)


def _fix_json_null_literals() -> None:
    """把 JSON 列里的 'null' 字面量改成真正的 SQL NULL。

    裸 `Column(JSON)` 会把 Python None 存成 JSON null（DB 里是字符串 'null'），
    于是 `col.isnot(None)` 对每一行都成立。模型已改用 JSONNull(none_as_null=True)，
    但**存量数据还是坏的** —— 建表时的类型不影响已经写进去的值。

    幂等：跑完之后 'null' 一条不剩，再跑就是 0 行受影响。
    """
    from sqlalchemy import text as sql_text

    targets = {
        "monitor_message": ["glossary_hits", "glossary_miss", "entities",
                            "game_scores", "topics", "spoiler_ranges"],
        "channel": ["games"],
        "glossary_entry": ["attrs"],
        "ai_provider": ["extra_headers"],
        "rss_feed": ["channel_ids", "games", "keywords"],
        "webhook": ["channel_ids"],
        "task": ["payload", "result"],
    }
    fixed = 0
    with engine.begin() as conn:
        for table, cols in targets.items():
            existing = {r[1] for r in
                        conn.execute(sql_text(f"PRAGMA table_info({table})"))}
            for col in cols:
                if col not in existing:
                    continue
                r = conn.execute(sql_text(
                    f"UPDATE {table} SET {col} = NULL WHERE {col} = 'null'"))
                if r.rowcount:
                    fixed += r.rowcount
                    logger.info("%s.%s 清掉 %d 个 JSON null 字面量",
                                table, col, r.rowcount)
    if fixed:
        logger.warning("共修正 %d 处 JSON null 字面量", fixed)


def _seed_prompt() -> None:
    with session_scope() as s:
        row = (s.query(PromptTemplate)
               .filter(PromptTemplate.scope == "global")
               .first())
        if row is None:
            s.add(PromptTemplate(scope="global", scope_key="",
                                 body=DEFAULT_GLOBAL_PROMPT))
            logger.info("已写入默认全局 prompt")


# 历史上发布过的默认 prompt 的 sha256。DB 里那行只要还等于其中之一，
# 就说明没人手工改过，可以安全升级到新默认值。
#
# 为什么需要这个：_seed_prompt 只在「没有 global 行」时插入，所以改
# DEFAULT_GLOBAL_PROMPT 常量对已经跑起来的实例**完全无效** —— 线上读的
# 永远是首次启动那天写进去的那份。和 settings 的 DEFAULTS 是同型的坑
# （改代码默认值 ≠ 线上生效，settings.get() 以 DB 为准）。
_SHIPPED_PROMPT_SHA = {
    "bfacc67b026066dbd4ce3c3c5d4e2cf2732495ed3504bd506165ee3379b82a29",
    # 首个版本（commit 5428dd5，287 字符，无多游戏说明、无「」专名规则）
    "673f4d54f9859daf6336453c8e5e849c35c3e2533e71ee329972068ffaeccc50",
}


def _upgrade_global_prompt() -> None:
    """把未被人改过的全局 prompt 升级到当前默认值。改过的一律不动。"""
    cur_sha = hashlib.sha256(DEFAULT_GLOBAL_PROMPT.encode()).hexdigest()
    with session_scope() as s:
        row = (s.query(PromptTemplate)
               .filter(PromptTemplate.scope == "global").first())
        if row is None:
            return
        sha = hashlib.sha256((row.body or "").encode()).hexdigest()
        if sha == cur_sha:
            return                                  # 已经是最新默认值
        if sha in _SHIPPED_PROMPT_SHA:
            row.body = DEFAULT_GLOBAL_PROMPT
            row.updated_at = datetime.utcnow()
            logger.warning("全局 prompt 是旧的出厂默认值且未被修改，已升级到新版本")
        else:
            logger.warning(
                "全局 prompt 被手工改过，不自动覆盖。新增的条款（专名用「」括起来、"
                "多游戏术语体系区分）不会生效 —— 需要的话去 /prompts 页面自己加")


def _disable_unreliable_aliases() -> int:
    """停用匹配不可靠的别名（单字汉字）。幂等，每次启动都跑。

    为什么必须清理存量：早期导入没有这道闸，线上留下了 9 条 —— ZZZ 的「本」
    （Ben Bigger 截断而来）用子串匹配会命中「版本」，实测让一条原神消息判成了
    绝区零；「照」命中「照旧」。它们同时也会被注入翻译对照表变成「本 → 本」。

    只停用别名不删实体：魈 还有 Xiao、本 还有 Ben，英文写法走词边界是可靠的。
    """
    n = 0
    with session_scope() as s:
        for a in s.query(GlossaryAlias).filter(GlossaryAlias.enabled.is_(True)).all():
            why = unreliable_alias_reason(a.surface)
            if why:
                a.enabled = False
                n += 1
                logger.info("停用别名 %r（entry %s）：%s", a.surface, a.entry_id, why)
    if n:
        logger.warning("已停用 %d 条匹配不可靠的别名", n)
    return n


def _seed_worker_row() -> None:
    with session_scope() as s:
        if s.get(WorkerState, 1) is None:
            s.add(WorkerState(id=1, status="booting"))


def _seed_admin() -> None:
    """用 TGMON_ADMIN_PASSWORD 建初始账号。已有账号则不动。"""
    with session_scope() as s:
        if s.query(AdminUser).count() > 0:
            return
        pwd = (os.getenv("TGMON_ADMIN_PASSWORD") or "").strip()
        if not pwd:
            logger.warning(
                "还没有后台账号，且 .env 里没有 TGMON_ADMIN_PASSWORD。"
                "填上它并重启 admin 容器即可建号。")
            return
        s.add(AdminUser(username="admin", password_hash=hash_password(pwd)))
        logger.info("已创建后台账号 admin。建议随后清空 .env 里的 TGMON_ADMIN_PASSWORD")


def _migrate_glossary() -> None:
    """把旧 glossary 表搬到 GlossaryEntry + GlossaryAlias。

    旧表一行一个 surface form，新表一个实体多个 surface form。搬迁时每行老数据
    建一个 entry + 一个 primary 别名。旧表不删，留作后路。
    """
    with session_scope() as s:
        if s.query(GlossaryEntry).count() > 0:
            return                     # 已经搬过或已有新数据，不重复搬
        old = s.query(Glossary).all()
        if not old:
            return

        moved = 0
        for row in old:
            game = row.game or ""
            zh = (row.target_term or "").strip()
            src = (row.source_term or "").strip()
            if not zh or not src:
                continue
            # 同一 (game, canonical_zh) 已建过就只挂别名 —— 旧表里多个外号
            # 指向同一译法的情况，正好在这里合并成一个实体
            entry = (s.query(GlossaryEntry)
                     .filter(GlossaryEntry.game == game,
                             GlossaryEntry.canonical_zh == zh).first())
            if entry is None:
                entry = GlossaryEntry(
                    game=game, category="other", canonical_zh=zh,
                    note=row.note, status="active", origin="manual",
                    enabled=row.enabled, hit_count=row.hit_count or 0,
                )
                s.add(entry)
                s.flush()
            exists = (s.query(GlossaryAlias)
                      .filter(GlossaryAlias.entry_id == entry.id,
                              GlossaryAlias.surface == src).first())
            if exists is None:
                s.add(GlossaryAlias(
                    entry_id=entry.id, surface=src,
                    lang="zh" if has_cjk(src) else "en",
                    alias_kind="primary",
                    match_mode=alias_match_mode(src),
                    case_sensitive=row.case_sensitive,
                ))
                moved += 1

    if moved:
        logger.info("已把旧术语表 %d 条搬到知识库", moved)
        log_event("info", "glossary", f"旧术语表搬迁完成：{moved} 条别名")


def _seed_jargon() -> None:
    """导入随仓库走的行话种子表。

    这些词官方数据里一条都没有（卡池/后台/精通/深测/自机），但爆料原文里到处都是。
    直接 active —— 是手工校对过的，不走待审。
    """
    path = Path(__file__).with_name("kb") / "seed" / "jargon.csv"
    if not path.exists():
        return

    with session_scope() as s:
        if s.query(GlossaryEntry).filter(
                GlossaryEntry.category == "jargon").count() > 0:
            return                     # 已导入过

        added = 0
        with path.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                zh = (row.get("canonical_zh") or "").strip()
                if not zh:
                    continue
                game = (row.get("game") or "").strip()
                entry = GlossaryEntry(
                    game=game, category="jargon", canonical_zh=zh,
                    note=(row.get("note") or "").strip() or None,
                    status="active", origin="manual",
                )
                s.add(entry)
                s.flush()
                # aliases 列用 | 分隔多个英文写法：EM|Elemental Mastery
                cs = row.get("case_sensitive", "0").strip() == "1"
                for surface in (row.get("aliases") or "").split("|"):
                    surface = surface.strip()
                    if not surface:
                        continue
                    s.add(GlossaryAlias(
                        entry_id=entry.id, surface=surface,
                        lang="zh" if has_cjk(surface) else "en",
                        alias_kind="primary",
                        match_mode=alias_match_mode(surface),
                        case_sensitive=cs,
                    ))
                added += 1

    if added:
        logger.info("已导入行话种子 %d 条", added)


def _seed_verified_terms() -> int:
    """Seed narrowly reviewed test-server names without overwriting corrections.

    This file is separate from broad upstream imports: these names came from
    the concrete HSR beta example, while the unconfirmed workshop brand stays
    provisional in translation output.
    """
    path = Path(__file__).with_name("kb") / "seed" / "verified_terms.csv"
    if not path.exists():
        return 0
    from .kb.importer import upsert_entity
    added = 0
    with session_scope() as s, path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            game = (row.get("game") or "").strip()
            canonical = (row.get("canonical_zh") or "").strip()
            if not game or not canonical:
                continue
            aliases = [(surface.strip(), "en", "primary")
                       for surface in (row.get("aliases") or "").split("|")
                       if surface.strip()]
            new_entries, _ = upsert_entity(
                s, game=game, category=(row.get("category") or "other").strip(),
                canonical_zh=canonical, aliases=aliases,
                origin=(row.get("origin") or "verified:manual").strip(),
                origin_ref=(row.get("origin_ref") or "").strip() or None,
                attrs={"verified_example": "hsr-beta", "review_note": row.get("note") or "",
                       "references": [value for value in (row.get("origin_ref"), row.get("source_cn"),
                           "https://t.me/s/sakurahaven?before=3083") if value]}, status="active",
                merge_aliases=True)
            added += new_entries
    if added:
        logger.info("已导入已确认测试服专名 %d 条", added)
    return added


def _seed_wiki_sources() -> None:
    """Seed public Fandom endpoints for all supported games once."""
    try:
        from .kb.wiki import DEFAULT_SOURCES
        from .models import KnowledgeSource
        with session_scope() as s:
            for game, url, categories in DEFAULT_SOURCES:
                row = (s.query(KnowledgeSource)
                       .filter(KnowledgeSource.game == game,
                               KnowledgeSource.url == url).first())
                if row is None:
                    s.add(KnowledgeSource(game=game, url=url,
                                          categories=categories, enabled=True))
                    logger.info("已登记 %s Wiki 来源", game)
    except Exception as e:
        logger.warning("登记 Wiki 来源失败: %s", e)
