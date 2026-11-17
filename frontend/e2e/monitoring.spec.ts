import { expect, test, type Page } from "@playwright/test";

async function signIn(page: Page, path: string) {
  await page.goto(path);
  await page.getByLabel("Username").fill("e2e-admin");
  await page.getByLabel("Password").fill("e2e-admin-password");
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("alerts and monitoring settings", async ({ page }) => {
  await signIn(page, "/alerts");
  await expect(page.getByRole("heading", { name: "Alerts" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /Open/ })).toHaveAttribute("aria-selected", "true");

  await page.getByRole("link", { name: "Monitoring settings" }).click();
  await expect(page.getByRole("heading", { name: "Monitoring" })).toBeVisible();
  const threshold = page.getByLabel("Pool usage in percent");
  await threshold.fill("85");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Monitoring saved")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Pool usage in percent")).toHaveValue("85");

  // A webhook: its URL is never shown again, only the host.
  await page.getByRole("button", { name: "Add webhook" }).click();
  const dialog = page.getByRole("dialog", { name: "Add webhook" });
  await dialog.getByLabel("Name").fill("On call");
  await dialog.getByLabel("Format").selectOption("ntfy");
  await dialog.getByLabel("URL").fill("https://ntfy.example.test/lanvex-secret-topic");
  await dialog.getByRole("button", { name: "Save" }).click();
  const row = page.getByRole("row", { name: /On call/ });
  await expect(row).toContainText("https://ntfy.example.test/…");
  await expect(page.getByText("lanvex-secret-topic")).toHaveCount(0);
  await row.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("row", { name: /On call/ })).toHaveCount(0);
});
