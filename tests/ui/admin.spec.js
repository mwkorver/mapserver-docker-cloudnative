const { test, expect } = require("@playwright/test");

test.describe("admin dashboard", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/admin/?v=test");
    await expect(page.getByRole("heading", { name: "MapServer Admin Dashboard" })).toBeVisible();
  });

  test("switches between all top-level tabs", async ({ page }) => {
    const expectedPanels = {
      Collections: "Collections",
      Runtime: "Service Health",
      Cache: "nginx Cache",
      Visualize: "Map Preview",
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
    await expect(page.locator("#collection-count")).toHaveText("3");
    // Collection-id rows
    await expect(page.locator("#collections-body").getByRole("cell", { name: "ky-2024-3in", exact: true }).first()).toBeVisible();
    await expect(page.locator("#collections-body").getByRole("cell", { name: "nj-2020-1ft", exact: true }).first()).toBeVisible();
    await expect(page.locator("#collections-body").getByRole("cell", { name: "ky-2024-season13in", exact: true }).first()).toBeVisible();
    // Source bucket column was folded into the main Collections table.
    await expect(page.locator("#collections-body").getByText("kyfromabove").first()).toBeVisible();
    await expect(page.locator("#collections-body").getByText("njogis-imagery").first()).toBeVisible();
  });

  test("can switch active collection for visualization", async ({ page }) => {
    await page.locator("#collections-body button", { hasText: "View" }).first().click();
    await expect(page.getByRole("tab", { name: "Visualize" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText("The admin dashboard does not embed the map viewer")).toBeVisible();
    await expect(page.locator("#viewer-full-link")).toHaveAttribute("href", /\/viewer\//);
  });

  test("refreshes cache stats with visible feedback", async ({ page }) => {
    await page.getByRole("tab", { name: "Cache" }).click();

    const previousRefresh = await page.locator("#nginx-cache-updated").textContent();
    await page.waitForTimeout(1100);
    await page.locator("#cache-refresh").click();

    await expect(page.locator("#nginx-cache-updated")).not.toHaveText(previousRefresh);
    await expect(page.locator("#nginx-cache-path")).not.toHaveText("—");
    await expect(page.locator("#nginx-cache-files")).not.toHaveText("—");
    await expect(page.locator("#nginx-cache-size")).not.toHaveText("—");
  });

  test("enables cache apply only after cache config changes", async ({ page }) => {
    await page.getByRole("tab", { name: "Cache" }).click();

    const save = page.locator("#cache-config-save");
    const maxSize = page.locator("#nginx-cache-max-size-input");
    await expect(save).toBeDisabled();

    const currentValue = Number.parseInt(await maxSize.inputValue(), 10);
    await maxSize.fill(String(currentValue === 20 ? 21 : 20));

    await expect(save).toBeEnabled();
    await expect(save).toHaveClass(/dirty/);
  });

  test("enables runtime apply only after tuning changes", async ({ page }) => {
    await page.getByRole("tab", { name: "Runtime" }).click();

    const save = page.locator("#runtime-save");
    const gdalCache = page.locator("#gdal-cachemax-input");
    await expect(save).toBeDisabled();

    const currentValue = Number.parseInt(await gdalCache.inputValue(), 10);
    await gdalCache.fill(String(currentValue === 128 ? 129 : 128));

    await expect(save).toBeEnabled();
    await expect(save).toHaveClass(/dirty/);
  });
});
