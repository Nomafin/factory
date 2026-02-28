"""Docker toolkit for Factory agents.

Provides a simple interface for agents to spin up and tear down
Docker test/preview environments with Traefik auto-discovery.

## Compose File Integration

When calling `spin_up()`, the following environment variables are passed
to your docker-compose file:

- `FACTORY_TASK_ID` — The task ID (e.g., "42")
- `FACTORY_REPO` — The repository name (e.g., "acme/webapp")
- `FACTORY_HOSTNAME` — The public hostname (e.g., "task-42.preview.factory.6a.fi")
- `FACTORY_SERVICE_PORT` — The service port passed to spin_up()
- `FACTORY_CREATED` — Unix timestamp of environment creation

Your compose file should use these to set Factory labels (for cleanup scripts)
and Traefik labels (for routing). Example:

```yaml
services:
  app:
    build: .
    labels:
      # Factory labels (required for cleanup scripts)
      - "factory.task-id=${FACTORY_TASK_ID}"
      - "factory.repo=${FACTORY_REPO}"
      - "factory.env-type=test"
      - "factory.created=${FACTORY_CREATED:-0}"
      # Traefik labels (required for routing)
      - "traefik.enable=true"
      - "traefik.http.routers.app.rule=Host(`${FACTORY_HOSTNAME}`)"
      - "traefik.http.routers.app.entrypoints=websecure"
      - "traefik.http.routers.app.tls=true"
      - "traefik.http.services.app.loadbalancer.server.port=${FACTORY_SERVICE_PORT}"
    networks:
      - default
      - factory-preview

networks:
  factory-preview:
    external: true
```

**Important:** Factory generates a compose override file that enforces the
correct Traefik labels (``websecure`` entrypoint + ``tls=true``) regardless
of what the agent's compose file contains.  You do not need to get Traefik
labels perfect — Factory will fix them at spin-up time.

Alternatively, use `get_labels()` and `get_traefik_labels()` to generate
labels programmatically if building compose files dynamically.
"""

import logging
import os
import subprocess
import time
from typing import Optional

import httpx
import yaml

logger = logging.getLogger(__name__)

# Docker network all preview/test containers join
FACTORY_NETWORK = "factory-preview"

# Preview domain suffix
PREVIEW_DOMAIN = "preview.factory.6a.fi"


