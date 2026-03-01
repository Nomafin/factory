"""Tests for the agent message board feature."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import ASGITransport, AsyncClient

from factory.db import Database
from factory.deps import get_db, get_orchestrator
from factory.main import app
from factory.models import (
    Message, MessageCreate, MessageType, TaskStatus,
)
from factory.orchestrator import Orchestrator


# ── Database tests ─────────────────────────────────────────────────────


async def test_create_message():
    db = Database(":memory:")
    await db.initialize()

    msg = await db.create_message(MessageCreate(
        sender="orchestrator",
        message="Task started",
        message_type=MessageType.STATUS,
    ))

    assert msg.id is not None
    assert msg.sender == "orchestrator"
    assert msg.message == "Task started"
    assert msg.message_type == MessageType.STATUS
    assert msg.recipient is None
    assert msg.task_id is None
    assert msg.workflow_id is None
    assert msg.reply_to is None
    assert msg.created_at is not None

    await db.close()


async def test_create_message_with_all_fields():
    db = Database(":memory:")
    await db.initialize()

    msg = await db.create_message(MessageCreate(
        sender="task-1",
        recipient="reviewer",
        task_id=1,
        workflow_id=2,
        message="Please review this code",
        message_type=MessageType.HANDOFF,
        reply_to=None,
    ))

    assert msg.sender == "task-1"
    assert msg.recipient == "reviewer"
    assert msg.task_id == 1
    assert msg.workflow_id == 2
    assert msg.message_type == MessageType.HANDOFF

    await db.close()


async def test_get_message():
    db = Database(":memory:")
    await db.initialize()

    created = await db.create_message(MessageCreate(
        sender="coder",
        message="Hello world",
    ))

    fetched = await db.get_message(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.sender == "coder"
    assert fetched.message == "Hello world"

    await db.close()


async def test_get_message_not_found():
    db = Database(":memory:")
    await db.initialize()

    msg = await db.get_message(999)
    assert msg is None

    await db.close()


async def test_list_messages():
    db = Database(":memory:")
    await db.initialize()

    await db.create_message(MessageCreate(sender="a", message="msg1"))
    await db.create_message(MessageCreate(sender="b", message="msg2"))
    await db.create_message(MessageCreate(sender="c", message="msg3"))

    messages = await db.list_messages()
    assert len(messages) == 3
    # Should be ordered by created_at DESC
    assert messages[0].sender == "c"
    assert messages[2].sender == "a"

    await db.close()


async def test_list_messages_filter_by_sender():
    db = Database(":memory:")
    await db.initialize()

    await db.create_message(MessageCreate(sender="coder", message="m1"))
    await db.create_message(MessageCreate(sender="reviewer", message="m2"))
    await db.create_message(MessageCreate(sender="coder", message="m3"))

    messages = await db.list_messages(sender="coder")
    assert len(messages) == 2
    assert all(m.sender == "coder" for m in messages)

    await db.close()


async def test_list_messages_filter_by_task_id():
    db = Database(":memory:")
    await db.initialize()

    await db.create_message(MessageCreate(sender="a", message="m1", task_id=1))
    await db.create_message(MessageCreate(sender="b", message="m2", task_id=2))
    await db.create_message(MessageCreate(sender="c", message="m3", task_id=1))

    messages = await db.list_messages(task_id=1)
    assert len(messages) == 2
    assert all(m.task_id == 1 for m in messages)

    await db.close()


async def test_list_messages_filter_by_workflow_id():
    db = Database(":memory:")
    await db.initialize()

    await db.create_message(MessageCreate(sender="a", message="m1", workflow_id=5))
    await db.create_message(MessageCreate(sender="b", message="m2", workflow_id=6))

    messages = await db.list_messages(workflow_id=5)
    assert len(messages) == 1
    assert messages[0].workflow_id == 5

    await db.close()


async def test_list_messages_filter_by_type():
    db = Database(":memory:")
    await db.initialize()

    await db.create_message(MessageCreate(
        sender="a", message="m1", message_type=MessageType.STATUS,
    ))
    await db.create_message(MessageCreate(
        sender="b", message="m2", message_type=MessageType.ERROR,
    ))
    await db.create_message(MessageCreate(
        sender="c", message="m3", message_type=MessageType.STATUS,
    ))

    messages = await db.list_messages(message_type="status")
    assert len(messages) == 2

    await db.close()


async def test_list_messages_with_limit_and_offset():
    db = Database(":memory:")
    await db.initialize()

    for i in range(10):
        await db.create_message(MessageCreate(sender=f"s{i}", message=f"msg{i}"))

    messages = await db.list_messages(limit=3, offset=0)
    assert len(messages) == 3

    messages2 = await db.list_messages(limit=3, offset=3)
    assert len(messages2) == 3
    assert messages[0].id != messages2[0].id

    await db.close()


async def test_search_messages():
    db = Database(":memory:")
    await db.initialize()

    await db.create_message(MessageCreate(sender="a", message="Login bug detected"))
    await db.create_message(MessageCreate(sender="b", message="Code review passed"))
    await db.create_message(MessageCreate(sender="c", message="Another login issue"))

    results = await db.search_messages("login")
    assert len(results) == 2

    await db.close()


async def test_get_thread():
    db = Database(":memory:")
    await db.initialize()

    parent = await db.create_message(MessageCreate(
        sender="coder", message="Need help with auth module",
    ))
    reply1 = await db.create_message(MessageCreate(
        sender="reviewer", message="Check the middleware",
        reply_to=parent.id,
    ))
    reply2 = await db.create_message(MessageCreate(
        sender="coder", message="Found it, thanks!",
        reply_to=parent.id,
    ))

    # Unrelated message
    await db.create_message(MessageCreate(
        sender="devops", message="Deploying now",
    ))

    thread = await db.get_thread(parent.id)
    assert len(thread) == 3
    assert thread[0].id == parent.id
    assert thread[1].id == reply1.id
    assert thread[2].id == reply2.id

    await db.close()


async def test_get_thread_not_found():
    db = Database(":memory:")
    await db.initialize()

    thread = await db.get_thread(999)
    assert thread == []

    await db.close()


async def test_message_type_enum():
    assert MessageType.INFO == "info"
    assert MessageType.QUESTION == "question"
    assert MessageType.HANDOFF == "handoff"
    assert MessageType.STATUS == "status"
    assert MessageType.ERROR == "error"


# ── API endpoint tests ─────────────────────────────────────────────────


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def mock_orchestrator(db):
    orch = MagicMock(spec=Orchestrator)
    orch.cancel_task = AsyncMock()
    orch.process_task = AsyncMock(return_value=True)
    orch.forward_message_to_telegram = AsyncMock()
    orch.runner = MagicMock()
    orch.runner.get_running_agents.return_value = {}
    orch.plane = None
    orch.config = MagicMock()
    orch.config.plane.default_repo = "factory"
    return orch


@pytest.fixture
async def client(db, mock_orchestrator):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_api_create_message(client):
    resp = await client.post("/api/messages", json={
        "sender": "orchestrator",
        "message": "Task started",
        "message_type": "status",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["sender"] == "orchestrator"
    assert data["message"] == "Task started"
    assert data["message_type"] == "status"
    assert data["id"] is not None


async def test_api_create_message_with_recipient(client):
    resp = await client.post("/api/messages", json={
        "sender": "coder",
        "recipient": "reviewer",
        "message": "Please check this",
        "message_type": "handoff",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["recipient"] == "reviewer"


async def test_api_create_message_with_task_id(client):
    resp = await client.post("/api/messages", json={
        "sender": "task-5",
        "message": "Working on feature",
        "task_id": 5,
        "message_type": "info",
    })
    assert resp.status_code == 201
    assert resp.json()["task_id"] == 5


async def test_api_create_message_with_reply(client):
    # Create parent
    resp1 = await client.post("/api/messages", json={
        "sender": "coder",
        "message": "Question about auth",
        "message_type": "question",
    })
    parent_id = resp1.json()["id"]

    # Create reply
    resp2 = await client.post("/api/messages", json={
        "sender": "reviewer",
        "message": "Use JWT",
        "message_type": "info",
        "reply_to": parent_id,
    })
    assert resp2.status_code == 201
    assert resp2.json()["reply_to"] == parent_id


async def test_api_list_messages(client):
    await client.post("/api/messages", json={"sender": "a", "message": "m1"})
    await client.post("/api/messages", json={"sender": "b", "message": "m2"})

    resp = await client.get("/api/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


async def test_api_list_messages_filter_type(client):
    await client.post("/api/messages", json={
        "sender": "a", "message": "m1", "message_type": "status",
    })
    await client.post("/api/messages", json={
        "sender": "b", "message": "m2", "message_type": "error",
    })

    resp = await client.get("/api/messages?message_type=error")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["message_type"] == "error"


async def test_api_list_messages_filter_sender(client):
    await client.post("/api/messages", json={"sender": "coder", "message": "m1"})
    await client.post("/api/messages", json={"sender": "reviewer", "message": "m2"})

    resp = await client.get("/api/messages?sender=coder")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["sender"] == "coder"


async def test_api_list_messages_filter_task(client):
    await client.post("/api/messages", json={
        "sender": "a", "message": "m1", "task_id": 1,
    })
    await client.post("/api/messages", json={
        "sender": "b", "message": "m2", "task_id": 2,
    })

    resp = await client.get("/api/messages?task_id=1")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["task_id"] == 1


async def test_api_list_messages_filter_workflow(client):
    await client.post("/api/messages", json={
        "sender": "a", "message": "m1", "workflow_id": 10,
    })
    await client.post("/api/messages", json={
        "sender": "b", "message": "m2", "workflow_id": 20,
    })

    resp = await client.get("/api/messages?workflow_id=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1


async def test_api_list_messages_search(client):
    await client.post("/api/messages", json={
        "sender": "a", "message": "Login bug found",
    })
    await client.post("/api/messages", json={
        "sender": "b", "message": "Deployment complete",
    })

    resp = await client.get("/api/messages?search=login")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert "Login" in data[0]["message"]


async def test_api_list_messages_with_limit(client):
    for i in range(5):
        await client.post("/api/messages", json={
            "sender": f"s{i}", "message": f"msg{i}",
        })

    resp = await client.get("/api/messages?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_api_get_message(client):
    create_resp = await client.post("/api/messages", json={
        "sender": "coder", "message": "Test message",
    })
    msg_id = create_resp.json()["id"]

    resp = await client.get(f"/api/messages/{msg_id}")
    assert resp.status_code == 200
    assert resp.json()["sender"] == "coder"


async def test_api_get_message_not_found(client):
    resp = await client.get("/api/messages/999")
    assert resp.status_code == 404


async def test_api_get_thread(client):
    parent_resp = await client.post("/api/messages", json={
        "sender": "coder", "message": "Need help",
    })
    parent_id = parent_resp.json()["id"]

    await client.post("/api/messages", json={
        "sender": "reviewer", "message": "Sure, what's up?",
        "reply_to": parent_id,
    })

    resp = await client.get(f"/api/messages/{parent_id}/thread")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


async def test_api_get_thread_not_found(client):
    resp = await client.get("/api/messages/999/thread")
    assert resp.status_code == 404


async def test_api_create_message_forwards_to_telegram(client, mock_orchestrator):
    await client.post("/api/messages", json={
        "sender": "coder",
        "message": "Error in deployment",
        "message_type": "error",
    })
    mock_orchestrator.forward_message_to_telegram.assert_awaited_once()


async def test_api_messages_default_type_is_info(client):
    resp = await client.post("/api/messages", json={
        "sender": "coder",
        "message": "Just a note",
    })
    assert resp.status_code == 201
    assert resp.json()["message_type"] == "info"


# ── Web UI test ────────────────────────────────────────────────────────


async def test_messages_page_redirects_to_dashboard(client):
    resp = await client.get("/messages", follow_redirects=False)
    assert resp.status_code == 302
    assert "/#/messages" in resp.headers["location"]


# ── Orchestrator integration tests ─────────────────────────────────────


async def test_orchestrator_post_message():
    """Test that the orchestrator can post a message to the board."""
    db = Database(":memory:")
    await db.initialize()

    config = MagicMock()
    config.max_concurrent_agents = 1
    config.message_board.enabled = True
    config.message_board.telegram_forward = False
    config.telegram.bot_token = ""
    config.telegram.chat_id = ""
    config.plane.api_key = ""
    config.plane.base_url = ""

    orch = Orchestrator(db=db, config=config)

    with patch("factory.api._message_subscribers", []):
        msg = await orch.post_message(
            sender="orchestrator",
            message="Task 1 started",
            message_type=MessageType.STATUS,
            task_id=1,
        )

    assert msg is not None
    assert msg.sender == "orchestrator"
    assert msg.message == "Task 1 started"
    assert msg.task_id == 1

    # Verify it was stored in DB
    stored = await db.get_message(msg.id)
    assert stored is not None
    assert stored.message == "Task 1 started"

    await db.close()


async def test_orchestrator_post_message_disabled():
    """Test that messages are not posted when message board is disabled."""
    db = Database(":memory:")
    await db.initialize()

    config = MagicMock()
    config.max_concurrent_agents = 1
    config.message_board.enabled = False
    config.telegram.bot_token = ""
    config.telegram.chat_id = ""
    config.plane.api_key = ""
    config.plane.base_url = ""

    orch = Orchestrator(db=db, config=config)

    msg = await orch.post_message(
        sender="orchestrator",
        message="This should not be stored",
    )
    assert msg is None

    messages = await db.list_messages()
    assert len(messages) == 0

    await db.close()


async def test_orchestrator_parse_agent_messages():
    """Test that agent output containing message board posts is parsed."""
    db = Database(":memory:")
    await db.initialize()

    config = MagicMock()
    config.max_concurrent_agents = 1
    config.message_board.enabled = True
    config.message_board.telegram_forward = False
    config.telegram.bot_token = ""
    config.telegram.chat_id = ""
    config.plane.api_key = ""
    config.plane.base_url = ""

    orch = Orchestrator(db=db, config=config)

    # Valid agent message
    content = json.dumps({
        "type": "message",
        "to": "reviewer",
        "content": "Code is ready for review",
        "message_type": "handoff",
    })

    with patch("factory.api._message_subscribers", []):
        result = orch._parse_agent_messages(42, content)

    assert result is True

    await db.close()


async def test_orchestrator_parse_agent_messages_non_message():
    """Test that non-message agent output is ignored."""
    db = Database(":memory:")
    await db.initialize()

    config = MagicMock()
    config.max_concurrent_agents = 1
    config.telegram.bot_token = ""
    config.telegram.chat_id = ""
    config.plane.api_key = ""
    config.plane.base_url = ""

    orch = Orchestrator(db=db, config=config)

    # Regular text output
    result = orch._parse_agent_messages(1, "Just regular text output")
    assert result is False

    # JSON but not a message type
    result = orch._parse_agent_messages(1, json.dumps({"type": "result", "data": "ok"}))
    assert result is False

    await db.close()


async def test_orchestrator_forward_telegram_when_configured():
    """Test message forwarding to Telegram when enabled."""
    db = Database(":memory:")
    await db.initialize()

    config = MagicMock()
    config.max_concurrent_agents = 1
    config.message_board.enabled = True
    config.message_board.telegram_forward = True
    config.message_board.telegram_chat_id = ""
    config.message_board.forward_types = ["error", "question"]
    config.telegram.bot_token = "test-token"
    config.telegram.chat_id = "test-chat"
    config.plane.api_key = ""
    config.plane.base_url = ""

    orch = Orchestrator(db=db, config=config)
    orch.notifier = MagicMock()
    orch.notifier.send = AsyncMock()
    orch.notifier.chat_id = "test-chat"

    # Error message should be forwarded
    error_msg = await db.create_message(MessageCreate(
        sender="task-1", message="Deployment failed",
        message_type=MessageType.ERROR,
    ))
    await orch.forward_message_to_telegram(error_msg)
    orch.notifier.send.assert_awaited_once()

    # Reset mock
    orch.notifier.send.reset_mock()

    # Info message should NOT be forwarded (not in forward_types)
    info_msg = await db.create_message(MessageCreate(
        sender="task-1", message="Just info",
        message_type=MessageType.INFO,
    ))
    await orch.forward_message_to_telegram(info_msg)
    orch.notifier.send.assert_not_awaited()

    await db.close()


async def test_orchestrator_forward_telegram_disabled():
    """Test that messages are NOT forwarded when Telegram forwarding is off."""
    db = Database(":memory:")
    await db.initialize()

    config = MagicMock()
    config.max_concurrent_agents = 1
    config.message_board.telegram_forward = False
    config.telegram.bot_token = "test-token"
    config.telegram.chat_id = "test-chat"
    config.plane.api_key = ""
    config.plane.base_url = ""

    orch = Orchestrator(db=db, config=config)
    orch.notifier = MagicMock()
    orch.notifier.send = AsyncMock()

    msg = await db.create_message(MessageCreate(
        sender="task-1", message="Error!",
        message_type=MessageType.ERROR,
    ))
    await orch.forward_message_to_telegram(msg)
    orch.notifier.send.assert_not_awaited()

    await db.close()


async def test_orchestrator_forward_telegram_separate_chat():
    """Test that a separate Telegram chat ID can be used for messages."""
    db = Database(":memory:")
    await db.initialize()

    config = MagicMock()
    config.max_concurrent_agents = 1
    config.message_board.enabled = True
    config.message_board.telegram_forward = True
    config.message_board.telegram_chat_id = "message-board-chat"
    config.message_board.forward_types = ["error"]
    config.telegram.bot_token = "test-token"
    config.telegram.chat_id = "main-chat"
    config.plane.api_key = ""
    config.plane.base_url = ""

    orch = Orchestrator(db=db, config=config)
    orch.notifier = MagicMock()
    orch.notifier.send = AsyncMock()
    orch.notifier.chat_id = "main-chat"

    msg = await db.create_message(MessageCreate(
        sender="task-1", message="Critical error",
        message_type=MessageType.ERROR,
    ))
    await orch.forward_message_to_telegram(msg)

    # Should have temporarily used the message-board-chat
    orch.notifier.send.assert_awaited_once()
    # chat_id should be restored to original
    assert orch.notifier.chat_id == "main-chat"

    await db.close()


# ── Config tests ───────────────────────────────────────────────────────


def test_message_board_config_defaults():
    from factory.config import MessageBoardConfig
    cfg = MessageBoardConfig()
    assert cfg.enabled is True
    assert cfg.telegram_forward is False
    assert cfg.telegram_chat_id == ""
    assert cfg.forward_types == ["error", "question", "handoff"]


def test_config_includes_message_board():
    from factory.config import Config
    cfg = Config()
    assert cfg.message_board.enabled is True


# ── Model tests ────────────────────────────────────────────────────────


def test_message_create_defaults():
    msg = MessageCreate(sender="test", message="hello")
    assert msg.message_type == MessageType.INFO
    assert msg.recipient is None
    assert msg.task_id is None
    assert msg.workflow_id is None
    assert msg.reply_to is None


def test_message_create_all_fields():
    msg = MessageCreate(
        sender="coder",
        recipient="reviewer",
        task_id=5,
        workflow_id=3,
        message="Ready for review",
        message_type=MessageType.HANDOFF,
        reply_to=10,
    )
    assert msg.sender == "coder"
    assert msg.recipient == "reviewer"
    assert msg.task_id == 5
    assert msg.workflow_id == 3
    assert msg.reply_to == 10


# ── SSE stream test ────────────────────────────────────────────────────


async def test_sse_stream_endpoint_returns_event_stream(client):
    """Test that the SSE endpoint returns the correct content type."""
    # We need to use a timeout since SSE is a long-running connection
    import asyncio

    async def fetch_sse():
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as c:
            async with c.stream("GET", "/api/messages/stream/sse") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                # Read first event (should be "connected")
                first_chunk = b""
                async for chunk in resp.aiter_bytes():
                    first_chunk += chunk
                    if b"connected" in first_chunk:
                        break
                assert b"event: connected" in first_chunk
                return

    try:
        await asyncio.wait_for(fetch_sse(), timeout=5.0)
    except asyncio.TimeoutError:
        pass  # Expected - SSE streams indefinitely


# ── Multiple filter combination tests ──────────────────────────────────


async def test_list_messages_multiple_filters():
    db = Database(":memory:")
    await db.initialize()

    await db.create_message(MessageCreate(
        sender="coder", message="m1",
        message_type=MessageType.ERROR, task_id=1,
    ))
    await db.create_message(MessageCreate(
        sender="coder", message="m2",
        message_type=MessageType.STATUS, task_id=1,
    ))
    await db.create_message(MessageCreate(
        sender="reviewer", message="m3",
        message_type=MessageType.ERROR, task_id=2,
    ))

    # Filter by sender AND type
    messages = await db.list_messages(sender="coder", message_type="error")
    assert len(messages) == 1
    assert messages[0].message == "m1"

    # Filter by task AND type
    messages = await db.list_messages(task_id=1, message_type="status")
    assert len(messages) == 1
    assert messages[0].message == "m2"

    await db.close()
