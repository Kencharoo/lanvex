import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { useSearchParams } from "react-router";
import { toast } from "sonner";
import { ApiError, api, errorMessage } from "./api";
import type { Option, WriteResult } from "./types";

export type LookupKind = "vrfs" | "prefix-roles" | "tenants" | "sites" | "vlan-groups" | "vlans" | "device-roles" | "manufacturers";

export function useLookup(kind: LookupKind, params?: { site_id?: number | null }, enabled = true) {
  return useQuery({
    queryKey: ["lookup", kind, params ?? {}],
    queryFn: () => api<Option[]>(`/lookups/${kind}`, { query: { site_id: params?.site_id ?? undefined } }),
    staleTime: 60_000,
    enabled,
  });
}

/** URL-backed filter state so filtered views are linkable and survive reloads. */
export function useUrlState<K extends string>(keys: readonly K[]) {
  const [params, setParams] = useSearchParams();
  const values = Object.fromEntries(keys.map((k) => [k, params.get(k) ?? ""])) as Record<K, string>;
  const set = React.useCallback(
    (patch: Partial<Record<K | "offset", string | number | null>>) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(patch)) {
            if (v === null || v === "" || v === undefined) next.delete(k);
            else next.set(k, String(v));
          }
          if (!("offset" in patch)) next.delete("offset");
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );
  const offset = Number(params.get("offset") ?? 0) || 0;
  return { values, set, offset };
}

/**
 * Mutation wrapper for NetBox writes: toasts that say where the change landed
 * ("Recorded in NetBox") and surface partial-success warnings instead of hiding them.
 */
export function useNetBoxWrite<TVars, TResult extends WriteResult | undefined>(opts: {
  fn: (vars: TVars) => Promise<TResult>;
  success: (result: TResult, vars: TVars) => string;
  invalidate: unknown[][];
  onSuccess?: (result: TResult, vars: TVars) => void;
}) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: opts.fn,
    onSuccess: (result, vars) => {
      toast.success(opts.success(result, vars), { description: "Recorded in NetBox. No device configuration was changed." });
      for (const w of result?.warnings ?? []) toast.warning(w, { duration: 12_000 });
      for (const key of [...opts.invalidate, ["dashboard"]]) void qc.invalidateQueries({ queryKey: key });
      opts.onSuccess?.(result, vars);
    },
    onError: (e) => {
      if (e instanceof ApiError && (e.status === 422 || e.status === 409)) return; // shown inline by the form
      toast.error(errorMessage(e));
    },
  });
}

export function fieldError(e: unknown, ...names: string[]): string | undefined {
  if (!(e instanceof ApiError) || !e.fields) return undefined;
  for (const n of names) if (e.fields[n]?.length) return e.fields[n].join(" ");
  return undefined;
}

export { api };
