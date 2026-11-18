import { expect, test, type Page } from "@playwright/test";

/**
 * Changing a switch port's VLANs through the UI: allowing changes, preparing, approving, writing over SNMP,
 * reading back, and changing it back. The switch is the SNMP TEST DOUBLE (backend/tests/snmp_lab.py), which
 * takes SET requests into its tables. Opt-in with the other change specs (LANVEX_E2E_LAB=1), since
 * changes are only enabled then.
 */
test.skip(process.env.LANVEX_E2E_LAB !== "1", "set LANVEX_E2E_LAB=1");

async function signIn(page: Page, path: string) {
  await page.goto(path);
  await page.getByLabel("Username").fill("e2e-admin");
  await page.getByLabel("Password").fill("e2e-admin-password");
  await page.getByRole("button", { name: "Sign in" }).click();
}

async function changeVlans(page: Page, untagged: string, tagged: Record<string, boolean>) {
  await page.getByRole("button", { name: "Change VLANs of Gi1/0/2" }).click();
  const dialog = page.getByRole("dialog", { name: "VLANs of Gi1/0/2" });
  await dialog.getByLabel("Untagged VLAN (PVID)").selectOption(untagged);
  for (const [label, on] of Object.entries(tagged)) await dialog.getByLabel(label).setChecked(on);
  await dialog.getByRole("button", { name: "Preview change" }).click();
}

async function approve(page: Page) {
  await page.getByRole("button", { name: "Approve and apply" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Type Gi1/0/2 to approve").fill("Gi1/0/2");
  await dialog.getByRole("button", { name: "Approve and apply" }).click();
}

test("changing a switch port's VLANs as an approved change", async ({ page }) => {
  const name = `sw-vlans-${Date.now() % 100000}`;
  await signIn(page, "/switches");
  await page.getByRole("button", { name: "Add switch" }).first().click();
  const add = page.getByRole("dialog", { name: "Add switch" });
  await add.getByLabel("Name").fill(name);
  await add.getByLabel("Address").fill("127.0.0.1");
  await add.getByLabel("Port", { exact: true }).fill("16101");
  await add.getByLabel("User").fill("lanvex");
  await add.getByLabel("Authentication password").fill("auth-pass-123");
  await add.getByLabel("Privacy password").fill("priv-pass-456");
  await add.getByRole("button", { name: "Add and read" }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
  await expect(page.getByRole("button", { name: "Change VLANs of Gi1/0/2" })).toHaveCount(0); // not allowed yet

  await page.getByRole("button", { name: "Switch actions" }).click();
  await page.getByRole("menuitem", { name: "Edit" }).click();
  const edit = page.getByRole("dialog", { name: `Edit ${name}` });
  await edit.getByLabel("Allow VLAN changes from Lanvex").check();
  await edit.getByRole("button", { name: "Save" }).click();
  await expect(edit).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Change VLANs of Po1" })).toHaveCount(0); // a LAG
  await expect(page.getByRole("button", { name: "Change VLANs of Gi1/0/7" })).toHaveCount(0); // in Po1

  await changeVlans(page, "20", { "30 · lab": true });
  await expect(page.getByRole("heading", { name: /Change #\d+: Gi1\/0\/2 on/ })).toBeVisible();
  await expect(page.getByText("1 device on Gi1/0/2 loses its connection until it has an address in VLAN 20.")).toBeVisible();
  await expect(page.getByText("VLAN 20: Gi1/0/2 untagged")).toBeVisible();
  await expect(page.getByText("Gi1/0/2: PVID 10 → 20")).toBeVisible();
  await approve(page);
  await expect(page.getByText("Gi1/0/2 is untagged 20, tagged 30, read back from the switch.").first()).toBeVisible({ timeout: 15_000 });

  await page.getByRole("link", { name: "Open switch" }).click();
  await expect(page.getByRole("row", { name: /^Gi1\/0\/2/ }).getByRole("cell").nth(2)).toHaveText("20 + 30");
  await changeVlans(page, "10", { "30 · lab": false });
  await approve(page);
  await expect(page.getByText("Gi1/0/2 is untagged 10, read back from the switch.").first()).toBeVisible({ timeout: 15_000 });

  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Automation" }).click();
  await expect(page.getByRole("row", { name: new RegExp(`${name} · Gi1/0/2`) })).toHaveCount(2);

  await page.goto("/switches");
  await page.getByRole("row", { name: new RegExp(name) }).click();
  await page.getByRole("button", { name: "Switch actions" }).click();
  await page.getByRole("menuitem", { name: "Remove switch" }).click();
  const confirm = page.getByRole("alertdialog");
  await confirm.getByLabel(`Type ${name} to confirm`).fill(name);
  await confirm.getByRole("button", { name: "Remove switch" }).click();
  await expect(page.getByRole("row", { name: new RegExp(name) })).toHaveCount(0);
});
