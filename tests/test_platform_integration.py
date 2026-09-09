"""Cross-module regressions for durable edits, extensions and model scheduling."""
import asyncio
import json
import sys
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from tgmon import jobs, member_profile, pipeline, settings, source_adapters
from tgmon.conversation_scope import activate
from tgmon.db import engine, session_scope
from tgmon.models import (Base, Channel, ContentRevision, MemberProfile, MessageMedia,
                          MonitorMessage, ProcessingJob, SourceCursor, SourceEvent)


@pytest.fixture(autouse=True)
def database():
    Base.metadata.create_all(engine)
    keys = ("RETRIEVAL_ENABLED", "MCP_ENABLED", "MCP_SERVERS", "MCP_BOT_TOOLS")
    old = {key: settings.get(key) for key in keys}
    settings.set_many({"RETRIEVAL_ENABLED": False})
    yield
    settings.set_many(old)
    with session_scope() as s:
        ids = [row.id for row in s.query(Channel).filter(Channel.title == "Platform test")]
        mids = [row.id for row in s.query(MonitorMessage).filter(MonitorMessage.channel_id.in_(ids))]
        s.query(ProcessingJob).delete()
        s.query(SourceEvent).filter(SourceEvent.channel_id.in_(ids)).delete(synchronize_session=False)
        s.query(SourceCursor).filter(SourceCursor.channel_id.in_(ids)).delete(synchronize_session=False)
        s.query(ContentRevision).filter(ContentRevision.message_id.in_(mids)).delete(synchronize_session=False)
        s.query(MessageMedia).filter(MessageMedia.message_id.in_(mids)).delete(synchronize_session=False)
        s.query(MonitorMessage).filter(MonitorMessage.id.in_(mids)).delete(synchronize_session=False)
        s.query(Channel).filter(Channel.id.in_(ids)).delete(synchronize_session=False)


@pytest.mark.asyncio
async def test_edit_replaces_current_media_but_preserves_source_versions():
    with session_scope() as s:
        channel = Channel(tg_id="platform-test", title="Platform test", theme="generic", translate=False, enabled=True)
        s.add(channel)
        s.flush()
        cid = channel.id
    message = SimpleNamespace(id=1, message="First edition", date=datetime.utcnow(),
        grouped_id=None, media=True, photo=SimpleNamespace(id=100), entities=[])
    mid = await pipeline.ingest(None, cid, [message], deferred=True, complete_snapshot=True)
    with session_scope() as s:
        before = s.query(ContentRevision).filter_by(message_id=mid).count()
    message.message, message.photo.id = "Updated edition", 101
    await pipeline.ingest(None, cid, [message], deferred=True, complete_snapshot=True)
    await pipeline.ingest(None, cid, [message], deferred=True, complete_snapshot=True)
    from tgmon.message_query import by_ids
    assert by_ids([mid])[0]["raw"] == "Updated edition"
    assert len(by_ids([mid])[0]["media"]) == 1
    with session_scope() as s:
        assert s.query(MessageMedia).filter_by(message_id=mid).count() == 2
        assert s.query(MessageMedia).filter_by(message_id=mid, status="superseded").count() == 1
        assert s.query(ContentRevision).filter_by(message_id=mid).count() == before + 1


@pytest.mark.asyncio
async def test_generic_source_replay_and_edit_use_existing_content_pipeline():
    cid = source_adapters.channel_for("test", {"id": "science", "title": "Platform test",
                                               "theme": "generic", "translate": False})
    page = {"events": [{"source_id": "article-1", "text": "Experiment 42",
                        "deeplink": "https://example.test/science"}], "cursor": "page-2"}
    source_adapters.save_page(cid, page)
    source_adapters.save_page(cid, page)
    job = jobs.claim("source")
    result = await source_adapters.ingest(None, job["payload"])
    jobs.finish(job, result)
    assert jobs.claim("source") is None
    page["events"][0]["text"] = "Experiment 43"
    source_adapters.save_page(cid, page)
    await source_adapters.ingest(None, jobs.claim("source")["payload"])
    from tgmon.outputs import serialize
    value = serialize(result["message_id"])
    assert value["theme"] == "generic" and value["text_raw"] == "Experiment 43"
    assert value["deeplink"] == "https://example.test/science"
    assert source_adapters.checkpoint(cid) == "page-2"


