import { defineConfig } from "@playwright/test";

/**
 * End-to-end tests run the real Lanvex API and UI against the NetBox TEST DOUBLE
 * (backend/tests/fake_netbox.py). They verify Lanvex's own behaviour, not NetBox compatibility.
 *
 * Needs PostgreSQL and the `lanvex_e2e` database (see docs/development.md).
 * Set LANVEX_E2E_CHROME to use a system Chrome instead of Playwright's bundled browser.
 */
const backend = "../backend";
const dbUrl = process.env.LANVEX_E2E_DATABASE_URL ?? "postgresql+asyncpg://lanvex:lanvex-dev-only@localhost:5432/lanvex_e2e";
const api = {
  LANVEX_DATABASE_URL: dbUrl,
  LANVEX_REDIS_URL: "",
  LANVEX_NETBOX_URL: "http://127.0.0.1:18001",
  LANVEX_NETBOX_TOKEN: "nbt_e2e.token",
  LANVEX_BOOTSTRAP_ADMIN_USERNAME: "e2e-admin",
  LANVEX_BOOTSTRAP_ADMIN_PASSWORD: "e2e-admin-password",
  LANVEX_LOGIN_RATE_LIMIT: "1000",
  LANVEX_SECRET_KEY: "e2e-only-secret-key-not-for-production-use",
  // Configuration changes are only ever exercised against the lab containers (servers.lab / changes.lab specs).
  LANVEX_AUTOMATION_ENABLED: process.env.LANVEX_E2E_LAB === "1" ? "true" : "false",
  LANVEX_CHANGE_ROLLBACK_SECONDS: "60",
};

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:15173",
    trace: "retain-on-failure",
    launchOptions: process.env.LANVEX_E2E_CHROME ? { executablePath: process.env.LANVEX_E2E_CHROME, args: ["--no-sandbox"] } : {},
  },
  webServer: [
    {
      command: `cd ${backend} && uv run uvicorn tests.fake_netbox:app --host 127.0.0.1 --port 18001 --log-level warning`,
      url: "http://127.0.0.1:18001/api/status/",
      reuseExistingServer: false,
    },
    {
      command: `cd ${backend} && uv run python -m tests.snmp_lab --ready 18002`,
      url: "http://127.0.0.1:18002/",
      reuseExistingServer: false,
    },
    {
      command: `cd ${backend} && uv run lanvex migrate && uv run uvicorn lanvex.main:app --host 127.0.0.1 --port 18000 --log-level warning`,
      url: "http://127.0.0.1:18000/api/v1/health/live",
      env: api,
      reuseExistingServer: false,
    },
    {
      command: "npx vite --host 127.0.0.1 --port 15173 --strictPort",
      url: "http://127.0.0.1:15173",
      env: { LANVEX_API_URL: "http://127.0.0.1:18000" },
      reuseExistingServer: false,
    },
  ],
});
