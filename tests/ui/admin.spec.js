const { test, expect } = require("@playwright/test");

test.describe("admin dashboard", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/admin/?v=test");
    await expect(page.getByRole("heading", { name: "MapServer Admin Dashboard" })).toBeVisible();
  });

  test("switches between all top-level tabs", async ({ page }) => {
    const expectedPanels = {
      Collections: "Collections",
      "MapServer/GDAL": "Service Health",
      Environment: "Runtime Environment",
      "Nginx Cache": "nginx Cache",
      Benchmark: "Benchmark",
    };

    for (const [tabName, heading] of Object.entries(expectedPanels)) {
      await page.getByRole("tab", { name: tabName }).click();
      await expect(page.getByRole("tab", { name: tabName })).toHaveAttribute("aria-selected", "true");
      await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    }
  });

  test("renders seeded collections with sources", async ({ page }) => {
    await expect(page.getByRole("tab", { name: "Collections" })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#collection-count")).toHaveText(/^[3-9][0-9]*$/);
    // Collection-id rows
    await expect(page.locator("#collections-body").getByRole("cell", { name: "ky-2024-3in", exact: true }).first()).toBeVisible();
    await expect(page.locator("#collections-body").getByRole("cell", { name: "nj-2020-1ft", exact: true }).first()).toBeVisible();
    await expect(page.locator("#collections-body").getByRole("cell", { name: "ky-2024-season13in", exact: true }).first()).toBeVisible();
    // Source bucket column was folded into the main Collections table.
    await expect(page.locator("#collections-body").getByText("kyfromabove").first()).toBeVisible();
    await expect(page.locator("#collections-body").getByText("njogis-imagery").first()).toBeVisible();
    await expect(page.getByText("AWS_PROFILE=your-profile ./scripts/auto_refresh_credentials.sh")).toBeVisible();
  });

  test("links only the active collection to the standalone viewer", async ({ page }) => {
    await expect(page.locator("#collections-body a", { hasText: "Viewer" })).toHaveCount(1);
    await expect(page.locator("#collections-body tr", { hasText: "yes" }).locator("a", { hasText: "Viewer" })).toHaveAttribute("href", /\/viewer\//);
    await expect(page.getByRole("tab", { name: "Visualize" })).toHaveCount(0);
  });

  test("keeps collection row order when active collection changes", async ({ page }) => {
    const rows = page.locator("#collections-body tr");
    await expect(rows.first()).toBeVisible();
    const namesBefore = await rows.locator("td:first-child").allTextContents();
    const targetButton = page.locator("#collections-body button", { hasText: "Set active" }).first();
    const targetName = await targetButton.locator("xpath=ancestor::tr/td[1]").textContent();

    await targetButton.click();
    await expect(rows.filter({ hasText: targetName || "" }).locator("a", { hasText: "Viewer" })).toHaveAttribute("href", /\/viewer\//);

    const namesAfter = await rows.locator("td:first-child").allTextContents();
    expect(namesAfter).toEqual(namesBefore);
  });

  test("refreshes cache stats with visible feedback", async ({ page }) => {
    await page.getByRole("tab", { name: "Nginx Cache" }).click();

    const previousRefresh = await page.locator("#nginx-cache-updated").textContent();
    await page.waitForTimeout(1100);
    await page.locator("#cache-refresh").click();

    await expect(page.locator("#nginx-cache-updated")).not.toHaveText(previousRefresh);
    await expect(page.locator("#nginx-cache-path")).not.toHaveText("—");
    await expect(page.locator("#nginx-cache-files")).not.toHaveText("—");
    await expect(page.locator("#nginx-cache-size")).not.toHaveText("—");
    await expect(page.locator("#nginx-workers")).not.toHaveText("—");
    await expect(page.locator("#nginx-cache-api-link")).toHaveAttribute("href", /\/admin\/api\/nginx-cache$/);
  });

  test("enables cache apply only after cache config changes", async ({ page }) => {
    await page.getByRole("tab", { name: "Nginx Cache" }).click();

    const save = page.locator("#cache-config-save");
    const maxSize = page.locator("#nginx-cache-max-size-input");
    await expect(maxSize).toBeEnabled();
    await expect(save).toBeDisabled();

    const currentValue = Number.parseInt(await maxSize.inputValue(), 10);
    await maxSize.fill(String(currentValue === 20 ? 21 : 20));

    await expect(save).toBeEnabled();
    await expect(save).toHaveClass(/dirty/);
  });

  test("enables runtime apply only after tuning changes", async ({ page }) => {
    await page.getByRole("tab", { name: "MapServer/GDAL" }).click();

    await expect(page.getByRole("heading", { name: "MapServer", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "GDAL Tuning" })).toBeVisible();
    await expect(page.locator("#capabilities-link")).toHaveAttribute("href", /GetCapabilities/);
    await expect(page.locator("#mapserver-observed-workers")).not.toHaveText("—");
    await expect(page.getByRole("heading", { name: /AWS Cost/ })).toHaveCount(0);

    const save = page.locator("#runtime-save");
    const gdalCache = page.locator("#gdal-cachemax-input");
    await expect(gdalCache).toBeEnabled();
    await expect(save).toBeDisabled();

    const currentValue = Number.parseInt(await gdalCache.inputValue(), 10);
    await gdalCache.fill(String(currentValue === 128 ? 129 : 128));

    await expect(save).toBeEnabled();
    await expect(save).toHaveClass(/dirty/);
  });

  test("refreshes environment details on demand", async ({ page }) => {
    await page.getByRole("tab", { name: "Environment" }).click();
    await expect(page.locator("#env-mode")).not.toHaveText("—");

    const previousRefresh = await page.locator("#environment-updated").textContent();
    await page.waitForTimeout(1100);
    await page.locator("#environment-refresh").click();

    await expect(page.locator("#environment-updated")).not.toHaveText(previousRefresh);
    await expect(page.locator("#env-mode")).not.toHaveText("—");
    await expect(page.locator("#env-index-backend")).not.toHaveText("—");
    await expect(page.getByRole("heading", { name: "Workers", exact: true })).toHaveCount(0);
  });
});
