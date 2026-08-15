import {
  DEFAULT_JOB_ID,
  expect,
  holdRoute,
  makeSnapshot,
  mutationsFor,
  openApp,
  setJobSnapshot,
  test,
} from "./mockApi";

async function openTrackedJob(page: Parameters<typeof openApp>[0], status: string) {
  await openApp(page, { jobId: DEFAULT_JOB_ID, language: "en" });
  await expect(page.locator(".task-monitor > .monitor-heading > .status")).toHaveText(status);
}

type SnapshotFetchProbe = { active: number; maxActive: number; started: number };

async function installSnapshotFetchProbe(page: Parameters<typeof openApp>[0], jobId: string): Promise<void> {
  await page.addInitScript((path) => {
    const probe: SnapshotFetchProbe = { active: 0, maxActive: 0, started: 0 };
    const scope = window as typeof window & { __snapshotFetchProbe?: SnapshotFetchProbe };
    const originalFetch = window.fetch.bind(window);
    scope.__snapshotFetchProbe = probe;
    window.fetch = async (...args) => {
      const input = args[0];
      const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (new URL(href, window.location.href).pathname !== path) return originalFetch(...args);
      probe.active += 1;
      probe.started += 1;
      probe.maxActive = Math.max(probe.maxActive, probe.active);
      try {
        return await originalFetch(...args);
      } finally {
        probe.active -= 1;
      }
    };
  }, `/api/jobs/${encodeURIComponent(jobId)}`);
}

async function snapshotFetchProbe(page: Parameters<typeof openApp>[0]): Promise<SnapshotFetchProbe> {
  return page.evaluate(() => {
    const probe = (window as typeof window & { __snapshotFetchProbe?: SnapshotFetchProbe }).__snapshotFetchProbe;
    if (!probe) throw new Error("snapshot fetch probe is not installed");
    return { ...probe };
  });
}

