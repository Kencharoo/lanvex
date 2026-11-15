import { expect, test, type Page } from "@playwright/test";

// Against the NetBox stand-in: Koblenz HQ (core, two access switches, a server) and Frankfurt DC,
// joined by a provider circuit and a planned cable.
const NETBOX = "http://127.0.0.1:18001";

async function signIn(page: Page) {
  await page.goto("/topology");
  await page.getByLabel("Username").fill("e2e-admin");
  await page.getByLabel("Password").fill("e2e-admin-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Topology" })).toBeVisible();
}

const device = (page: Page, name: string) => page.locator(".react-flow__node-device", { hasText: name });

const browserErrors: string[] = [];

test.beforeEach(async ({ request, page }) => {
  await request.post(`${NETBOX}/__reset__`);
  browserErrors.length = 0;
  page.on("pageerror", (e) => browserErrors.push(`page error: ${e.message}`));
  page.on("console", (m) => m.type() === "error" && browserErrors.push(`console: ${m.text()}`));
});

// When a map test fails, say what the page showed: CI logs are often all there is to go on.
test.afterEach(async ({ page }, info) => {
  if (info.status === info.expectedStatus) return;
  console.log(`--- page at ${page.url()} ---\n${await page.locator("main").innerText().catch(() => "(no main)")}`);
  console.log(`--- browser errors ---\n${browserErrors.join("\n") || "(none)"}`);
});

test("draws documented devices, sites and cables, with details on click", async ({ page }) => {
  await signIn(page);
  for (const name of ["kob-core-01", "kob-acc-01", "kob-acc-02", "kob-srv-01", "fra-core-01"]) {
    await expect(device(page, name)).toBeVisible();
  }
  await expect(page.locator(".react-flow__node-circuit", { hasText: "CID-4711" })).toBeVisible();
  await expect(page.locator(".react-flow__node-site", { hasText: "Koblenz HQ" })).toBeVisible();
  await expect(page.locator(".react-flow__edge")).toHaveCount(6);

  await device(page, "kob-core-01").click();
  const panel = page.getByRole("complementary", { name: "Details" });
  await expect(panel.getByRole("heading", { name: "kob-core-01" })).toBeVisible();
  await expect(panel.getByText("Cables (3)")).toBeVisible();
  await panel.getByRole("button", { name: "kob-acc-01" }).click(); // follow a cable to its other end
  await expect(panel.getByRole("heading", { name: "kob-acc-01" })).toBeVisible();
  await expect(page).toHaveURL(/node=device%3A\d+/);
  await expect(panel.getByRole("link", { name: "Open device" })).toHaveAttribute("href", /^\/devices\/\d+$/);
});

test("search, the cable list and a site of its own", async ({ page }) => {
  await signIn(page);
  await expect(device(page, "kob-core-01")).toBeVisible(); // map arranged; its "Arranging…" status is gone
  await page.getByLabel("Find on the map").fill("srv");
  await expect(page.getByRole("status").filter({ hasText: "found" })).toHaveText("1 found and highlighted.");

  await page.getByRole("button", { name: "Cables" }).click();
  await expect(page.getByRole("row")).toHaveCount(7); // header + 6 cables
  await expect(page.getByRole("row", { name: /kob-acc-02.*fra-core-01/ }).getByText("Planned")).toBeVisible();

  await page.getByRole("button", { name: "Map" }).click();
  await page.getByLabel("Site").selectOption({ label: "Frankfurt DC" });
  await expect(device(page, "fra-core-01")).toBeVisible();
  // The planned cable leads to a switch at Koblenz HQ: drawn, marked as another site.
  await expect(device(page, "kob-acc-02")).toContainText("Another site");
  await expect(device(page, "kob-core-01")).toHaveCount(0);
});
