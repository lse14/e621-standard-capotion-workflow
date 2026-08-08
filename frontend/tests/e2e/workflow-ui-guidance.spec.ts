import { expect, openApp, test } from "./mockApi";

async function expectNoHorizontalOverflow(page: Parameters<typeof openApp>[0]) {
  const size = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  expect(size.scrollWidth).toBeLessThanOrEqual(size.clientWidth);
}

test.describe("workflow field guidance foundation", () => {
  test("shows an evidence-backed Setup default without changing the control", async ({ page }) => {
    await openApp(page, { language: "en" });
    const workMode = page.getByLabel("Work mode", { exact: true });
    await expect(workMode).toHaveValue("in_place");
    const info = page.getByRole("button", { name: "Work mode information" });
    await info.hover();
    const tooltip = page.getByRole("tooltip");
    await expect(tooltip).toContainText("Controls whether annotations are committed to the source dataset or a copied dataset.");
    await expect(tooltip).toContainText("Default: In place");
    await expect(workMode).toHaveValue("in_place");
  });

  test("opens guidance by keyboard and closes it with Escape", async ({ page }) => {
    await openApp(page, { language: "en" });
    const info = page.getByRole("button", { name: "Source dataset information" });
    await info.focus();
    await expect(page.getByRole("tooltip")).toBeVisible();
    await info.press("Escape");
    await expect(page.getByRole("tooltip")).toBeHidden();
  });
});

test.describe("workflow guidance for resource-backed steps", () => {
  test("shows the selected resource explanation and profile recommendation", async ({ page }) => {
    await openApp(page, { language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /Caption/ }).click();
    const info = page.getByRole("button", { name: "Tagging model information" });
    await info.hover();
    await expect(page.getByRole("tooltip")).toContainText("Recommended: E621 tagger");

    await page.getByRole("button", { name: "Danbooru", exact: true }).click();
    await info.hover();
    await expect(page.getByRole("tooltip")).toContainText("Recommended: Danbooru tagger");
  });

  test("wraps every visible Caption, Classify, and Replace control in a setting field", async ({ page }) => {
    await openApp(page, { language: "en" });
    for (const stepName of [/Caption/, /Classify/, /Replace/]) {
      await page.locator(".workflow-rail").getByRole("button", { name: stepName }).click();
      const controls = page.locator(".step-content input:visible, .step-content select:visible, .step-content textarea:visible");
      const count = await controls.count();
      expect(count).toBeGreaterThan(0);
      for (let index = 0; index < count; index += 1) {
        await expect(controls.nth(index).locator("xpath=ancestor::*[@data-setting-field][1]")).toHaveCount(1);
      }
    }
  });
});

test.describe("workflow guidance for remaining modules", () => {
  test("shows OCR defaults and runtime recommendations without enabling disabled controls", async ({ page }) => {
    await openApp(page, { language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /OCR/ }).click();
    await expect(page.getByLabel("OCR device", { exact: true })).toBeDisabled();
    const deviceInfo = page.getByRole("button", { name: "OCR device information" });
    await deviceInfo.hover();
    await expect(page.getByRole("tooltip")).toContainText("Default: Auto");
    await expect(page.getByRole("tooltip")).toContainText("Recommended: Auto");

    await page.getByLabel("Enable OCR", { exact: true }).check();
    await page.locator(".workflow-rail").getByRole("button", { name: /Dataset and preflight/ }).click();
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\guidance");
    await page.getByRole("button", { name: "Preflight", exact: true }).click();
    await expect.poll(() => page.getByRole("button", { name: "Preflight", exact: true }).getAttribute("aria-busy")).toBe("false");
    await page.locator(".workflow-rail").getByRole("button", { name: /OCR/ }).click();
    await page.getByRole("button", { name: "OCR detection limit information" }).hover();
    await expect(page.getByRole("tooltip")).toContainText("Recommended: 2560");
    await page.getByRole("button", { name: "OCR text batch information" }).hover();
    await expect(page.getByRole("tooltip")).toContainText("Recommended: 4");
  });

  test("separates Token Budget defaults from the tokenizer range", async ({ page, api }) => {
    const tokenizer = api.resources.resources.find((item) => item.resourceId === "tokenizer-qwen3-0.6b-anima-v1");
    if (!tokenizer) throw new Error("tokenizer fixture missing");
    tokenizer.defaultForProfiles = ["shared"];
    await openApp(page, { language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /Token Budget/ }).click();
    await page.getByRole("button", { name: "Maximum training tokens information" }).hover();
    await expect(page.getByRole("tooltip")).toContainText("Default: 512");
    await expect(page.getByRole("tooltip")).toContainText("Range: 1-40960");
    const tokenizerInfo = page.getByRole("button", { name: "Tokenizer resource information" });
    await tokenizerInfo.hover();
    await expect(page.getByRole("tooltip")).toContainText("Recommended: Qwen/Qwen3-0.6B");
    await expect(page.getByRole("tooltip")).not.toContainText("Recommended: 40960");

  });

  test("wraps Count Review, Policy, and Export controls and documents policy probabilities", async ({ page }) => {
    await openApp(page, { language: "en" });
    for (const stepName of [/Count Review/, /Dropout/, /Export/]) {
      await page.locator(".workflow-rail").getByRole("button", { name: stepName }).click();
      const controls = page.locator(".step-content input:visible, .step-content select:visible, .step-content textarea:visible");
      const count = await controls.count();
      expect(count).toBeGreaterThan(0);
      for (let index = 0; index < count; index += 1) {
        await expect(controls.nth(index).locator("xpath=ancestor::*[@data-setting-field][1]")).toHaveCount(1);
      }
    }
    await page.locator(".workflow-rail").getByRole("button", { name: /Dropout/ }).click();
    await page.getByRole("button", { name: "Drop description information" }).first().hover();
    await expect(page.getByRole("tooltip")).toContainText("Default: 0.7");
    await expect(page.getByRole("tooltip")).toContainText("Range: 0-1, step 0.01");
  });
});

