"""Playwright test runner for Factory agents.

Provides helpers for running Playwright end-to-end tests against Docker
test/preview environments.  Designed to integrate seamlessly with the
:mod:`factory.docker_toolkit` module.

## Typical workflow

```python
from factory.docker_toolkit import spin_up_test_env, tear_down_test_env
from factory.playwright_runner import run_playwright_tests

# 1. Spin up a test environment
url = spin_up_test_env("docker-compose.yml", service_port=3000)

# 2. Run Playwright tests against it
success, output = run_playwright_tests(base_url=url)

if success:
    print("All tests passed!")
else:
    print(f"Tests failed:\\n{output}")

# 3. Tear down when done
tear_down_test_env()
```

## Configuration

Drop a ``playwright.config.ts`` (or ``.js``) in your test directory.
A reference template is available at ``prompts/templates/playwright.config.ts``.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Supported browser projects
SUPPORTED_BROWSERS = ("chromium", "firefox", "webkit")

# Default test directory relative to the repo root
DEFAULT_TEST_DIR = "tests/e2e"

# Default timeout for the entire Playwright run (seconds)
DEFAULT_TIMEOUT = 300


class PlaywrightRunner:
    """Run Playwright tests against a target URL.

    Args:
        test_dir: Directory containing Playwright test files.
        base_url: The base URL tests run against (e.g. from docker_toolkit).
        browser: Browser project to use (chromium, firefox, webkit).
        headless: Whether to run in headless mode.
        timeout: Max seconds before the subprocess is killed.
        config_file: Optional path to a Playwright config file.
    """

    def __init__(
        self,
        test_dir: str = DEFAULT_TEST_DIR,
        base_url: Optional[str] = None,
        browser: str = "chromium",
        headless: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
        config_file: Optional[str] = None,
    ):
        if browser not in SUPPORTED_BROWSERS:
            raise ValueError(
                f"Unsupported browser {browser!r}. "
                f"Choose from: {', '.join(SUPPORTED_BROWSERS)}"
            )

        self.test_dir = test_dir
        self.base_url = base_url
        self.browser = browser
        self.headless = headless
        self.timeout = timeout
        self.config_file = config_file

    def build_command(self) -> list[str]:
        """Build the ``npx playwright test`` command list.

        Returns:
            The command as a list of strings ready for :func:`subprocess.run`.
        """
        cmd = ["npx", "playwright", "test"]

        cmd.extend(["--project", self.browser])

        if self.config_file:
            cmd.extend(["--config", self.config_file])

        if not self.headless:
            cmd.append("--headed")

        # Pass base URL via environment instead of CLI for broader compat
        # but also include the reporter flag for readable output
        cmd.extend(["--reporter", "list"])

        return cmd

    def build_env(self) -> dict[str, str]:
        """Build environment variables for the Playwright subprocess.

        The ``BASE_URL`` variable is set when :attr:`base_url` is provided,
        which Playwright's ``use.baseURL`` config option can pick up via
        ``process.env.BASE_URL``.

        Returns:
            Environment dict to pass to :func:`subprocess.run`.
        """
        env = os.environ.copy()

        if self.base_url:
            env["BASE_URL"] = self.base_url
            # Also set the Playwright-native env var
            env["PLAYWRIGHT_BASE_URL"] = self.base_url

        # Force headless if requested (CI-style)
        if self.headless:
            env["CI"] = "true"

        return env

    def run(self) -> tuple[bool, str]:
        """Execute the Playwright tests.

        Returns:
            A ``(success, output)`` tuple where *success* is ``True`` when
            all tests passed (return code 0) and *output* contains the
            combined stdout and stderr.

        Raises:
            PlaywrightNotFoundError: If ``npx`` is not on ``$PATH``.
            FileNotFoundError: If the test directory does not exist.
        """
        # Validate npx is available
        if not _npx_available():
            raise PlaywrightNotFoundError(
                "npx is not available on $PATH. "
                "Install Node.js and Playwright to run E2E tests."
            )

        test_path = Path(self.test_dir)
        if not test_path.is_dir():
            raise FileNotFoundError(
                f"Test directory does not exist: {self.test_dir}"
            )

        cmd = self.build_command()
        env = self.build_env()

        logger.info(
            "Running Playwright tests: dir=%s, browser=%s, base_url=%s",
            self.test_dir,
            self.browser,
            self.base_url or "(not set)",
        )
        logger.debug("Command: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                cwd=self.test_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "")
            logger.error(
                "Playwright tests timed out after %ds", self.timeout,
            )
            return False, f"Timed out after {self.timeout}s\n{output}"

        output = result.stdout + result.stderr
        success = result.returncode == 0

        if success:
            logger.info("Playwright tests passed")
        else:
            logger.warning(
                "Playwright tests failed (rc=%d)", result.returncode,
            )

        return success, output


class PlaywrightNotFoundError(RuntimeError):
    """Raised when Playwright / npx cannot be found."""


# ── Helper functions ────────────────────────────────────────────────────


def _npx_available() -> bool:
    """Check if ``npx`` is available on ``$PATH``."""
    return shutil.which("npx") is not None


def _playwright_available() -> bool:
    """Check if Playwright browsers are installed.

    Runs ``npx playwright --version`` to verify the installation.
    """
    if not _npx_available():
        return False
    try:
        result = subprocess.run(
            ["npx", "playwright", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def install_browsers(browser: str = "chromium") -> tuple[bool, str]:
    """Install Playwright browser binaries.

    Args:
        browser: Which browser to install (chromium, firefox, webkit,
                 or empty string for all browsers).

    Returns:
        A ``(success, output)`` tuple.
    """
    if not _npx_available():
        return False, "npx is not available on $PATH"

    cmd = ["npx", "playwright", "install", "--with-deps"]
    if browser:
        cmd.append(browser)

    logger.info("Installing Playwright browser: %s", browser or "all")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "Browser installation timed out after 300s"

    output = result.stdout + result.stderr
    success = result.returncode == 0

    if success:
        logger.info("Playwright browser installed successfully")
    else:
        logger.warning("Playwright browser install failed (rc=%d)", result.returncode)

    return success, output


# ── Convenience functions for agents ────────────────────────────────────


def run_playwright_tests(
    test_dir: str = DEFAULT_TEST_DIR,
    base_url: Optional[str] = None,
    browser: str = "chromium",
    headless: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    config_file: Optional[str] = None,
) -> tuple[bool, str]:
    """Run Playwright tests against a URL (convenience wrapper).

    This is the primary entry point for agents.  It creates a
    :class:`PlaywrightRunner` and calls :meth:`~PlaywrightRunner.run`.

    Args:
        test_dir: Directory containing Playwright test files.
        base_url: The base URL for tests (e.g. from docker_toolkit).
        browser: Browser project to use.
        headless: Whether to run headless.
        timeout: Max seconds for the test run.
        config_file: Optional Playwright config file path.

    Returns:
        A ``(success, output)`` tuple.

    Example::

        from factory.playwright_runner import run_playwright_tests

        success, output = run_playwright_tests(
            base_url="https://task-42.preview.factory.6a.fi",
            browser="chromium",
        )
    """
    runner = PlaywrightRunner(
        test_dir=test_dir,
        base_url=base_url,
        browser=browser,
        headless=headless,
        timeout=timeout,
        config_file=config_file,
    )
    return runner.run()
