import { execFileSync } from "node:child_process";
import { expect, test } from "@playwright/test";

/**
 * Server management through the UI against a REAL SSH server from deploy/lab (lab-ifup).
 * Opt-in: LANVEX_E2E_LAB=1 with the lab running (deploy/lab/up.sh). NetBox is the stand-in.
 */
test.skip(process.env.LANVEX_E2E_LAB !== "1", "set LANVEX_E2E_LAB=1 with the lab running");

const HOST = "lab-ifup";
const sh = (script: string) => execFileSync("docker", ["exec", HOST, "sh", "-c", script], { encoding: "utf8" });
const fingerprint = () => sh("ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub").split(" ")[1];

test("add a server, verify its host key, read it, and refuse a changed key", async ({ page }) => {
  test.setTimeout(90_000);
  await page.goto("/servers");
  await page.getByLabel("Username").fill("e2e-admin");
  await page.getByLabel("Password").fill("e2e-admin-password");
  await page.getByRole("button", { name: "Sign in" }).click();

  const name = `e2e-${Date.now().toString(36)}`;
  await page.getByRole("button", { name: "Add server" }).first().click();
  await page.locator("#sv-name").fill(name);
  await page.locator("#sv-address").fill("172.30.0.13");
  await page.getByRole("button", { name: "Fetch host key" }).click();
  await expect(page.getByText(fingerprint(), { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Trust this key" })).toBeDisabled();
  await page.getByLabel("I compared the fingerprints and they match").check();
  await page.getByRole("button", { name: "Trust this key" }).click();
  await page.getByRole("button", { name: "Add server", exact: true }).last().click();

  const line = (await page.locator("code").first().innerText()).trim();
  expect(line).toMatch(/^restrict ssh-ed25519 /);
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByText("The server rejected the credentials for user 'lanvex'.")).toBeVisible();
  sh(`printf '%s\\n' '${line}' > /home/lanvex/.ssh/authorized_keys && chown lanvex: /home/lanvex/.ssh/authorized_keys && chmod 600 /home/lanvex/.ssh/authorized_keys`);
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByText(`Signed in as lanvex on ${HOST}.`)).toBeVisible();
  await page.getByRole("button", { name: "Read network state" }).click();

  await expect(page.getByRole("heading", { name: name })).toBeVisible();
  await expect(page.getByText("ifupdown (/etc/network/interfaces)", { exact: true })).toBeVisible();
  // Networks only on this stack: the changes section says so and offers no interface change.
  await expect(
    page.getByText("ifupdown (/etc/network/interfaces) (VLAN networks only)"),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Change IP settings" })).toHaveCount(0);
  await expect(page.getByRole("row", { name: /eth0.*172\.30\.0\.13\/24.*ifupdown/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /172\.30\.0\.13.*Not in NetBox/ })).toBeVisible();

  sh("rm -f /etc/ssh/ssh_host_* && ssh-keygen -A >/dev/null && systemctl restart ssh.service 2>/dev/null; true");
  await page.getByRole("button", { name: "Read now" }).click();
  await expect(page.getByText("The server's host key changed")).toBeVisible();
  await page.getByRole("button", { name: "Verify new key" }).click();
  await expect(page.getByText(fingerprint(), { exact: true })).toBeVisible();
  await page.getByLabel("I compared the fingerprints and they match").check();
  await page.getByRole("button", { name: "Trust new key" }).click();
  sh("ping -c1 -W1 172.30.0.12 >/dev/null; true"); // traffic to lab-nm, so the neighbour table lists it
  await page.getByRole("button", { name: "Read now" }).click();
  await expect(page.getByText("Read the server's network state")).toBeVisible();

  // History: the first state, the refused read, and the same state again once the key was trusted.
  const history = page.locator("section", { has: page.getByRole("heading", { name: "History" }) }).last();
  await expect(history.getByText("Readable again; nothing changed since the last successful read.")).toBeVisible();
  await expect(history.getByText("Host key changed", { exact: true })).toBeVisible();
  await expect(history.getByText("The oldest state kept.")).toBeVisible();
  await expect(history.getByText("read by e2e-admin")).toHaveCount(3);

  // Discovery: lab-ifup's lldpd reports lab-nm on eth0, and its neighbour table the other lab hosts.
  await expect(page.getByText("↔ lab-nm / eth0")).toBeVisible();
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Discovery" }).click();
  await expect(page.getByRole("row", { name: new RegExp(`${name} / eth0.*lab-nm / eth0.*Server not linked`) })).toBeVisible();
  await expect(page.getByRole("row", { name: new RegExp(`172\\.30\\.0\\.12.*${name} on eth0.*Not recorded`) })).toBeVisible();
  await page.getByRole("row", { name: new RegExp(`${name} / eth0`) }).getByRole("link", { name }).click();

  await page.getByRole("button", { name: "Server actions" }).click();
  await page.getByRole("menuitem", { name: "Remove server" }).click();
  await page.getByLabel(`Type ${name} to confirm`).fill(name);
  await page.getByRole("button", { name: "Remove server" }).click();
  await expect(page.getByRole("heading", { name: "Servers" })).toBeVisible();
});
