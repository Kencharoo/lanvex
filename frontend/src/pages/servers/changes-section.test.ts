import { describe, expect, it } from "vitest";
import { isProtected } from "./changes-section";

describe("isProtected", () => {
  it("matches names and shell-style patterns as the server does", () => {
    expect(isProtected("vlan-bw", ["vlan-*"])).toBe(true);
    expect(isProtected("eno1", ["vlan-*", "eno1"])).toBe(true);
    expect(isProtected("eno10", ["eno?"])).toBe(false);
    expect(isProtected("br0", ["br[01]"])).toBe(true);
    expect(isProtected("eth0.10", ["eth0"])).toBe(false);
    expect(isProtected("eth0", [])).toBe(false);
  });
});
