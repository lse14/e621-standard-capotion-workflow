import {
  DEFAULT_JOB_ID,
  expect,
  failRoute,
  makeSnapshot,
  mutationsFor,
  openApp,
  setJobSnapshot,
  test,
} from "./mockApi";

function pathField(page: Parameters<typeof openApp>[0], inputId: string) {
  return page.locator(`#${inputId}`).locator("xpath=ancestor::*[@data-setting-field][1]");
}

test.describe("native path picker", () => {
  test("selects each configured path purpose and keeps a custom value when cancelled", async ({ page, api }) => {
    api.selectedPaths.source_dataset = "E:\\picked\\source";
    api.selectedPaths.output_dataset = "E:\\picked\\output";
    api.selectedPaths.replacement_csv = null;
    await openApp(page, { language: "en" });

    const source = page.getByLabel("Source dataset", { exact: true });
    await pathField(page, "setup-source-dataset").getByRole("button", { name: "Select path", exact: true }).click();
    await expect(source).toHaveValue("E:\\picked\\source");

    await page.getByLabel("Work mode", { exact: true }).selectOption("full_copy");
    const output = page.getByLabel("Output dataset", { exact: true });
    await pathField(page, "setup-output-dataset").getByRole("button", { name: "Select path", exact: true }).click();
    await expect(output).toHaveValue("E:\\picked\\output");

    await page.locator(".workflow-rail").getByRole("button", { name: /Replace/ }).click();
    await page.getByLabel("Index source", { exact: true }).selectOption("custom");
    const custom = page.getByLabel("Custom index path", { exact: true });
    await custom.fill("E:\\typed\\keep.csv");
    await pathField(page, "replace-custom-index-path").getByRole("button", { name: "Select path", exact: true }).click();
    await expect(custom).toHaveValue("E:\\typed\\keep.csv");

    expect(mutationsFor(api, "POST", "/api/application/select-path").map((item) => item.body)).toEqual([
      { purpose: "source_dataset", currentPath: null },
      { purpose: "output_dataset", currentPath: null },
      { purpose: "replacement_csv", currentPath: "E:\\typed\\keep.csv" },
    ]);
  });

  test("keeps a manually typed path on picker failure and shows a stable busy message", async ({ page, api }) => {
    await openApp(page, { language: "en" });
    const source = page.getByLabel("Source dataset", { exact: true });
    await source.fill("E:\\typed\\source");
    failRoute(api, "POST /api/application/select-path", "path_picker_busy", 409);

    const picker = pathField(page, "setup-source-dataset");
    await picker.getByRole("button", { name: "Select path", exact: true }).click();
    await expect(source).toHaveValue("E:\\typed\\source");
    await expect(picker.getByRole("alert")).toHaveText("Another path selector is open.");
  });

  test("disables picker controls for an active task and keeps the 320px row in bounds", async ({ page, api }) => {
    setJobSnapshot(api, makeSnapshot({ status: "running", currentModuleId: "caption" }));
    await page.setViewportSize({ width: 320, height: 844 });
    await openApp(page, { jobId: DEFAULT_JOB_ID, language: "en" });

    const picker = pathField(page, "setup-source-dataset");
    await expect(picker.getByRole("button", { name: "Select path", exact: true })).toBeDisabled();
    const geometry = await picker.evaluate((node) => {
      const rect = node.getBoundingClientRect();
      return { left: rect.left, right: rect.right, width: window.innerWidth };
    });
    expect(geometry.left).toBeGreaterThanOrEqual(0);
    expect(geometry.right).toBeLessThanOrEqual(geometry.width);
    const documentSize = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
    expect(documentSize.scrollWidth).toBeLessThanOrEqual(documentSize.clientWidth);
    expect(mutationsFor(api, "POST", "/api/application/select-path")).toHaveLength(0);
  });
});
