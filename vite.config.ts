import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Keep the dev proxy in step with the backend port. Windows can reserve the
// usual port block for Hyper-V/WSL, so it is overridable via TOUCHLINE_PORT.
const apiTarget = `http://127.0.0.1:${process.env.TOUCHLINE_PORT || "9080"}`;

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": apiTarget } },
  build: { outDir: "dist", emptyOutDir: true },
});