test.describe("task status and issue characterization", () => {
  test("loads task switches without overlapping a repeated snapshot target", async ({ page, api }) => {
    const nextJobId = "job-selected-while-previous-pending";
    setJobSnapshot(api, makeSnapshot({ jobId: DEFAULT_JOB_ID, status: "running", currentModuleId: "nl" }));
    api.snapshots.set(nextJobId, makeSnapshot({ jobId: nextJobId, status: "failed", currentModuleId: "ocr" }));
    await installSnapshotFetchProbe(page, DEFAULT_JOB_ID);
    const releasePrevious = holdRoute(api, `GET /api/jobs/${DEFAULT_JOB_ID}`);
    try {
      await openApp(page, { jobId: DEFAULT_JOB_ID, language: "en" });
      await expect.poll(async () => (await snapshotFetchProbe(page)).started).toBe(1);
      await page.getByLabel("Task ID").fill(nextJobId);
      await expect(page.locator(".task-monitor > .monitor-heading > .status")).toHaveText("failed", { timeout: 2000 });
      await page.getByLabel("Task ID").fill(DEFAULT_JOB_ID);
      await expect.poll(async () => (await snapshotFetchProbe(page)).started).toBe(2);
      expect((await snapshotFetchProbe(page)).maxActive).toBe(1);
    } finally {
      releasePrevious();
    }
    await expect(page.locator(".task-monitor > .monitor-heading > .status")).toHaveText("running");
  });

  test("switches issue pages without overlapping snapshot requests", async ({ page, api }) => {
    const snapshot = makeSnapshot({ jobId: DEFAULT_JOB_ID, status: "running", currentModuleId: "nl" });
    snapshot.issues = [{
      issue_id: "issue-page-one", sample_id: 1, module_id: "nl", code: "review", severity: "warning",
      message: "Review this sample", retriable: 1, attempt: 1, repair_start_module: "nl",
    }];
    snapshot.nextIssueAfterSampleId = 1;
    snapshot.nextIssueAfterIssueId = "issue-page-one";
    setJobSnapshot(api, snapshot);
    await installSnapshotFetchProbe(page, DEFAULT_JOB_ID);
    await openApp(page, { jobId: DEFAULT_JOB_ID, language: "en" });
    await expect(page.getByText("Review this sample")).toBeVisible();

    const releasePrevious = holdRoute(api, `GET /api/jobs/${DEFAULT_JOB_ID}`);
    try {
      await expect.poll(async () => (await snapshotFetchProbe(page)).active, { timeout: 2000 }).toBe(1);
      await page.getByRole("button", { name: "Next page", exact: true }).click();
      await expect.poll(async () => (await snapshotFetchProbe(page)).started).toBeGreaterThanOrEqual(3);
      expect((await snapshotFetchProbe(page)).maxActive).toBe(1);
    } finally {
      releasePrevious();
    }
  });

  test("submits task-only OCR execution tuning and shows the compact frozen runtime", async ({ page, api }) => {
    await openApp(page, { language: "en" });

    await page.locator(".workflow-rail").getByRole("button", { name: /OCR/ }).click();
    await page.getByRole("checkbox", { name: "Enable OCR" }).check();
    await expect(page.getByLabel("OCR device", { exact: true })).toBeVisible({ timeout: 2000 });
    await page.getByLabel("OCR device", { exact: true }).selectOption("cuda");
    await page.getByRole("radio", { name: "Manual detection limit" }).check();
    await page.getByLabel("OCR detection limit", { exact: true }).fill("2560");
    await page.getByRole("radio", { name: "Manual text batch" }).check();
    await page.getByLabel("OCR text batch", { exact: true }).fill("4");

    await page.locator(".workflow-rail").getByRole("button", { name: "Dataset and preflight" }).click();
    await page.getByRole("textbox", { name: "Source dataset" }).fill("E:\\datasets\\ocr-ui");
    await page.getByRole("button", { name: "Preflight", exact: true }).click();
    await expect.poll(() => mutationsFor(api, "POST", "/api/jobs/preflight").length).toBe(1);
    expect(mutationsFor(api, "POST", "/api/jobs/preflight")[0]?.body).toMatchObject({
      config: { schemaVersion: 8, ocr: { enabled: true, device: "cuda" } },
      ocrExecution: {
        textDetLimitSideLen: { mode: "manual", value: 2560 },
        textBatchSize: { mode: "manual", value: 4 },
      },
    });
    await expect(page.getByLabel("OCR runtime")).toContainText("GPU 24 GiB");
    await expect(page.getByLabel("OCR runtime")).toContainText("2560 / 4");
    await page.locator(".workflow-rail").getByRole("button", { name: /OCR/ }).click();
    await expect(page.getByLabel("OCR device", { exact: true })).toBeDisabled();
  });

  test("shows a running NL task and pauses it through the existing endpoint", async ({ page, api }) => {
    setJobSnapshot(api, makeSnapshot({ status: "running", currentModuleId: "nl" }));
    await openTrackedJob(page, "running");

    await page.getByRole("button", { name: "Pause NL" }).click();
    await expect.poll(() => mutationsFor(api, "POST", `/api/jobs/${DEFAULT_JOB_ID}/nl/pause`).length).toBe(1);
    await expect(page.locator(".task-monitor > .monitor-heading > .status")).toHaveText("paused");
  });

  test("shows a paused NL task and resumes it through the existing endpoint", async ({ page, api }) => {
    setJobSnapshot(api, makeSnapshot({ status: "paused", currentModuleId: "nl" }));
    await openTrackedJob(page, "paused");

    await page.getByRole("button", { name: "Resume NL" }).click();
    await expect.poll(() => mutationsFor(api, "POST", `/api/jobs/${DEFAULT_JOB_ID}/nl/resume`).length).toBe(1);
    await expect(page.locator(".task-monitor > .monitor-heading > .status")).toHaveText("running");
  });

  test("pauses and resumes a running Policy task through the existing endpoints", async ({ page, api }) => {
    setJobSnapshot(api, makeSnapshot({ status: "running", currentModuleId: "dropout" }));
    await openTrackedJob(page, "running");

    await page.getByRole("button", { name: "Pause policy" }).click();
    await expect.poll(() => mutationsFor(api, "POST", `/api/jobs/${DEFAULT_JOB_ID}/policy/pause`).length).toBe(1);
    await expect(page.locator(".task-monitor > .monitor-heading > .status")).toHaveText("paused");

    await page.getByRole("button", { name: "Resume policy" }).click();
    await expect.poll(() => mutationsFor(api, "POST", `/api/jobs/${DEFAULT_JOB_ID}/policy/resume`).length).toBe(1);
    await expect(page.locator(".task-monitor > .monitor-heading > .status")).toHaveText("running");
  });

  test("shows an interrupted task and recovers it through the existing endpoint", async ({ page, api }) => {
    setJobSnapshot(api, makeSnapshot({ status: "interrupted", currentModuleId: "caption" }));
    await openTrackedJob(page, "interrupted");

    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Recover task" }).click();
    await expect.poll(() => mutationsFor(api, "POST", `/api/jobs/${DEFAULT_JOB_ID}/recover`).length).toBe(1);
    await expect(page.locator(".task-monitor > .monitor-heading > .status")).toHaveText("running");
  });

  test("shows OCR diagnostics and starts OCR repair through the existing endpoint", async ({ page, api }) => {
    const snapshot = makeSnapshot({ status: "failed", currentModuleId: "ocr", schemaVersion: 5 });
    snapshot.ocrDiagnostics = [
      { code: "ocr_total", severity: "info", count: 3 },
      { code: "ocr_new", severity: "info", count: 1 },
      { code: "ocr_reused", severity: "info", count: 2 },
      { code: "ocr_success", severity: "info", count: 1 },
      { code: "ocr_no_text", severity: "info", count: 1 },
      { code: "ocr_failed", severity: "info", count: 1 },
      { code: "ocr_text_items", severity: "info", count: 7 },
      { code: "ocr_included_for_llm", severity: "info", count: 5 },
      { code: "nl_ocr_context_omitted_too_large", severity: "warning", count: 1 },
    ];
    snapshot.issues = [{
      issue_id: "issue-1",
      sample_id: 1,
      module_id: "ocr",
      code: "ocr_inference_failed",
      severity: "error",
      message: "A test issue is available for repair.",
      retriable: 1,
      attempt: 2,
      repair_start_module: "ocr",
    }];
    setJobSnapshot(api, snapshot);
    await openTrackedJob(page, "failed");

    await page.locator(".workflow-rail").getByRole("button", { name: /OCR/ }).click();
    await expect(page.getByText("OCR summary")).toBeVisible();
    await expect(page.getByLabel("OCR summary").getByText("3", { exact: true })).toBeVisible();
    await expect(page.getByText("A test issue is available for repair.")).toBeVisible();
    await page.getByRole("button", { name: "Reprocess retriable samples", exact: true }).click();
    await expect.poll(() => mutationsFor(api, "POST", `/api/jobs/${DEFAULT_JOB_ID}/repair`).length).toBe(1);
  });
});
