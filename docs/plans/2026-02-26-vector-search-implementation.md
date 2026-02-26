# Vector Search for Agent Memory — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add OpenAI embedding-based vector search to agent memory, with BM25 full-text search as fallback.

**Architecture:** On `store()`, embed the summary via OpenAI and save the vector alongside existing fields. On `recall()`, embed the query, run KNN search on SurrealDB's HNSW index filtered by repo, fall back to BM25 if no vectors found. Everything degrades gracefully if `OPENAI_API_KEY` is not set.

**Tech Stack:** OpenAI Python SDK (`AsyncOpenAI`), SurrealDB HNSW vector index, `text-embedding-3-small` (1536 dimensions)

---

### Task 1: Add `openai` dependency

**Files:**
- Modify: `orchestrator/pyproject.toml:14`

**Step 1: Add the dependency**

In `pyproject.toml`, add `"openai>=1.0.0"` to the dependencies list, after the `surrealdb` line.

**Step 2: Commit**

```bash
git add orchestrator/pyproject.toml
git commit -m "chore: add openai dependency for vector embeddings"
```

---

### Task 2: Add embedding generation to `memory.py`

**Files:**
- Modify: `orchestrator/src/factory/memory.py`
- Test: `orchestrator/tests/test_memory.py`

**Step 1: Write the failing test**

Create `orchestrator/tests/test_memory.py`:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `cd orchestrator && python -m pytest tests/test_memory.py -v`
Expected: FAIL — `_embed` method does not exist yet

**Step 3: Implement embedding in memory.py**

Add OpenAI import at top of `memory.py`:

```python
try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None
```

Add `openai_api_key` parameter to `AgentMemory.__init__`:

```python
def __init__(self, url: str, user: str, password: str, openai_api_key: str = ""):
    self._url = url
    self._user = user
    self._password = password
    self._db: AsyncSurreal | None = None
    self._openai = None
    if openai_api_key and AsyncOpenAI is not None:
        self._openai = AsyncOpenAI(api_key=openai_api_key)
```

Add `_embed` method:

```python
async def _embed(self, text: str) -> list[float] | None:
    if not self._openai:
        return None
    try:
        response = await self._openai.embeddings.create(
            input=text, model="text-embedding-3-small"
        )
        return response.data[0].embedding
    except Exception as e:
        logger.warning("Embedding generation failed: %s", e)
        return None
```

**Step 4: Run test to verify it passes**

Run: `cd orchestrator && python -m pytest tests/test_memory.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/memory.py orchestrator/tests/test_memory.py
git commit -m "feat: add embedding generation via OpenAI"
```

---

### Task 3: Add HNSW schema and vector store

**Files:**
- Modify: `orchestrator/src/factory/memory.py` (SCHEMA, store method)
- Test: `orchestrator/tests/test_memory.py`

**Step 1: Write the failing test**

Add to `orchestrator/tests/test_memory.py`:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `cd orchestrator && python -m pytest tests/test_memory.py::test_store_includes_embedding -v`
Expected: FAIL — store doesn't call `_embed` or include embedding

**Step 3: Update schema and store method**

Add to SCHEMA string (after the BM25 index line):

```
DEFINE FIELD IF NOT EXISTS embedding ON memory TYPE option<array<float>>;
DEFINE INDEX IF NOT EXISTS idx_memory_vector ON memory FIELDS embedding HNSW DIMENSION 1536 DIST COSINE;
```

Update `store()` — add embedding generation before the create call:

```python
async def store(self, ...):
    if not self._db:
        return
    try:
        embedding = await self._embed(summary[:2000])
        await self._db.create("memory", {
            "task_id": task_id,
            "repo": repo,
            "agent_type": agent_type,
            "title": title,
            "description": description[:500],
            "outcome": outcome,
            "summary": summary[:2000],
            "error": error[:500] if error else None,
            "embedding": embedding,
        })
        logger.info("Stored memory for task %d (%s)", task_id, outcome)
    except Exception as e:
        logger.warning("Failed to store memory for task %d: %s", task_id, e)
```

**Step 4: Run tests to verify they pass**

Run: `cd orchestrator && python -m pytest tests/test_memory.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/memory.py orchestrator/tests/test_memory.py
git commit -m "feat: store embeddings with memory records"
```

---

### Task 4: Add vector recall with BM25 fallback

**Files:**
- Modify: `orchestrator/src/factory/memory.py` (recall method, add VECTOR_QUERY)
- Test: `orchestrator/tests/test_memory.py`

**Step 1: Write the failing tests**

Add to `orchestrator/tests/test_memory.py`:

```python
async def test_recall_uses_vector_search_when_available(memory):
    mock_db = AsyncMock()
    vector_results = [{"title": "Vector match", "outcome": "success", "summary": "found via vector"}]
    mock_db.query.return_value = vector_results
    memory._db = mock_db
    memory._embed = AsyncMock(return_value=FAKE_EMBEDDING)

    results = await memory.recall(repo="test/repo", query="login bug")

    assert len(results) == 1
    assert results[0]["title"] == "Vector match"
    # First query should be the vector query
    first_query = mock_db.query.call_args_list[0][0][0]
    assert "<|" in first_query  # KNN operator


async def test_recall_falls_back_to_fts_when_no_embedding(memory):
    mock_db = AsyncMock()
    fts_results = [{"title": "FTS match", "outcome": "success", "summary": "found via fts"}]
    mock_db.query.return_value = fts_results
    memory._db = mock_db
    memory._openai = None  # No OpenAI → no embedding

    results = await memory.recall(repo="test/repo", query="login bug")

    assert len(results) == 1
    assert results[0]["title"] == "FTS match"
    first_query = mock_db.query.call_args_list[0][0][0]
    assert "@@" in first_query  # BM25 operator


async def test_recall_falls_back_to_fts_when_vector_empty(memory):
    mock_db = AsyncMock()
    # First call (vector) returns empty, second call (FTS) returns results
    mock_db.query.side_effect = [
        [],
        [{"title": "FTS fallback", "outcome": "success"}],
    ]
    memory._db = mock_db
    memory._embed = AsyncMock(return_value=FAKE_EMBEDDING)

    results = await memory.recall(repo="test/repo", query="login bug")

    assert len(results) == 1
    assert results[0]["title"] == "FTS fallback"
```

