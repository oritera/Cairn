import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/auth": "http://localhost:8000",
      "/projects": "http://localhost:8000",
      "/settings": "http://localhost:8000",
    },
  },
});
