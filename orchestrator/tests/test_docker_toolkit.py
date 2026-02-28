"""Tests for the Docker toolkit module.

Unit tests for label generation, URL construction, and Traefik labels.
Integration test for spinning up nginx, verifying the URL, and tearing down.
"""

import os
import subprocess
import time
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

import yaml

from factory.docker_toolkit import (
    FACTORY_NETWORK,
    PREVIEW_DOMAIN,
    DockerEnvironment,
    _ensure_network,
    _get_task_context,
    _wait_for_healthy,
    spin_up_preview_env,
    spin_up_test_env,
    tear_down_test_env,
    validate_compose_file,
)


def _docker_available() -> bool:
    """Check if Docker daemon is available."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ── DockerEnvironment.__init__ ──────────────────────────────────────────


class TestDockerEnvironmentInit:
    def test_basic_init(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        assert env.task_id == 42
        assert env.repo == "acme/webapp"
        assert env.pr_number is None
        assert env.project_name == "factory-task-42"
        assert env.env_type == "test"

    def test_preview_init(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp", pr_number=15)
        assert env.pr_number == 15
        assert env.env_type == "preview"
        assert env.project_name == "factory-task-42"

    def test_test_env_type_without_pr(self):
        env = DockerEnvironment(task_id=1, repo="r")
        assert env.env_type == "test"

    def test_preview_env_type_with_pr(self):
        env = DockerEnvironment(task_id=1, repo="r", pr_number=0)
        # pr_number=0 is still a valid PR number
        assert env.env_type == "preview"


# ── get_labels ──────────────────────────────────────────────────────────


class TestGetLabels:
    def test_test_env_labels(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        labels = env.get_labels()

        assert labels["factory.task-id"] == "42"
        assert labels["factory.repo"] == "acme/webapp"
        assert labels["factory.env-type"] == "test"
        assert "factory.created" in labels
        # created should be a valid unix timestamp
        created = int(labels["factory.created"])
        assert created > 0
        assert "factory.pr-number" not in labels

    def test_preview_env_labels(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp", pr_number=15)
        labels = env.get_labels()

        assert labels["factory.task-id"] == "42"
        assert labels["factory.repo"] == "acme/webapp"
        assert labels["factory.env-type"] == "preview"
        assert labels["factory.pr-number"] == "15"

    def test_created_timestamp_is_recent(self):
        before = int(time.time())
        env = DockerEnvironment(task_id=1, repo="r")
        labels = env.get_labels()
        after = int(time.time())

        created = int(labels["factory.created"])
        assert before <= created <= after

    def test_labels_all_strings(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp", pr_number=15)
        labels = env.get_labels()
        for key, value in labels.items():
            assert isinstance(key, str), f"Key {key!r} is not a string"
            assert isinstance(value, str), f"Value for {key!r} is not a string"

    def test_required_label_keys_present(self):
        """Verify all required label keys exist for cleanup scripts."""
        env = DockerEnvironment(task_id=1, repo="test/repo")
        labels = env.get_labels()
        required_keys = {"factory.task-id", "factory.repo", "factory.env-type", "factory.created"}
        assert required_keys.issubset(labels.keys())

    def test_preview_has_pr_number_label(self):
        env = DockerEnvironment(task_id=1, repo="test/repo", pr_number=99)
        labels = env.get_labels()
        assert "factory.pr-number" in labels
        assert labels["factory.pr-number"] == "99"


# ── get_traefik_labels ──────────────────────────────────────────────────


class TestGetTraefikLabels:
    def test_traefik_labels_for_test_env(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        labels = env.get_traefik_labels(service_port=3000)

        assert labels["traefik.enable"] == "true"
        assert labels["traefik.http.routers.task-42.rule"] == (
            f"Host(`task-42.{PREVIEW_DOMAIN}`)"
        )
        assert labels["traefik.http.routers.task-42.entrypoints"] == "websecure"
        assert labels["traefik.http.routers.task-42.tls"] == "true"
        assert labels["traefik.http.services.task-42.loadbalancer.server.port"] == "3000"

    def test_traefik_labels_for_preview_env(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp", pr_number=15)
        labels = env.get_traefik_labels(service_port=8080)

        assert labels["traefik.http.routers.task-42.rule"] == (
            f"Host(`pr-15.{PREVIEW_DOMAIN}`)"
        )
        assert labels["traefik.http.services.task-42.loadbalancer.server.port"] == "8080"

    def test_traefik_labels_custom_port(self):
        env = DockerEnvironment(task_id=1, repo="r")
        labels = env.get_traefik_labels(service_port=9999)
        assert labels["traefik.http.services.task-1.loadbalancer.server.port"] == "9999"


# ── get_url / _get_hostname ─────────────────────────────────────────────


class TestGetUrl:
    def test_test_env_url(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        assert env.get_url() == f"https://task-42.{PREVIEW_DOMAIN}"

    def test_preview_env_url(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp", pr_number=15)
        assert env.get_url() == f"https://pr-15.{PREVIEW_DOMAIN}"

    def test_hostname_test(self):
        env = DockerEnvironment(task_id=7, repo="r")
        assert env._get_hostname() == f"task-7.{PREVIEW_DOMAIN}"

    def test_hostname_preview(self):
        env = DockerEnvironment(task_id=7, repo="r", pr_number=3)
        assert env._get_hostname() == f"pr-3.{PREVIEW_DOMAIN}"


# ── spin_up ─────────────────────────────────────────────────────────────


class TestSpinUp:
    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_calls_docker_compose(self, mock_run, mock_net, mock_health):
        mock_run.return_value = MagicMock(stdout="abc123\n", returncode=0)

        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        url = env.spin_up(compose_file="dc.yml", service_port=3000)

        assert url == f"https://task-42.{PREVIEW_DOMAIN}"

        # First call should be docker compose up
        compose_call = mock_run.call_args_list[0]
        cmd = compose_call[0][0]
        assert "docker" in cmd
        assert "compose" in cmd
        assert "-p" in cmd
        assert "factory-task-42" in cmd
        assert "-f" in cmd
        assert "dc.yml" in cmd
        assert "up" in cmd
        assert "-d" in cmd

    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_sets_env_vars(self, mock_run, mock_net, mock_health):
        mock_run.return_value = MagicMock(stdout="", returncode=0)

        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        env.spin_up(service_port=8080)

        compose_call = mock_run.call_args_list[0]
        call_env = compose_call[1].get("env") or compose_call[0][1] if len(compose_call[0]) > 1 else compose_call[1].get("env")
        assert call_env["FACTORY_TASK_ID"] == "42"
        assert call_env["FACTORY_REPO"] == "acme/webapp"
        assert call_env["FACTORY_SERVICE_PORT"] == "8080"

    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_sets_factory_created_env_var(self, mock_run, mock_net, mock_health):
        mock_run.return_value = MagicMock(stdout="", returncode=0)

        before = int(time.time())
        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        env.spin_up(service_port=3000)
        after = int(time.time())

        compose_call = mock_run.call_args_list[0]
        call_env = compose_call[1].get("env") or compose_call[0][1] if len(compose_call[0]) > 1 else compose_call[1].get("env")
        assert "FACTORY_CREATED" in call_env
        created = int(call_env["FACTORY_CREATED"])
        assert before <= created <= after

    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_sets_factory_hostname_env_var(self, mock_run, mock_net, mock_health):
        mock_run.return_value = MagicMock(stdout="", returncode=0)

        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        env.spin_up(service_port=3000)

        compose_call = mock_run.call_args_list[0]
        call_env = compose_call[1].get("env") or compose_call[0][1] if len(compose_call[0]) > 1 else compose_call[1].get("env")
        assert call_env["FACTORY_HOSTNAME"] == f"task-42.{PREVIEW_DOMAIN}"

    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_ensures_network(self, mock_run, mock_net, mock_health):
        mock_run.return_value = MagicMock(stdout="", returncode=0)

        env = DockerEnvironment(task_id=1, repo="r")
        env.spin_up()

        mock_net.assert_called_once()

    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_waits_for_healthy(self, mock_run, mock_net, mock_health):
        mock_run.return_value = MagicMock(stdout="", returncode=0)

        env = DockerEnvironment(task_id=42, repo="r")
        env.spin_up(health_endpoint="/ready", timeout_seconds=60)

        mock_health.assert_called_once_with(
            f"https://task-42.{PREVIEW_DOMAIN}/ready",
            60,
        )

    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_connects_to_network(self, mock_run, mock_net, mock_health):
        env = DockerEnvironment(task_id=1, repo="r")

        def run_side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0)
            # docker ps returns container IDs
            if "ps" in cmd and "-q" in cmd:
                result.stdout = "abc123\ndef456\n"
            else:
                result.stdout = ""
            return result

        mock_run.side_effect = run_side_effect

        env.spin_up()

        # Check network connect calls by inspecting the command (first positional arg)
        network_calls = [
            c for c in mock_run.call_args_list
            if len(c[0]) > 0 and "network" in c[0][0] and "connect" in c[0][0]
        ]
        assert len(network_calls) == 2


# ── _generate_compose_override ──────────────────────────────────────────


class TestGenerateComposeOverride:
    def test_generates_override_with_correct_labels(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: nginx\n")

        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        override_path = env._generate_compose_override(str(compose), 3000)

        assert override_path != ""
        assert os.path.exists(override_path)

        content = open(override_path).read()
        assert "websecure" in content
        assert ".tls:" in content
        assert "factory-preview" in content
        assert f"Host(`task-42.{PREVIEW_DOMAIN}`)" in content
        assert "factory.task-id:" in content
        assert '"42"' in content

        # Clean up
        os.unlink(override_path)

    def test_override_targets_first_service(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n  web:\n    image: nginx\n  db:\n    image: postgres\n"
        )

        env = DockerEnvironment(task_id=1, repo="r")
        override_path = env._generate_compose_override(str(compose), 8080)

        assert override_path != ""
        content = open(override_path).read()
        # Should target 'web' (first service), not 'db'
        assert "  web:" in content

        os.unlink(override_path)

    def test_override_includes_traefik_and_factory_labels(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: nginx\n")

        env = DockerEnvironment(task_id=42, repo="acme/webapp", pr_number=15)
        override_path = env._generate_compose_override(str(compose), 3000)

        content = open(override_path).read()
        # Traefik labels
        assert "traefik.enable" in content
        assert "websecure" in content
        assert ".tls:" in content
        assert "loadbalancer.server.port" in content
        # Factory labels
        assert "factory.task-id" in content
        assert "factory.repo" in content
        assert "factory.env-type" in content
        assert "factory.created" in content
        assert "factory.pr-number" in content

        os.unlink(override_path)

    def test_override_returns_empty_for_missing_file(self):
        env = DockerEnvironment(task_id=1, repo="r")
        result = env._generate_compose_override("/nonexistent/compose.yml", 3000)
        assert result == ""

    def test_override_returns_empty_for_invalid_yaml(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(": : : not valid yaml [[[")

        env = DockerEnvironment(task_id=1, repo="r")
        result = env._generate_compose_override(str(compose), 3000)
        assert result == ""

    def test_override_returns_empty_for_no_services(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("version: '3'\n")

        env = DockerEnvironment(task_id=1, repo="r")
        result = env._generate_compose_override(str(compose), 3000)
        assert result == ""

    def test_override_file_is_valid_yaml(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: nginx\n")

        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        override_path = env._generate_compose_override(str(compose), 3000)

        # Should be parseable as YAML
        with open(override_path) as f:
            data = yaml.safe_load(f)

        assert "services" in data
        assert "app" in data["services"]
        assert "labels" in data["services"]["app"]
        assert "networks" in data
        assert "factory-preview" in data["networks"]

        os.unlink(override_path)

    def test_override_uses_correct_port(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: nginx\n")

        env = DockerEnvironment(task_id=1, repo="r")
        override_path = env._generate_compose_override(str(compose), 9999)

        content = open(override_path).read()
        assert '"9999"' in content

        os.unlink(override_path)


# ── spin_up with compose override ──────────────────────────────────────


class TestSpinUpWithOverride:
    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_uses_override_file(self, mock_run, mock_net, mock_health, tmp_path):
        mock_run.return_value = MagicMock(stdout="", returncode=0)

        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: nginx\n")

        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        url = env.spin_up(compose_file=str(compose), service_port=3000)

        assert url == f"https://task-42.{PREVIEW_DOMAIN}"

        # First subprocess call should be docker compose with TWO -f flags
        compose_call = mock_run.call_args_list[0]
        cmd = compose_call[0][0]

        # Count -f flags — should be 2 (original + override)
        f_indices = [i for i, x in enumerate(cmd) if x == "-f"]
        assert len(f_indices) == 2, f"Expected 2 -f flags, got {len(f_indices)} in {cmd}"

        # First -f should be the original compose file
        assert cmd[f_indices[0] + 1] == str(compose)
        # Second -f should be the override file
        override_arg = cmd[f_indices[1] + 1]
        assert ".factory-override-42" in override_arg

    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_cleans_up_override_file(self, mock_run, mock_net, mock_health, tmp_path):
        mock_run.return_value = MagicMock(stdout="", returncode=0)

        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: nginx\n")

        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        env.spin_up(compose_file=str(compose), service_port=3000)

        # Override file should be cleaned up after spin_up
        override_path = tmp_path / ".factory-override-42.yml"
        assert not override_path.exists(), "Override file should be cleaned up"

    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_cleans_up_override_on_failure(self, mock_run, mock_net, mock_health, tmp_path):
        mock_run.side_effect = subprocess.CalledProcessError(1, "docker compose")

        compose = tmp_path / "docker-compose.yml"
        compose.write_text("services:\n  app:\n    image: nginx\n")

        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        with pytest.raises(subprocess.CalledProcessError):
            env.spin_up(compose_file=str(compose), service_port=3000)

        # Override file should be cleaned up even on failure
        override_path = tmp_path / ".factory-override-42.yml"
        assert not override_path.exists(), "Override file should be cleaned up on failure"

    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_override_enforces_websecure(self, mock_run, mock_net, mock_health, tmp_path):
        """Even if compose file uses 'web' entrypoint, override uses 'websecure'."""
        mock_run.return_value = MagicMock(stdout="", returncode=0)

        # Write a compose file with WRONG entrypoint
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  app:\n"
            "    image: nginx\n"
            "    labels:\n"
            '      - "traefik.enable=true"\n'
            '      - "traefik.http.routers.app.entrypoints=web"\n'
        )

        env = DockerEnvironment(task_id=42, repo="acme/webapp")

        # Capture what the override file contains before it's deleted
        original_unlink = os.unlink
        override_content = {}

        def capture_and_unlink(path):
            if ".factory-override" in str(path):
                override_content["data"] = open(path).read()
            original_unlink(path)

        with patch("factory.docker_toolkit.os.unlink", side_effect=capture_and_unlink):
            env.spin_up(compose_file=str(compose), service_port=3000)

        # The override should enforce websecure, not web
        assert "websecure" in override_content["data"]
        assert "entrypoints=web" not in override_content["data"]


# ── validate_compose_file ──────────────────────────────────────────────


class TestValidateComposeFile:
    def test_valid_compose_no_warnings(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  app:\n"
            "    image: nginx\n"
            "    labels:\n"
            '      - "traefik.enable=true"\n'
            '      - "traefik.http.routers.app.entrypoints=websecure"\n'
            '      - "traefik.http.routers.app.tls=true"\n'
            "    networks:\n"
            "      - factory-preview\n"
            "networks:\n"
            "  factory-preview:\n"
            "    external: true\n"
        )
        warnings = validate_compose_file(str(compose))
        assert warnings == []

    def test_detects_wrong_entrypoint(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            '      - "traefik.http.routers.app.entrypoints=web"\n'
            '      - "traefik.http.routers.app.tls=true"\n'
            "    networks:\n"
            "      - factory-preview\n"
        )
        warnings = validate_compose_file(str(compose))
        assert any("entrypoints=web" in w for w in warnings)

    def test_does_not_flag_websecure(self, tmp_path):
        """entrypoints=websecure should NOT trigger the wrong-entrypoint warning."""
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            '      - "traefik.http.routers.app.entrypoints=websecure"\n'
            '      - "traefik.http.routers.app.tls=true"\n'
            "    networks:\n"
            "      - factory-preview\n"
        )
        warnings = validate_compose_file(str(compose))
        assert not any("entrypoints=web" in w for w in warnings)

    def test_detects_missing_tls(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            '      - "traefik.enable=true"\n'
            '      - "traefik.http.routers.app.entrypoints=websecure"\n'
            "    networks:\n"
            "      - factory-preview\n"
        )
        warnings = validate_compose_file(str(compose))
        assert any("tls" in w.lower() for w in warnings)

    def test_detects_missing_factory_preview_network(self, tmp_path):
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  app:\n"
            "    image: nginx\n"
        )
        warnings = validate_compose_file(str(compose))
        assert any("factory-preview" in w for w in warnings)

    def test_file_not_found(self):
        warnings = validate_compose_file("/nonexistent/file.yml")
        assert len(warnings) == 1
        assert "not found" in warnings[0]

    def test_multiple_warnings(self, tmp_path):
        """A compose with all issues should report multiple warnings."""
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  app:\n"
            "    labels:\n"
            '      - "traefik.enable=true"\n'
            '      - "traefik.http.routers.app.entrypoints=web"\n'
        )
        warnings = validate_compose_file(str(compose))
        # Should have warnings for: wrong entrypoint, missing tls, missing network
        assert len(warnings) == 3

    def test_no_traefik_no_tls_warning(self, tmp_path):
        """If traefik.enable is not present, don't warn about missing tls."""
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            "services:\n"
            "  app:\n"
            "    image: nginx\n"
            "    networks:\n"
            "      - factory-preview\n"
        )
        warnings = validate_compose_file(str(compose))
        assert not any("tls" in w.lower() for w in warnings)


