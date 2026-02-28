from pathlib import Path

from factory.prompts import load_prompt


# Path to the real prompts directory (project root)
# tests/test_prompts.py -> orchestrator/tests/ -> orchestrator/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


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


# ── Docker environment prompt tests ─────────────────────────────────────


class TestCoderPromptDockerSection:
    """Verify the coder prompt includes Docker test environment instructions."""

    def test_coder_prompt_loads_successfully(self):
        """The real coder.md prompt file loads without error."""
        result = load_prompt("prompts/coder.md", PROJECT_ROOT)
        assert len(result) > 0, "coder.md should not be empty"

    def test_coder_prompt_contains_docker_section(self):
        """The coder prompt includes the Docker Test Environments section."""
        result = load_prompt("prompts/coder.md", PROJECT_ROOT)
        assert "## Docker Test Environments" in result

    def test_coder_prompt_contains_testing_workflow(self):
        """The coder prompt documents the testing workflow steps."""
        result = load_prompt("prompts/coder.md", PROJECT_ROOT)
        assert "### Testing Workflow" in result
        assert "spin_up_test_env" in result
        assert "tear_down_test_env" in result

    def test_coder_prompt_contains_preview_env_section(self):
        """The coder prompt documents PR preview environments."""
        result = load_prompt("prompts/coder.md", PROJECT_ROOT)
        assert "### PR Preview Environments" in result
        assert "spin_up_preview_env" in result

    def test_coder_prompt_contains_project_requirements(self):
        """The coder prompt documents project requirements for Docker."""
        result = load_prompt("prompts/coder.md", PROJECT_ROOT)
        assert "### Requirements for Projects" in result
        assert "docker-compose.yml" in result
        assert "/health" in result

    def test_coder_prompt_contains_customization_options(self):
        """The coder prompt documents how to customize environment options."""
        result = load_prompt("prompts/coder.md", PROJECT_ROOT)
        assert "### Customizing Environment Options" in result
        assert "service_port" in result
        assert "health_endpoint" in result
        assert "timeout_seconds" in result

    def test_coder_prompt_documents_auto_cleanup(self):
        """The coder prompt mentions automatic cleanup on task completion."""
        result = load_prompt("prompts/coder.md", PROJECT_ROOT)
        assert "automatically cleaned up" in result

    def test_coder_prompt_retains_existing_sections(self):
        """Adding Docker section did not remove existing prompt sections."""
        result = load_prompt("prompts/coder.md", PROJECT_ROOT)
        assert "## Rules" in result
        assert "## Inter-Agent Communication" in result
        assert "## Questions for the Human" in result


class TestDockerComposeTemplate:
    """Verify the docker-compose preview template exists and is valid."""

    def test_template_file_exists(self):
        """The docker-compose preview template file exists."""
        template_path = PROJECT_ROOT / "prompts" / "templates" / "docker-compose.preview.yml"
        assert template_path.is_file(), f"Template not found at {template_path}"

    def test_template_contains_factory_labels(self):
        """The template includes required Factory labels."""
        template_path = PROJECT_ROOT / "prompts" / "templates" / "docker-compose.preview.yml"
        content = template_path.read_text()
        assert "factory.task-id" in content
        assert "factory.repo" in content
        assert "factory.env-type" in content
        assert "factory.created" in content

    def test_template_contains_traefik_labels(self):
        """The template includes Traefik routing labels."""
        template_path = PROJECT_ROOT / "prompts" / "templates" / "docker-compose.preview.yml"
        content = template_path.read_text()
        assert "traefik.enable=true" in content
        assert "traefik.http.routers" in content
        assert "traefik.http.services" in content

    def test_template_uses_factory_env_vars(self):
        """The template references Factory environment variables."""
        template_path = PROJECT_ROOT / "prompts" / "templates" / "docker-compose.preview.yml"
        content = template_path.read_text()
        assert "FACTORY_TASK_ID" in content
        assert "FACTORY_REPO" in content
        assert "FACTORY_HOSTNAME" in content
        assert "FACTORY_SERVICE_PORT" in content

    def test_template_uses_factory_preview_network(self):
        """The template joins the factory-preview network."""
        template_path = PROJECT_ROOT / "prompts" / "templates" / "docker-compose.preview.yml"
        content = template_path.read_text()
        assert "factory-preview" in content

    def test_template_has_healthcheck(self):
        """The template includes a healthcheck configuration."""
        template_path = PROJECT_ROOT / "prompts" / "templates" / "docker-compose.preview.yml"
        content = template_path.read_text()
        assert "healthcheck" in content
        assert "/health" in content

    def test_template_has_usage_comments(self):
        """The template has usage documentation in comments."""
        template_path = PROJECT_ROOT / "prompts" / "templates" / "docker-compose.preview.yml"
        content = template_path.read_text()
        assert "spin_up_test_env" in content
        assert "spin_up_preview_env" in content


