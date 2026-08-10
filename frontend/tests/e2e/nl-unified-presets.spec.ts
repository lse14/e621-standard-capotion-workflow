import { expect, openApp, test } from "./mockApi";

async function openNl(page: Parameters<typeof openApp>[0]) {
  await openApp(page, { language: "en" });
  await page.locator(".workflow-rail").getByRole("button", { name: /NL/ }).click();
}

test.describe("unified NL prompt presets", () => {
  test("shows all three prompt texts in one library and removes the supplement input", async ({ page }) => {
    await openNl(page);
    const library = page.locator(".nl-preset-library");
    await expect(library).toBeVisible();
    await expect(page.getByRole("heading", { name: "Prompt presets", exact: true })).toBeVisible();
    await expect(library.getByRole("button", { name: "General", exact: true })).toBeVisible();
    await expect(library.getByRole("button", { name: "Style", exact: true })).toBeVisible();
    await expect(library.getByRole("button", { name: "Character", exact: true })).toBeVisible();
    const presetCards = library.locator("[data-nl-preset-card]");
    await expect(presetCards).toHaveCount(3);
    await expect(presetCards.nth(0)).toContainText("General task preset prompt.");
    await expect(presetCards.nth(1)).toContainText("Style task preset prompt.");
    await expect(presetCards.nth(2)).toContainText("Character task preset prompt.");
    await expect(library).toContainText("General task preset prompt.");
    await expect(library).toContainText("Style task preset prompt.");
    await expect(library).toContainText("Character task preset prompt.");
    await expect(page.locator("#nl-user-supplement")).toHaveCount(0);
    await expect(page.getByLabel("Caption preset", { exact: true })).toHaveCount(0);
  });

  test("requires a type when creating a custom preset and can reset a built-in", async ({ page }) => {
    await openNl(page);
    const library = page.locator(".nl-preset-library");
    await library.getByRole("button", { name: "New preset", exact: true }).click();
    await expect(library.getByLabel("Preset type", { exact: true })).toBeVisible();
    await expect(library.getByRole("button", { name: "Create preset", exact: true })).toBeDisabled();
    await library.getByLabel("Preset name", { exact: true }).fill("Custom style");
    await library.getByLabel("Prompt text", { exact: true }).fill("Custom prompt");
    await library.getByLabel("Preset type", { exact: true }).selectOption("style");
    await expect(library.getByRole("button", { name: "Create preset", exact: true })).toBeEnabled();
    await library.getByRole("button", { name: "Create preset", exact: true }).click();
    await expect(library.getByText("Custom style", { exact: true })).toBeVisible();
    await library.getByRole("button", { name: "General", exact: true }).click();
    await library.getByLabel("Prompt text", { exact: true }).fill("Changed built-in");
    await library.getByRole("button", { name: "Save changes", exact: true }).click();
    await library.getByRole("button", { name: "Reset to default", exact: true }).click();
    await expect(library.getByLabel("Prompt text", { exact: true })).not.toHaveValue("Changed built-in");
  });
});
