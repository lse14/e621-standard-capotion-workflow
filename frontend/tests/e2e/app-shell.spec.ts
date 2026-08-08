import {
  clearRouteFailure,
  expect,
  failRoute,
  holdRoute,
  openApp,
  test,
} from "./mockApi";

test.describe("application shell characterization", () => {
  test("shows local resource loading before the selectable E621 tagger", async ({ page, api }) => {
    const releaseResources = holdRoute(api, "GET /api/resources");

    await openApp(page, { language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /Caption/ }).click();

    const tagger = page.getByLabel("Tagging model", { exact: true });
    await expect(tagger).toBeDisabled();
    await expect(page.locator(".resource-picker").getByRole("status")).toHaveText("Loading resources...");

    releaseResources();
    await expect(tagger).toBeEnabled();
    await expect(tagger).toHaveValue("caption-e621-eva02-large-full-v1");
  });

  test("shows the empty recent-task state", async ({ page }) => {
    await openApp(page, { language: "en" });

    const recentTasks = page.getByLabel("Recent tasks");
    await expect(recentTasks).toHaveValue("");
    await expect(recentTasks.locator("option:checked")).toHaveText("no task yet");
  });

  test("returns a retried resource control to its refresh state after recovery", async ({ page, api }) => {
    failRoute(api, "GET /api/resources", "resource catalog unavailable");

    await openApp(page, { language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /Caption/ }).click();

    const picker = page.locator(".resource-picker");
    await expect(picker.getByRole("alert")).toHaveText("resource catalog unavailable");

    clearRouteFailure(api, "GET /api/resources");
    await picker.getByRole("button", { name: "Retry resources" }).click();
    await expect(page.getByLabel("Tagging model", { exact: true })).toBeEnabled();
    await expect(picker.getByRole("button", { name: "Refresh resources" })).toBeVisible();
  });

  test("resource failure exposes a dedicated scoped retry action", async ({ page, api }) => {
    failRoute(api, "GET /api/resources", "resource catalog unavailable");
    await openApp(page, { language: "en" });
    await page.locator(".workflow-rail").getByRole("button", { name: /Caption/ }).click();

    const picker = page.locator(".resource-picker");
    await expect(picker.getByRole("alert")).toHaveText("resource catalog unavailable");
    await expect(picker.getByRole("button", { name: /retry resources/i })).toBeVisible();

    clearRouteFailure(api, "GET /api/resources");
    await picker.getByRole("button", { name: /retry resources/i }).click();
    await expect(page.getByLabel("Tagging model", { exact: true })).toBeEnabled();
  });

  test("shows a recent-task error in its own region and retries only that request", async ({ page, api }) => {
    failRoute(api, "GET /api/jobs", "recent tasks unavailable");
    await openApp(page, { language: "en" });

    const taskState = page.locator(".recent-task-state");
    await expect(taskState.getByRole("alert")).toHaveText("recent tasks unavailable");
    await expect(taskState.getByRole("button", { name: "Retry tasks" })).toBeVisible();

    clearRouteFailure(api, "GET /api/jobs");
    await taskState.getByRole("button", { name: "Retry tasks" }).click();
    await expect(page.getByLabel("Recent tasks")).toBeEnabled();
  });

  test("shows a selected-task error inside Task Monitor and retries the snapshot", async ({ page, api }) => {
    const jobId = "job-snapshot-error";
    failRoute(api, `GET /api/jobs/${jobId}`, "selected task unavailable");
    await openApp(page, { jobId, language: "en" });

    const monitor = page.locator(".task-monitor");
    await expect(monitor.getByRole("alert")).toHaveText("selected task unavailable");
    await expect(monitor.getByRole("button", { name: "Retry task" })).toBeVisible();

    clearRouteFailure(api, `GET /api/jobs/${jobId}`);
    await monitor.getByRole("button", { name: "Retry task" }).click();
    await expect(monitor.locator(".monitor-heading > .status")).toHaveText("ready");
    await expect(monitor.locator(".monitor-heading > .status")).toHaveAttribute("aria-live", "polite");
  });

  test("gives every visible form control an accessible name", async ({ page }) => {
    await openApp(page, { language: "en" });

    const controls = page.locator("button, input, select, textarea");
    for (let index = 0; index < await controls.count(); index += 1) {
      await expect(controls.nth(index)).toHaveAccessibleName(/\S/);
    }
  });

  test("persists an explicitly selected English interface across reload", async ({ page }) => {
    await openApp(page);

    await page.getByRole("button", { name: "EN", exact: true }).click();
    await expect(page.getByRole("navigation", { name: "Workflow" })).toBeVisible();

    await page.reload();
    await expect(page.getByRole("navigation", { name: "Workflow" })).toBeVisible();
  });
});
