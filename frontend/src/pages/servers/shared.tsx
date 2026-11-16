import { ShieldCheck } from "lucide-react";
import { CodeBlock } from "@/components/copy";
import { Checkbox, Field, Input, Textarea } from "@/components/ui/form";
import { Badge, type Tone } from "@/components/ui/misc";
import type { AuthMethod, ConnectionStatus, HostKeyScan } from "@/lib/types";
import { cn } from "@/lib/utils";

const STATUS: Record<ConnectionStatus, { tone: Tone; label: string }> = {
  ok: { tone: "ok", label: "Reachable" },
  unreachable: { tone: "danger", label: "Unreachable" },
  auth_failed: { tone: "danger", label: "Sign-in failed" },
  host_key_mismatch: { tone: "danger", label: "Host key changed" },
  not_allowed: { tone: "warn", label: "Address not allowed" },
  refused: { tone: "danger", label: "Refused for now" },
  error: { tone: "danger", label: "Error" },
};

/** Result of Lanvex's last SSH attempt. Says nothing about whether the server itself is healthy. */
export function ConnectionBadge({ status }: { status: ConnectionStatus | null }) {
  if (!status) return <Badge>Not checked</Badge>;
  const s = STATUS[status];
  return (
    <Badge tone={s.tone}>
      <span className="size-1.5 rounded-full bg-current" aria-hidden />
      {s.label}
    </Badge>
  );
}

const KEY_FILES: Record<string, string> = {
  "ssh-ed25519": "/etc/ssh/ssh_host_ed25519_key.pub",
  "ecdsa-sha2-nistp256": "/etc/ssh/ssh_host_ecdsa_key.pub",
  "ecdsa-sha2-nistp384": "/etc/ssh/ssh_host_ecdsa_key.pub",
  "ecdsa-sha2-nistp521": "/etc/ssh/ssh_host_ecdsa_key.pub",
  "rsa-sha2-512": "/etc/ssh/ssh_host_rsa_key.pub",
  "rsa-sha2-256": "/etc/ssh/ssh_host_rsa_key.pub",
};

export function HostKeyVerification({
  scan,
  confirmed,
  onConfirm,
  previous,
}: {
  scan: HostKeyScan;
  confirmed: boolean;
  onConfirm: (v: boolean) => void;
  previous?: string;
}) {
  const file = KEY_FILES[scan.algorithm] ?? "/etc/ssh/ssh_host_*_key.pub";
  return (
    <div className="grid gap-4">
      <div className="grid gap-1.5 rounded-[var(--radius-control)] border border-line p-3">
        <p className="text-xs text-muted">
          The server at {scan.resolved_addresses.join(", ")} presented this {scan.algorithm} host key:
        </p>
        <p className="addr break-all text-[15px] font-medium tracking-tight">{scan.fingerprint_sha256}</p>
        {previous && (
          <p className="addr break-all text-xs text-muted">
            <span className="font-sans">Previously pinned: </span>
            {previous}
          </p>
        )}
      </div>
      <div className="grid gap-1.5 text-[13px]">
        <p>On the server, through a console or a session you already trust, run:</p>
        <CodeBlock text={`ssh-keygen -lf ${file}`} label="Copy command" />
        <p className="text-muted">The fingerprint it prints must be identical. If it differs, do not continue: something other than your server answered.</p>
      </div>
      <Checkbox
        id="hk-confirm"
        label="I compared the fingerprints and they match"
        checked={confirmed}
        onChange={(e) => onConfirm(e.target.checked)}
      />
    </div>
  );
}

export interface CredentialDraft {
  method: AuthMethod;
  private_key: string;
  passphrase: string;
  password: string;
}

export const EMPTY_CREDENTIALS: CredentialDraft = { method: "generated_key", private_key: "", passphrase: "", password: "" };

export function credentialPayload(c: CredentialDraft) {
  if (c.method === "generated_key") return { method: c.method };
  if (c.method === "private_key") return { method: c.method, private_key: c.private_key, passphrase: c.passphrase || null };
  return { method: c.method, password: c.password };
}

