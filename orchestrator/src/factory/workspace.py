import asyncio
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class RepoManager:
    def __init__(self, repos_dir: Path, worktrees_dir: Path):
        self.repos_dir = repos_dir
        self.worktrees_dir = worktrees_dir

    def _auth_url(self, url: str) -> str:
        """Inject GitHub token into HTTPS URLs for authentication."""
        token = os.environ.get("GITHUB_TOKEN", "")
        if token and url.startswith("https://github.com/"):
            return url.replace("https://github.com/", f"https://x-access-token:{token}@github.com/")
        return url

    async def _run(self, *args: str, cwd: str | Path | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Command {args} failed: {stderr.decode()}")
        return stdout.decode().strip()

    async def ensure_repo(self, name: str, url: str) -> Path:
        repo_path = self.repos_dir / name
        auth_url = self._auth_url(url)
        if repo_path.exists():
            await self._run("git", "fetch", "--all", cwd=repo_path)
            await self._run("git", "checkout", "main", cwd=repo_path)
            await self._run("git", "reset", "--hard", "origin/main", cwd=repo_path)
        else:
            await self._run("git", "clone", auth_url, str(repo_path))
        return repo_path

    async def create_worktree(self, repo_name: str, branch_name: str) -> Path:
        repo_path = self.repos_dir / repo_name
        slug = branch_name.replace("/", "-")
        wt_path = self.worktrees_dir / slug

        await self._run("git", "worktree", "add", "-b", branch_name, str(wt_path), cwd=repo_path)
        return wt_path

    async def checkout_existing_branch(self, repo_name: str, branch_name: str) -> Path:
        """Create a worktree from an existing remote branch for revision work.

        Fetches the latest changes and creates a worktree that tracks the
        existing remote branch, so the agent can push updates to it.
        """
        repo_path = self.repos_dir / repo_name
        slug = branch_name.replace("/", "-")
        wt_path = self.worktrees_dir / slug

        # Clean up any stale worktree at this path
        if wt_path.exists():
            try:
                await self._run(
                    "git", "worktree", "remove", str(wt_path), "--force",
                    cwd=repo_path,
                )
            except RuntimeError:
                pass
            if wt_path.exists():
                shutil.rmtree(wt_path)

        # Fetch all remotes to get the latest branch state
        await self._run("git", "fetch", "--all", cwd=repo_path)

        # Create worktree tracking the existing remote branch
        await self._run(
            "git", "worktree", "add", str(wt_path), branch_name,
            cwd=repo_path,
        )
        return wt_path

    async def remove_worktree(self, repo_name: str, wt_path: Path):
        repo_path = self.repos_dir / repo_name
        await self._run("git", "worktree", "remove", str(wt_path), "--force", cwd=repo_path)
        if wt_path.exists():
            shutil.rmtree(wt_path)

    async def cleanup_task_worktree(
        self,
        repo_name: str,
        branch_name: str,
        delete_remote_branch: bool = False,
    ) -> dict:
        """Remove the worktree and branches for a completed/failed task.

        Cleans up:
        1. The git worktree directory on disk
        2. The local branch
        3. Optionally the remote branch (for tasks that never created a PR)

        This is best-effort: individual failures are logged but do not
        propagate exceptions.

        Args:
            repo_name: The repository name (key in repos_dir).
            branch_name: The full branch name (e.g. ``agent/task-42-fix-bug``).
            delete_remote_branch: Whether to also delete the remote branch.

        Returns:
            A dict summarising what was cleaned:
            ``{"worktree": bool, "local_branch": bool, "remote_branch": bool}``.
        """
        result = {"worktree": False, "local_branch": False, "remote_branch": False}

        if not branch_name:
            return result

        repo_path = self.repos_dir / repo_name
        slug = branch_name.replace("/", "-")
        wt_path = self.worktrees_dir / slug

        if not repo_path.exists():
            logger.warning(
                "Repo path %s does not exist, skipping worktree cleanup for %s",
                repo_path, branch_name,
            )
            return result

        # 1. Remove the worktree
        if wt_path.exists():
            try:
                await self._run(
                    "git", "worktree", "remove", str(wt_path), "--force",
                    cwd=repo_path,
                )
                result["worktree"] = True
                logger.info("Removed worktree %s", wt_path)
            except RuntimeError as exc:
                logger.warning("git worktree remove failed for %s: %s", wt_path, exc)
                # Fallback: delete the directory manually
                try:
                    shutil.rmtree(wt_path)
                    result["worktree"] = True
                    logger.info("Removed worktree directory %s via rmtree", wt_path)
                except OSError as rm_exc:
                    logger.warning("rmtree failed for %s: %s", wt_path, rm_exc)

        # Prune stale worktree references
        try:
            await self._run("git", "worktree", "prune", cwd=repo_path)
        except RuntimeError as exc:
            logger.warning("git worktree prune failed for %s: %s", repo_path, exc)

        # 2. Delete local branch
        if branch_name:
            try:
                await self._run(
                    "git", "branch", "-D", branch_name, cwd=repo_path,
                )
                result["local_branch"] = True
                logger.info("Deleted local branch %s", branch_name)
            except RuntimeError as exc:
                logger.debug(
                    "Could not delete local branch %s: %s", branch_name, exc,
                )

        # 3. Optionally delete remote branch
        if delete_remote_branch and branch_name:
            try:
                await self._run(
                    "git", "push", "origin", "--delete", branch_name,
                    cwd=repo_path,
                )
                result["remote_branch"] = True
                logger.info("Deleted remote branch %s", branch_name)
            except RuntimeError as exc:
                logger.debug(
                    "Could not delete remote branch %s: %s", branch_name, exc,
                )

        return result

    async def list_worktrees(self, repo_name: str) -> list[dict]:
        """List all worktrees for a repository.

        Returns a list of dicts with ``path`` and ``branch`` keys.
        """
        repo_path = self.repos_dir / repo_name
        if not repo_path.exists():
            return []

        try:
            output = await self._run(
                "git", "worktree", "list", "--porcelain", cwd=repo_path,
            )
        except RuntimeError:
            return []

        worktrees: list[dict] = []
        current: dict = {}
        for line in output.splitlines():
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line.split(" ", 1)[1]}
            elif line.startswith("branch "):
                current["branch"] = line.split(" ", 1)[1].replace("refs/heads/", "")
            elif line == "":
                if current:
                    worktrees.append(current)
                    current = {}
        if current:
            worktrees.append(current)

        return worktrees


async def cleanup_task_worktree(
    repos_dir: Path,
    worktrees_dir: Path,
    repo_name: str,
    branch_name: str,
    delete_remote_branch: bool = False,
) -> dict:
    """Convenience wrapper to clean up a task's worktree and branches.

    Creates a temporary :class:`RepoManager` and delegates to
    :meth:`RepoManager.cleanup_task_worktree`.
    """
    mgr = RepoManager(repos_dir=repos_dir, worktrees_dir=worktrees_dir)
    return await mgr.cleanup_task_worktree(
        repo_name=repo_name,
        branch_name=branch_name,
        delete_remote_branch=delete_remote_branch,
    )