class DockerEnvironment:
    """Manage Docker test/preview environments for agents."""

    def __init__(
        self,
        task_id: int,
        repo: str,
        pr_number: Optional[int] = None,
    ):
        self.task_id = task_id
        self.repo = repo
        self.pr_number = pr_number
        self.project_name = f"factory-task-{task_id}"
        self.env_type = "preview" if pr_number is not None else "test"

    def get_labels(self) -> dict[str, str]:
        """Return standard Factory labels for containers.

        These labels are used by cleanup scripts to identify and
        manage Factory-created containers.
        """
        labels: dict[str, str] = {
            "factory.task-id": str(self.task_id),
            "factory.repo": self.repo,
            "factory.env-type": self.env_type,
            "factory.created": str(int(time.time())),
        }
        if self.pr_number is not None:
            labels["factory.pr-number"] = str(self.pr_number)
        return labels

    def get_traefik_labels(self, service_port: int) -> dict[str, str]:
        """Return Traefik labels for auto-discovery routing.

        Args:
            service_port: The port the service listens on inside the container.
        """
        hostname = self._get_hostname()
        router_name = f"task-{self.task_id}"
        return {
            "traefik.enable": "true",
            f"traefik.http.routers.{router_name}.rule": f"Host(`{hostname}`)",
            f"traefik.http.routers.{router_name}.entrypoints": "websecure",
            f"traefik.http.routers.{router_name}.tls": "true",
            f"traefik.http.services.{router_name}.loadbalancer.server.port": str(
                service_port
            ),
        }

    def get_url(self) -> str:
        """Return the preview URL for this environment."""
        return f"https://{self._get_hostname()}"

    def _get_hostname(self) -> str:
        """Build the hostname for this environment."""
        if self.pr_number is not None:
            return f"pr-{self.pr_number}.{PREVIEW_DOMAIN}"
        return f"task-{self.task_id}.{PREVIEW_DOMAIN}"

    def spin_up(
        self,
        compose_file: str = "docker-compose.yml",
        service_port: int = 3000,
        health_endpoint: str = "/health",
        timeout_seconds: int = 120,
    ) -> str:
        """Start environment, wait for healthy, return URL.

        Generates a compose override file that enforces correct Traefik
        labels (``websecure`` entrypoint + ``tls=true``) and Factory
        metadata labels, so environments always get HTTPS routing
        regardless of what the agent's compose file contains.

        Args:
            compose_file: Path to the docker-compose file.
            service_port: Port the main service listens on.
            health_endpoint: HTTP path to poll for readiness.
            timeout_seconds: Max seconds to wait for healthy state.

        Returns:
            The public preview URL.

        Raises:
            subprocess.CalledProcessError: If docker compose fails.
            TimeoutError: If the health check doesn't pass in time.
        """
        url = self.get_url()
        hostname = self._get_hostname()

        # Ensure the factory-preview network exists
        _ensure_network()

        # Build environment variables for compose
        env = os.environ.copy()
        env.update(
            {
                "FACTORY_TASK_ID": str(self.task_id),
                "FACTORY_REPO": self.repo,
                "FACTORY_HOSTNAME": hostname,
                "FACTORY_SERVICE_PORT": str(service_port),
                "FACTORY_CREATED": str(int(time.time())),
            }
        )

        # Validate the compose file and log warnings
        warnings = validate_compose_file(compose_file)
        for warning in warnings:
            logger.warning("Compose validation: %s", warning)

        # Generate override file with correct Traefik/Factory labels
        override_path = self._generate_compose_override(
            compose_file, service_port,
        )

        logger.info(
            "Spinning up environment for task %d (project=%s, compose=%s)",
            self.task_id,
            self.project_name,
            compose_file,
        )

        # Build the compose command — use override file to enforce correct labels
        compose_cmd = [
            "docker",
            "compose",
            "-p",
            self.project_name,
            "-f",
            compose_file,
        ]
        if override_path:
            compose_cmd.extend(["-f", override_path])
        compose_cmd.extend(["up", "-d"])

        try:
            # Start with docker compose
            subprocess.run(
                compose_cmd,
                env=env,
                check=True,
                capture_output=True,
            )
        finally:
            # Clean up the override file
            if override_path and os.path.exists(override_path):
                os.unlink(override_path)

        # Connect containers to the factory-preview network
        self._connect_to_network()

        # Wait for health check
        health_url = url + health_endpoint
        logger.info(
            "Waiting for %s to become healthy (timeout=%ds)",
            health_url,
            timeout_seconds,
        )
        _wait_for_healthy(health_url, timeout_seconds)

        logger.info("Environment ready at %s", url)
        return url

    def _generate_compose_override(
        self,
        compose_file: str,
        service_port: int,
    ) -> str:
        """Generate a compose override file with correct Factory/Traefik labels.

        Reads the original compose file to discover service names, then
        creates a temporary override that sets the correct Traefik routing
        labels (``websecure`` entrypoint, ``tls=true``) and Factory metadata
        labels on the first service.

        The override file uses YAML mapping syntax for labels so that
        Docker Compose merges individual label keys instead of replacing
        the entire labels list.

        Args:
            compose_file: Path to the original docker-compose file.
            service_port: Port the main service listens on.

        Returns:
            Path to the generated override file, or empty string on failure.
        """
        try:
            with open(compose_file) as f:
                compose_data = yaml.safe_load(f)
        except (FileNotFoundError, yaml.YAMLError) as exc:
            logger.warning(
                "Could not parse compose file %s: %s", compose_file, exc,
            )
            return ""

        if not isinstance(compose_data, dict):
            logger.warning("Invalid compose file structure in %s", compose_file)
            return ""

        services = compose_data.get("services", {})
        if not services:
            logger.warning("No services found in compose file %s", compose_file)
            return ""

        # Use the first service as the main routable service
        service_name = next(iter(services))

        # Build all required labels
        all_labels = {**self.get_labels(), **self.get_traefik_labels(service_port)}

        # Generate override YAML as a string to avoid yaml.dump issues
        # with special characters (backticks in Host() rules, etc.)
        label_lines = []
        for key, value in all_labels.items():
            label_lines.append(f'      {key}: "{value}"')

        override_content = (
            f"services:\n"
            f"  {service_name}:\n"
            f"    labels:\n"
            + "\n".join(label_lines) + "\n"
            f"    networks:\n"
            f"      - default\n"
            f"      - factory-preview\n"
            f"networks:\n"
            f"  factory-preview:\n"
            f"    external: true\n"
        )

        override_path = os.path.join(
            os.path.dirname(os.path.abspath(compose_file)),
            f".factory-override-{self.task_id}.yml",
        )
        try:
            with open(override_path, "w") as f:
                f.write(override_content)
        except OSError as exc:
            logger.warning(
                "Could not write compose override to %s: %s",
                override_path, exc,
            )
            return ""

        logger.info("Generated compose override at %s", override_path)
        return override_path

    def tear_down(self, compose_file: str = "docker-compose.yml") -> None:
        """Stop and remove the environment.

        Args:
            compose_file: Path to the docker-compose file used for spin_up.
        """
        logger.info(
            "Tearing down environment for task %d (project=%s)",
            self.task_id,
            self.project_name,
        )
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                self.project_name,
                "-f",
                compose_file,
                "down",
                "-v",
                "--remove-orphans",
            ],
            check=True,
            capture_output=True,
        )

    def _connect_to_network(self) -> None:
        """Connect all project containers to the factory-preview network."""
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={self.project_name}",
            ],
            capture_output=True,
            text=True,
        )
        container_ids = result.stdout.strip().split("\n")
        for cid in container_ids:
            if cid:
                subprocess.run(
                    ["docker", "network", "connect", FACTORY_NETWORK, cid],
                    capture_output=True,
                    # Don't check — may already be connected
                )


