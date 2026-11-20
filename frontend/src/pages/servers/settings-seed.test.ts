import { describe, expect, it } from "vitest";
import type { HostFacts, SettingRow } from "@/lib/types";
import { settingSeedFor } from "./record";

const facts = {
  interfaces: [
    { name: "eth0", kind: "ethernet", master: null, addresses: [{ address: "172.30.0.11/24", family: 4, scope: "global", dynamic: false }] },
    { name: "eth1", kind: "ethernet", master: null, addresses: [{ address: "198.51.100.5/24", family: 4, scope: "global", dynamic: true }] },
  ],
  routes: [
    { destination: "default", family: 4, gateway: "172.30.0.1", interface: "eth0", protocol: "static", metric: null },
    { destination: "10.50.0.0/16", family: 4, gateway: "172.30.0.254", interface: "eth0", protocol: "static", metric: 100 },
  ],
  dns: { resolved: [{ scope: "eth0", servers: ["127.0.0.11"], domains: ["lab.example.net"] }] },
} as unknown as HostFacts;

const row = (over: Partial<SettingRow>): SettingRow => ({
  id: "x",
  kind: "route",
  status: "differs",
  key: "10.50.0.0/16",
  interface: "eth0",
  server: null,
  documented: null,
  prefix: null,
  prefix_id: null,
  ...over,
});

describe("settingSeedFor", () => {
  it("sets the documented gateway", () => {
    const seed = settingSeedFor(row({ kind: "gateway", key: "IPv4", server: "172.30.0.1", documented: "172.30.0.2", prefix: "172.30.0.0/24" }), facts, "NetBox", false);
    expect(seed).toMatchObject({ iface: "eth0", gateway: "172.30.0.2" });
    expect(settingSeedFor(row({ kind: "gateway", key: "IPv4", interface: "eth1", documented: "198.51.100.1" }), facts, "NetBox", false)).toBe("eth1 gets its address and gateway from DHCP.");
  });

  it("sets the documented DNS servers and keeps the search domains", () => {
    const seed = settingSeedFor(row({ kind: "dns_servers", key: "dns_servers", interface: null, server: "127.0.0.11", documented: "10.0.0.53" }), facts, "NetBox", false);
    expect(seed).toMatchObject({ iface: "eth0", dns: { servers: "10.0.0.53", search: "lab.example.net" } });
  });

  it("changes, adds and removes routes as documented", () => {
    expect(settingSeedFor(row({ server: "172.30.0.254", documented: "172.30.0.253" }), facts, "NetBox", true)).toMatchObject({
      routes: [{ to: "10.50.0.0/16", via: "172.30.0.253", metric: "100" }],
    });
    const add = settingSeedFor(row({ key: "192.0.2.0/24", status: "only_documented", interface: null, documented: "172.30.0.254" }), facts, "NetBox", true);
    expect(add).toMatchObject({ iface: "eth0", routes: [{ to: "10.50.0.0/16" }, { to: "192.0.2.0/24", via: "172.30.0.254" }] });
    expect(settingSeedFor(row({ status: "only_on_server", server: "172.30.0.254" }), facts, "NetBox", true)).toMatchObject({ routes: [] });
    expect(settingSeedFor(row({ status: "only_on_server", server: "172.30.0.254" }), facts, "NetBox", false)).toBe("NetBox documents no routes for this server.");
  });
});
