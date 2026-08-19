import {
  expect,
  failRoute,
  holdRoute,
  mutationsFor,
  openApp,
  test,
} from "./mockApi";

function expectedE621Draft(sourceRoot: string, systemPrompt: string): Record<string, unknown> {
  return {
    schemaVersion: 9,
    workMode: "in_place",
    overwriteMode: "incremental",
    sourceRoot,
    annotationBackup: "required",
    recursive: false,
    captionFormat: { replaceUnderscoresWithSpaces: true, preserveEscapes: true, triggersEnabled: false, triggerTerms: [] },
    imageDecode: {
      extensions: [".jpg", ".jpeg", ".png", ".webp", ".bmp"],
      rejectMultiFrame: true,
      applyExifTranspose: true,
      alphaBackground: "#FFFFFF",
      invalidImageAction: "block",
    },
    caption: {
      enabled: true,
      thresholdMode: "model_default",
      overwriteTxt: false,
      inputTxtMode: "tag",
      taggerFallbackOnMissingTxt: true,
      resourceId: "caption-e621-eva02-large-full-v1",
    },
    classify: {
      enabled: true,
      indexMode: "bundled",
      overwriteJson: false,
      overwriteCount: false,
      wikiDataSourceId: "e621-wiki-count-20260724-v1",
      resourceId: "classify-e621-20260724-v1",
    },
    replace: { enabled: true, indexMode: "bundled", resourceId: "replace-e621-20260726-v2" },
    ocr: {
      enabled: false,
      device: "auto",
      llmMinConfidence: 0.5,
      forceReprocess: false,
      resourceId: "ocr-ppocrv5-server-paddle-v1",
    },
    nl: {
      enabled: true,
      reuseOriginalNl: true,
      apiEnabled: true,
      useImage: true,
      useFullJson: false,
      systemPrompt,
      promptVersion: "nl-default-prompt-v4",
      captionPreset: "general",
      lengthDistribution: { short: 33, medium: 34, long: 33 },
      lengthSeed: "anima-nl-length-v1",
      apiProfileId: "default",
      apiPolicy: { concurrency: 3, maxRequestsPerMinute: 60, backupEnabled: false },
    },
    countReview: { enabled: true, protocolVersion: "count-review-v1" },
    dropout: {
      enabled: false,
      policyVersion: "dataset-batch-policy-v1",
      seed: "anima-policy-default-v1",
      artist: { enabled: true, dropoutProbability: 0 },
      quality: {
        enabled: true,
        dropoutProbability: 0,
        device: "auto",
        batchSize: 4,
        resourceId: "lse14-scorer-5k-v1",
      },
      appearanceNl: {
        enabled: true,
        solo: { dropNl: 0.7, dropAppearance: 0.05 },
        nonSolo: { dropNl: 0.05, dropAppearance: 0.7 },
        unknown: { dropNl: 0.15, dropAppearance: 0.15 },
      },
    },
    tokenBudget: { enabled: true, maxTokens: 512, resourceId: "tokenizer-qwen3-0.6b-anima-v1" },
    export: { format: "both" },
  };
}