# ── Validation ─────────────────────────────────────────────────────────


def validate_compose_file(compose_file: str) -> list[str]:
    """Validate a docker-compose file for common Factory/Traefik issues.

    Checks for problems that would prevent HTTPS routing or proper
    container lifecycle management:

    - Wrong Traefik entrypoint (``web`` instead of ``websecure``)
    - Missing ``tls=true`` label when Traefik is enabled
    - Missing ``factory-preview`` network declaration

    Args:
        compose_file: Path to the docker-compose file to validate.

    Returns:
        A list of warning messages.  Empty list means no issues found.
    """
    warnings: list[str] = []

    try:
        with open(compose_file) as f:
            content = f.read()
    except FileNotFoundError:
        return [f"Compose file not found: {compose_file}"]

    # Check for wrong entrypoint — HTTP-only "web" instead of HTTPS "websecure"
    if "entrypoints=web" in content and "entrypoints=websecure" not in content:
        warnings.append(
            "Compose file uses entrypoints=web (HTTP) instead of "
            "entrypoints=websecure (HTTPS). Factory will override this "
            "with the correct HTTPS entrypoint."
        )

    # Check for missing tls label when Traefik is enabled
    has_traefik = "traefik.enable" in content
    has_tls = ".tls=true" in content or '.tls="true"' in content or ".tls: " in content
    if has_traefik and not has_tls:
        warnings.append(
            "Compose file enables Traefik but is missing tls=true label. "
            "Factory will add the correct TLS configuration."
        )

    # Check for missing factory-preview network
    if "factory-preview" not in content:
        warnings.append(
            "Compose file is missing factory-preview network declaration. "
            "Factory will add this via the compose override."
        )

    return warnings


# ── Helper functions ────────────────────────────────────────────────────


def _ensure_network() -> None:
    """Create the factory-preview Docker network if it doesn't exist."""
    result = subprocess.run(
        [
            "docker",
            "network",
            "ls",
            "--filter",
            f"name=^{FACTORY_NETWORK}$",
            "--format",
            "{{.Name}}",
        ],
        capture_output=True,
        text=True,
    )
    if FACTORY_NETWORK not in result.stdout:
        logger.info("Creating Docker network: %s", FACTORY_NETWORK)
        subprocess.run(
            ["docker", "network", "create", FACTORY_NETWORK],
            check=True,
            capture_output=True,
        )


def _wait_for_healthy(health_url: str, timeout: int) -> None:
    """Poll a health endpoint until it returns 200.

    Args:
        health_url: Full URL to poll.
        timeout: Max seconds to wait.

    Raises:
        TimeoutError: If the endpoint doesn't become healthy in time.
    """
    start = time.monotonic()
    last_error: str = ""

    while time.monotonic() - start < timeout:
        try:
            resp = httpx.get(health_url, timeout=5, verify=False)
            if resp.status_code == 200:
                return
            last_error = f"status {resp.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(2)

    elapsed = int(time.monotonic() - start)
    raise TimeoutError(
        f"Environment not healthy after {elapsed}s (last error: {last_error})"
    )


