import { describe, expect, it } from "vitest";
import { formatV6, parseCidr, suggestGateway, usableHosts } from "./cidr";

function ok(text: string) {
  const r = parseCidr(text);
  if (!r.ok) throw new Error(r.error);
  return r.value;
}

describe("parseCidr", () => {
  it("parses IPv4", () => {
    const n = ok("10.20.0.0/24");
    expect(n.family).toBe(4);
    expect(n.size).toBe(256n);
    expect(usableHosts(n)).toBe(254n);
    expect(suggestGateway(n)).toBe("10.20.0.1/24");
  });

  it("flags host bits and suggests the network", () => {
    const r = parseCidr("10.20.0.7/24");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.suggestion).toBe("10.20.0.0/24");
  });

  it("parses and canonicalises IPv6", () => {
    const n = ok("2001:0db8:0000:0000::/48");
    expect(n.canonical).toBe("2001:db8::/48");
    expect(suggestGateway(n)).toBe("2001:db8::1/48");
    expect(usableHosts(ok("2001:db8::/127"))).toBe(2n);
  });

  it("handles IPv4 point-to-point and host routes", () => {
    expect(usableHosts(ok("10.0.0.0/31"))).toBe(2n);
    expect(usableHosts(ok("10.0.0.1/32"))).toBe(1n);
    expect(suggestGateway(ok("10.0.0.0/31"))).toBeNull();
  });

  it.each(["10.0.0.0", "10.0.0.0/33", "256.0.0.0/8", "10.0.0/24", "01.2.3.0/24", "2001:db8:::/48", "fe80::1::2/64", "/24"])(
    "rejects %s",
    (input) => {
      expect(parseCidr(input).ok).toBe(false);
    },
  );

  it("formats IPv6 per RFC 5952", () => {
    expect(formatV6(0n)).toBe("::");
    expect(formatV6(1n)).toBe("::1");
    expect(ok("2001:db8:0:1:0:0:0:0/64").canonical).toBe("2001:db8:0:1::/64");
  });
});
