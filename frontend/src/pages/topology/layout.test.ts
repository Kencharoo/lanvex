import { describe, expect, it } from "vitest";
import type { TopologyLink, TopologyNode } from "@/lib/types";
import { orient, stacks, tiers } from "./layout";

const node = (id: string, role: string | null, kind: TopologyNode["kind"] = "device"): TopologyNode => ({
  id,
  kind,
  object_id: 1,
  name: id,
  role: role ? { id: 1, name: role, slug: role.toLowerCase().replace(/ /g, "-") } : null,
  color: null,
  site: null,
  status: null,
  device_type: null,
  primary_ip: null,
  provider: null,
  external: false,
  server_id: null,
  switch_id: null,
  netbox_url: null,
});
const link = (a: string, b: string): TopologyLink => ({
  id: `${a}-${b}`,
  cable_id: 1,
  a,
  b,
  a_ports: [],
  b_ports: [],
  kind: "data",
  status: null,
  type: null,
  label: "",
  color: null,
  netbox_url: null,
});

describe("tiers", () => {
  it("ranks by role: circuit, core, distribution, access, patch panel, server", () => {
    const nodes = [
      node("isp", null, "circuit"),
      node("core", "Core switch"),
      node("dist", "Distribution"),
      node("acc", "Access switch"),
      node("pp", "Patch panel"),
      node("srv", "Server"),
      node("ap", "Access point"),
    ];
    const t = tiers(nodes, []);
    const order = ["isp", "core", "dist", "acc", "pp", "srv"].map((id) => t.get(id)!);
    expect([...order].sort((x, y) => x - y)).toEqual(order);
    expect(t.get("ap")).toBe(t.get("srv")); // an access point is an end device, not an access switch
  });

  it("places devices with unknown roles one below their nearest known neighbour", () => {
    const nodes = [node("core", "Core switch"), node("thing", "Widget"), node("other", null)];
    const t = tiers(nodes, [link("core", "thing"), link("thing", "other")]);
    expect(t.get("thing")).toBe(t.get("core")! + 1);
    expect(t.get("other")).toBe(t.get("thing")! + 1);
  });

  it("starts a network no role describes at its busiest device", () => {
    const nodes = ["a", "hub", "b", "c"].map((id) => node(id, null));
    const t = tiers(nodes, [link("a", "hub"), link("b", "hub"), link("c", "hub")]);
    expect(t.get("hub")).toBe(0);
    expect(t.get("a")).toBe(1);
  });
});

describe("orient", () => {
  it("draws cables from the upper end down, and same-tier cables as peers", () => {
    const nodes = [node("core1", "Core switch"), node("core2", "Core switch"), node("acc", "Access switch")];
    const links = [link("acc", "core1"), link("core1", "core2")];
    expect(orient(links, tiers(nodes, links))).toEqual([["core1", "acc"], null]);
  });
});

describe("stacks", () => {
  it("stacks end devices under the switch they hang off, not a device with other cables", () => {
    const nodes = [node("acc", "Access switch"), node("core", "Core switch"), node("s1", "Server"), node("s2", "Server"), node("dual", "Server")];
    const links = [link("core", "acc"), link("acc", "s1"), link("s2", "acc"), link("acc", "dual"), link("core", "dual")];
    const st = stacks(nodes, links, tiers(nodes, links));
    expect(st.get("acc")).toEqual(["s1", "s2"]);
    expect([...st.values()].flat()).not.toContain("dual");
  });

  it("never stacks under a circuit or under a device at another site", () => {
    const lab = { id: 1, name: "Lab", slug: "lab" };
    const branch = { id: 2, name: "Branch", slug: "branch" };
    const nodes = [
      { ...node("isp", null, "circuit") },
      { ...node("core", "Core switch"), site: lab },
      { ...node("acc", "Access switch"), site: lab },
      { ...node("br", "Access switch"), site: branch },
      { ...node("far", "Server"), site: branch },
    ];
    const links = [link("isp", "core"), link("isp", "br"), link("core", "acc"), link("core", "far")];
    const stacked = [...stacks(nodes, links, tiers(nodes, links)).values()].flat();
    expect(stacked).not.toContain("br");
    expect(stacked).not.toContain("far");
    expect(stacked).toContain("acc");
  });

  it("never stacks a device from another site", () => {
    const remote = { ...node("remote", "Server"), external: true };
    const nodes = [node("core", "Core switch"), node("acc", "Access switch"), remote];
    const links = [link("core", "acc"), link("core", "remote")];
    expect([...stacks(nodes, links, tiers(nodes, links)).values()].flat()).not.toContain("remote");
  });
});
