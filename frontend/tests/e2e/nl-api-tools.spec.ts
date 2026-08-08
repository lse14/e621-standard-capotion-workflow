import { expect, holdRoute, mutationsFor, openApp, test } from "./mockApi";

const BUILTIN_PROMPT = "You are the verified Anima v4 base prompt for local diagnostics.";

async function openNl(page: Parameters<typeof openApp>[0]) {
  await openApp(page, { language: "en" });
  await page.locator(".workflow-rail").getByRole("button", { name: /NL/ }).click();
}

async function saveBuiltInAsCustom(page: Parameters<typeof openApp>[0], name = "Custom base", basePrompt = "Custom base prompt") {
  await page.getByLabel("Preset name", { exact: true }).fill(name);
  await page.getByLabel("Base prompt", { exact: true }).fill(basePrompt);
  await page.getByRole("button", { name: "Save as custom", exact: true }).click();
  await expect(page.getByRole("button", { name: "Save changes", exact: true })).toBeVisible();
}

function redactedMutationText(api: { mutations: unknown[] }) {
  return JSON.stringify(api.mutations);
}

test.describe("NL API tools", () => {
  test("shows the complete built-in base prompt", async ({ page }) => {
    await openNl(page);
    await expect(page.getByLabel("Prompt preset", { exact: true })).toHaveValue("builtin:nl-default-prompt-v4-base");
    await expect(page.getByLabel("Base prompt", { exact: true })).toHaveValue(BUILTIN_PROMPT);
  });

  test("saves an edited built-in prompt as custom without changing the built-in", async ({ page, api }) => {
    await openNl(page);
    await saveBuiltInAsCustom(page, "Custom base", "Custom prompt text");
    await expect.poll(() => mutationsFor(api, "POST", "/api/nl/prompt-presets").length).toBe(1);
    expect(mutationsFor(api, "POST", "/api/nl/prompt-presets")[0].body).toEqual({ name: "Custom base", basePrompt: "Custom prompt text" });

    await page.getByLabel("Prompt preset", { exact: true }).selectOption("builtin:nl-default-prompt-v4-base");
    await expect(page.getByLabel("Base prompt", { exact: true })).toHaveValue(BUILTIN_PROMPT);
  });

  test("keeps a renamed custom prompt after selecting it again", async ({ page, api }) => {
    await openNl(page);
    await saveBuiltInAsCustom(page, "Custom base", "Original custom prompt");
    await page.getByLabel("Preset name", { exact: true }).fill("Renamed custom base");
    await page.getByLabel("Base prompt", { exact: true }).fill("Updated custom prompt");
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    await expect.poll(() => mutationsFor(api, "PUT", "/api/nl/prompt-presets/custom%3Amock-1").length).toBe(1);

    await page.getByLabel("Prompt preset", { exact: true }).selectOption("builtin:nl-default-prompt-v4-base");
    await expect(page.getByLabel("Prompt preset", { exact: true })).toBeEnabled();
    await page.getByLabel("Prompt preset", { exact: true }).selectOption({ label: "Renamed custom base" });
    await expect(page.getByLabel("Preset name", { exact: true })).toHaveValue("Renamed custom base");
    await expect(page.getByLabel("Base prompt", { exact: true })).toHaveValue("Updated custom prompt");
  });

  test("confirms custom deletion and selects the built-in prompt", async ({ page }) => {
    await openNl(page);
    await saveBuiltInAsCustom(page);
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Delete preset", exact: true }).click();

    await expect(page.getByLabel("Prompt preset", { exact: true })).toHaveValue("builtin:nl-default-prompt-v4-base");
    await expect(page.getByLabel("Base prompt", { exact: true })).toHaveValue(BUILTIN_PROMPT);
  });

  test("discovers selectable models and applies one to the unsaved profile", async ({ page, api }) => {
    await openNl(page);
    await page.getByLabel("Endpoint", { exact: true }).fill("https://unsaved.example/v1");
    await page.getByRole("button", { name: "Get models", exact: true }).click();

    await expect.poll(() => mutationsFor(api, "POST", "/api/nl/diagnostics/models").length).toBe(1);
    await expect(page.getByLabel("Discovered models", { exact: true })).toContainText("provider-model-2");
    await page.getByLabel("Discovered models", { exact: true }).selectOption("provider-model-2");
    await expect(page.getByLabel("Model", { exact: true })).toHaveValue("provider-model-2");
  });

  test("uses unsaved values once and shows structured successful diagnostic feedback", async ({ page, api }) => {
    const rawKey = "transient-key-must-never-leak";
    const release = holdRoute(api, "POST /api/nl/diagnostics/test-message");
    await openNl(page);
    await page.getByLabel("Endpoint", { exact: true }).fill("https://unsaved.example/v1");
    await page.getByLabel("Model", { exact: true }).fill("unsaved-model");
    await page.getByLabel("Base prompt", { exact: true }).fill("Unsaved diagnostic prompt");
    await page.getByLabel("API key", { exact: true }).fill(rawKey);

    const send = page.getByRole("button", { name: "Send test message", exact: true });
    await send.click();
    await expect(send).toBeDisabled();
    await send.dispatchEvent("click");
    await expect.poll(() => mutationsFor(api, "POST", "/api/nl/diagnostics/test-message").length).toBe(1);
    expect(redactedMutationText(api)).not.toContain(rawKey);
    await expect(page.locator("body")).not.toContainText(rawKey);
    release();

    await expect(page.getByText("Success", { exact: true })).toBeVisible();
    await expect(page.getByText("17 ms", { exact: true })).toBeVisible();
    await expect(page.getByText("actual-unsaved-model", { exact: true })).toBeVisible();
    await expect(page.getByText("Local diagnostic reply.", { exact: true })).toBeVisible();
    await expect(page.getByText("Prompt tokens: 12", { exact: true })).toBeVisible();
  });

  test("shows only a sanitized diagnostic failure reason", async ({ page }) => {
    const rawKey = "failure-key-must-never-leak";
    await openNl(page);
    await page.getByLabel("Endpoint", { exact: true }).fill("https://failure.example/v1");
    await page.getByLabel("Model", { exact: true }).fill("failure-model");
    await page.getByLabel("API key", { exact: true }).fill(rawKey);
    await page.getByRole("button", { name: "Send test message", exact: true }).click();

    await expect(page.getByText("Provider rejected the test request.", { exact: true })).toBeVisible();
    await expect(page.locator("body")).not.toContainText(rawKey);
  });

  test("uses the credential reference without an apiKey when transient key is blank", async ({ page, api }) => {
    await openNl(page);
    await page.getByLabel("Endpoint", { exact: true }).fill("https://saved.example/v1");
    await page.getByLabel("Model", { exact: true }).fill("saved-model");
    await page.getByLabel("Credential ref", { exact: true }).fill("anima-test");
    await page.getByLabel("API key", { exact: true }).fill("");
    await page.getByRole("button", { name: "Send test message", exact: true }).click();

    await expect.poll(() => mutationsFor(api, "POST", "/api/nl/diagnostics/test-message").length).toBe(1);
    const body = mutationsFor(api, "POST", "/api/nl/diagnostics/test-message")[0].body;
    expect(body).toMatchObject({ apiCredentialRef: "anima-test" });
    expect(body).not.toHaveProperty("apiKey");
    await expect(page.getByText("Success", { exact: true })).toBeVisible();
  });

  test("keeps the NL tools contained at desktop and 390px without local mock errors", async ({ page }) => {
    for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      await openNl(page);
      const bounds = await page.locator(".nl-api-tools [data-setting-field]:visible").evaluateAll((nodes) => nodes.map((node) => {
        const rect = node.getBoundingClientRect();
        return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
      }));
      for (let index = 0; index < bounds.length; index += 1) {
        for (let other = index + 1; other < bounds.length; other += 1) {
          const first = bounds[index];
          const second = bounds[other];
          const overlaps = first.left < second.right && first.right > second.left && first.top < second.bottom && first.bottom > second.top;
          expect(overlaps).toBe(false);
        }
      }
      const documentWidth = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
      expect(documentWidth.scrollWidth).toBeLessThanOrEqual(documentWidth.clientWidth);
    }
  });
});