**Step 2: Run tests to verify they fail**

Run: `cd orchestrator && python -m pytest tests/test_memory.py::test_recall_uses_vector_search_when_available -v`
Expected: FAIL — recall doesn't attempt vector search

**Step 3: Add vector recall query and update recall method**

Add new query constant:

```python
VECTOR_RECALL_QUERY = """\
SELECT *, vector::distance::knn() AS distance
FROM memory
WHERE repo = $repo AND embedding <|$limit,100|> $embedding
ORDER BY distance;
"""
```

Rewrite `recall()`:

```python
async def recall(self, repo: str, query: str, limit: int = 5) -> list[dict]:
    if not self._db:
        return []
    try:
        # Try vector search first
        embedding = await self._embed(query)
        if embedding:
            rows = await self._db.query(
                VECTOR_RECALL_QUERY,
                {"repo": repo, "embedding": embedding, "limit": limit},
            )
            results = self._parse_results(rows, limit)
            if results:
                return results

        # Fall back to BM25 full-text search
        rows = await self._db.query(
            RECALL_QUERY, {"repo": repo, "query": query, "limit": limit}
        )
        results = self._parse_results(rows, limit)
        if results:
            return results

        # Fall back to recent memories
        rows = await self._db.query(
            RECALL_FALLBACK_QUERY, {"repo": repo, "limit": limit}
        )
        return self._parse_results(rows, limit)
    except Exception as e:
        logger.warning("Failed to recall memories for repo %s: %s", repo, e)
        return []
```

Extract result parsing into a helper:

```python
def _parse_results(self, rows: list, limit: int) -> list[dict]:
    if not rows or not isinstance(rows, list) or len(rows) == 0:
        return []
    if isinstance(rows[0], dict) and "result" not in rows[0]:
        return rows[:limit]
    if isinstance(rows[0], dict) and "result" in rows[0]:
        result = rows[0]["result"]
        if result:
            return result[:limit]
    return []
```

**Step 4: Run all tests to verify they pass**

Run: `cd orchestrator && python -m pytest tests/test_memory.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/memory.py orchestrator/tests/test_memory.py
git commit -m "feat: add vector recall with BM25 fallback"
```

---

### Task 5: Wire OpenAI API key through deps.py

**Files:**
- Modify: `orchestrator/src/factory/deps.py:17-30`
- Modify: `orchestrator/.env.example`

**Step 1: Update `_init_memory` to pass OpenAI key**

In `deps.py`, add `openai_api_key` to the `AgentMemory` constructor call:

```python
async def _init_memory() -> AgentMemory | None:
    url = os.environ.get("SURREALDB_URL", "")
    user = os.environ.get("SURREALDB_USER", "")
    password = os.environ.get("SURREALDB_PASS", "")
    if not (url and user and password):
        logger.info("SurrealDB env vars not set, agent memory disabled")
        return None
    openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    memory = AgentMemory(url=url, user=user, password=password, openai_api_key=openai_api_key)
    try:
        await memory.initialize()
        return memory
    except Exception as e:
        logger.warning("Failed to initialize agent memory: %s", e)
        return None
```

**Step 2: Update `.env.example`**

Add after the SurrealDB section:

```
# Vector search embeddings (optional — omit for BM25 full-text search only)
OPENAI_API_KEY=sk-...
```

**Step 3: Commit**

```bash
git add orchestrator/src/factory/deps.py .env.example
git commit -m "feat: wire OpenAI API key for vector embeddings"
```

---

### Task 6: Update documentation

**Files:**
- Modify: `README.md` (Agent Memory section)

**Step 1: Update the Agent Memory section**

Update the existing Agent Memory section in README.md to mention vector search:

After the existing paragraph about BM25, replace the "Currently uses" line with:

```markdown
**Search strategy:**
- With `OPENAI_API_KEY` set: vector similarity search (OpenAI `text-embedding-3-small`, 1536d) with BM25 fallback
- Without `OPENAI_API_KEY`: BM25 full-text search only

To enable vector search, add to `.env`:

```
OPENAI_API_KEY=sk-...
```

Old memories stored without embeddings are still findable via BM25 fallback.
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add vector search setup to README"
```

---

### Task 7: Deploy and verify on VPS

**Step 1: Push to main**

```bash
git push
```

Wait for auto-deploy to complete.

**Step 2: Add OpenAI API key to VPS .env**

```bash
ssh root@reitti.6a.fi "echo 'OPENAI_API_KEY=sk-...' >> /opt/factory/.env"
```

**Step 3: Restart service to pick up new env var**

```bash
ssh root@reitti.6a.fi "systemctl restart factory-orchestrator"
```

**Step 4: Verify via integration test**

Run a quick Python test on VPS to confirm vector store/recall works end-to-end with the live SurrealDB and OpenAI API.

**Step 5: Verify graceful degradation**

Temporarily remove `OPENAI_API_KEY`, restart, confirm BM25 still works.