test.describe("workflow characterization", () => {
  test("keeps the E621 Draft shape through preflight, workspace confirmation, and start", async ({ page, api }) => {
    const sourceRoot = "E:\\datasets\\e621-characterization";
    await openApp(page, { language: "en" });

    await page.locator(".workflow-rail").getByRole("button", { name: /NL/ }).click();
    await expect(page.getByLabel("User supplement", { exact: true })).toHaveCount(0);
    await expect(page.locator("[data-nl-preset-card]")).toHaveCount(3);
    expect(api.promptRequests).toEqual([]);
    await page.locator(".workflow-rail").getByRole("button", { name: /Dataset and preflight/ }).click();

    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill(sourceRoot);
    await page.getByRole("button", { name: "Preflight", exact: true }).click();

    await expect.poll(() => mutationsFor(api, "POST", "/api/jobs/preflight").length).toBe(1);
    expect(mutationsFor(api, "POST", "/api/jobs/preflight")[0].body).toEqual({
      config: expectedE621Draft(sourceRoot, "General task preset prompt."),
    });
    await expect(page.getByRole("button", { name: "Confirm workspace" })).toBeEnabled();

    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Confirm workspace" }).click();
    await expect.poll(() => mutationsFor(api, "POST", "/api/jobs/job-e621-characterization/confirm-workspace").length).toBe(1);
    expect(mutationsFor(api, "POST", "/api/jobs/job-e621-characterization/confirm-workspace")[0].body).toEqual({
      confirmed: true,
      confirmedRebuild: false,
    });
    await expect(page.locator(".task-monitor > .monitor-heading > .status")).toHaveText("preparing workspace");

    await page.locator(".workflow-rail").getByRole("button", { name: /Export/ }).click();
    await page.getByRole("button", { name: "Start pipeline" }).click();
    await expect.poll(() => mutationsFor(api, "POST", "/api/jobs/job-e621-characterization/start").length).toBe(1);
  });

  test("lets Caption choose v8 TXT input handling and sends the selected mode", async ({ page, api }) => {
    await openApp(page, { language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /Caption/ }).click();

    const inputMode = page.getByLabel("TXT input mode", { exact: true });
    const tagger = page.getByRole("checkbox", { name: "Enable caption" });
    const fallback = page.getByRole("checkbox", { name: "Use Tagger for missing or empty TXT" });
    await expect(inputMode).toHaveValue("tag");
    await expect(fallback).toBeChecked();
    await page.getByRole("button", { name: "TXT input mode information" }).click();
    await expect(page.getByRole("tooltip").filter({ hasText: "JSON nl" })).toBeVisible();
    await page.getByRole("button", { name: "Use Tagger for missing or empty TXT information" }).click();
    await expect(page.getByRole("tooltip").filter({ hasText: "new task" })).toBeVisible();

    await tagger.uncheck();
    await inputMode.selectOption("nl");
    await expect(tagger).toBeChecked();
    await expect(tagger).toBeDisabled();
    await expect(fallback).toHaveCount(0);
    await expect(page.getByRole("checkbox", { name: "Overwrite TXT" })).toBeDisabled();

    await page.locator(".workflow-rail").getByRole("button", { name: /Dataset and preflight/ }).click();
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\txt-input-nl");
    await page.getByRole("button", { name: "Preflight", exact: true }).click();
    await expect.poll(() => mutationsFor(api, "POST", "/api/jobs/preflight").length).toBe(1);
    const config = (mutationsFor(api, "POST", "/api/jobs/preflight")[0].body as { config: Record<string, unknown> }).config;
    expect(config.caption).toEqual({
      enabled: true,
      thresholdMode: "model_default",
      overwriteTxt: false,
      inputTxtMode: "nl",
      taggerFallbackOnMissingTxt: true,
      resourceId: "caption-e621-eva02-large-full-v1",
    });
  });

  test("shows the agreed Chinese TXT input guidance", async ({ page }) => {
    await openApp(page, { language: "zh-CN" });
    await page.locator(".workflow-rail").getByRole("button", { name: /Caption/ }).click();
    await page.getByRole("button", { name: "TXT 输入模式说明" }).click();
    await expect(page.getByRole("tooltip").filter({ hasText: "写入 JSON nl" })).toBeVisible();
    await page.getByRole("button", { name: "缺失或空 TXT 时启用 Tagger 补全说明" }).click();
    await expect(page.getByRole("tooltip").filter({ hasText: "重新创建任务运行" })).toBeVisible();
  });

  test("keeps OCR and NL independent while sending the exact v9 OCR request object", async ({ page, api }) => {
    await openApp(page, { language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /OCR/ }).click();
    const enableOcr = page.getByRole("checkbox", { name: "Enable OCR" });
    const confidence = page.getByLabel("Minimum LLM confidence", { exact: true });
    await expect(enableOcr).not.toBeChecked();
    await expect(confidence).toHaveValue("0.5");
    await expect(confidence).toBeDisabled();
    await enableOcr.check();
    const device = page.getByLabel("OCR device", { exact: true });
    await expect(device).toBeEnabled();
    await expect(device).toHaveValue("auto");
    await expect(page.getByLabel("Automatic detection limit")).toBeChecked();
    await expect(page.getByLabel("Automatic text batch")).toBeChecked();
    await expect(page.getByRole("spinbutton").nth(1)).toBeDisabled();
    await expect(page.getByRole("spinbutton").nth(2)).toBeDisabled();
    await confidence.fill("1.4");
    await confidence.press("Tab");
    await expect(confidence).toHaveValue("1");
    await page.locator(".workflow-rail").getByRole("button", { name: /NL/ }).click();
    await expect(page.getByRole("checkbox", { name: "Enable NL" })).toBeChecked();
    await page.locator(".workflow-rail").getByRole("button", { name: /Dataset and preflight/ }).click();
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\ocr-independent");
    await page.getByRole("button", { name: "Preflight", exact: true }).click();
    await expect.poll(() => mutationsFor(api, "POST", "/api/jobs/preflight").length).toBe(1);
    const config = (mutationsFor(api, "POST", "/api/jobs/preflight")[0].body as { config: Record<string, unknown> }).config;
    expect(mutationsFor(api, "POST", "/api/jobs/preflight")[0].body).toMatchObject({
      config: { schemaVersion: 9, ocr: { enabled: true, device: "auto", llmMinConfidence: 1, forceReprocess: false, resourceId: "ocr-ppocrv5-server-paddle-v1" } },
      ocrExecution: {
        textDetLimitSideLen: { mode: "auto", value: null },
        textBatchSize: { mode: "auto", value: null },
      },
    });
  });

  test("allows OCR-disabled preflight and disables unavailable OCR while showing the install command", async ({ page, api }) => {
    const resource = api.resources.resources.find((item) => item.resourceId === "ocr-ppocrv5-server-paddle-v1");
    if (!resource) throw new Error("OCR fixture resource is missing");
    resource.available = false;
    await openApp(page, { language: "en" });
    await expect(page.getByText("Resources ready", { exact: true })).toBeVisible();
    await page.locator(".workflow-rail").getByRole("button", { name: /OCR/ }).click();
    await expect(page.getByText("Import-OcrResource.bat -Apply", { exact: true })).toBeVisible();
    await page.locator(".workflow-rail").getByRole("button", { name: /Dataset and preflight/ }).click();
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\ocr-disabled");
    await page.getByRole("button", { name: "Preflight", exact: true }).click();
    await expect.poll(() => mutationsFor(api, "POST", "/api/jobs/preflight").length).toBe(1);
    await page.locator(".workflow-rail").getByRole("button", { name: /OCR/ }).click();
    await expect(page.getByRole("checkbox", { name: "Enable OCR" })).toBeDisabled();
  });

  test("creates v9 tasks with independent E621 defaults", async ({ page, api }) => {
    await openApp(page, { language: "en" });
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\e621-v9");
    await page.getByRole("button", { name: "Preflight", exact: true }).click();
    await expect.poll(() => mutationsFor(api, "POST", "/api/jobs/preflight").length).toBe(1);
    const config = (mutationsFor(api, "POST", "/api/jobs/preflight")[0].body as { config: Record<string, unknown> }).config;
    expect(config.schemaVersion).toBe(9);
    expect(config.profile).toBeUndefined();
    expect(config.classify).toMatchObject({ indexMode: "bundled", resourceId: "classify-e621-20260724-v1" });
    expect(config.ocr).toEqual({ enabled: false, device: "auto", llmMinConfidence: 0.5, forceReprocess: false, resourceId: "ocr-ppocrv5-server-paddle-v1" });
  });

  test("selects a custom classification resource.json independently", async ({ page, api }) => {
    await openApp(page, { language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /Classify/ }).click();
    await page.getByRole("button", { name: "Custom resource", exact: true }).click();
    await page.getByRole("button", { name: "Select path", exact: true }).click();
    await expect(page.getByLabel("Classification resource.json", { exact: true })).toHaveValue("E:\\picked\\resource.json");

    await page.locator(".workflow-rail").getByRole("button", { name: /Dataset and preflight/ }).click();
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\custom-classify");
    await page.getByRole("button", { name: "Preflight", exact: true }).click();
    await expect.poll(() => mutationsFor(api, "POST", "/api/jobs/preflight").length).toBe(1);
    const config = (mutationsFor(api, "POST", "/api/jobs/preflight")[0].body as { config: Record<string, unknown> }).config;
    expect(config).toMatchObject({
      classify: { indexMode: "custom", customResourcePath: "E:\\picked\\resource.json" },
    });
    expect((config.classify as Record<string, unknown>).resourceId).toBeUndefined();
  });

  test("a pending preflight disables only its duplicate trigger", async ({ page, api }) => {
    const releasePreflight = holdRoute(api, "POST /api/jobs/preflight");
    await openApp(page, { language: "en" });
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\pending-preflight");

    const preflight = page.getByRole("button", { name: "Preflight", exact: true });
    await preflight.click();
    await expect(preflight).toBeDisabled();
    releasePreflight();
  });

  test("a pending preflight freezes Caption, Classify, and Replace configuration", async ({ page, api }) => {
    const releasePreflight = holdRoute(api, "POST /api/jobs/preflight");
    await openApp(page, { language: "en" });
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\pending-preflight-controls");
    await page.getByRole("button", { name: "Preflight", exact: true }).click();
    await expect(page.getByRole("button", { name: "Preflight", exact: true })).toBeDisabled();

    await page.locator(".workflow-rail").getByRole("button", { name: /Caption/ }).click();
    await expect(page.locator("#caption-tagging-model")).toBeDisabled();

    await page.locator(".workflow-rail").getByRole("button", { name: /Classify/ }).click();
    await expect(page.getByRole("button", { name: "Custom resource", exact: true })).toBeDisabled();
    await expect(page.locator("#classify-resource")).toBeDisabled();

    await page.locator(".workflow-rail").getByRole("button", { name: /Replace/ }).click();
    await expect(page.getByRole("checkbox", { name: "Enable E621 replacement" })).toBeDisabled();
    await expect(page.locator("#replace-mode")).toBeDisabled();
    releasePreflight();
  });

  test("a pending preflight cannot submit the same mutation twice", async ({ page, api }) => {
    const releasePreflight = holdRoute(api, "POST /api/jobs/preflight");
    await openApp(page, { language: "en" });
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\pending-preflight");

    const preflight = page.getByRole("button", { name: "Preflight", exact: true });
    await preflight.click();
    await expect(preflight).toBeDisabled();
    await preflight.dispatchEvent("click");
    releasePreflight();
    await expect.poll(() => mutationsFor(api, "POST", "/api/jobs/preflight").length).toBe(1);
  });

  test("the pending preflight region advertises scoped busy state", async ({ page, api }) => {
    const releasePreflight = holdRoute(api, "POST /api/jobs/preflight");
    await openApp(page, { language: "en" });
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\pending-preflight");
    await page.getByRole("button", { name: "Preflight", exact: true }).click();

    await expect(page.locator(".step-actions").first()).toHaveAttribute("aria-busy", "true");
    releasePreflight();
  });

  test("renders a failed preflight in the action-feedback region", async ({ page, api }) => {
    failRoute(api, "POST /api/jobs/preflight", "preflight unavailable");
    await openApp(page, { language: "en" });
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\failed-preflight");
    await page.getByRole("button", { name: "Preflight", exact: true }).click();

    await expect(page.locator(".action-feedback").getByRole("alert")).toHaveText("preflight unavailable");
  });

  test("renders actionable dataset claim guidance after workspace confirmation conflicts", async ({ page, api }) => {
    const detail = "Dataset is claimed by task 3bc585. Select it under Recent tasks: Recover keeps its progress and continues to hold the dataset; Discard deletes its overlay and releases the dataset.";
    failRoute(api, "POST /api/jobs/job-e621-characterization/confirm-workspace", detail, 409);
    await openApp(page, { language: "en" });
    await page.getByRole("textbox", { name: "Source dataset", exact: true }).fill("E:\\datasets\\claimed");
    await page.getByRole("button", { name: "Preflight", exact: true }).click();

    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Confirm workspace" }).click();

    const alert = page.locator(".action-feedback").getByRole("alert");
    await expect(alert).toHaveText(detail);
    await expect(alert).toContainText("task 3bc585");
    await expect(alert).toContainText("Recent tasks");
    await expect(alert).toContainText("Recover keeps its progress and continues to hold the dataset");
    await expect(alert).toContainText("Discard deletes its overlay and releases the dataset");
    await expect(alert).not.toContainText("request failed: 409");
    await expect(alert).not.toContainText("request failed: 500");
  });
});
