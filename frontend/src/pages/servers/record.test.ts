import { describe, expect, it } from "vitest";
import type { ComparisonRow, HostFacts, HostInterface } from "@/lib/types";
import { changeSeedFor } from "./record";

const iface = (name: string, addresses: [string, boolean?][], extra: Partial<HostInterface> = {}): HostInterface => ({
  name,
  index: 1,
  kind: "ethernet",
  mac: null,
  mtu: 1500,
  oper_state: "up",
  admin_up: true,
  carrier: true,
  parent: null,
  master: null,
  vlan_id: null,
  addresses: addresses.map(([address, dynamic]) => ({ address, family: address.includes(":") ? 6 : 4, scope: "global", dynamic: !!dynamic }) as HostInterface["addresses"][number]),
  managed_by: "systemd-networkd",
  manager_state: null,
  manager_profile: null,
  ...extra,
});

const facts = (...interfaces: HostInterface[]) => ({ interfaces }) as unknown as HostFacts;

const row = (over: Partial<ComparisonRow>): ComparisonRow => ({
  address: "192.0.2.10",
  status: "only_on_server",
  server_address: null,
  server_interface: null,
  netbox_address: null,
  netbox_interface: null,
  netbox_ip_id: null,
  recorded_elsewhere: null,
  ...over,
});

const host = facts(iface("eth0", [["192.0.2.10/24"], ["192.0.2.11/24"]]), iface("eth1", [["198.51.100.5/24", true]]), iface("lo", [["127.0.0.1/8"]], { kind: "loopback" }));

describe("changeSeedFor", () => {
  it("adds an address only NetBox documents", () => {
    const seed = changeSeedFor(row({ address: "192.0.2.12", status: "only_in_netbox", netbox_address: "192.0.2.12/24", netbox_interface: "eth0" }), host, "NetBox");
    expect(seed).toEqual({ iface: "eth0", addresses: ["192.0.2.10/24", "192.0.2.11/24", "192.0.2.12/24"], note: "Adds 192.0.2.12/24, as documented in NetBox." });
  });

  it("removes an address NetBox does not document, keeping the others", () => {
    const seed = changeSeedFor(row({ address: "192.0.2.11", server_address: "192.0.2.11/24", server_interface: "eth0" }), host, "Lanvex Core");
    expect(seed).toMatchObject({ iface: "eth0", addresses: ["192.0.2.10/24"] });
  });

  it("uses the documented prefix length", () => {
    const seed = changeSeedFor(row({ status: "mask_differs", server_address: "192.0.2.10/24", server_interface: "eth0", netbox_address: "192.0.2.10/25" }), host, "NetBox");
    expect(seed).toMatchObject({ addresses: ["192.0.2.10/25", "192.0.2.11/24"] });
  });

  it("says why when no change can be prepared", () => {
    expect(changeSeedFor(row({ address: "2001:db8::1", server_address: "2001:db8::1/64", server_interface: "eth0" }), host, "NetBox")).toBe("Change IPv6 addresses with Change IP settings.");
    expect(changeSeedFor(row({ address: "198.51.100.5", server_address: "198.51.100.5/24", server_interface: "eth1" }), host, "NetBox")).toBe("eth1 gets its address from DHCP.");
    expect(changeSeedFor(row({ status: "only_in_netbox", netbox_address: "192.0.2.99/24", netbox_interface: "eth9" }), host, "NetBox")).toBe("The server has no interface eth9.");
    expect(changeSeedFor(row({ status: "only_in_netbox", netbox_address: "192.0.2.99/24", netbox_interface: null }), host, "NetBox")).toBe("NetBox does not say which interface the address belongs to.");
    expect(typeof changeSeedFor(row({ status: "interface_differs", server_interface: "eth0", netbox_interface: "eth1" }), host, "NetBox")).toBe("string");
  });
});
