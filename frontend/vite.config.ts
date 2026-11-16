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
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
