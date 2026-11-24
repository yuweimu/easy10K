import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  define: {},
  plugins: [react(), {
    name: "local-project-directory",
    configResolved(config) {
      // Vite normalizes its absolute frontend root to forward slashes.
      const projectDir = config.root.slice(0, config.root.lastIndexOf("/") + 1);
      if (config.define) {
        config.define["import.meta.env.VITE_PROJECT_DIR"] = JSON.stringify(projectDir);
      }
    },
  }],
  build: {
    rollupOptions: {
      input: {
        main: "index.html",
        reportsAdmin: "reports-admin.html",
        reportDigestion: "report-digestion.html",
        stockReports: "stock-reports.html",
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        // Keep the proxy target aligned with the FastAPI launch configuration.
        // On systems where localhost resolves to ::1 first, targeting localhost
        // can produce ECONNREFUSED even though uvicorn is bound to 127.0.0.1.
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      "/stocks": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
