import { act, renderHook } from "@testing-library/react";
import * as React from "react";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it } from "vitest";
import { useUrlState } from "./hooks";

function setup(initial: string) {
  const wrapper = ({ children }: { children: React.ReactNode }) => <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>;
  return renderHook(() => ({ state: useUrlState(["view", "site", "q"] as const), location: useLocation() }), { wrapper });
}

describe("useUrlState", () => {
  it("keeps both of two changes made before React renders", () => {
    const { result } = setup("/topology?view=cables&q=srv");
    act(() => {
      result.current.state.set({ view: null }); // back to the map…
      result.current.state.set({ site: "7" }); // …and straight to a site, before any render
    });
    expect(result.current.location.search).toBe("?q=srv&site=7");
    expect(result.current.state.values).toEqual({ view: "", site: "7", q: "srv" });
  });

  it("drops the page offset unless the change sets it", () => {
    const { result } = setup("/devices?offset=50&q=a");
    act(() => result.current.state.set({ q: "b" }));
    expect(result.current.location.search).toBe("?q=b");
  });
});
