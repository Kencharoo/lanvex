/**
 * Minimal IPv4/IPv6 parsing for instant form feedback. The backend re-validates everything;
 * this only exists so people see mistakes as they type.
 */

export interface ParsedNetwork {
  family: 4 | 6;
  network: bigint;
  prefixLength: number;
  size: bigint;
  canonical: string;
}

function parseV4(text: string): bigint | null {
  const parts = text.split(".");
  if (parts.length !== 4) return null;
  let value = 0n;
  for (const p of parts) {
    if (!/^\d{1,3}$/.test(p)) return null;
    const n = Number(p);
    if (n > 255 || (p.length > 1 && p.startsWith("0"))) return null;
    value = (value << 8n) | BigInt(n);
  }
  return value;
}

function parseV6(text: string): bigint | null {
  if (!/^[0-9a-fA-F:.]+$/.test(text) || text.includes(":::")) return null;
  const halves = text.split("::");
  if (halves.length > 2) return null;
  const toGroups = (s: string): number[] | null => {
    if (s === "") return [];
    const out: number[] = [];
    const parts = s.split(":");
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      if (i === parts.length - 1 && p.includes(".")) {
        const v4 = parseV4(p);
        if (v4 === null) return null;
        out.push(Number(v4 >> 16n), Number(v4 & 0xffffn));
      } else {
        if (!/^[0-9a-fA-F]{1,4}$/.test(p)) return null;
        out.push(parseInt(p, 16));
      }
    }
    return out;
  };
  const head = toGroups(halves[0]);
  const tail = halves.length === 2 ? toGroups(halves[1]) : [];
  if (!head || !tail) return null;
  let groups: number[];
  if (halves.length === 2) {
    const missing = 8 - head.length - tail.length;
    if (missing < 1) return null;
    groups = [...head, ...Array(missing).fill(0), ...tail];
  } else {
    groups = head;
  }
  if (groups.length !== 8) return null;
  return groups.reduce((acc, g) => (acc << 16n) | BigInt(g), 0n);
}

export function formatV4(v: bigint): string {
  return [24n, 16n, 8n, 0n].map((s) => String((v >> s) & 0xffn)).join(".");
}

export function formatV6(v: bigint): string {
  const groups = Array.from({ length: 8 }, (_, i) => Number((v >> BigInt((7 - i) * 16)) & 0xffffn));
  // Longest run of zero groups (length ≥ 2) is compressed, per RFC 5952.
  let best = { start: -1, len: 0 };
  for (let i = 0; i < 8; ) {
    if (groups[i] !== 0) {
      i++;
      continue;
    }
    let j = i;
    while (j < 8 && groups[j] === 0) j++;
    if (j - i > best.len && j - i >= 2) best = { start: i, len: j - i };
    i = j;
  }
  const hex = groups.map((g) => g.toString(16));
  if (best.start < 0) return hex.join(":");
  const left = hex.slice(0, best.start).join(":");
  const right = hex.slice(best.start + best.len).join(":");
  return `${left}::${right}`;
}

/** Whether `ip` is in the network of `address` (an address with its prefix length, host bits allowed). */
export function inNetworkOf(address: string, ip: string): boolean {
  const [a, len] = address.split("/");
  const family = a.includes(":") ? 6 : 4;
  if (ip.includes(":") !== (family === 6) || !/^\d{1,3}$/.test(len ?? "")) return false;
  const parse = family === 4 ? parseV4 : parseV6;
  const x = parse(a);
  const y = parse(ip.split("%")[0]);
  const bits = family === 4 ? 32 : 128;
  if (x === null || y === null || Number(len) > bits) return false;
  const shift = BigInt(bits - Number(len));
  return x >> shift === y >> shift;
}

export type ParseResult = { ok: true; value: ParsedNetwork } | { ok: false; error: string; suggestion?: string };

export function parseCidr(input: string): ParseResult {
  const text = input.trim();
  if (!text) return { ok: false, error: "Enter a prefix, e.g. 10.20.0.0/24." };
  const slash = text.indexOf("/");
  if (slash < 0) return { ok: false, error: "Include the prefix length, e.g. /24." };
  const addr = text.slice(0, slash);
  const lenText = text.slice(slash + 1);
  const family: 4 | 6 = addr.includes(":") ? 6 : 4;
  const bits = family === 4 ? 32 : 128;
  if (!/^\d{1,3}$/.test(lenText) || Number(lenText) > bits) {
    return { ok: false, error: `Prefix length must be 0–${bits} for IPv${family}.` };
  }
  const prefixLength = Number(lenText);
  const value = family === 4 ? parseV4(addr) : parseV6(addr);
  if (value === null) return { ok: false, error: `“${addr}” is not a valid IPv${family} address.` };
  const hostBits = BigInt(bits - prefixLength);
  const mask = ((1n << BigInt(bits)) - 1n) ^ ((1n << hostBits) - 1n);
  const network = value & mask;
  const fmt = family === 4 ? formatV4 : formatV6;
  const canonical = `${fmt(network)}/${prefixLength}`;
  if (network !== value) {
    return { ok: false, error: `Host bits are set. The network address is ${canonical}.`, suggestion: canonical };
  }
  return { ok: true, value: { family, network, prefixLength, size: 1n << hostBits, canonical } };
}

/** Usable host count, matching NetBox (IPv4 excludes network + broadcast below /31; IPv6 excludes anycast below /127). */
export function usableHosts(n: ParsedNetwork, isPool = false): bigint {
  if (isPool) return n.size;
  if (n.family === 4) return n.prefixLength >= 31 ? n.size : n.size - 2n;
  return n.prefixLength >= 127 ? n.size : n.size - 1n;
}

export function suggestGateway(n: ParsedNetwork): string | null {
  if ((n.family === 4 && n.prefixLength >= 31) || (n.family === 6 && n.prefixLength >= 127)) return null;
  const fmt = n.family === 4 ? formatV4 : formatV6;
  return `${fmt(n.network + 1n)}/${n.prefixLength}`;
}

export function formatBig(n: bigint): string {
  if (n < 1_000_000_000_000n) return Number(n).toLocaleString();
  const exp = n.toString(2).length - 1;
  return `2^${exp}`;
}