# ── tear_down ───────────────────────────────────────────────────────────


class TestTearDown:
    @patch("factory.docker_toolkit.subprocess.run")
    def test_tear_down_calls_compose_down(self, mock_run):
        env = DockerEnvironment(task_id=42, repo="acme/webapp")
        env.tear_down(compose_file="dc.yml")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "docker" in cmd
        assert "compose" in cmd
        assert "-p" in cmd
        assert "factory-task-42" in cmd
        assert "down" in cmd
        assert "-v" in cmd
        assert "--remove-orphans" in cmd
        assert "-f" in cmd
        assert "dc.yml" in cmd

    @patch("factory.docker_toolkit.subprocess.run")
    def test_tear_down_default_compose_file(self, mock_run):
        env = DockerEnvironment(task_id=1, repo="r")
        env.tear_down()

        cmd = mock_run.call_args[0][0]
        assert "docker-compose.yml" in cmd


# ── _ensure_network ─────────────────────────────────────────────────────


class TestEnsureNetwork:
    @patch("factory.docker_toolkit.subprocess.run")
    def test_creates_network_when_missing(self, mock_run):
        # First call: network ls returns empty
        mock_run.side_effect = [
            MagicMock(stdout="", returncode=0),  # network ls
            MagicMock(returncode=0),  # network create
        ]

        _ensure_network()

        assert mock_run.call_count == 2
        create_cmd = mock_run.call_args_list[1][0][0]
        assert "network" in create_cmd
        assert "create" in create_cmd
        assert FACTORY_NETWORK in create_cmd

    @patch("factory.docker_toolkit.subprocess.run")
    def test_skips_create_when_exists(self, mock_run):
        mock_run.return_value = MagicMock(stdout=f"{FACTORY_NETWORK}\n", returncode=0)

        _ensure_network()

        # Only the ls call, no create
        assert mock_run.call_count == 1


