import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // In compose the backend is reachable as http://backend:8000; locally, on localhost.
  server: { proxy: { "/api": process.env.API_URL ?? "http://localhost:8000" } },
});
