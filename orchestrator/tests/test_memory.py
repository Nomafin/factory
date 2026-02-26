from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from factory.memory import AgentMemory

FAKE_EMBEDDING = [0.1] * 1536


@pytest.fixture
def memory():
    return AgentMemory(url="ws://localhost:8200/rpc", user="root", password="pass")


async def test_embed_text_calls_openai(memory):
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.data = [MagicMock(embedding=FAKE_EMBEDDING)]
    mock_client.embeddings.create.return_value = mock_response
    memory._openai = mock_client

    result = await memory._embed("test text")

    assert result == FAKE_EMBEDDING
    mock_client.embeddings.create.assert_awaited_once_with(
        input="test text", model="text-embedding-3-small"
    )


async def test_embed_text_returns_none_without_openai(memory):
    memory._openai = None
    result = await memory._embed("test text")
    assert result is None


async def test_embed_text_returns_none_on_api_error(memory):
    mock_client = AsyncMock()
    mock_client.embeddings.create.side_effect = Exception("API error")
    memory._openai = mock_client

    result = await memory._embed("test text")
    assert result is None


async def test_store_includes_embedding(memory):
    mock_db = AsyncMock()
    memory._db = mock_db
    memory._embed = AsyncMock(return_value=FAKE_EMBEDDING)

    await memory.store(
        task_id=1, repo="test/repo", agent_type="coder",
        title="Fix bug", description="Fix the login bug",
        outcome="success", summary="Fixed the login timeout",
    )

    mock_db.create.assert_awaited_once()
    call_args = mock_db.create.call_args
    record = call_args[0][1]
    assert record["embedding"] == FAKE_EMBEDDING
    memory._embed.assert_awaited_once_with("Fixed the login timeout")


async def test_store_works_without_embedding(memory):
    mock_db = AsyncMock()
    memory._db = mock_db
    memory._embed = AsyncMock(return_value=None)

    await memory.store(
        task_id=1, repo="test/repo", agent_type="coder",
        title="Fix bug", description="Fix it",
        outcome="success", summary="Fixed it",
    )

    mock_db.create.assert_awaited_once()
    record = mock_db.create.call_args[0][1]
    assert record["embedding"] is None
