import { expect, test } from "@playwright/test";

/**
 * Single sign-on through the browser, against the OpenID Connect TEST DOUBLE (backend/tests/oidc_idp.py),
 * which lives on another site (localhost) than Lanvex (127.0.0.1): the way back from it is a cross-site
 * navigation, as with a real provider, and the session must still hold afterwards.
 */
test("signing in with single sign-on", async ({ page }) => {
  await page.goto("/servers");
  await page.getByRole("link", { name: "School account" }).click();
  await expect(page).toHaveURL(/127\.0\.0\.1:15173\/servers$/);
  await expect(page.getByRole("heading", { name: "Servers", exact: true })).toBeVisible();

  await page.goto("/settings");
  await expect(page.getByText("Signed in as sso-erik, administrator, with single sign-on.", { exact: false })).toBeVisible();
  const row = page.getByRole("row", { name: /Erik Example/ });
  await expect(row.getByText("SSO", { exact: true })).toBeVisible();
  await expect(row.getByRole("combobox", { name: "Role for sso-erik" })).toBeDisabled();
  await expect(page.getByText("http://127.0.0.1:15173/api/v1/auth/sso/callback")).toBeVisible(); // to register with the provider
  await page.getByRole("heading", { name: "Sign-in" }).scrollIntoViewIfNeeded();
});
