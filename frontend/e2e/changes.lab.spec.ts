import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";

/**
 * A configuration change through the UI, end to end, against the REAL netplan lab host from deploy/lab:
 * helper installation, enabling, preparing, approving, applying, verifying.
 * Opt-in: LANVEX_E2E_LAB=1 with the lab running. Changes only the lab container's br0 and restores it.
 */
test.skip(process.env.LANVEX_E2E_LAB !== "1", "set LANVEX_E2E_LAB=1 with the lab running");

const HOST = "lab-netplan";
const HELPER = "/usr/local/libexec/lanvex/lanvex-netapply";
const sh = (script: string) => execFileSync("docker", ["exec", HOST, "sh", "-c", script], { encoding: "utf8" });
const fingerprint = () => sh("ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub").split(" ")[1];
const br0 = () => sh("ip -4 -o address show dev br0").split("\n").filter(Boolean).map((l) => l.split(/\s+/)[3]).sort();

function resetLab() {
  sh("systemctl stop 'lanvex-rollback-*.timer' 2>/dev/null; rm -rf /var/lib/lanvex/changes; true");
  execFileSync("docker", ["exec", "-i", HOST, "sh", "-c", "cat > /etc/netplan/50-lab.yaml && chmod 600 /etc/netplan/50-lab.yaml && netplan apply"], {
    input: readFileSync(new URL("../../deploy/lab/netplan-networkd/50-lab.yaml", import.meta.url)),
  });
}

async function addServer(page: Page, name: string) {
  await page.getByRole("button", { name: "Add server" }).first().click();
  await page.locator("#sv-name").fill(name);
  await page.locator("#sv-address").fill("172.30.0.11");
  await page.getByRole("button", { name: "Fetch host key" }).click();
  await expect(page.getByText(fingerprint(), { exact: true })).toBeVisible();
  await page.getByLabel("I compared the fingerprints and they match").check();
  await page.getByRole("button", { name: "Trust this key" }).click();
  await page.getByRole("button", { name: "Add server", exact: true }).last().click();
  const line = (await page.locator("code").first().innerText()).trim();
  sh(`printf '%s\\n' '${line}' > /home/lanvex/.ssh/authorized_keys && chown lanvex: /home/lanvex/.ssh/authorized_keys && chmod 600 /home/lanvex/.ssh/authorized_keys`);
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByText(`Signed in as lanvex on ${HOST}.`)).toBeVisible();
  await page.getByRole("button", { name: "Read network state" }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
}

test.beforeAll(() => {
  resetLab();
  // Start without the helper, as on a fresh server.
  sh(`rm -f ${HELPER} /etc/sudoers.d/lanvex /home/lanvex/lanvex-netapply`);
});
test.afterAll(() => resetLab());

