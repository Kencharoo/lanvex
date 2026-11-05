import { expect, test, type Page } from "@playwright/test";

const NETBOX = "http://127.0.0.1:18001";

async function signIn(page: Page) {
  await page.goto("/networks");
  await page.getByLabel("Username").fill("e2e-admin");
  await page.getByLabel("Password").fill("e2e-admin-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Networks" })).toBeVisible();
}

test.beforeEach(async ({ request }) => {
  await request.post(`${NETBOX}/__reset__`);
});

test("rejects a wrong password without revealing which part was wrong", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Username").fill("e2e-admin");
  await page.getByLabel("Password").fill("not-the-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("Incorrect username or password.")).toBeVisible();
});

test("lists prefixes from NetBox with hierarchy and utilization", async ({ page }) => {
  await signIn(page);
  const row = page.getByRole("row", { name: /10\.10\.10\.0\/24/ });
  await expect(row).toContainText("Staff LAN");
  await expect(row).toContainText("52%");
});

test("creates a prefix through the wizard and persists it in NetBox", async ({ page, request }) => {
  await signIn(page);
  await page.getByRole("button", { name: "New network" }).first().click();
  await page.getByLabel("Name", { exact: true }).fill("E2E lab");
  await page.getByLabel("Prefix", { exact: true }).fill("10.10.40.9/24");
  await expect(page.getByText(/Host bits are set/)).toBeVisible();
  await page.getByRole("button", { name: "Use 10.10.40.0/24" }).click();
  await expect(page.getByText("Will be nested under 10.10.0.0/16 (Koblenz campus).")).toBeVisible();
  await page.getByRole("button", { name: "Continue" }).click();
  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page.getByLabel("Gateway", { exact: true })).toHaveValue("10.10.40.1/24");
  await page.getByLabel("Gateway", { exact: true }).fill("10.10.40.255/24");
  await expect(page.getByText(/broadcast address/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Continue" })).toBeDisabled();
  await page.getByLabel("Gateway", { exact: true }).fill("10.10.40.1/24");
  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page.getByText("It does not configure routers, switches, firewalls, or DHCP servers.", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Create network" }).click();
  await expect(page.getByText("Created 10.10.40.0/24")).toBeVisible();
  await expect(page.getByText("Recorded in NetBox. No device configuration was changed.")).toBeVisible();

  // Verify on the NetBox side, not through Lanvex.
  const headers = { Authorization: "Token e2e" };
  const prefixes = await (await request.get(`${NETBOX}/api/ipam/prefixes/?prefix=10.10.40.0/24`, { headers })).json();
  expect(prefixes.count).toBe(1);
  expect(prefixes.results[0].description).toBe("E2E lab");
  const gw = await (await request.get(`${NETBOX}/api/ipam/ip-addresses/?address=10.10.40.1/24`, { headers })).json();
  expect(gw.results[0].tags.map((t: { slug: string }) => t.slug)).toContain("lanvex-gateway");

  // The new prefix opens in the detail panel; Escape closes it.
  await expect(page.getByRole("dialog", { name: "10.10.40.0/24" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);

  // The action is in the audit log.
  await page.getByRole("link", { name: "Activity & Audit Logs" }).click();
  await expect(page.getByRole("row", { name: /Created prefix.*10\.10\.40\.0\/24/ }).first()).toBeVisible();
});

test("blocks a duplicate prefix before anything is written", async ({ page }) => {
  await signIn(page);
  await page.getByRole("button", { name: "New network" }).first().click();
  await page.getByLabel("Prefix", { exact: true }).fill("10.10.10.0/24");
  await expect(page.getByText("10.10.10.0/24 already exists in the global table.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Continue" })).toBeDisabled();
});

test("delete requires typing the prefix", async ({ page }) => {
  await signIn(page);
  await page.getByRole("row", { name: /10\.10\.128\.0\/20/ }).click();
  await page.getByRole("button", { name: "Prefix actions" }).click();
  await page.getByRole("menuitem", { name: "Delete prefix" }).click();
  const confirm = page.getByRole("button", { name: "Delete prefix" });
  await expect(confirm).toBeDisabled();
  await page.getByLabel("Type 10.10.128.0/20 to confirm").fill("10.10.128.0/20");
  await confirm.click();
  await expect(page.getByText("Deleted 10.10.128.0/20")).toBeVisible();
  await expect(page.getByRole("row", { name: /10\.10\.128\.0\/20/ })).toHaveCount(0);
});
