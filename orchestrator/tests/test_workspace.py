import subprocess

import pytest
from factory.config import RepoConfig
from factory.workspace import RepoManager, resolve_repo


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


def test_resolve_repo_from_config():
    repos = {"factory": RepoConfig(url="https://github.com/Nomafin/factory.git", default_agent="coder")}
    url, settings = resolve_repo("factory", repos, "Nomafin")
    assert url == "https://github.com/Nomafin/factory.git"
    assert settings.default_agent == "coder"


def test_resolve_repo_owner_slash_name():
    url, settings = resolve_repo("other-org/some-repo", {}, "Nomafin")
    assert url == "https://github.com/other-org/some-repo.git"
    assert settings.default_agent == "coder"


def test_resolve_repo_short_name_with_default_org():
    url, settings = resolve_repo("myapp", {}, "Nomafin")
    assert url == "https://github.com/Nomafin/myapp.git"
    assert settings.default_agent == "coder"


def test_resolve_repo_short_name_no_default_org():
    with pytest.raises(ValueError, match="Cannot resolve repo"):
        resolve_repo("myapp", {}, "")


def test_resolve_repo_config_overrides_default_org():
    repos = {"myapp": RepoConfig(url="https://github.com/custom/myapp.git", default_agent="reviewer")}
    url, settings = resolve_repo("myapp", repos, "Nomafin")
    assert url == "https://github.com/custom/myapp.git"
    assert settings.default_agent == "reviewer"
