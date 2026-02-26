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
