"""Tests for the Playwright test runner module.

Unit tests for command building, environment setup, browser validation,
and test execution.  Integration tests that require a real Node.js / npx
installation are conditionally skipped.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from factory.playwright_runner import (
    DEFAULT_TEST_DIR,
    DEFAULT_TIMEOUT,
    SUPPORTED_BROWSERS,
    PlaywrightNotFoundError,
    PlaywrightRunner,
    _npx_available,
    _playwright_available,
    install_browsers,
    run_playwright_tests,
)


# ── PlaywrightRunner.__init__ ──────────────────────────────────────────


class TestPlaywrightRunnerInit:
    def test_default_init(self):
        runner = PlaywrightRunner()
        assert runner.test_dir == DEFAULT_TEST_DIR
        assert runner.base_url is None
        assert runner.browser == "chromium"
        assert runner.headless is True
        assert runner.timeout == DEFAULT_TIMEOUT
        assert runner.config_file is None

    def test_custom_init(self):
        runner = PlaywrightRunner(
            test_dir="e2e",
            base_url="https://example.com",
            browser="firefox",
            headless=False,
            timeout=60,
            config_file="pw.config.ts",
        )
        assert runner.test_dir == "e2e"
        assert runner.base_url == "https://example.com"
        assert runner.browser == "firefox"
        assert runner.headless is False
        assert runner.timeout == 60
        assert runner.config_file == "pw.config.ts"

    def test_webkit_browser(self):
        runner = PlaywrightRunner(browser="webkit")
        assert runner.browser == "webkit"

    def test_invalid_browser_raises(self):
        with pytest.raises(ValueError, match="Unsupported browser"):
            PlaywrightRunner(browser="ie11")

    def test_all_supported_browsers_accepted(self):
        for browser in SUPPORTED_BROWSERS:
            runner = PlaywrightRunner(browser=browser)
            assert runner.browser == browser


# ── build_command ──────────────────────────────────────────────────────


class TestBuildCommand:
    def test_default_command(self):
        runner = PlaywrightRunner()
        cmd = runner.build_command()
        assert cmd[:3] == ["npx", "playwright", "test"]
        assert "--project" in cmd
        assert "chromium" in cmd
        assert "--reporter" in cmd
        assert "list" in cmd

    def test_browser_in_command(self):
        runner = PlaywrightRunner(browser="firefox")
        cmd = runner.build_command()
        idx = cmd.index("--project")
        assert cmd[idx + 1] == "firefox"

    def test_config_file_included(self):
        runner = PlaywrightRunner(config_file="custom.config.ts")
        cmd = runner.build_command()
        assert "--config" in cmd
        idx = cmd.index("--config")
        assert cmd[idx + 1] == "custom.config.ts"

    def test_no_config_file(self):
        runner = PlaywrightRunner()
        cmd = runner.build_command()
        assert "--config" not in cmd

    def test_headed_mode(self):
        runner = PlaywrightRunner(headless=False)
        cmd = runner.build_command()
        assert "--headed" in cmd

    def test_headless_mode_no_headed_flag(self):
        runner = PlaywrightRunner(headless=True)
        cmd = runner.build_command()
        assert "--headed" not in cmd

    def test_reporter_is_list(self):
        runner = PlaywrightRunner()
        cmd = runner.build_command()
        idx = cmd.index("--reporter")
        assert cmd[idx + 1] == "list"


# ── build_env ──────────────────────────────────────────────────────────


class TestBuildEnv:
    def test_env_inherits_os_environ(self, monkeypatch):
        monkeypatch.setenv("EXISTING_VAR", "hello")
        runner = PlaywrightRunner()
        env = runner.build_env()
        assert env["EXISTING_VAR"] == "hello"

    def test_base_url_sets_env_vars(self):
        runner = PlaywrightRunner(base_url="https://test.example.com")
        env = runner.build_env()
        assert env["BASE_URL"] == "https://test.example.com"
        assert env["PLAYWRIGHT_BASE_URL"] == "https://test.example.com"

    def test_no_base_url_no_env_vars(self):
        runner = PlaywrightRunner(base_url=None)
        env = runner.build_env()
        assert "BASE_URL" not in env or env.get("BASE_URL") != ""
        # Should not have been explicitly set
        assert "PLAYWRIGHT_BASE_URL" not in env

    def test_headless_sets_ci_true(self):
        runner = PlaywrightRunner(headless=True)
        env = runner.build_env()
        assert env["CI"] == "true"

    def test_headed_no_ci(self, monkeypatch):
        monkeypatch.delenv("CI", raising=False)
        runner = PlaywrightRunner(headless=False)
        env = runner.build_env()
        assert "CI" not in env


# ── run ────────────────────────────────────────────────────────────────


class TestRun:
    @patch("factory.playwright_runner._npx_available", return_value=False)
    def test_raises_when_npx_missing(self, mock_npx, tmp_path):
        runner = PlaywrightRunner(test_dir=str(tmp_path))
        with pytest.raises(PlaywrightNotFoundError, match="npx is not available"):
            runner.run()

    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_raises_when_test_dir_missing(self, mock_npx):
        runner = PlaywrightRunner(test_dir="/nonexistent/path/e2e")
        with pytest.raises(FileNotFoundError, match="does not exist"):
            runner.run()

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_successful_run(self, mock_npx, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Running 3 tests\n3 passed\n",
            stderr="",
        )

        runner = PlaywrightRunner(
            test_dir=str(tmp_path),
            base_url="https://test.example.com",
        )
        success, output = runner.run()

        assert success is True
        assert "3 passed" in output
        mock_run.assert_called_once()

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_failed_run(self, mock_npx, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="Running 3 tests\n1 failed\n",
            stderr="Error: expected 200 got 404\n",
        )

        runner = PlaywrightRunner(test_dir=str(tmp_path))
        success, output = runner.run()

        assert success is False
        assert "1 failed" in output
        assert "expected 200 got 404" in output

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_timeout_handling(self, mock_npx, mock_run, tmp_path):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["npx", "playwright", "test"],
            timeout=10,
            output="partial output",
            stderr="partial error",
        )

        runner = PlaywrightRunner(test_dir=str(tmp_path), timeout=10)
        success, output = runner.run()

        assert success is False
        assert "Timed out after 10s" in output

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_run_passes_cwd(self, mock_npx, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        runner = PlaywrightRunner(test_dir=str(tmp_path))
        runner.run()

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == str(tmp_path)

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_run_passes_timeout(self, mock_npx, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        runner = PlaywrightRunner(test_dir=str(tmp_path), timeout=42)
        runner.run()

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 42

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_run_passes_env_with_base_url(self, mock_npx, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        runner = PlaywrightRunner(
            test_dir=str(tmp_path),
            base_url="https://test.example.com",
        )
        runner.run()

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["env"]["BASE_URL"] == "https://test.example.com"

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_run_combines_stdout_stderr(self, mock_npx, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="STDOUT_CONTENT",
            stderr="STDERR_CONTENT",
        )

        runner = PlaywrightRunner(test_dir=str(tmp_path))
        success, output = runner.run()

        assert "STDOUT_CONTENT" in output
        assert "STDERR_CONTENT" in output


# ── PlaywrightNotFoundError ────────────────────────────────────────────


class TestPlaywrightNotFoundError:
    def test_is_runtime_error(self):
        assert issubclass(PlaywrightNotFoundError, RuntimeError)

    def test_message(self):
        exc = PlaywrightNotFoundError("test message")
        assert str(exc) == "test message"


# ── _npx_available ─────────────────────────────────────────────────────


class TestNpxAvailable:
    @patch("factory.playwright_runner.shutil.which", return_value="/usr/bin/npx")
    def test_returns_true_when_found(self, mock_which):
        assert _npx_available() is True
        mock_which.assert_called_once_with("npx")

    @patch("factory.playwright_runner.shutil.which", return_value=None)
    def test_returns_false_when_missing(self, mock_which):
        assert _npx_available() is False


# ── _playwright_available ──────────────────────────────────────────────


class TestPlaywrightAvailable:
    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_returns_true_when_installed(self, mock_npx, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="1.40.0")
        assert _playwright_available() is True

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_returns_false_on_error(self, mock_npx, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        assert _playwright_available() is False

    @patch("factory.playwright_runner._npx_available", return_value=False)
    def test_returns_false_when_npx_missing(self, mock_npx):
        assert _playwright_available() is False

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_returns_false_on_timeout(self, mock_npx, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=30)
        assert _playwright_available() is False

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_returns_false_on_file_not_found(self, mock_npx, mock_run):
        mock_run.side_effect = FileNotFoundError("npx not found")
        assert _playwright_available() is False


# ── install_browsers ───────────────────────────────────────────────────


class TestInstallBrowsers:
    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_install_chromium(self, mock_npx, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Downloading chromium...\nDone\n",
            stderr="",
        )

        success, output = install_browsers("chromium")

        assert success is True
        assert "Done" in output
        cmd = mock_run.call_args[0][0]
        assert "install" in cmd
        assert "--with-deps" in cmd
        assert "chromium" in cmd

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_install_all_browsers(self, mock_npx, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        success, output = install_browsers("")

        assert success is True
        cmd = mock_run.call_args[0][0]
        assert "install" in cmd
        assert "--with-deps" in cmd
        # No specific browser name when empty string
        assert "chromium" not in cmd
        assert "firefox" not in cmd

    @patch("factory.playwright_runner._npx_available", return_value=False)
    def test_fails_when_npx_missing(self, mock_npx):
        success, output = install_browsers()
        assert success is False
        assert "npx is not available" in output

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_install_failure(self, mock_npx, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: installation failed\n",
        )

        success, output = install_browsers("chromium")

        assert success is False
        assert "installation failed" in output

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_install_timeout(self, mock_npx, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=300)

        success, output = install_browsers()

        assert success is False
        assert "timed out" in output.lower()


# ── run_playwright_tests (convenience function) ────────────────────────


class TestRunPlaywrightTests:
    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_convenience_function_passes_all_args(self, mock_npx, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        success, output = run_playwright_tests(
            test_dir=str(tmp_path),
            base_url="https://example.com",
            browser="chromium",
            headless=True,
            timeout=120,
        )

        assert success is True
        assert "ok" in output

        # Verify the subprocess was called with correct params
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == str(tmp_path)
        assert call_kwargs["timeout"] == 120
        assert call_kwargs["env"]["BASE_URL"] == "https://example.com"

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_convenience_function_defaults(self, mock_npx, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Create the default test dir inside tmp_path for this test
        test_dir = tmp_path / "tests" / "e2e"
        test_dir.mkdir(parents=True)

        success, output = run_playwright_tests(test_dir=str(test_dir))

        assert success is True

    @patch("factory.playwright_runner._npx_available", return_value=False)
    def test_convenience_function_raises_when_npx_missing(self, mock_npx, tmp_path):
        with pytest.raises(PlaywrightNotFoundError):
            run_playwright_tests(test_dir=str(tmp_path))

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_convenience_function_with_config_file(self, mock_npx, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_playwright_tests(
            test_dir=str(tmp_path),
            config_file="custom.config.ts",
        )

        cmd = mock_run.call_args[0][0]
        assert "--config" in cmd
        idx = cmd.index("--config")
        assert cmd[idx + 1] == "custom.config.ts"

    @patch("factory.playwright_runner.subprocess.run")
    @patch("factory.playwright_runner._npx_available", return_value=True)
    def test_convenience_function_firefox(self, mock_npx, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        run_playwright_tests(
            test_dir=str(tmp_path),
            browser="firefox",
        )

        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--project")
        assert cmd[idx + 1] == "firefox"


# ── Constants ──────────────────────────────────────────────────────────


class TestConstants:
    def test_supported_browsers(self):
        assert "chromium" in SUPPORTED_BROWSERS
        assert "firefox" in SUPPORTED_BROWSERS
        assert "webkit" in SUPPORTED_BROWSERS
        assert len(SUPPORTED_BROWSERS) == 3

    def test_default_test_dir(self):
        assert DEFAULT_TEST_DIR == "tests/e2e"

    def test_default_timeout(self):
        assert DEFAULT_TIMEOUT == 300
