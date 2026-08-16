import { expect, holdRoute, mutationsFor, openApp, test } from "./mockApi";

async function openNl(page: Parameters<typeof openApp>[0]) {
  await openApp(page, { language: "en" });
  await page.locator(".workflow-rail").getByRole("button", { name: /NL/ }).click();
  await page.locator(".nl-preset-library").waitFor();
}

async function createCustomPrompt(page: Parameters<typeof openApp>[0], name = "Custom base", prompt = "Custom prompt text") {
  const library = page.locator(".nl-preset-library");
  await library.getByRole("button", { name: "New preset", exact: true }).click();
  await library.getByLabel("Preset name", { exact: true }).fill(name);
  await library.getByLabel("Preset type", { exact: true }).selectOption("general");
  await library.getByLabel("Prompt text", { exact: true }).fill(prompt);
  await library.getByRole("button", { name: "Create preset", exact: true }).click();
  await expect(library.getByRole("button", { name, exact: true })).toBeVisible();
  return library;
}

function redactedMutationText(api: { mutations: unknown[] }) {
  return JSON.stringify(api.mutations);
}

test.describe("NL API tools", () => {
  test("selects an existing prompt card and loads its prompt type", async ({ page }) => {
    await openNl(page);
    const library = page.locator(".nl-preset-library");

    await library.getByRole("button", { name: "Style", exact: true }).click();

    await expect(library.getByLabel("Preset type", { exact: true })).toHaveValue("style");
    await expect(library.getByLabel("Prompt text", { exact: true })).toHaveValue("Style task preset prompt.");
  });

  test("saves an edited custom prompt through the unified library", async ({ page, api }) => {
    await openNl(page);
    const library = await createCustomPrompt(page, "Custom base", "Original custom prompt");
    await library.getByLabel("Preset name", { exact: true }).fill("Renamed custom base");
    await library.getByLabel("Prompt text", { exact: true }).fill("Updated custom prompt");
    await library.getByRole("button", { name: "Save changes", exact: true }).click();

    await expect.poll(() => mutationsFor(api, "PUT", "/api/nl/prompt-presets/custom%3Amock-3").length).toBe(1);
    expect(mutationsFor(api, "PUT", "/api/nl/prompt-presets/custom%3Amock-3")[0].body).toEqual({
      name: "Renamed custom base", type: "general", promptText: "Updated custom prompt",
    });
    await expect(library.getByRole("button", { name: "Renamed custom base", exact: true })).toBeVisible();
  });

  test("confirms custom deletion without exposing a separate caption preset", async ({ page }) => {
    await openNl(page);
    const library = await createCustomPrompt(page);
    page.once("dialog", (dialog) => dialog.accept());
    await library.getByRole("button", { name: "Delete preset", exact: true }).click();

    await expect(library.getByRole("button", { name: "Custom base", exact: true })).toHaveCount(0);
    await expect(page.getByLabel("Caption preset", { exact: true })).toHaveCount(0);
  });

  test("discovers one model list for primary and backup and warns for remote HTTP", async ({ page, api }) => {
    await openNl(page);
    await expect(page.getByLabel("API credential reference", { exact: true })).toHaveCount(0);
    await page.getByLabel("Endpoint URL", { exact: true }).fill("http://provider.example/v1");
    await expect(page.getByText("HTTP sends the API key and images in plaintext.", { exact: true })).toBeVisible();
    const backup = page.getByLabel("Backup model", { exact: true });
    await expect(backup).toBeDisabled();
    await expect(page.getByRole("button", { name: "Show API key", exact: true })).toBeDisabled();
    await page.getByLabel("API key", { exact: true }).fill("temporary-key");
    await page.getByRole("button", { name: "Show API key", exact: true }).click();
    await expect(page.getByLabel("API key", { exact: true })).toHaveAttribute("type", "text");
    await page.getByRole("button", { name: "Get models", exact: true }).click();

    await expect.poll(() => mutationsFor(api, "POST", "/api/nl/diagnostics/models").length).toBe(1);
    await expect(page.getByLabel("Primary model", { exact: true }).locator("option")).toHaveText(["Select a model", "provider-model-2", "provider-model-1", "Enter a model manually"]);
    await page.getByLabel("Primary model", { exact: true }).selectOption("provider-model-2");
    await expect(page.getByLabel("Primary model", { exact: true })).toHaveValue("provider-model-2");
    await page.getByLabel("Enable backup model", { exact: true }).check();
    await page.getByLabel("Backup model", { exact: true }).selectOption("provider-model-1");
    await expect(page.getByLabel("Backup model", { exact: true })).toHaveValue("provider-model-1");
    await page.getByLabel("Primary model", { exact: true }).selectOption("__manual__");
    await expect(page.getByLabel("Manual primary model", { exact: true })).toBeVisible();
    await page.getByLabel("Manual primary model", { exact: true }).fill("manual-primary");
  });

  test("uses a saved prompt with transient credentials and shows structured diagnostic feedback", async ({ page, api }) => {
    const rawKey = "transient-key-must-never-leak";
    const release = holdRoute(api, "POST /api/nl/diagnostics/test-message");
    await openNl(page);
    await createCustomPrompt(page, "Diagnostic prompt", "Saved diagnostic prompt");
    await page.getByLabel("Endpoint URL", { exact: true }).fill("https://unsaved.example/v1");
    await page.getByLabel("Manual primary model", { exact: true }).fill("unsaved-model");
    await page.getByLabel("API key", { exact: true }).fill(rawKey);

    const send = page.getByRole("button", { name: "Send test message", exact: true });
    await send.click();
    await expect(send).toBeDisabled();
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
    await page.getByLabel("Endpoint URL", { exact: true }).fill("https://failure.example/v1");
    await page.getByLabel("Manual primary model", { exact: true }).fill("failure-model");
    await page.getByLabel("API key", { exact: true }).fill(rawKey);
    await page.getByRole("button", { name: "Send test message", exact: true }).click();

    await expect(page.getByText("Provider rejected the test request.", { exact: true })).toBeVisible();
    await expect(page.locator("body")).not.toContainText(rawKey);
  });

  test("uses the saved credential reference without an apiKey when transient key is blank", async ({ page, api }) => {
    await openNl(page);
    await page.getByLabel("Endpoint URL", { exact: true }).fill("https://saved.example/v1");
    await page.getByLabel("Manual primary model", { exact: true }).fill("saved-model");
    await page.getByLabel("API key", { exact: true }).fill("");
    await page.getByRole("button", { name: "Send test message", exact: true }).click();

    await expect.poll(() => mutationsFor(api, "POST", "/api/nl/diagnostics/test-message").length).toBe(1);
    const body = mutationsFor(api, "POST", "/api/nl/diagnostics/test-message")[0].body;
    expect(body).toMatchObject({ apiCredentialRef: "anima-test" });
    expect(body).not.toHaveProperty("apiKey");
    await expect(page.getByText("Success", { exact: true })).toBeVisible();
  });

  test("saves selected models without the transient key and derives an internal reference", async ({ page, api }) => {
    api.profiles[0].apiCredentialRef = "nl-profile:default";
    await openNl(page);
    await page.getByLabel("Endpoint URL", { exact: true }).fill("http://provider.example/v1");
    await page.getByLabel("API key", { exact: true }).fill("temporary-key");
    await page.getByRole("button", { name: "Get models", exact: true }).click();
    await expect.poll(() => mutationsFor(api, "POST", "/api/nl/diagnostics/models").length).toBe(1);
    await page.getByLabel("Primary model", { exact: true }).selectOption("provider-model-2");
    await page.getByLabel("Enable backup model", { exact: true }).check();
    await page.getByLabel("Backup model", { exact: true }).selectOption("provider-model-1");
    await page.getByRole("button", { name: "Save profile", exact: true }).click();

    await expect.poll(() => mutationsFor(api, "PUT", "/api/nl/profiles/default").length).toBe(1);
    const profileBody = mutationsFor(api, "PUT", "/api/nl/profiles/default")[0].body as Record<string, unknown>;
    expect(profileBody).toMatchObject({ endpoint: "http://provider.example/v1", model: "provider-model-2", backupModel: "provider-model-1", apiCredentialRef: "nl-profile-default" });
    expect(profileBody).not.toHaveProperty("apiKey");
    await expect.poll(() => mutationsFor(api, "PUT", "/api/nl/credentials/nl-profile-default").length).toBe(1);
    await expect(page.getByLabel("API key", { exact: true })).toHaveAttribute("type", "password");
    await expect(page.getByLabel("API key", { exact: true })).toHaveValue("");
  });

  test("switches the active profile and clears transient discovery state", async ({ page, api }) => {
    api.profiles.push({
      profileId: "secondary",
      endpoint: "https://secondary.example/v1",
      model: "secondary-model",
      backupModel: null,
      apiCredentialRef: "secondary-key",
      systemPrompt: "Describe the image concisely.",
      apiPolicy: { maxRequestsPerMinute: 60 },
      hasCredential: false,
    });
    await openNl(page);
    await page.getByLabel("Endpoint URL", { exact: true }).fill("http://provider.example/v1");
    await page.getByLabel("API key", { exact: true }).fill("temporary-key");
    await page.getByRole("button", { name: "Show API key", exact: true }).click();
    await page.getByRole("button", { name: "Get models", exact: true }).click();
    await expect.poll(() => mutationsFor(api, "POST", "/api/nl/diagnostics/models").length).toBe(1);

    await page.getByLabel("Profile ID", { exact: true }).selectOption("secondary");
    await expect(page.getByLabel("Endpoint URL", { exact: true })).toHaveValue("https://secondary.example/v1");
    await expect(page.getByLabel("Primary model", { exact: true }).locator("option")).toHaveText(["Select a model", "Enter a model manually"]);
    await expect(page.getByLabel("API key", { exact: true })).toHaveAttribute("type", "password");
    await expect(page.getByLabel("API key", { exact: true })).toHaveValue("");
    await page.getByRole("button", { name: "Save profile", exact: true }).click();
    await expect.poll(() => mutationsFor(api, "PUT", "/api/nl/profiles/secondary").length).toBe(1);
  });

  test("keeps the API controls contained at desktop and 390px without local mock errors", async ({ page }) => {
    for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport);
      await openNl(page);
      const bounds = await page.locator(".nl-api-section [data-setting-field]:visible").evaluateAll((nodes) => nodes.map((node) => {
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