# ── _wait_for_healthy ───────────────────────────────────────────────────


class TestWaitForHealthy:
    @patch("factory.docker_toolkit.time.sleep")
    @patch("factory.docker_toolkit.httpx.get")
    def test_returns_on_200(self, mock_get, mock_sleep):
        mock_get.return_value = MagicMock(status_code=200)

        # Should not raise
        _wait_for_healthy("http://localhost/health", timeout=10)

        mock_get.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("factory.docker_toolkit.time.sleep")
    @patch("factory.docker_toolkit.httpx.get")
    @patch("factory.docker_toolkit.time.monotonic")
    def test_retries_on_error(self, mock_monotonic, mock_get, mock_sleep):
        # Simulate: t=0, t=1 (retry), t=2 (retry), t=3 (success)
        mock_monotonic.side_effect = [0, 1, 1, 3, 3, 5]
        mock_get.side_effect = [
            httpx.ConnectError("refused"),
            MagicMock(status_code=503),
            MagicMock(status_code=200),
        ]

        _wait_for_healthy("http://localhost/health", timeout=10)

        assert mock_get.call_count == 3

    @patch("factory.docker_toolkit.time.sleep")
    @patch("factory.docker_toolkit.httpx.get")
    @patch("factory.docker_toolkit.time.monotonic")
    def test_raises_timeout(self, mock_monotonic, mock_get, mock_sleep):
        # Calls: start=0, while-check=5, after-except sleep, while-check=11 (exit), elapsed=11
        mock_monotonic.side_effect = [0, 5, 5, 11, 11]
        mock_get.side_effect = httpx.ConnectError("refused")

        with pytest.raises(TimeoutError, match="not healthy after"):
            _wait_for_healthy("http://localhost/health", timeout=10)

    @patch("factory.docker_toolkit.time.sleep")
    @patch("factory.docker_toolkit.httpx.get")
    @patch("factory.docker_toolkit.time.monotonic")
    def test_timeout_includes_last_error(self, mock_monotonic, mock_get, mock_sleep):
        # Calls: start=0, while-check=5, after-resp sleep, while-check=11 (exit), elapsed=11
        mock_monotonic.side_effect = [0, 5, 5, 11, 11]
        mock_get.return_value = MagicMock(status_code=503)

        with pytest.raises(TimeoutError, match="status 503"):
            _wait_for_healthy("http://localhost/health", timeout=10)


