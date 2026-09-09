"""worker 主循环。独占 user session。

状态机：
  need_credentials  DB 里还没有 API_ID/API_HASH → 只跑心跳，等你在后台填
  need_login        有凭据但 session 未授权 → 保持连接，等 send_code/sign_in 任务
  online            已授权 → 注册监听、消费队列
  paused            WORKER_PAUSED=true → 断开连接（换凭据时用）
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import Integer, cast, func
from telethon import TelegramClient, events

from .. import pipeline, settings
from ..db import session_scope
from ..models import Channel, MonitorMessage, Task, WorkerState
from ..paths import SESSIONS_DIR, USER_SESSION
from ..util import log_event
from . import tasks as task_mod

logger = logging.getLogger(__name__)

ALBUM_DEBOUNCE = 2.5     # 相册消息逐条到达，等这么久再当一条处理
HEARTBEAT_EVERY = 10.0
WATCHLIST_EVERY = 15.0
TASK_POLL = 1.0
INGEST_WORKERS = 2
CATCHUP_FIRST_DELAY = 20.0   # 上线后先等监听稳定再首轮对账


class WorkerRunner:
    def __init__(self) -> None:
        self.client: TelegramClient | None = None
        self.status = "booting"
        self.detail: str | None = None
        self.tg_user: str | None = None
        self._cred_fingerprint: tuple | None = None
        self._restart_token: str = ""
        self._exit = asyncio.Event()
        self._handler_registered = False
        self.watchlist: dict[str, int] = {}
        self.ingest_q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._albums: dict[str, list] = {}
        self._album_timers: dict[str, asyncio.Task] = {}
        # 登录流程的中间状态，只在内存里
        self.login_phone: str | None = None
        self.login_hash: str | None = None
        # 对账循环的唤醒开关（后台「立即对齐」按钮经任务队列置位）。
        # _catchup_only：非 None 时只对齐该频道
        self._catchup_flag = asyncio.Event()
        self._catchup_only: int | None = None
        self.started_at = datetime.utcnow()

    # ---------------- 生命周期 ----------------

    def _creds(self) -> tuple[str, str, str]:
        return (str(settings.get("API_ID") or "").strip(),
                str(settings.get("API_HASH") or "").strip(),
                str(settings.get("PHONE_NUMBER") or "").strip())

    async def ensure_client(self) -> None:
        """按需建立 / 重建 Telethon 客户端。凭据变了就重建。"""
        if settings.get("WORKER_PAUSED"):
            if self.client is not None:
                await self._teardown_client("已按 WORKER_PAUSED 断开")
            self._set_status("paused", "已暂停连接（换凭据 / 登录流程中）")
            return

        api_id, api_hash, _phone = self._creds()
        if not api_id or not api_hash:
            if self.client is not None:
                await self._teardown_client("凭据被清空")
            self._set_status("need_credentials",
                             "还没填 API_ID / API_HASH。去后台「账号与凭据」页填")
            return

        fp = (api_id, api_hash)
        if self.client is not None and fp != self._cred_fingerprint:
            await self._teardown_client("API_ID/API_HASH 变了，重建客户端")

        if self.client is None:
            try:
                self.client = TelegramClient(
                    str(SESSIONS_DIR / "user"), int(api_id), api_hash,
                    device_model="tgmon", system_version="linux",
                    app_version="tgmon 0.1", connection_retries=None,
                    retry_delay=5, request_retries=5,
                )
            except ValueError:
                self._set_status("error", "API_ID 必须是数字")
                self.client = None
                return
            self._cred_fingerprint = fp

        if not self.client.is_connected():
            try:
                await self.client.connect()
            except Exception as e:
                self._set_status("error", f"连接 Telegram 失败: {e}")
                logger.warning("连接失败: %s", e)
                return

        try:
            authorized = await self.client.is_user_authorized()
        except Exception as e:
            self._set_status("error", f"检查授权状态失败: {e}")
            return

        if not authorized:
            self._handler_registered = False
            self._set_status("need_login",
                             "已连上 Telegram，但还没登录。去后台点「发送验证码」")
            return

        if not self._handler_registered:
            await self.after_login()

    async def after_login(self) -> None:
        """登录成功（或重启后发现已授权）后注册监听。"""
        if self.client is None:
            return
        try:
            me = await self.client.get_me()
            self.tg_user = task_mod._me_name(me)
        except Exception:
            self.tg_user = None
        if not self._handler_registered:
            self.client.add_event_handler(self._on_new_message, events.NewMessage())
            self.client.add_event_handler(self._on_new_message, events.MessageEdited())
            self._handler_registered = True
            logger.info("已注册消息监听器")
        await self.refresh_watchlist()
        self._set_status("online", f"监听 {len(self.watchlist)} 个频道")
        log_event("info", "worker", f"已上线: {self.tg_user}")

    async def _teardown_client(self, why: str) -> None:
        logger.info("断开 Telethon: %s", why)
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self._handler_registered = False
        self._cred_fingerprint = None

    async def require_client(self) -> TelegramClient:
        """任务处理器用：拿到已连接（不一定已授权）的客户端。"""
        if settings.get("WORKER_PAUSED"):
            settings.set_many({"WORKER_PAUSED": False})
        await self.ensure_client()
        if self.client is None or not self.client.is_connected():
            raise RuntimeError("Telegram 客户端未连接。检查 API_ID / API_HASH 是否填对")
        return self.client

    async def require_authorized_client(self) -> TelegramClient:
        client = await self.require_client()
        if not await client.is_user_authorized():
            raise RuntimeError("还没登录。先在「账号与凭据」页完成登录")
        return client

    async def drop_session(self) -> None:
        await self._teardown_client("登出")
        for p in (USER_SESSION, USER_SESSION.with_suffix(".session-journal")):
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("删除 session 文件失败 %s: %s", p, e)
        self.tg_user = None
        self._set_status("need_login", "已登出")

    def request_exit(self, why: str) -> None:
        logger.info("准备退出: %s（compose 会自动重启）", why)
        self._exit.set()

    # ---------------- 状态与心跳 ----------------

    def _set_status(self, status: str, detail: str | None = None) -> None:
        if status != self.status or detail != self.detail:
            logger.info("状态 %s → %s (%s)", self.status, status, detail or "")
        self.status = status
        self.detail = detail

    async def _heartbeat_loop(self) -> None:
        while not self._exit.is_set():
            try:
                with session_scope() as s:
                    row = s.get(WorkerState, 1)
                    if row is None:
                        row = WorkerState(id=1)
                        s.add(row)
                    row.heartbeat_at = datetime.utcnow()
                    row.status = self.status
                    row.detail = self.detail
                    row.tg_user = self.tg_user
                    from ..models import ProcessingJob
                    row.queue_depth = s.query(ProcessingJob).filter(
                        ProcessingJob.status.in_(("pending", "retry", "running"))).count()
                    row.started_at = self.started_at
            except Exception as e:
                logger.debug("写心跳失败: %s", e)
            await asyncio.sleep(HEARTBEAT_EVERY)

    async def refresh_watchlist(self) -> None:
        try:
            with session_scope() as s:
                rows = (s.query(Channel.id, Channel.tg_id)
                        .filter(Channel.enabled.is_(True),
                                Channel.source_type == "telegram",
                                Channel.tg_id.isnot(None))
                        .all())
            self.watchlist = {str(tg): int(cid) for cid, tg in rows}
        except Exception as e:
            logger.warning("刷新监听列表失败: %s", e)

    async def _watchlist_loop(self) -> None:
        while not self._exit.is_set():
            await asyncio.sleep(WATCHLIST_EVERY)
            before = set(self.watchlist)
            await self.refresh_watchlist()
            if set(self.watchlist) != before and self.status == "online":
                self._set_status("online", f"监听 {len(self.watchlist)} 个频道")

    # ---------------- 消息接收 ----------------

    async def _on_new_message(self, event) -> None:
        try:
            chat_key = str(event.chat_id)
            cid = self.watchlist.get(chat_key)
            if cid is None:
                return
            gid = getattr(event.message, "grouped_id", None)
            from ..jobs import save_events
            await asyncio.to_thread(save_events, cid, [event.message],
                                    delay=ALBUM_DEBOUNCE if gid else 0)
        except Exception as e:
            logger.exception("接收消息出错: %s", e)

    async def _flush_album_later(self, key: str, cid: int) -> None:
        try:
            await asyncio.sleep(ALBUM_DEBOUNCE)
        except asyncio.CancelledError:
            return
        msgs = self._albums.pop(key, [])
        self._album_timers.pop(key, None)
        if msgs:
            self._enqueue(cid, msgs)

    def _enqueue(self, cid: int, msgs: list) -> None:
        from ..jobs import save_events
        save_events(cid, msgs)

    async def _processing_loop(self, queue: str) -> None:
        from .. import jobs
        from .processing import HANDLERS, RetryLater
        while not self._exit.is_set():
            try:
                job = await asyncio.to_thread(jobs.claim, queue)
            except Exception:
                logger.exception("领取 %s 队列失败", queue)
                await asyncio.sleep(2)
                continue
            if not job:
                await asyncio.sleep(0.5)
                continue
            heartbeat = asyncio.create_task(jobs.heartbeat(job))
            try:
                result = await HANDLERS[queue](self, job["payload"])
                await asyncio.to_thread(jobs.finish, job, result)
            except RetryLater as exc:
                await asyncio.to_thread(jobs.finish, job, error=str(exc), retry_after=exc.seconds)
            except Exception as e:
                logger.exception("%s 任务失败: %s", queue, e)
                await asyncio.to_thread(jobs.finish, job, error=str(e),
                    retry_after=min(3600, 30 * job["attempts"]) if job["attempts"] < 10 else None)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    # ---------------- 任务队列 ----------------

    async def _task_loop(self) -> None:
        while not self._exit.is_set():
            claimed = None
            try:
                claimed = task_mod.claim_next()
            except Exception as e:
                logger.debug("取任务失败: %s", e)
            if claimed is None:
                await asyncio.sleep(TASK_POLL)
                continue
            tid, kind, payload = claimed
            logger.info("执行任务 #%s %s", tid, kind)
            try:
                result = await task_mod.handle(self, tid, kind, payload)
                task_mod.finish(tid, result, None)
            except Exception as e:
                logger.warning("任务 #%s %s 失败: %s", tid, kind, e)
                task_mod.finish(tid, None, f"{type(e).__name__}: {e}")

    def request_catchup(self, only_cid: int | None = None) -> None:
        """后台「立即对齐」按钮：唤醒对齐循环，不等下一个间隔。

        only_cid 非空 → 只对齐该频道（单频道按钮）。
        存实例字段而不是传参：flag 是无参 Event，循环侧自行取走。
        """
        self._catchup_only = only_cid
        self._catchup_flag.set()

    async def _catchup_loop(self) -> None:
        """断线补拉：定期按时间窗口对齐每个启用频道。

        实时监听只覆盖 worker 在线的时段 —— 重启/断线窗口的消息 Telegram
        不会重发，这个循环是唯一能补回来的机制（也是「监听间隔」的实现）。
        """
        await asyncio.sleep(CATCHUP_FIRST_DELAY)
        while not self._exit.is_set():
            interval = max(60, int(settings.get("CATCHUP_INTERVAL") or 300))
            try:
                await asyncio.wait_for(self._catchup_flag.wait(),
                                       timeout=interval)
            except asyncio.TimeoutError:
                pass
            self._catchup_flag.clear()
            only = self._catchup_only
            self._catchup_only = None
            if self._exit.is_set():
                return
            if not settings.get("CATCHUP_ENABLED") and only is None:
                continue
            if self.client is None or self.status != "online":
                continue          # 掉线/未登录时跳过本轮，等下个间隔
            try:
                await self._run_catchup_round(only_cid=only)
            except Exception as e:
                logger.warning("对齐循环出错: %s", e)

    async def _run_catchup_round(self, only_cid: int | None = None) -> None:
        """Reconcile recent edits and page through the whole recorded outage."""
        items = list(self.watchlist.items())
        if only_cid is not None:
            items = [(tg, cid) for tg, cid in items if cid == only_cid]
        if not items:
            return
        window_days = max(1, int(settings.get("ALIGN_WINDOW_DAYS") or 3))
        # 单轮单频道上限：防一轮补几千条把 worker 占死（相册分批逐条 AI）
        max_gap = max(1, int(settings.get("CATCHUP_MAX_GAP") or 50))
        min_date = datetime.utcnow() - timedelta(days=window_days)

        # 进度 Task：概览页活动任务直接可见。
        # 直接以 running 创建 —— 任务轮询器只领 pending，不这样写它会把
        # catchup_round 当未知类型领走报错
        with session_scope() as s:
            t = Task(kind="catchup_round", status="running",
                     payload={"channels": len(items), "only": only_cid})
            s.add(t)
            s.flush()
            tid = t.id

        def _progress(ch_idx: int, title: str, detail: str) -> None:
            """进度回写 Task.result：汇总 + 分频道明细一起，一个写入点。

            之前 _progress 和收尾各自写 result，后写的会盖掉先写的字段
            （percent/detail 丢失）—— 统一成全量覆盖，谁写都带全字段。
            """
            pct = int(ch_idx / len(items) * 100) if items else 0
            task_mod._set_action(
                f"对齐 {title}（{ch_idx}/{len(items)}）{detail}")
            _write_task_result(
                {"done": ch_idx, "total": len(items),
                 "aligned": aligned, "skipped": skipped,
                 "current": title, "percent": pct, "detail": detail,
                 "channels": chan_detail})

        def _write_task_result(payload: dict, final: bool = False) -> None:
            try:
                with session_scope() as s:
                    row = s.get(Task, tid)
                    if row is None:
                        return
                    if final:
                        row.status = "done"
                        row.finished_at = datetime.utcnow()
                    elif row.status not in ("pending", "running"):
                        return
                    row.result = payload
            except Exception:
                pass

        done_idx, aligned, skipped = 0, 0, 0
        # 分频道明细：频道页「上轮对齐」列与概览页健康度都读这个。
        # {cid: {"title","status","gap","ingested","error"}} —— status:
        # aligned(补了)/synced(已同步)/empty(空频道)/error
        chan_detail: dict[int, dict] = {}

        def _chan(cid, title, status, *, gap=None, ingested=None, error=None):
            chan_detail[cid] = {"title": title, "status": status,
                                "gap": gap, "ingested": ingested, "error": error}

        def _flush_result(final: bool = False) -> None:
            """当前汇总 + 分频道明细回写（percent 字段沿用最近一次进度）。"""
            _write_task_result(
                {"done": done_idx, "total": len(items),
                 "aligned": aligned, "skipped": skipped,
                 "channels": chan_detail}, final=final)

        try:
            for tg_id, cid in items:
                if self._exit.is_set():
                    break
                done_idx += 1
                try:
                    entity = await self.client.get_entity(int(tg_id))
                    latest = await self.client.get_messages(entity, limit=1)
                    latest_id = latest[0].id if latest else None
                    with session_scope() as s:
                        ch = s.get(Channel, cid)
                        if ch is None or not ch.enabled:
                            continue
                        title = ch.title
                        previous_sync = ch.last_catchup_at or ch.last_message_at
                        channel_cutoff = min(min_date, previous_sync - timedelta(hours=1)) if previous_sync else min_date
                        # tg_message_id 是 VARCHAR：字典序会让 '999' > '1000'，
                        # 必须 CAST 成整数再取 max
                        db_max = (s.query(
                            func.max(cast(MonitorMessage.tg_message_id, Integer)))
                            .filter(MonitorMessage.channel_id == cid).scalar())
                    if latest_id is None:
                        skipped += 1
                        _chan(cid, title, "empty")
                        _progress(done_idx, title, "空频道")
                        _flush_result()
                        continue
                    gap = latest_id - (db_max or 0)
                    # 窗口内补拉：limit 封顶防单轮过载，
                    # min_date 让 iter 在碰到窗口外老消息时停下
                    n = max_gap
                    _progress(done_idx, title, f"补 {gap} 条（拉 {n}）")
                    logger.info("对齐：%s 落后 %d 条，补窗口内最多 %d 条",
                                title, gap, n)
                    r = await task_mod._iter_and_ingest(
                        self.client, cid, int(tg_id), limit=n,
                        min_date=channel_cutoff, persistent_cursor=True)
                    # Advance the cursor after the fetch and ingest complete;
                    # a failed or interrupted batch remains visible next round.
                    with session_scope() as s:
                        ch = s.get(Channel, cid)
                        if ch is not None:
                            ch.last_tg_id = latest_id
                            if r.get("complete"):
                                ch.last_catchup_at = datetime.utcnow()
                        from ..models import SourceCursor
                        cursor = s.get(SourceCursor, cid)
                        if cursor is not None:
                            cursor.state = {"gap_after": db_max or 0, "gap_through": latest_id,
                                            "complete": bool(r.get("complete"))}
                    aligned += 1
                    ingested = int(r.get("ingested", 0) or 0)
                    _chan(cid, title, "aligned", gap=gap, ingested=ingested)
                    _progress(done_idx, title,
                              f"入库 {ingested} 条")
                    _flush_result()
                    if not r.get("complete"):
                        log_event("info", "catchup",
                                  f"{title} 已保存分页游标 {r.get('offset_id')}，下轮继续补拉")
                except Exception as e:
                    logger.warning("对齐频道 %s 失败: %s", tg_id, e)
                    try:
                        with session_scope() as s:
                            ch = s.get(Channel, cid)
                            _chan(cid, ch.title if ch else str(tg_id), "error",
                                  error=f"{type(e).__name__}: {e}"[:120])
                    except Exception:
                        pass
                    _flush_result()
            # 收尾：Task 标记完成（终值含分频道明细）
            _flush_result(final=True)
        finally:
            task_mod._set_action("")

    # ---------------- 配置监视 ----------------

    async def _config_loop(self) -> None:
        while not self._exit.is_set():
            try:
                token = str(settings.get("WORKER_RESTART_TOKEN") or "")
                if not self._restart_token:
                    self._restart_token = token
                elif token != self._restart_token:
                    self.request_exit("后台请求重启")
                    return
                await self.ensure_client()
            except Exception as e:
                logger.warning("配置巡检出错: %s", e)
            await asyncio.sleep(5.0)

    # ---------------- 入口 ----------------

    async def run(self) -> None:
        from .maintenance import maintenance_loop
        from .. import extension_runtime
        await self.ensure_client()
        jobs = [
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._watchlist_loop()),
            asyncio.create_task(self._task_loop()),
            asyncio.create_task(self._config_loop()),
            asyncio.create_task(self._catchup_loop()),
            asyncio.create_task(maintenance_loop(self._exit)),
            asyncio.create_task(extension_runtime.poll_sources()),
        ]
        for queue in ("ingest", "source", "media", "media", "archive", "translate", "translate", "publish"):
            jobs.append(asyncio.create_task(self._processing_loop(queue)))
        try:
            await self._exit.wait()
        finally:
            for j in jobs:
                j.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await extension_runtime.close()
            await self._teardown_client("退出")