@pytest.mark.asyncio
async def test_processing_ingest_commits_source_links_before_return(monkeypatch):
    from types import SimpleNamespace
    from tgmon.worker import processing

    with session_scope() as s:
        channel = Channel(tg_id="processing-test", title="Platform test", theme="generic", enabled=True)
        s.add(channel)
        s.flush()
        source = SourceEvent(channel_id=channel.id, source_id="123456",
                             revision="r1", snapshot={"id": "123456",
                         "text": "Processing event", "grouped_id": "",
                         "media_id": "", "date": "", "edited": "",
                         "entities": []})
        s.add(source)
        s.flush()
        channel_id, event_id = channel.id, source.id

    with session_scope() as s:
        message = MonitorMessage(channel_id=channel_id, tg_message_id="123456",
                                 text_raw="Processing event", text_zh="Processing event",
                                 theme="generic", translate_status="skipped")
        s.add(message)
        s.flush()
        message_id = message.id

    async def fake_ingest(*args, **kwargs):
        return message_id

    monkeypatch.setattr(processing.pipeline, "ingest", fake_ingest)
    runner = SimpleNamespace(client=object(), status="online")
    await processing.ingest(runner, {"event_id": event_id})

    with session_scope() as s:
        assert s.get(SourceEvent, event_id).message_id == message_id
        job = s.query(ProcessingJob).filter_by(queue="publish").one()
        assert job.payload == {"message_id": message_id}


def test_correction_keeps_evidence_and_scoped_history():
    with activate({"app_id": "platform-test"}, "room", "person", "source-1"):
        member_profile.add_fact("person", "我喜欢红色")
        assert member_profile.revise_fact("person", "红色", "我喜欢绿色")
        profile = member_profile.get_profile("person")
        assert profile["facts"] == ["我喜欢绿色"]
        from tgmon.conversation_scope import profile_key
        with session_scope() as s:
            row = s.query(MemberProfile).filter_by(member_openid=profile_key("person")).one()
            fact = next(iter(row.facts.values()))
            assert fact["revision"] == 2 and fact["history"][0]["text"] == "我喜欢红色"
            assert row.scope_data["bot"] == "platform-test"


@pytest.mark.asyncio
async def test_chat_has_priority_over_waiting_background_calls():
    from tgmon.providers.registry import PriorityLimiter
    limiter, order = PriorityLimiter(1), []
    release = asyncio.Event()

    async def call(name, priority, hold=False):
        async with limiter.slot(priority):
            order.append(name)
            if hold:
                await release.wait()

    first = asyncio.create_task(call("running", 10, True))
    await asyncio.sleep(0)
    background = asyncio.create_task(call("translate", 10))
    await asyncio.sleep(0)
    chat = asyncio.create_task(call("chat", 0))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, background, chat)
    assert order == ["running", "chat", "translate"]


@pytest.mark.asyncio
async def test_official_mcp_stdio_tool_and_revoked_authorization(tmp_path):
    from tgmon import mcp_bridge
    script = tmp_path / "mcp_fixture.py"
    script.write_text("from mcp.server.fastmcp import FastMCP\n"
        "server = FastMCP('fixture')\n"
        "@server.tool()\n"
        "def add(a: int, b: int) -> int: return a + b\n"
        "server.run(transport='stdio')\n", encoding="utf-8")
    settings.set_many({"MCP_ENABLED": True,
        "MCP_SERVERS": [{"name": "local", "transport": "stdio", "command": sys.executable,
                         "args": [str(script)], "enabled": True, "timeout": 10}],
        "MCP_BOT_TOOLS": {"platform-test": ["mcp:local:add"]}})
    with activate({"app_id": "platform-test"}, "room", "person"):
        tools = await mcp_bridge.available_tools()
        assert tools[0]["name"] == "mcp:local:add"
        result = await mcp_bridge.call("mcp:local:add", {"a": 2, "b": 3})
        assert result["content"][0]["text"] == "5"
        settings.set_many({"MCP_ENABLED": False})
        assert await mcp_bridge.available_tools() == []
        with pytest.raises(PermissionError):
            await mcp_bridge.call("mcp:local:add", {"a": 2, "b": 3})


