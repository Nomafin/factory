from pathlib import Path

from factory.prompts import load_prompt


def test_load_public_prompt_only(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "coder.md").write_text("You are a coder.")

    result = load_prompt("prompts/coder.md", tmp_path)
    assert result == "You are a coder."


def test_load_public_and_private_prompt(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "coder.md").write_text("You are a coder.")

    private_dir = prompts_dir / "private"
    private_dir.mkdir()
    (private_dir / "coder.md").write_text("Always use tabs.")

    result = load_prompt("prompts/coder.md", tmp_path)
    assert result == "You are a coder.\n\nAlways use tabs."


def test_load_private_prompt_only(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    private_dir = prompts_dir / "private"
    private_dir.mkdir()
    (private_dir / "coder.md").write_text("Secret instructions.")

    result = load_prompt("prompts/coder.md", tmp_path)
    assert result == "Secret instructions."


def test_load_prompt_no_files(tmp_path):
    result = load_prompt("prompts/coder.md", tmp_path)
    assert result == ""


def test_load_prompt_empty_file(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "coder.md").write_text("   ")

    result = load_prompt("prompts/coder.md", tmp_path)
    assert result == ""


def test_load_prompt_empty_string():
    result = load_prompt("", Path("/nonexistent"))
    assert result == ""
