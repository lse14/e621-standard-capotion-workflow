import path from "node:path";

import { createServer } from "vite";

const frontendRoot = path.resolve(__dirname, "../..");

function readE2ePort(): number {
  const rawPort = process.env.ANIMA_E2E_PORT ?? "4173";
  if (!/^\d+$/.test(rawPort)) throw new Error("ANIMA_E2E_PORT must be an integer between 1 and 65535");
  const port = Number(rawPort);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
    throw new Error("ANIMA_E2E_PORT must be an integer between 1 and 65535");
  }
  return port;
}

async function isAvailable(origin: string): Promise<boolean> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 1_000);
  try {
    const response = await fetch(origin, { redirect: "manual", signal: controller.signal });
    return response.status >= 200 && response.status < 400;
  } catch {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

export default async function globalSetup(): Promise<() => Promise<void>> {
  const port = readE2ePort();
  const origin = `http://127.0.0.1:${port}`;
  if (process.env.ANIMA_E2E_REUSE_EXISTING_SERVER === "1" && await isAvailable(origin)) {
    return async () => {};
  }

  const server = await createServer({
    root: frontendRoot,
    server: { host: "127.0.0.1", port, strictPort: true },
  });
  try {
    await server.listen();
  } catch (error) {
    await server.close().catch(() => {});
    throw error;
  }
  return async () => {
    await server.close();
  };
}