def test_hung_plugin_is_terminated_without_holding_core(tmp_path):
    from tgmon.plugins import PluginManager
    source = tmp_path / "package"
    source.mkdir()
    (source / "plugin.json").write_text(json.dumps({"name": "hang", "entrypoint": "main.py"}))
    (source / "main.py").write_text("import time\ntime.sleep(60)\n")
    host = PluginManager(tmp_path / "installed")
    host.install(source)
    host.configure("hang", enabled=True)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            host.request("hang", "tool", {}, timeout=0.15)
        assert time.monotonic() - started < 3
        assert host.status("hang")["status"] == "error"
    finally:
        host.close()


@pytest.mark.asyncio
async def test_official_mcp_http_and_disconnected_server():
    import socket
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    from tgmon import mcp_bridge
    sdk = FastMCP("http-fixture", stateless_http=True)

    @sdk.tool()
    def echo(text: str) -> str:
        return text

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(sdk.streamable_http_app(), log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    config = {"name": "http", "transport": "streamable_http",
              "url": f"http://127.0.0.1:{port}/mcp", "timeout": 3}
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        assert (await mcp_bridge.discover(config))[0]["name"] == "echo"
        async with mcp_bridge.session_for(config) as session:
            result = await session.call_tool("echo", {"text": "confirmed"})
            assert result.content[0].text == "confirmed"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()
    with pytest.raises(Exception):
        async with mcp_bridge.session_for(config):
            pass


@pytest.mark.asyncio
async def test_interrupted_download_resumes_and_publishes_only_complete_file(tmp_path):
    from tgmon.media import _download_source
    data = b"x" * (512 * 1024) + b"y" * 500
    offsets = []

    class Client:
        fail = True

        async def iter_download(self, document, offset, request_size):
            offsets.append(offset)
            for pos in range(offset, len(data), request_size):
                yield data[pos:pos + request_size]
                if self.fail:
                    self.fail = False
                    raise ConnectionError("interrupted")

    client, destination = Client(), tmp_path / "original.mp4"
    message = SimpleNamespace(document=object())
    with pytest.raises(ConnectionError):
        await _download_source(client, message, destination, len(data))
    assert not destination.exists()
    await _download_source(client, message, destination, len(data))
    assert offsets == [0, 512 * 1024] and destination.read_bytes() == data
    assert not destination.with_suffix(".mp4.partial").exists()


@pytest.mark.asyncio
async def test_full_disk_preserves_waiting_video_and_does_not_download(tmp_path, monkeypatch):
    from tgmon import media
    monkeypatch.setattr(media, "VIDEO_DIR", tmp_path)
    monkeypatch.setattr(media, "_has_space", lambda size: False)
    old = {k: settings.get(k) for k in ("VIDEO_ARCHIVE_ENABLED", "VIDEO_MAX_MB")}
    settings.set_many({"VIDEO_ARCHIVE_ENABLED": True, "VIDEO_MAX_MB": 2000})
    output = media.MediaOut(kind="video", orig_bytes=1024 ** 3)
    try:
        await media._archive_video(None, SimpleNamespace(id=1, document=SimpleNamespace(id=1)), 1, output, 0)
        assert output.status == "waiting_space" and not output.video_path
    finally:
        settings.set_many(old)


def test_crashed_conversation_can_resume_generation_but_never_resend_unknown_part():
    from tgmon.qqbot import events
    from tgmon.qqbot.runtime import recover_conversation
    from tgmon.models import QqEvent
    payload = {"event": {"t": "GROUP_AT_MESSAGE_CREATE", "d": {
        "group_openid": "recovery-group", "id": "recovery-event"}}, "app_id": "recovery-bot"}
    key, fresh, _ = events.claim("group", "recovery-group", "recovery-event", "recovery-bot")
    assert fresh
    recover_conversation(payload)
    assert events.claim("group", "recovery-group", "recovery-event", "recovery-bot")[1]
    with session_scope() as s:
        row = s.get(QqEvent, key)
        row.status, row.parts = "delivering", {"1": {"status": "sending"}}
    recover_conversation(payload)
    assert events.delivery_parts(key)["1"]["status"] == "unknown"
    assert not events.begin_delivery(key, 1)


def test_broadcast_is_existing_push_only_without_daily_digest_scheduler():
    from tgmon import settings
    from tgmon.worker import maintenance

    assert settings.get("QQ_DAILY_DIGEST_ENABLED") is None
    assert not hasattr(maintenance, "daily_digest")