export function credentialsComplete(c: CredentialDraft) {
  return c.method === "generated_key" || (c.method === "private_key" ? c.private_key.trim().length > 0 : c.password.length > 0);
}

const METHODS: { v: AuthMethod; title: string; text: string }[] = [
  { v: "generated_key", title: "Lanvex creates a key", text: "Recommended. A new key for this server only; you add its public half to the server." },
  { v: "private_key", title: "Use an existing private key", text: "Paste a private key the server already trusts." },
  { v: "password", title: "Password", text: "For lab machines. Keys are safer for anything else." },
];

export function CredentialsForm({
  value,
  onChange,
  errors,
}: {
  value: CredentialDraft;
  onChange: (v: CredentialDraft) => void;
  errors?: Partial<Record<"private_key" | "passphrase" | "password", string>>;
}) {
  const set = <K extends keyof CredentialDraft>(k: K, v: CredentialDraft[K]) => onChange({ ...value, [k]: v });
  return (
    <div className="grid gap-4">
      <div role="radiogroup" aria-label="How Lanvex signs in" className="grid gap-2">
        {METHODS.map((m) => (
          <label
            key={m.v}
            className={cn(
              "flex cursor-pointer gap-3 rounded-[var(--radius-control)] border p-3",
              value.method === m.v ? "border-accent bg-accent-soft/50" : "border-line hover:border-line-strong",
            )}
          >
            <input type="radio" name="auth-method" className="mt-0.5 accent-[var(--accent)]" checked={value.method === m.v} onChange={() => set("method", m.v)} />
            <span className="grid gap-0.5">
              <span className="flex items-center gap-1.5 text-[13px] font-medium">
                {m.title}
                {m.v === "generated_key" && <ShieldCheck className="size-3.5 text-ok" aria-hidden />}
              </span>
              <span className="text-xs text-muted">{m.text}</span>
            </span>
          </label>
        ))}
      </div>
      {value.method === "private_key" && (
        <>
          <Field label="Private key" htmlFor="cr-key" error={errors?.private_key} hint="OpenSSH or PEM format. Stored encrypted; never shown again.">
            <Textarea id="cr-key" className="addr min-h-32 text-xs" value={value.private_key} onChange={(e) => set("private_key", e.target.value)} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" spellCheck={false} />
          </Field>
          <Field label="Passphrase" htmlFor="cr-pass" optional error={errors?.passphrase} hint="Used once to unlock the key. Not stored.">
            <Input id="cr-pass" type="password" autoComplete="off" value={value.passphrase} onChange={(e) => set("passphrase", e.target.value)} />
          </Field>
        </>
      )}
      {value.method === "password" && (
        <Field label="Password" htmlFor="cr-pw" error={errors?.password} hint="Stored encrypted; never shown again.">
          <Input id="cr-pw" type="password" autoComplete="off" value={value.password} onChange={(e) => set("password", e.target.value)} />
        </Field>
      )}
    </div>
  );
}

export function InstallKeyInstructions({ username, line }: { username: string; line: string }) {
  const home = username === "root" ? "/root" : `/home/${username}`;
  const script = [
    `sudo install -d -m 700 -o ${username} -g ${username} ${home}/.ssh`,
    `echo '${line}' | sudo tee -a ${home}/.ssh/authorized_keys >/dev/null`,
    `sudo chown ${username}: ${home}/.ssh/authorized_keys && sudo chmod 600 ${home}/.ssh/authorized_keys`,
  ].join("\n");
  return (
    <div className="grid gap-2 text-[13px]">
      <p>
        Add this key to <span className="addr">{home}/.ssh/authorized_keys</span> on the server. <span className="addr">restrict</span> turns off forwarding and
        terminals for it; Lanvex only runs a fixed set of read-only commands.
      </p>
      <CodeBlock text={line} label="Copy key" />
      <p className="text-muted">Or run this on the server as an administrator:</p>
      <CodeBlock text={script} label="Copy commands" />
    </div>
  );
}