# ── Convenience functions for agents ────────────────────────────────────

_current_env: Optional[DockerEnvironment] = None


def _get_task_context() -> tuple[int, str]:
    """Read task context from environment variables."""
    task_id = int(os.environ.get("FACTORY_TASK_ID", "0"))
    repo = os.environ.get("FACTORY_REPO", "unknown")
    return task_id, repo


def spin_up_test_env(
    compose_file: str = "docker-compose.yml", **kwargs: object
) -> str:
    """Spin up an ephemeral test environment. Returns URL when healthy.

    Reads FACTORY_TASK_ID and FACTORY_REPO from the environment.
    The environment is tracked as a module-level singleton so it can
    be torn down later with :func:`tear_down_test_env`.

    Args:
        compose_file: Path to docker-compose file.
        **kwargs: Forwarded to :meth:`DockerEnvironment.spin_up`.

    Returns:
        The public preview URL.
    """
    global _current_env  # noqa: PLW0603
    task_id, repo = _get_task_context()
    _current_env = DockerEnvironment(task_id, repo)
    return _current_env.spin_up(compose_file, **kwargs)


def tear_down_test_env(compose_file: str = "docker-compose.yml") -> None:
    """Tear down the current test environment.

    Args:
        compose_file: Path to docker-compose file used during spin-up.
    """
    global _current_env  # noqa: PLW0603
    if _current_env is not None:
        _current_env.tear_down(compose_file)
        _current_env = None
    else:
        logger.warning("tear_down_test_env called but no environment is active")


def spin_up_preview_env(
    pr_number: int,
    compose_file: str = "docker-compose.yml",
    **kwargs: object,
) -> str:
    """Spin up a PR preview environment. Returns URL when healthy.

    Preview environments persist until the PR is merged/closed and
    are cleaned up by the scheduled cleanup scripts.

    Args:
        pr_number: The pull request number.
        compose_file: Path to docker-compose file.
        **kwargs: Forwarded to :meth:`DockerEnvironment.spin_up`.

    Returns:
        The public preview URL.
    """
    task_id, repo = _get_task_context()
    env = DockerEnvironment(task_id, repo, pr_number=pr_number)
    return env.spin_up(compose_file, **kwargs)


# ── Task-lifecycle cleanup ──────────────────────────────────────────────


def cleanup_test_environments(task_id: int) -> int:
    """Remove test environments for a completed task.

    Finds all Docker containers with matching ``factory.task-id`` **and**
    ``factory.env-type=test`` labels, then stops and removes them.

    Preview environments (``factory.env-type=preview``) are intentionally
    left running — they are cleaned up by a separate cron job when the
    associated PR is merged or closed.

    This function is best-effort: individual container failures are logged
    but do not raise exceptions.

    Args:
        task_id: The Factory task ID whose test containers should be removed.

    Returns:
        The number of containers that were successfully removed.
    """
    logger.info("Cleaning up test environments for task %d", task_id)

    try:
        result = subprocess.run(
            [
                "docker", "ps", "-aq",
                "--filter", f"label=factory.task-id={task_id}",
                "--filter", "label=factory.env-type=test",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning(
            "Failed to list containers for task %d: %s", task_id, exc,
        )
        return 0

    if result.returncode != 0:
        logger.warning(
            "docker ps failed for task %d (rc=%d): %s",
            task_id, result.returncode, result.stderr.strip(),
        )
        return 0

    container_ids = [cid for cid in result.stdout.strip().split("\n") if cid]

    if not container_ids:
        logger.info("No test containers found for task %d", task_id)
        return 0

    logger.info(
        "Found %d test container(s) for task %d: %s",
        len(container_ids), task_id, ", ".join(container_ids),
    )

    removed = 0
    for container_id in container_ids:
        try:
            subprocess.run(
                ["docker", "stop", container_id],
                capture_output=True,
                timeout=30,
                check=False,
            )
            subprocess.run(
                ["docker", "rm", container_id],
                capture_output=True,
                timeout=30,
                check=False,
            )
            logger.info(
                "Removed test container %s for task %d",
                container_id, task_id,
            )
            removed += 1
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.warning(
                "Failed to remove container %s for task %d: %s",
                container_id, task_id, exc,
            )

    logger.info(
        "Cleaned up %d/%d test container(s) for task %d",
        removed, len(container_ids), task_id,
    )
    return removed
