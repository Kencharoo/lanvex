import { expect, test, type Page } from "@playwright/test";

// The switches are SNMP agents on localhost (backend/tests/snmp_lab.py, started with the other servers).
async function signIn(page: Page, path: string) {
  await page.goto(path);
  await page.getByLabel("Username").fill("e2e-admin");
  await page.getByLabel("Password").fill("e2e-admin-password");
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("adding a switch over SNMPv3 and seeing what is on its ports", async ({ page }) => {
  const name = `sw-access-${Date.now() % 100000}`;
  await signIn(page, "/switches");
  await expect(page.getByRole("heading", { name: "Switches" })).toBeVisible();
  await page.getByRole("button", { name: "Add switch" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Add switch" });
  await dialog.getByLabel("Address").fill("127.0.0.1");
  await dialog.getByLabel("Port", { exact: true }).fill("16101");
  await dialog.getByLabel("User").fill("lanvex");
  await dialog.getByLabel("Authentication password").fill("wrong-password");
  await dialog.getByLabel("Privacy password").fill("priv-pass-456");
  await dialog.getByRole("button", { name: "Test connection" }).click();
  await expect(dialog.getByRole("status")).toContainText("authentication password");
  await dialog.getByLabel("Authentication password").fill("auth-pass-123");
  await dialog.getByRole("button", { name: "Test connection" }).click();
  await expect(dialog.getByRole("status")).toContainText("Connected to sw-access.");
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByRole("button", { name: "Add and read" }).click();

  await expect(page.getByRole("heading", { name })).toBeVisible();
  await expect(page.getByText("SNMPv3 as lanvex (SHA-1, AES-128)")).toBeVisible();
  const gi1 = page.getByRole("row", { name: /Gi1\/0\/1 desk 1/ });
  await expect(gi1).toContainText("1 Gbit/s");
  await expect(gi1).toContainText("02:00:00:00:00:0a");
  await expect(gi1).toContainText("Switch not linked");
  await expect(page.getByRole("row", { name: /Gi1\/0\/3/ })).toContainText("web-01");
  await expect(page.getByRole("row", { name: /^Po1/ })).toContainText("sw-core");
  await expect(page.getByText("auth-pass-123")).toHaveCount(0);

  await page.getByRole("link", { name: "Discovery" }).click();
  const ports = page.getByRole("region", { name: "Switch ports" });
  await ports.getByLabel(/Only those that differ/).uncheck();
  await expect(ports.getByRole("row", { name: new RegExp(`${name} / Gi1/0/5`) })).toContainText("02:00:00:00:00:0e");

  await page.getByRole("link", { name: "Switches" }).click();
  await expect(page.getByRole("heading", { name: "Switches" })).toBeVisible();
  await expect(page.getByRole("row", { name: new RegExp(name) })).toContainText("7 / 8");
  await page.getByRole("row", { name: new RegExp(name) }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
  await page.getByRole("button", { name: "Switch actions" }).click();
  await page.getByRole("menuitem", { name: "Remove switch" }).click();
  const confirm = page.getByRole("alertdialog");
  await confirm.getByLabel(`Type ${name} to confirm`).fill(name);
  await confirm.getByRole("button", { name: "Remove switch" }).click();
  await expect(page.getByRole("heading", { name: "Switches" })).toBeVisible();
  await expect(page.getByRole("row", { name: new RegExp(name) })).toHaveCount(0);
});
