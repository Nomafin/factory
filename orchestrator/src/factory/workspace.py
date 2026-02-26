import asyncio
import os
import shutil
from pathlib import Path


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
            await self._run("git", "pull", "--ff-only", cwd=repo_path)
        else:
            await self._run("git", "clone", auth_url, str(repo_path))
        return repo_path

    async def create_worktree(self, repo_name: str, branch_name: str) -> Path:
        repo_path = self.repos_dir / repo_name
        slug = branch_name.replace("/", "-")
        wt_path = self.worktrees_dir / slug

        await self._run("git", "worktree", "add", "-b", branch_name, str(wt_path), cwd=repo_path)
        return wt_path

    async def remove_worktree(self, repo_name: str, wt_path: Path):
        repo_path = self.repos_dir / repo_name
        await self._run("git", "worktree", "remove", str(wt_path), "--force", cwd=repo_path)
        if wt_path.exists():
            shutil.rmtree(wt_path)
