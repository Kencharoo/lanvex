import { expect, test, type Page } from "@playwright/test";

/**
 * Lanvex with Lanvex Core, end to end: one sign-in for both, roles carried over, links both ways,
 * and signing out of Lanvex Core signs out of Lanvex. Needs a running Lanvex that serves Lanvex Core
 * (docs/development.md, "Lanvex Core"), so it is opt-in:
 *
 *   LANVEX_E2E_CORE_URL=http://127.0.0.1:18300 LANVEX_E2E_CORE_ADMIN=admin \
 *   LANVEX_E2E_CORE_PASSWORD=... npx playwright test core.live
 */
const BASE = process.env.LANVEX_E2E_CORE_URL ?? "";
const ADMIN = process.env.LANVEX_E2E_CORE_ADMIN ?? "admin";
const PASSWORD = process.env.LANVEX_E2E_CORE_PASSWORD ?? "";
test.skip(!BASE || !PASSWORD, "set LANVEX_E2E_CORE_URL and LANVEX_E2E_CORE_PASSWORD");

const NET = `10.${100 + Math.floor(Math.random() * 100)}.${Math.floor(Math.random() * 250)}.0/24`;

async function signIn(page: Page, username: string, password: string) {
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("one sign-in for Lanvex and Lanvex Core, with roles and links carried across", async ({ page, request }) => {
  // Lanvex Core while signed out: Lanvex's sign-in, then straight on to where you were going.
  await page.goto(`${BASE}/core/ipam/prefixes/`);
  await expect(page).toHaveURL(`${BASE}/login?next=%2Fcore%2Fipam%2Fprefixes%2F`);
  await signIn(page, ADMIN, PASSWORD);
  await expect(page).toHaveURL(`${BASE}/core/ipam/prefixes/`);
  await expect(page).toHaveTitle("Prefixes | Lanvex Core");
  await expect(page.locator("html")).toHaveAttribute("data-netbox-user-name", ADMIN);

  // Lanvex Core's own forms work through Lanvex (CSRF, form posts, redirects).
  await page.goto(`${BASE}/core/ipam/prefixes/add/`);
  await page.locator("#id_prefix").fill(NET);
  await page.locator("#id_description").fill("Created in Lanvex Core");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page).toHaveURL(/\/core\/ipam\/prefixes\/\d+\/$/);
  const coreUrl = new URL(page.url()).pathname;

  // Back in Lanvex, the network is there and links lead into Lanvex Core.
  await page.getByRole("link", { name: "Back to Lanvex" }).first().click();
  await expect(page).toHaveURL(`${BASE}/`);
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Lanvex Core" })).toHaveAttribute("href", "/core/");
  await page.goto(`${BASE}/networks`);
  await page.getByText(NET).first().click();
  await expect(page.getByRole("link", { name: "Open in Lanvex Core" })).toHaveAttribute("href", coreUrl);
  await expect(page.getByText("Lanvex Core · NetBox")).toBeVisible();

  // A read-only Lanvex user may look but not change.
  const reader = `reader-${Date.now().toString(36)}`;
  const csrf = await page.evaluate(async () => (await (await fetch("/api/v1/auth/session")).json()).csrf_token);
  const created = await page.evaluate(
    async ([csrf, reader]) =>
      (
        await fetch("/api/v1/users", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
          body: JSON.stringify({ username: reader, password: "reader-password-123", role: "readonly", display_name: "Jens Müller" }),
        })
      ).status,
    [csrf, reader],
  );
  expect(created).toBe(201);

  // Logging out in Lanvex Core signs out of Lanvex.
  await page.goto(`${BASE}/core/`);
  await page.goto(`${BASE}/core/logout/`, { referer: `${BASE}/core/` });
  await expect(page).toHaveURL(`${BASE}/`);
  await expect(page.getByLabel("Username")).toBeVisible();
  const after = await request.get(`${BASE}/core/`, { headers: { Accept: "text/html" }, maxRedirects: 0 });
  expect(after.status()).toBe(303);

  await signIn(page, reader, "reader-password-123");
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await page.goto(`${BASE}/core/ipam/prefixes/`);
  await expect(page.getByText(NET)).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-netbox-user-name", reader);
  const add = await page.goto(`${BASE}/core/ipam/prefixes/add/`);
  expect(add?.status()).toBe(403);
});
