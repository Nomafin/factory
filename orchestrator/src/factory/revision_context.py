"""Revision workflow support.

Fetches review feedback from GitHub PR comments/reviews and Plane task
comments so that agents can address feedback when a task is re-queued
for revision.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field

from factory.plane import PlaneClient

logger = logging.getLogger(__name__)

# Limit how much feedback text we inject into the prompt
MAX_FEEDBACK_CHARS = 8000
MAX_COMMENT_CHARS = 4000


@dataclass
class RevisionContext:
    """Collected feedback for a revision task."""

    pr_url: str = ""
    pr_number: int | None = None
    branch_name: str = ""
    github_comments: list[dict] = field(default_factory=list)
    github_reviews: list[dict] = field(default_factory=list)
    plane_comments: list[dict] = field(default_factory=list)

    @property
    def is_revision(self) -> bool:
        """True if this task has an existing PR/branch indicating revision work."""
        return bool(self.pr_url or self.pr_number)

    @property
    def has_feedback(self) -> bool:
        """True if any feedback was collected."""
        return bool(self.github_comments or self.github_reviews or self.plane_comments)

    def format_prompt_section(self) -> str:
        """Format collected feedback as a prompt section for the agent."""
        if not self.has_feedback:
            return ""

        parts: list[str] = ["\n## Review Feedback"]

        if self.pr_url:
            parts.append(f"PR: {self.pr_url}")

        # GitHub review comments (from formal reviews)
        if self.github_reviews:
            parts.append("\n### GitHub Reviews")
            remaining = MAX_FEEDBACK_CHARS
            for review in self.github_reviews:
                state = review.get("state", "")
                body = review.get("body", "").strip()
                author = review.get("author", "unknown")
                if not body and state not in ("CHANGES_REQUESTED", "APPROVED"):
                    continue
                entry = f"- **{author}** ({state}): {body}" if body else f"- **{author}**: {state}"
                if len(entry) > remaining:
                    entry = entry[:remaining - 3] + "..."
                    parts.append(entry)
                    break
                parts.append(entry)
                remaining -= len(entry)

        # GitHub PR comments (inline and general)
        if self.github_comments:
            parts.append("\n### GitHub PR Comments")
            remaining = MAX_COMMENT_CHARS
            for comment in self.github_comments:
                author = comment.get("author", "unknown")
                body = comment.get("body", "").strip()
                path = comment.get("path", "")
                line = comment.get("line")
                if not body:
                    continue
                location = f" ({path}:{line})" if path and line else f" ({path})" if path else ""
                entry = f"- **{author}**{location}: {body}"
                if len(entry) > remaining:
                    entry = entry[:remaining - 3] + "..."
                    parts.append(entry)
                    break
                parts.append(entry)
                remaining -= len(entry)

        # Plane task comments
        if self.plane_comments:
            parts.append("\n### Task Comments")
            remaining = MAX_COMMENT_CHARS
            for comment in self.plane_comments:
                body = comment.get("body", "").strip()
                author = comment.get("author", "")
                if not body:
                    continue
                entry = f"- **{author}**: {body}" if author else f"- {body}"
                if len(entry) > remaining:
                    entry = entry[:remaining - 3] + "..."
                    parts.append(entry)
                    break
                parts.append(entry)
                remaining -= len(entry)

        parts.append("\nAddress the feedback above and push updates to the existing PR.")

        return "\n".join(parts)


def extract_pr_number(pr_url: str) -> int | None:
    """Extract the PR number from a GitHub PR URL."""
    if not pr_url:
        return None
    match = re.search(r"/pull/(\d+)", pr_url)
    return int(match.group(1)) if match else None


async def fetch_github_pr_comments(
    pr_number: int,
    repo_dir: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Fetch comments and reviews from a GitHub PR using the gh CLI.

    Args:
        pr_number: The PR number to fetch comments for.
        repo_dir: Working directory with the git repo (for gh CLI context).

    Returns:
        Tuple of (comments, reviews) where each is a list of dicts.
    """
    comments: list[dict] = []
    reviews: list[dict] = []

    env = os.environ.copy()
    env["GH_TOKEN"] = os.environ.get("GITHUB_TOKEN", "")

    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "view", str(pr_number),
            "--json", "comments,reviews",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.warning(
                "Failed to fetch PR %d comments: %s",
                pr_number, stderr.decode().strip(),
            )
            return comments, reviews

        data = json.loads(stdout.decode())

        # Parse comments
        for c in data.get("comments", []):
            author_data = c.get("author", {})
            comments.append({
                "author": author_data.get("login", "unknown") if isinstance(author_data, dict) else "unknown",
                "body": c.get("body", ""),
                "created_at": c.get("createdAt", ""),
            })

        # Parse reviews
        for r in data.get("reviews", []):
            author_data = r.get("author", {})
            review_entry = {
                "author": author_data.get("login", "unknown") if isinstance(author_data, dict) else "unknown",
                "state": r.get("state", ""),
                "body": r.get("body", ""),
                "created_at": r.get("submittedAt", ""),
            }
            reviews.append(review_entry)

    except FileNotFoundError:
        logger.warning("gh CLI not found, cannot fetch PR comments")
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to parse PR %d comment data: %s", pr_number, e)
    except Exception as e:
        logger.warning("Error fetching PR %d comments: %s", pr_number, e)

    return comments, reviews


