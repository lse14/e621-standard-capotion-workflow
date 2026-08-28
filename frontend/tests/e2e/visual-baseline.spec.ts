import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

import { expect, makeSnapshot, openApp, setJobSnapshot, test } from "./mockApi";

const outputDirectory = resolve(__dirname, "../../../output/playwright");

function screenshotPath(name: string): string {
  mkdirSync(outputDirectory, { recursive: true });
  return resolve(outputDirectory, name);
}

async function expectStableLayout(page: Parameters<typeof openApp>[0]) {
  const viewport = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);

  const regions = await page.locator(".workflow-rail, .step-panel, .task-monitor").evaluateAll((elements) => elements.map((element) => {
    const rect = element.getBoundingClientRect();
    return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
  }));
  for (let index = 0; index < regions.length; index += 1) {
    for (let other = index + 1; other < regions.length; other += 1) {
      const first = regions[index];
      const second = regions[other];
      const overlaps = first.left < second.right && first.right > second.left && first.top < second.bottom && first.bottom > second.top;
      expect(overlaps).toBe(false);
    }
  }
}

test("captures the Chinese desktop characterization baseline", async ({ page }) => {
  await openApp(page);
  await expect(page.getByRole("heading", { name: "Anima Dataset Tool" })).toBeVisible();
  const languageSwitch = page.getByRole("button", { name: "EN", exact: true });
  await languageSwitch.focus();
  await expect(languageSwitch).toHaveCSS("outline-style", "solid");
  await page.locator(".workflow-rail").getByRole("button", { name: /OCR/ }).click();
  await expectStableLayout(page);
  await page.screenshot({ path: screenshotPath("ocr-task-9-desktop.png"), fullPage: false });
});

test.describe("mobile characterization baseline", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("captures the Chinese mobile characterization baseline", async ({ page }) => {
    await openApp(page);
    await expect(page.getByRole("heading", { name: "Anima Dataset Tool" })).toBeVisible();
    await page.locator(".workflow-rail").getByRole("button", { name: /OCR/ }).click();
    await expectStableLayout(page);
    await page.screenshot({ path: screenshotPath("ocr-task-9-mobile.png"), fullPage: false });
  });
});

test("captures the Token Budget desktop configuration and keeps its edit grid stable", async ({ page, api }) => {
  setJobSnapshot(api, makeSnapshot({ schemaVersion: 10, status: "reviewing", currentModuleId: "token_budget" }));
  await openApp(page, { jobId: "job-e621-characterization", language: "en" });
  await page.locator(".workflow-rail").getByRole("button", { name: /Token Budget/ }).click();
  const editGrid = page.locator(".token-budget-edit-grid").first();
  await expect(editGrid).toBeVisible();
  await expect.poll(() => editGrid.evaluate((element) => getComputedStyle(element).display)).toBe("grid");
  await expectStableLayout(page);
  await page.getByLabel("Maximum training tokens", { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: screenshotPath("token-budget-config-desktop.png"), fullPage: false });
  await editGrid.scrollIntoViewIfNeeded();
  await page.screenshot({ path: screenshotPath("token-budget-review-desktop.png"), fullPage: false });
});

test.describe("mobile Token Budget baseline", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("keeps the review workflow within the mobile viewport", async ({ page, api }) => {
  setJobSnapshot(api, makeSnapshot({ schemaVersion: 10, status: "reviewing", currentModuleId: "token_budget" }));
    await openApp(page, { jobId: "job-e621-characterization", language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /Token Budget/ }).click();
    const reviewItem = page.locator(".token-budget-review-item").first();
    await expect(reviewItem).toBeVisible();
    await expectStableLayout(page);
    await page.getByLabel("Maximum training tokens", { exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: screenshotPath("token-budget-config-mobile.png"), fullPage: false });
    await reviewItem.scrollIntoViewIfNeeded();
    await page.screenshot({ path: screenshotPath("token-budget-review-mobile.png"), fullPage: false });
  });
});

test("captures the unified NL desktop configuration with a tooltip open", async ({ page }) => {
  await openApp(page, { language: "en" });
  await page.locator(".workflow-rail").getByRole("button", { name: /NL/ }).click();
  await page.locator('[data-config-surface="nl"]').scrollIntoViewIfNeeded();
  await page.getByRole("button", { name: "Batch size information" }).click();
  await expect(page.getByRole("tooltip")).toBeVisible();
  await page.screenshot({ path: screenshotPath("workflow-ui-desktop-nl.png"), fullPage: false });
});

test("captures the complete Policy desktop configuration", async ({ page }) => {
  await openApp(page, { language: "en" });
  await page.locator(".workflow-rail").getByRole("button", { name: /Dropout/ }).click();
  await page.locator('[data-config-surface="policy"]').scrollIntoViewIfNeeded();
  await page.screenshot({ path: screenshotPath("workflow-ui-desktop-policy.png"), fullPage: false });
});

test.describe("mobile workflow UI evidence", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("captures the Chinese workflow configuration", async ({ page }) => {
    await openApp(page, { language: "zh" });
    await page.locator(".workflow-rail").getByRole("button", { name: /OCR/ }).click();
    await page.locator('[data-config-surface="ocr"]').scrollIntoViewIfNeeded();
    await page.screenshot({ path: screenshotPath("workflow-ui-mobile-zh.png"), fullPage: false });
  });
});

test.describe("320px workflow UI evidence", () => {
  test.use({ viewport: { width: 320, height: 844 } });

  test("captures the English narrow workflow configuration", async ({ page }) => {
    await openApp(page, { language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /NL/ }).click();
    await page.locator('[data-config-surface="nl"]').scrollIntoViewIfNeeded();
    await page.screenshot({ path: screenshotPath("workflow-ui-mobile-en-320.png"), fullPage: false });
  });
});