# ── _get_task_context ───────────────────────────────────────────────────


class TestGetTaskContext:
    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("FACTORY_TASK_ID", "42")
        monkeypatch.setenv("FACTORY_REPO", "acme/webapp")

        task_id, repo = _get_task_context()
        assert task_id == 42
        assert repo == "acme/webapp"

    def test_defaults_when_missing(self, monkeypatch):
        monkeypatch.delenv("FACTORY_TASK_ID", raising=False)
        monkeypatch.delenv("FACTORY_REPO", raising=False)

        task_id, repo = _get_task_context()
        assert task_id == 0
        assert repo == "unknown"


# ── Convenience functions ───────────────────────────────────────────────


class TestConvenienceFunctions:
    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_test_env(self, mock_run, mock_health, mock_net, monkeypatch):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        monkeypatch.setenv("FACTORY_TASK_ID", "42")
        monkeypatch.setenv("FACTORY_REPO", "acme/webapp")

        import factory.docker_toolkit as dt

        dt._current_env = None  # Reset state

        url = spin_up_test_env("dc.yml", service_port=8080)
        assert url == f"https://task-42.{PREVIEW_DOMAIN}"
        assert dt._current_env is not None
        assert dt._current_env.task_id == 42
        assert dt._current_env.env_type == "test"

    @patch("factory.docker_toolkit.subprocess.run")
    def test_tear_down_test_env(self, mock_run, monkeypatch):
        import factory.docker_toolkit as dt

        # Set up a fake current env
        dt._current_env = DockerEnvironment(task_id=42, repo="acme/webapp")

        tear_down_test_env("dc.yml")

        assert dt._current_env is None
        mock_run.assert_called_once()

    def test_tear_down_test_env_when_none(self):
        import factory.docker_toolkit as dt

        dt._current_env = None
        # Should not raise, just log a warning
        tear_down_test_env()

    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_preview_env(self, mock_run, mock_health, mock_net, monkeypatch):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        monkeypatch.setenv("FACTORY_TASK_ID", "42")
        monkeypatch.setenv("FACTORY_REPO", "acme/webapp")

        url = spin_up_preview_env(pr_number=15, compose_file="dc.yml")
        assert url == f"https://pr-15.{PREVIEW_DOMAIN}"

    @patch("factory.docker_toolkit._ensure_network")
    @patch("factory.docker_toolkit._wait_for_healthy")
    @patch("factory.docker_toolkit.subprocess.run")
    def test_spin_up_preview_env_default_compose(self, mock_run, mock_health, mock_net, monkeypatch):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        monkeypatch.setenv("FACTORY_TASK_ID", "10")
        monkeypatch.setenv("FACTORY_REPO", "r")

        url = spin_up_preview_env(pr_number=5)
        assert url == f"https://pr-5.{PREVIEW_DOMAIN}"

        compose_call = mock_run.call_args_list[0]
        cmd = compose_call[0][0]
        assert "docker-compose.yml" in cmd