test("install the helper, prepare, approve and apply an address change, verified on the server", async ({ page }) => {
  test.setTimeout(180_000);
  await page.goto("/servers");
  await page.getByLabel("Username").fill("e2e-admin");
  await page.getByLabel("Password").fill("e2e-admin-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  const name = `e2e-chg-${Date.now().toString(36)}`;
  await addServer(page, name);

  // Readiness: the helper is not there yet, so nothing can be prepared.
  const section = page.getByRole("region", { name: "Configuration changes" });
  await section.getByRole("button", { name: "Check again" }).click();
  await expect(section.getByText("Change helper: Not allowed through sudo")).toBeVisible();
  await expect(page.getByRole("button", { name: "Change IP settings" })).toHaveCount(0);

  // Install it the way the dialog explains: copy, then run the commands on the server as an administrator.
  await section.getByRole("button", { name: "Install the helper" }).click();
  const dialog = page.getByRole("dialog", { name: "Install the change helper" });
  await dialog.getByRole("button", { name: "Copy to the server" }).click();
  await expect(dialog.getByText("Copied to /home/lanvex/lanvex-netapply")).toBeVisible();
  const commands = (await dialog.locator("pre, code").first().innerText()).trim();
  expect(commands).toContain(`sha256sum -c -`);
  expect(sh(commands)).toContain("lanvex-netapply: OK");
  await dialog.getByRole("button", { name: "Check now" }).click();
  await expect(page.getByText("The helper is installed and allowed.")).toBeVisible();
  await expect(section.getByText("Change helper: Installed")).toBeVisible();

  await section.getByRole("button", { name: "Turn on" }).click();
  await page.getByRole("button", { name: "Change IP settings" }).click();

  const form = page.getByRole("dialog", { name: `Change IP settings on ${name}` });
  await form.getByLabel("Interface", { exact: true }).selectOption("br0");
  await expect(form.getByLabel("Address 1", { exact: true })).toHaveValue("10.11.0.1/24");
  await form.getByRole("button", { name: "Add address" }).click();
  await form.getByLabel("Address 2", { exact: true }).fill("10.11.0.2/24");
  await form.getByRole("button", { name: "Prepare change" }).click();

  // The preview: generated by the helper on the server, nothing changed yet.
  await expect(page.getByRole("heading", { name: new RegExp(`Change #\\d+: br0 on ${name}`) })).toBeVisible();
  await expect(page.getByText("Waiting for approval")).toBeVisible();
  await expect(page.getByText("+      addresses: [10.11.0.1/24, 10.11.0.2/24]")).toBeVisible();
  await expect(page.getByRole("row", { name: /DNS servers.*Unchanged/ })).toBeVisible();
  expect(br0()).toEqual(["10.11.0.1/24"]);

  await page.getByRole("button", { name: "Approve and apply" }).click();
  const approve = page.getByRole("dialog", { name: /Apply change #\d+/ });
  await expect(approve.getByRole("button", { name: "Approve and apply" })).toBeDisabled();
  await approve.getByLabel(`Type ${name} to approve`).fill(name);
  await approve.getByRole("button", { name: "Approve and apply" }).click();

  await expect(page.getByText("Applied and verified").first()).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("Applied, verified and confirmed. The new configuration is permanent.").first()).toBeVisible();
  expect(br0()).toEqual(["10.11.0.1/24", "10.11.0.2/24"]);
  expect(sh("systemctl list-timers --all --no-legend 'lanvex-rollback-*' | wc -l").trim()).toBe("0");
  await expect(page.getByText("NetBox was not updated")).toBeVisible(); // the server is not linked yet
  const changeUrl = page.url();

  // It is listed under Automation, and the server page shows the new address from Lanvex's fresh read.
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Automation" }).click();
  await expect(page.getByRole("row", { name: new RegExp(`${name}.*br0.*Applied and verified`) })).toBeVisible();
  await page.getByRole("row", { name: new RegExp(name) }).click();
  await page.getByRole("link", { name: "Open server" }).click();
  await expect(page.getByRole("row", { name: /br0.*10\.11\.0\.2\/24/ })).toBeVisible();

  // Documentation: link the server to its NetBox device, which documents none of its addresses yet.
  const cmp = page.getByRole("region", { name: /vs\. on the server/ });
  await cmp.getByLabel("Find in NetBox").fill("kob-srv");
  await cmp.getByLabel("Object").selectOption({ label: "kob-srv-01 (device)" });
  await cmp.getByRole("button", { name: "Link", exact: true }).click();
  await expect(cmp.getByText("3 of 3 addresses differ.")).toBeVisible();

  // A server change prepared from a difference starts from the server's settings with that one difference undone.
  await cmp.getByRole("button", { name: "Fix 10.11.0.2" }).click();
  await page.getByRole("menuitem", { name: "Change the server to match…" }).click();
  const prepared = page.getByRole("dialog", { name: `Change IP settings on ${name}` });
  await expect(prepared.getByText("Removes 10.11.0.2/24, which NetBox does not document for this server.")).toBeVisible();
  await expect(prepared.getByLabel("Interface", { exact: true })).toHaveValue("br0");
  await expect(prepared.getByLabel("Address 1", { exact: true })).toHaveValue("10.11.0.1/24");
  await expect(prepared.getByLabel("Address 2", { exact: true })).toHaveCount(0);
  await prepared.getByRole("button", { name: "Cancel" }).click();

  // Or record what the server has: a preview first, then exactly those updates.
  await cmp.getByRole("button", { name: "Record them in NetBox…" }).click();
  const rec = page.getByRole("dialog", { name: "Record in NetBox" });
  await expect(rec.getByText("Add interface br0 to kob-srv-01 (type Bridge)")).toBeVisible();
  await expect(rec.getByText("Record 10.11.0.2/24 on kob-srv-01 / br0")).toBeVisible();
  await expect(rec.getByText("Record 172.30.0.11/24 on kob-srv-01 / eth0")).toBeVisible();
  await rec.getByRole("button", { name: "Make 5 updates" }).click();
  await expect(page.getByText("Recorded in NetBox")).toBeVisible();
  await expect(cmp.getByText("All 3 addresses match.")).toBeVisible();
  expect(br0()).toEqual(["10.11.0.1/24", "10.11.0.2/24"]); // documentation only: the server is unchanged
  await page.goto(changeUrl);
  await expect(page.getByText("NetBox matches the server")).toBeVisible();
  await page.getByRole("link", { name: "Open server" }).click();

  await page.getByRole("button", { name: "Server actions" }).click();
  await page.getByRole("menuitem", { name: "Remove server" }).click();
  await page.getByLabel(`Type ${name} to confirm`).fill(name);
  await page.getByRole("button", { name: "Remove server" }).click();
  await expect(page.getByRole("heading", { name: "Servers" })).toBeVisible();
});