class TestAgentsDocumentation:
    """Verify AGENTS.md documentation exists and covers Docker environments."""

    def test_agents_md_exists(self):
        """AGENTS.md documentation file exists."""
        agents_path = PROJECT_ROOT / "AGENTS.md"
        assert agents_path.is_file(), f"AGENTS.md not found at {agents_path}"

    def test_agents_md_covers_docker_environments(self):
        """AGENTS.md documents Docker test environments."""
        agents_path = PROJECT_ROOT / "AGENTS.md"
        content = agents_path.read_text()
        assert "## Docker Test Environments" in content

    def test_agents_md_covers_agent_api(self):
        """AGENTS.md documents the agent convenience API."""
        agents_path = PROJECT_ROOT / "AGENTS.md"
        content = agents_path.read_text()
        assert "spin_up_test_env" in content
        assert "tear_down_test_env" in content
        assert "spin_up_preview_env" in content

    def test_agents_md_covers_environment_variables(self):
        """AGENTS.md documents environment variables injected into compose."""
        agents_path = PROJECT_ROOT / "AGENTS.md"
        content = agents_path.read_text()
        assert "FACTORY_TASK_ID" in content
        assert "FACTORY_REPO" in content
        assert "FACTORY_HOSTNAME" in content
        assert "FACTORY_SERVICE_PORT" in content

    def test_agents_md_covers_cleanup_behaviour(self):
        """AGENTS.md documents cleanup behaviour for test and preview envs."""
        agents_path = PROJECT_ROOT / "AGENTS.md"
        content = agents_path.read_text()
        assert "Cleanup" in content
        assert "test" in content.lower()
        assert "preview" in content.lower()

    def test_agents_md_covers_container_labels(self):
        """AGENTS.md documents container labels."""
        agents_path = PROJECT_ROOT / "AGENTS.md"
        content = agents_path.read_text()
        assert "Container Labels" in content
        assert "factory.task-id" in content
        assert "factory.env-type" in content

    def test_agents_md_covers_project_configuration(self):
        """AGENTS.md documents how to configure projects for Docker envs."""
        agents_path = PROJECT_ROOT / "AGENTS.md"
        content = agents_path.read_text()
        assert "Configuring Your Project" in content
        assert "docker-compose" in content

    def test_agents_md_references_template(self):
        """AGENTS.md references the docker-compose template."""
        agents_path = PROJECT_ROOT / "AGENTS.md"
        content = agents_path.read_text()
        assert "docker-compose.preview.yml" in content

    def test_agents_md_lists_all_agent_types(self):
        """AGENTS.md documents all five agent types."""
        agents_path = PROJECT_ROOT / "AGENTS.md"
        content = agents_path.read_text()
        for agent_type in ["coder", "coder_revision", "reviewer", "researcher", "devops"]:
            assert agent_type in content, f"Agent type '{agent_type}' not in AGENTS.md"