test.describe("workflow guidance for NL", () => {
  test("wraps configurable controls and distinguishes NL defaults from provider settings", async ({ page }) => {
    await openApp(page, { language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /NL/ }).click();
    const controls = page.locator(".step-content input:visible, .step-content select:visible, .step-content textarea:visible");
    const count = await controls.count();
    expect(count).toBeGreaterThan(0);
    for (let index = 0; index < count; index += 1) {
      await expect(controls.nth(index).locator("xpath=ancestor::*[@data-setting-field][1]")).toHaveCount(1);
    }

    await page.getByRole("button", { name: "Concurrency information" }).hover();
    await expect(page.getByRole("tooltip")).toContainText("Default: 3");
    await expect(page.getByRole("tooltip")).toContainText("Range: 1-16");
    await page.getByRole("button", { name: "Endpoint information" }).hover();
    await expect(page.getByRole("tooltip")).not.toContainText("Default:");
    await expect(page.getByRole("tooltip")).not.toContainText("Recommended:");
  });
});

test.describe("workflow guidance responsive contracts", () => {
  test("keeps every visible workflow control in a setting field without desktop overlap", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openApp(page, { language: "en" });
    const navigation = page.locator(".workflow-rail .step-nav");
    const stepCount = await navigation.count();
    expect(stepCount).toBeGreaterThan(0);
    for (let stepIndex = 0; stepIndex < stepCount; stepIndex += 1) {
      await navigation.nth(stepIndex).click();
      const controls = page.locator(".step-content input:visible, .step-content select:visible, .step-content textarea:visible");
      for (let controlIndex = 0; controlIndex < await controls.count(); controlIndex += 1) {
        await expect(controls.nth(controlIndex).locator("xpath=ancestor::*[@data-setting-field][1]")).toHaveCount(1);
      }
      const fields = await page.locator(".step-content .form-field:visible").evaluateAll((nodes) => nodes.map((node) => {
        const rect = node.getBoundingClientRect();
        return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
      }));
      for (let firstIndex = 0; firstIndex < fields.length; firstIndex += 1) {
        for (let secondIndex = firstIndex + 1; secondIndex < fields.length; secondIndex += 1) {
          const first = fields[firstIndex];
          const second = fields[secondIndex];
          const overlaps = first.left < second.right && first.right > second.left && first.top < second.bottom && first.bottom > second.top;
          expect(overlaps, `overlapping fields at step ${stepIndex}`).toBe(false);
        }
      }
    }
    await page.locator(".workflow-rail").getByRole("button", { name: /NL/ }).click();
    await expect(page.locator('.step-content [data-config-surface="nl"]')).toHaveCount(1);
  });

  test("keeps a clicked mobile tooltip inside the Chinese viewport and closes it outside", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openApp(page, { language: "zh" });
    await page.locator(".field-info-button:visible").first().click();
    const tooltip = page.locator(".field-tooltip:visible");
    await expect(tooltip).toBeVisible();
    const bounds = await tooltip.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: window.innerWidth, height: window.innerHeight };
    });
    expect(bounds.left).toBeGreaterThanOrEqual(0);
    expect(bounds.right).toBeLessThanOrEqual(bounds.width);
    expect(bounds.top).toBeGreaterThanOrEqual(0);
    expect(bounds.bottom).toBeLessThanOrEqual(bounds.height);
    await page.mouse.click(2, 2);
    await expect(tooltip).toBeHidden();

    const navigation = page.locator(".workflow-rail .step-nav");
    for (let stepIndex = 0; stepIndex < await navigation.count(); stepIndex += 1) {
      await navigation.nth(stepIndex).click();
      await expectNoHorizontalOverflow(page);
    }
  });

  test("wraps English labels and keeps every control inside the 320px viewport", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 844 });
    await openApp(page, { language: "en" });
    const navigation = page.locator(".workflow-rail .step-nav");
    for (let stepIndex = 0; stepIndex < await navigation.count(); stepIndex += 1) {
      await navigation.nth(stepIndex).click();
      await expectNoHorizontalOverflow(page);
      const viewportWidth = await page.evaluate(() => document.documentElement.clientWidth);
      const elements = page.locator(".step-content .form-field:visible, .step-content button:visible, .step-content .field-label:visible");
      for (let elementIndex = 0; elementIndex < await elements.count(); elementIndex += 1) {
        const geometry = await elements.nth(elementIndex).evaluate((node) => {
          const rect = node.getBoundingClientRect();
          return { left: rect.left, right: rect.right, scrollWidth: node.scrollWidth, clientWidth: node.clientWidth };
        });
        expect(geometry.left, `element extends left at step ${stepIndex}`).toBeGreaterThanOrEqual(0);
        expect(geometry.right, `element extends right at step ${stepIndex}`).toBeLessThanOrEqual(viewportWidth);
        expect(geometry.scrollWidth, `element clips text at step ${stepIndex}`).toBeLessThanOrEqual(geometry.clientWidth + 1);
      }
    }
  });
});
