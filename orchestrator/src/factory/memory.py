import logging

from surrealdb import AsyncSurreal

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

logger = logging.getLogger(__name__)

SCHEMA = """
DEFINE TABLE IF NOT EXISTS memory SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS task_id ON memory TYPE int;
DEFINE FIELD IF NOT EXISTS repo ON memory TYPE string;
DEFINE FIELD IF NOT EXISTS agent_type ON memory TYPE string;
DEFINE FIELD IF NOT EXISTS title ON memory TYPE string;
DEFINE FIELD IF NOT EXISTS description ON memory TYPE string;
DEFINE FIELD IF NOT EXISTS outcome ON memory TYPE string;
DEFINE FIELD IF NOT EXISTS summary ON memory TYPE string;
DEFINE FIELD IF NOT EXISTS error ON memory TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON memory TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_memory_repo ON memory FIELDS repo;
DEFINE ANALYZER IF NOT EXISTS memory_analyzer TOKENIZERS blank, class FILTERS lowercase, snowball(english);
DEFINE INDEX IF NOT EXISTS idx_memory_search ON memory FIELDS summary FULLTEXT ANALYZER memory_analyzer BM25;
DEFINE FIELD IF NOT EXISTS embedding ON memory TYPE option<array<float>>;
DEFINE INDEX IF NOT EXISTS idx_memory_vector ON memory FIELDS embedding HNSW DIMENSION 1536 DIST COSINE;
"""

RECALL_QUERY = """\
SELECT *
FROM memory
WHERE repo = $repo AND summary @@ $query
LIMIT $limit;
"""

RECALL_FALLBACK_QUERY = """\
SELECT *
FROM memory
WHERE repo = $repo
ORDER BY created_at DESC
LIMIT $limit;
"""


class AgentMemory:
    def __init__(self, url: str, user: str, password: str, openai_api_key: str = ""):
        self._url = url
        self._user = user
        self._password = password
        self._db: AsyncSurreal | None = None
        self._openai = None
        if openai_api_key and AsyncOpenAI is not None:
            self._openai = AsyncOpenAI(api_key=openai_api_key)

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

    async def initialize(self):
        self._db = AsyncSurreal(self._url)
        await self._db.signin({"username": self._user, "password": self._password})
        await self._db.use("factory", "memory")
        for statement in SCHEMA.strip().split(";"):
            statement = statement.strip()
            if statement:
                await self._db.query(statement + ";")
        logger.info("AgentMemory initialized (SurrealDB at %s)", self._url)

    async def store(
        self,
        task_id: int,
        repo: str,
        agent_type: str,
        title: str,
        description: str,
        outcome: str,
        summary: str,
        error: str | None = None,
    ):
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

    async def recall(self, repo: str, query: str, limit: int = 5) -> list[dict]:
        if not self._db:
            return []
        try:
            rows = await self._db.query(
                RECALL_QUERY, {"repo": repo, "query": query, "limit": limit}
            )
            if rows and isinstance(rows, list) and len(rows) > 0:
                # SDK may return list of dicts or list of result wrappers
                if isinstance(rows[0], dict) and "result" not in rows[0]:
                    return rows[:limit]
                if isinstance(rows[0], dict) and "result" in rows[0]:
                    result = rows[0]["result"]
                    if result:
                        return result[:limit]

            # Fall back to recent memories if full-text search returns nothing
            rows = await self._db.query(
                RECALL_FALLBACK_QUERY, {"repo": repo, "limit": limit}
            )
            if rows and isinstance(rows, list) and len(rows) > 0:
                if isinstance(rows[0], dict) and "result" not in rows[0]:
                    return rows[:limit]
                if isinstance(rows[0], dict) and "result" in rows[0]:
                    return (rows[0]["result"] or [])[:limit]
            return []
        except Exception as e:
            logger.warning("Failed to recall memories for repo %s: %s", repo, e)
            return []

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("AgentMemory connection closed")
