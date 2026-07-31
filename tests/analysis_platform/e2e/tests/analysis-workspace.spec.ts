import { test, expect, Page } from "@playwright/test";

/**
 * Playwright scenarios for Analysis Workspace UX.
 * Requires ANALYSIS_E2E=1 and a running frontend + backend with seeded APT data.
 *
 * Env:
 *   ANALYSIS_E2E_BASE_URL   default http://localhost:5173
 *   ANALYSIS_E2E_EMAIL      researcher email from seeder
 *   ANALYSIS_E2E_PASSWORD   password (UserFactory default if known)
 *   ANALYSIS_E2E_BOOKING_ID booking pk
 */

const email = process.env.ANALYSIS_E2E_EMAIL || "";
const password = process.env.ANALYSIS_E2E_PASSWORD || "apt-test-password";
const bookingId = process.env.ANALYSIS_E2E_BOOKING_ID || "";

async function login(page: Page) {
  await page.goto("/login");
  // Flexible selectors — match common portal login forms
  const emailInput = page.getByLabel(/email/i).or(page.locator('input[type="email"]')).first();
  const passwordInput = page.getByLabel(/password/i).or(page.locator('input[type="password"]')).first();
  await emailInput.fill(email);
  await passwordInput.fill(password);
  await page.getByRole("button", { name: /sign in|log in|login/i }).first().click();
  await page.waitForLoadState("networkidle");
}

test.describe("Analysis Platform E2E", () => {
  test.beforeEach(() => {
    test.skip(!process.env.ANALYSIS_E2E, "Set ANALYSIS_E2E=1 to run Playwright suite");
    test.skip(!email || !bookingId, "Set ANALYSIS_E2E_EMAIL and ANALYSIS_E2E_BOOKING_ID");
  });

  test("researcher login and open booking", async ({ page }) => {
    await login(page);
    await page.goto(`/bookings/${bookingId}`);
    await expect(page.getByText(/booking|equipment|status/i).first()).toBeVisible({ timeout: 30_000 });
  });

  test("verify Analyze Data button and launch workspace", async ({ page }) => {
    await login(page);
    await page.goto(`/bookings/${bookingId}`);
    const analyze = page.getByRole("button", { name: /analyze data/i }).first();
    await expect(analyze).toBeVisible({ timeout: 30_000 });
    // Prefer workspace navigation when available
    const openWorkspace = page.getByRole("button", { name: /open analysis workspace|analysis workspace/i });
    if (await openWorkspace.count()) {
      await openWorkspace.first().click();
    } else {
      await page.goto(`/analysis-workspace/${bookingId}`);
    }
    await expect(page.getByText(/workflow|analysis|progress|step/i).first()).toBeVisible({
      timeout: 30_000,
    });
  });

  test("verify workflow progress UI", async ({ page }) => {
    await login(page);
    await page.goto(`/analysis-workspace/${bookingId}`);
    await expect(page.locator("body")).toContainText(/workflow|step|analysis/i);
  });

  test("unauthorized access blocked for wrong booking", async ({ page }) => {
    await login(page);
    await page.goto(`/analysis-workspace/99999999`);
    await expect(page.getByText(/denied|forbidden|not found|error|unable/i).first()).toBeVisible({
      timeout: 30_000,
    });
  });
});
