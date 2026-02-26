import tempfile
from pathlib import Path

from factory.config import load_config


def test_load_config():
    cfg_text = """
max_concurrent_agents: 2
agent_timeout_minutes: 15
plane:
  base_url: "https://plane.example.com"
  api_key: "test-key"
  workspace_slug: "test"
  project_id: "proj-123"
orchestrator:
  host: "0.0.0.0"
  port: 8100
  auth_token: "secret"
repos:
  myapp:
    url: "git@github.com:user/myapp.git"
    default_agent: "coder"
agent_templates:
  coder:
    system_prompt_file: "prompts/coder.md"
    allowed_tools: ["Read", "Edit", "Bash"]
    timeout_minutes: 30
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(cfg_text)
        f.flush()
        config = load_config(Path(f.name))

    assert config.max_concurrent_agents == 2
    assert config.agent_timeout_minutes == 15
    assert config.plane.base_url == "https://plane.example.com"
    assert config.repos["myapp"].url == "git@github.com:user/myapp.git"
    assert config.agent_templates["coder"].allowed_tools == ["Read", "Edit", "Bash"]


def test_load_config_defaults():
    cfg_text = """
plane:
  base_url: "https://plane.example.com"
orchestrator:
  port: 8100
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(cfg_text)
        f.flush()
        config = load_config(Path(f.name))

    assert config.max_concurrent_agents == 3
    assert config.agent_timeout_minutes == 30
