import subprocess

from factory.workspace import RepoManager


async def test_clone_repo(tmp_path):
    # Create a bare repo to clone from
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)

    # Create initial commit in a temp working copy
    temp_work = tmp_path / "temp_work"
    subprocess.run(["git", "clone", str(origin), str(temp_work)], check=True, capture_output=True)
    (temp_work / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=str(temp_work), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@test.com", "commit", "-m", "init"],
        cwd=str(temp_work), check=True, capture_output=True,
    )
    subprocess.run(["git", "push"], cwd=str(temp_work), check=True, capture_output=True)

    repos_dir = tmp_path / "repos"
    worktrees_dir = tmp_path / "worktrees"
    repos_dir.mkdir()
    worktrees_dir.mkdir()

    mgr = RepoManager(repos_dir=repos_dir, worktrees_dir=worktrees_dir)

    # Clone
    repo_path = await mgr.ensure_repo("testrepo", str(origin))
    assert repo_path.exists()
    assert (repo_path / ".git").exists()

    # Create worktree
    wt_path = await mgr.create_worktree("testrepo", "agent/task-1-test")
    assert wt_path.exists()
    assert (wt_path / "README.md").exists()

    # Cleanup worktree
    await mgr.remove_worktree("testrepo", wt_path)
    assert not wt_path.exists()
