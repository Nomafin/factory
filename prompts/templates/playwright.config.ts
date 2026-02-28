// Factory Playwright Configuration Template
//
// Reference configuration for projects using Factory's Playwright test runner.
// Copy this file to your test directory and customize as needed.
//
// Environment variables injected by Factory's playwright_runner:
//   BASE_URL           — The target URL (e.g., "https://task-42.preview.factory.6a.fi")
//   PLAYWRIGHT_BASE_URL — Same as BASE_URL (Playwright-native)
//   CI                 — Set to "true" when running headless
//
// Usage with Factory:
//   from factory.playwright_runner import run_playwright_tests
//   success, output = run_playwright_tests(base_url=url, config_file="playwright.config.ts")

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  // Look for test files in the current directory
  testDir: ".",

  // Glob patterns for test files
  testMatch: "**/*.spec.ts",

  // Run tests in parallel
  fullyParallel: true,

  // Fail the build on CI if you accidentally left test.only in the source code
  forbidOnly: !!process.env.CI,

  // Retry failed tests (more retries in CI)
  retries: process.env.CI ? 2 : 0,

  // Limit parallel workers in CI to avoid flakiness
  workers: process.env.CI ? 2 : undefined,

  // Reporter configuration
  reporter: process.env.CI ? "list" : "html",

  // Shared settings for all projects
  use: {
    // Base URL from Factory environment or fallback
    baseURL: process.env.BASE_URL || "http://localhost:3000",

    // Collect trace on first retry for debugging
    trace: "on-first-retry",

    // Screenshot on failure
    screenshot: "only-on-failure",

    // Record video on first retry
    video: "on-first-retry",

    // Reasonable default timeouts
    actionTimeout: 10_000,
    navigationTimeout: 30_000,
  },

  // Timeout for each test
  timeout: 30_000,

  // Configure browser projects
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
      },
    },
    {
      name: "firefox",
      use: {
        ...devices["Desktop Firefox"],
      },
    },
    {
      name: "webkit",
      use: {
        ...devices["Desktop Safari"],
      },
    },
  ],

  // Web server configuration (optional)
  // Uncomment if you want Playwright to start the dev server automatically.
  // When using Factory's docker_toolkit, the server is already running.
  //
  // webServer: {
  //   command: "npm run dev",
  //   url: "http://localhost:3000",
  //   reuseExistingServer: !process.env.CI,
  //   timeout: 120_000,
  // },
});
