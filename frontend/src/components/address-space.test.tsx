import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TooltipProvider } from "./ui/misc";
import { UtilStrip } from "./address-space";

const wrap = (ui: React.ReactNode) => render(<TooltipProvider>{ui}</TooltipProvider>);

describe("UtilStrip", () => {
  it("states utilization and its basis for screen readers", () => {
    wrap(<UtilStrip u={{ percent: 51.57, used: 131, total: 254, basis: "ip_addresses" }} />);
    expect(screen.getByLabelText(/52% utilized\. 131 of 254 usable addresses recorded/)).toBeInTheDocument();
  });

  it("explains container utilization as coverage by child prefixes", () => {
    wrap(<UtilStrip u={{ percent: 7.1, used: 4672, total: 65536, basis: "child_prefixes" }} />);
    expect(screen.getByLabelText(/covered by child prefixes/)).toBeInTheDocument();
  });

  it("shows a dash when utilization is unknown rather than implying zero", () => {
    wrap(<UtilStrip u={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
