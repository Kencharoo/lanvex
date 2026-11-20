import { describe, expect, it } from "vitest";
import { manualUrl } from "./layout";

describe("manualUrl", () => {
  it("links the manual of the running release, in the browser's language", () => {
    expect(manualUrl("1.4.0", "de-DE")).toBe("https://github.com/Kencharoo/lanvex/blob/v1.4.0/docs/manual/de.md");
    expect(manualUrl("1.4.0", "en-GB")).toBe("https://github.com/Kencharoo/lanvex/blob/v1.4.0/docs/manual/en.md");
    expect(manualUrl("1.4.0.dev0", "fr")).toBe("https://github.com/Kencharoo/lanvex/blob/main/docs/manual/en.md");
    expect(manualUrl(undefined, "de")).toBe("https://github.com/Kencharoo/lanvex/blob/main/docs/manual/de.md");
  });
});
