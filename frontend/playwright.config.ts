import { defineConfig } from "@playwright/test";

process.env.PLAYWRIGHT_BROWSERS_PATH ??= "0";

const e2ePort = process.env.ANIMA_E2E_PORT ?? "4173";
const e2eOrigin = `http://127.0.0.1:${e2ePort}`;

export default defineConfig({
  globalSetup: "./tests/e2e/globalSetup.ts",
  testDir: "./tests/e2e",
  outputDir: "./test-results",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: e2eOrigin,
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    reducedMotion: "reduce",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "off",
    viewport: { width: 1440, height: 900 },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
