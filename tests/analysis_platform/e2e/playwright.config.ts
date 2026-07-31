import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.ANALYSIS_E2E_BASE_URL || "http://localhost:5173";

export default defineConfig({
  testDir: "./tests",
  timeout: 90_000,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ["list"],
    ["html", { outputFolder: "../report/playwright-html", open: "never" }],
    ["junit", { outputFile: "../report/playwright-junit.xml" }],
    ["json", { outputFile: "../report/playwright-results.json" }],
  ],
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
