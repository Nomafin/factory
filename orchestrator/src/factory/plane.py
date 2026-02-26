import hashlib
import hmac
import logging
import re
from enum import Enum

import httpx

logger = logging.getLogger(__name__)

AGENT_TYPES = {"coder", "reviewer", "researcher", "devops"}


class PlaneAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class PlaneEvent:
    def __init__(
        self,
        event_type: str,
        action: PlaneAction,
        issue_id: str = "",
        issue_title: str = "",
        description: str = "",
        state_name: str = "",
        state_group: str = "",
        repo: str = "",
        agent_type: str = "coder",
    ):
        self.event_type = event_type
        self.action = action
        self.issue_id = issue_id
        self.issue_title = issue_title
        self.description = description
        self.state_name = state_name
        self.state_group = state_group
        self.repo = repo
        self.agent_type = agent_type


def parse_webhook_event(payload: dict) -> PlaneEvent:
    data = payload.get("data", {})
    labels = data.get("labels", [])

    repo = ""
    agent_type = "coder"
    for label in labels:
        name = label.get("name", "")
        if name.startswith("repo:"):
            repo = name[5:]
        elif name in AGENT_TYPES:
            agent_type = name

    desc_html = data.get("description_html", "") or ""
    description = re.sub(r"<[^>]+>", "", desc_html).strip()

    state = data.get("state", {})

    return PlaneEvent(
        event_type=payload.get("event", ""),
        action=PlaneAction(payload.get("action", "create")),
        issue_id=data.get("id", ""),
        issue_title=data.get("name", ""),
        description=description,
        state_name=state.get("name", ""),
        state_group=state.get("group", ""),
        repo=repo,
        agent_type=agent_type,
    )


def verify_signature(payload_bytes: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class PlaneClient:
    def __init__(self, base_url: str, api_key: str, workspace_slug: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.workspace_slug = workspace_slug
        self._client = httpx.AsyncClient(
            headers={"X-API-Key": api_key},
            timeout=30.0,
        )

    async def update_issue_state(self, project_id: str, issue_id: str, state_id: str):
        url = f"{self.base_url}/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/work-items/{issue_id}/"
        resp = await self._client.patch(url, json={"state": state_id})
        resp.raise_for_status()

    async def add_comment(self, project_id: str, issue_id: str, comment_html: str):
        url = f"{self.base_url}/api/v1/workspaces/{self.workspace_slug}/projects/{project_id}/work-items/{issue_id}/comments/"
        resp = await self._client.post(url, json={"comment_html": comment_html})
        resp.raise_for_status()

    async def close(self):
        await self._client.aclose()