async def fetch_plane_comments(
    plane_client: PlaneClient | None,
    project_id: str,
    issue_id: str,
) -> list[dict]:
    """Fetch comments from a Plane issue.

    Args:
        plane_client: The Plane API client (may be None if not configured).
        project_id: The Plane project ID.
        issue_id: The Plane issue ID.

    Returns:
        List of comment dicts with 'body' and 'author' keys.
    """
    if not plane_client or not project_id or not issue_id:
        return []

    try:
        raw_comments = await plane_client.get_comments(project_id, issue_id)
        result = []
        for c in raw_comments:
            html = c.get("comment_html", "")
            # Strip HTML tags to get plain text
            body = re.sub(r"<[^>]+>", "", html).strip()
            if body:
                actor = c.get("actor_detail", {})
                author = ""
                if isinstance(actor, dict):
                    author = actor.get("display_name", "") or actor.get("email", "")
                result.append({
                    "body": body,
                    "author": author,
                    "created_at": c.get("created_at", ""),
                })
        return result
    except Exception as e:
        logger.warning("Failed to fetch Plane comments for issue %s: %s", issue_id, e)
        return []


async def build_revision_context(
    pr_url: str,
    branch_name: str,
    plane_client: PlaneClient | None = None,
    plane_project_id: str = "",
    plane_issue_id: str = "",
    repo_dir: str | None = None,
) -> RevisionContext:
    """Build a complete revision context by fetching all available feedback.

    Args:
        pr_url: The GitHub PR URL (from previous task run).
        branch_name: The existing branch name.
        plane_client: Optional Plane API client.
        plane_project_id: Plane project ID for fetching comments.
        plane_issue_id: Plane issue ID for fetching comments.
        repo_dir: Git repo directory for gh CLI context.

    Returns:
        RevisionContext with all collected feedback.
    """
    ctx = RevisionContext(
        pr_url=pr_url,
        branch_name=branch_name,
    )

    pr_number = extract_pr_number(pr_url)
    ctx.pr_number = pr_number

    # Fetch GitHub PR comments and Plane comments concurrently
    github_task = None
    plane_task = None

    if pr_number:
        github_task = asyncio.create_task(
            fetch_github_pr_comments(pr_number, repo_dir=repo_dir)
        )

    if plane_client and plane_issue_id:
        plane_task = asyncio.create_task(
            fetch_plane_comments(plane_client, plane_project_id, plane_issue_id)
        )

    if github_task:
        try:
            comments, reviews = await github_task
            ctx.github_comments = comments
            ctx.github_reviews = reviews
        except Exception as e:
            logger.warning("GitHub comment fetch failed: %s", e)

    if plane_task:
        try:
            ctx.plane_comments = await plane_task
        except Exception as e:
            logger.warning("Plane comment fetch failed: %s", e)

    return ctx
