import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { SystemInfo } from "@/lib/types";

export function useSystemInfo() {
  return useQuery({ queryKey: ["system-info"], queryFn: () => api<SystemInfo>("/system/info"), staleTime: 5 * 60_000 });
}

/** "Lanvex Core" when Lanvex runs its own, "NetBox" when it uses an existing NetBox. */
export function useNetBoxLabel(): string {
  return useSystemInfo().data?.netbox_label ?? "NetBox";
}