# ── Integration test (Docker required) ──────────────────────────────────


@pytest.mark.skipif(
    not _docker_available(),
    reason="Docker not available",
)
class TestDockerIntegration:
    """Integration tests that require a running Docker daemon.

    These tests spin up real containers and verify the environment
    lifecycle. They are skipped if Docker is not available.
    """

    def test_nginx_spin_up_and_tear_down(self, tmp_path):
        """Spin up nginx, verify it responds, tear it down."""
        # Write a minimal docker-compose file
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(
            """\
services:
  web:
    image: nginx:alpine
    ports:
      - "0:80"
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://localhost/"]
      interval: 2s
      timeout: 2s
      retries: 3
"""
        )

        env = DockerEnvironment(task_id=99999, repo="integration/test")

        try:
            # Spin up (skip health check since we can't reach traefik URL)
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    env.project_name,
                    "-f",
                    str(compose),
                    "up",
                    "-d",
                ],
                check=True,
                capture_output=True,
            )

            # Wait for container to be running
            for _ in range(30):
                result = subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-p",
                        env.project_name,
                        "-f",
                        str(compose),
                        "ps",
                        "--format",
                        "json",
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.stdout.strip():
                    break
                time.sleep(1)

            # Verify container is running
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    env.project_name,
                    "-f",
                    str(compose),
                    "ps",
                    "-q",
                ],
                capture_output=True,
                text=True,
            )
            container_ids = [
                cid for cid in result.stdout.strip().split("\n") if cid
            ]
            assert len(container_ids) >= 1, "Expected at least one running container"

            # Get the mapped port and verify nginx responds
            port_result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    env.project_name,
                    "-f",
                    str(compose),
                    "port",
                    "web",
                    "80",
                ],
                capture_output=True,
                text=True,
            )
            if port_result.stdout.strip():
                host_port = port_result.stdout.strip().split(":")[-1]
                # Try to reach nginx
                for _ in range(10):
                    try:
                        resp = httpx.get(
                            f"http://localhost:{host_port}/",
                            timeout=2,
                        )
                        if resp.status_code == 200:
                            break
                    except httpx.HTTPError:
                        time.sleep(1)
                else:
                    pytest.fail("nginx did not respond")

        finally:
            # Always tear down
            env.tear_down(compose_file=str(compose))

            # Verify container is gone
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    env.project_name,
                    "-f",
                    str(compose),
                    "ps",
                    "-q",
                ],
                capture_output=True,
                text=True,
            )
            remaining = [
                cid for cid in result.stdout.strip().split("\n") if cid
            ]
            assert len(remaining) == 0, "Containers should be removed after tear_down"
