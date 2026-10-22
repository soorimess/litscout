import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs separately: `uvicorn app.main:app_factory --factory --app-dir backend --port 8080`.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8080" } },
  build: { outDir: "dist", sourcemap: false },
});
