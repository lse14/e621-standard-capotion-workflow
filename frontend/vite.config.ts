import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

function normalizeHtmlLineEndings(): Plugin {
  return {
    name: "normalize-html-line-endings",
    apply: "build",
    transformIndexHtml: {
      order: "post",
      handler(html) {
        return html.replace(/\r\n?/g, "\n");
      },
    },
  };
}

export default defineConfig({
  plugins: [react(), normalizeHtmlLineEndings()],
  server: { proxy: { "/api": "http://127.0.0.1:8765", "/health": "http://127.0.0.1:8765" } },
  build: { outDir: "dist", emptyOutDir: true },
});
