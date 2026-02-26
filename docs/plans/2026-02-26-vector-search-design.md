# Vector Search for Agent Memory

## Context

Agent memory currently uses BM25 full-text search on task summaries to find relevant past experience. This works for keyword matches but misses semantic similarity. Adding vector search via OpenAI embeddings enables semantic recall — finding memories that are conceptually related even when they use different words.

## Design

### Embedding provider

OpenAI `text-embedding-3-small` (1536 dimensions, $0.02/1M tokens). Accessed via the `openai` Python SDK.

### Schema changes

Add to existing `memory` table (no migration needed — field is optional):

```surql
DEFINE FIELD IF NOT EXISTS embedding ON memory TYPE option<array<float>>;
DEFINE INDEX IF NOT EXISTS idx_memory_vector ON memory FIELDS embedding HNSW DIMENSION 1536 DIST COSINE;
```

### Store flow

1. Agent completes task → orchestrator calls `memory.store()`
2. `store()` generates embedding of the `summary` field via OpenAI API
3. Record saved with embedding vector alongside all existing fields
4. If OpenAI API fails, record saved without embedding (graceful degradation)

### Recall flow

1. Orchestrator calls `memory.recall(repo, query)` before starting agent
2. `recall()` embeds the query (`"{title} {description}"`) via OpenAI
3. KNN vector search: `WHERE repo = $repo AND embedding <|K,EF|> $query_embedding`
4. If vector search returns nothing → fall back to BM25 full-text search
5. If BM25 returns nothing → fall back to recent memories by date

### Search strategy

Vector primary, BM25 fallback. This means:
- New memories (with embeddings) are found by semantic similarity
- Old memories (without embeddings) are still findable via keyword match
- If OpenAI is down, the entire system degrades gracefully to BM25

### Configuration

New env var: `OPENAI_API_KEY` — if not set, vector search is disabled and BM25 is used exclusively. No other config changes needed.

New dependency: `openai>=1.0.0` in pyproject.toml.

### Files to modify

| File | Change |
|------|--------|
| `memory.py` | Add embedding generation, vector search query, HNSW schema |
| `pyproject.toml` | Add `openai` dependency |
| `.env` / `.env.example` | Add `OPENAI_API_KEY` |
| `deps.py` | Pass `OPENAI_API_KEY` to AgentMemory |
| `README.md` | Document vector search setup |
