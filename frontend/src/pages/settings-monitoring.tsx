import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import * as React from "react";
import { toast } from "sonner";
import { DataTable, type Column } from "@/components/data-table";
import { Section } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Checkbox, Field, Input, Select } from "@/components/ui/form";
import { Badge, Notice, Skeleton } from "@/components/ui/misc";
import { api, errorMessage } from "@/lib/api";
import { fieldError } from "@/lib/hooks";
import type { DeliveryResult, MonitoringOut, MonitoringSettings, Severity, Webhook, WebhookFormat } from "@/lib/types";
import { relativeTime } from "@/lib/utils";

const SEVERITIES: { v: Severity; label: string }[] = [
  { v: "info", label: "All" },
  { v: "warning", label: "Warning and critical" },
  { v: "critical", label: "Critical only" },
];

const FORMATS: { v: WebhookFormat; label: string }[] = [
  { v: "generic", label: "JSON" },
  { v: "ntfy", label: "ntfy" },
  { v: "gotify", label: "Gotify" },
  { v: "slack", label: "Slack / Mattermost" },
  { v: "discord", label: "Discord" },
  { v: "teams", label: "Microsoft Teams" },
];

const lines = (v: string) =>
  v
    .split(/[\n,]/)
    .map((x) => x.trim())
    .filter(Boolean);

function useTest() {
  return useMutation({
    mutationFn: (channel: string) => api<DeliveryResult>("/monitoring/test", { method: "POST", body: { channel } }),
    onSuccess: (r) => (r.ok ? toast.success("Test message sent") : toast.error(r.error ?? "Sending failed")),
    onError: (e) => toast.error(errorMessage(e)),
  });
}

export function MonitoringSection() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["monitoring"], queryFn: () => api<MonitoringOut>("/monitoring") });
  const [form, setForm] = React.useState<MonitoringSettings | null>(null);
  const [ignoreIfaces, setIgnoreIfaces] = React.useState("");
  const [ignoreNets, setIgnoreNets] = React.useState("");
  const [emails, setEmails] = React.useState("");
  React.useEffect(() => {
    if (!q.data) return;
    const s = q.data.settings;
    setForm(s);
    setIgnoreIfaces(s.drift_ignore_interfaces.join(", "));
    setIgnoreNets(s.unknown_device_ignore.join(", "));
    setEmails(s.email_to.join(", "));
  }, [q.data]);
  const save = useMutation({
    mutationFn: () =>
      api<MonitoringOut>("/monitoring", {
        method: "PUT",
        body: { ...form, drift_ignore_interfaces: lines(ignoreIfaces), unknown_device_ignore: lines(ignoreNets), email_to: lines(emails) },
      }),
    onSuccess: (r) => {
      qc.setQueryData(["monitoring"], r);
      void qc.invalidateQueries({ queryKey: ["alerts"] });
      toast.success("Monitoring saved");
    },
  });
  const test = useTest();
  if (!form || !q.data) {
    return (
      <Section title="Monitoring">
        <Skeleton className="h-64 w-full" />
      </Section>
    );
  }
  const set = <K extends keyof MonitoringSettings>(k: K, v: MonitoringSettings[K]) => setForm({ ...form, [k]: v });
  const m = q.data;
  const panel = "grid gap-4 rounded-[var(--radius-panel)] border border-line bg-surface p-4";

  return (
    <div className="grid gap-10">
      <Section
        title="Monitoring"
        description={m.updated_by ? `Changed by ${m.updated_by} ${relativeTime(m.updated_at)}` : undefined}
        actions={
          <Button size="sm" variant="primary" onClick={() => save.mutate()} loading={save.isPending}>
            Save
          </Button>
        }
      >
        {!m.enabled && <Notice tone="warn">Monitoring is turned off (LANVEX_MONITOR_ENABLED).</Notice>}
        {save.error && !fieldError(save.error) && <Notice tone="danger">{errorMessage(save.error)}</Notice>}
        <div className={panel}>
          <h3 className="text-[13px] font-medium">Checks</h3>
          <div className="grid gap-3 sm:grid-cols-[14rem_1fr] sm:items-center">
            <Checkbox id="m-unreach" label="Server unreachable" checked={form.unreachable} onChange={(e) => set("unreachable", e.target.checked)} />
            <label className="flex items-center gap-2 text-[13px] text-muted">
              after
              <Input
                type="number"
                min={1}
                max={60}
                className="w-16"
                value={form.unreachable_after}
                onChange={(e) => set("unreachable_after", Number(e.target.value))}
                aria-label="Failed checks before an alert"
              />
              failed checks, every {Math.round(m.probe_seconds / 60)} min
            </label>
            <Checkbox id="m-read" label="Read failed" checked={form.read_failed} onChange={(e) => set("read_failed", e.target.checked)} />
            <span />
            <Checkbox id="m-drift" label="Drift from documentation" checked={form.drift} onChange={(e) => set("drift", e.target.checked)} />
            <Input
              value={ignoreIfaces}
              onChange={(e) => setIgnoreIfaces(e.target.value)}
              placeholder="Ignored interfaces"
              aria-label="Interfaces not compared"
              title="Interfaces not compared, e.g. docker*, veth*"
            />
            <Checkbox id="m-pool" label="DHCP pool nearly full" checked={form.pool_full} onChange={(e) => set("pool_full", e.target.checked)} />
            <label className="flex items-center gap-2 text-[13px] text-muted">
              at
              <Input
                type="number"
                min={50}
                max={100}
                className="w-16"
                value={form.pool_threshold}
                onChange={(e) => set("pool_threshold", Number(e.target.value))}
                aria-label="Pool usage in percent"
              />
              %
            </label>
            <Checkbox id="m-unknown" label="Unknown devices" checked={form.unknown_device} onChange={(e) => set("unknown_device", e.target.checked)} />
            <Input
              value={ignoreNets}
              onChange={(e) => setIgnoreNets(e.target.value)}
              placeholder="Ignored networks or prefixes"
              aria-label="Networks without unknown-device alerts"
              title="Network names or prefixes, e.g. guest, 10.20.0.0/16"
            />
            <Checkbox id="m-change" label="Failed changes" checked={form.change_failed} onChange={(e) => set("change_failed", e.target.checked)} />
            <span />
          </div>
          {fieldError(save.error, "drift_ignore_interfaces", "unknown_device_ignore", "unreachable_after", "pool_threshold") && (
            <p role="alert" className="text-xs text-danger">
              {fieldError(save.error, "drift_ignore_interfaces", "unknown_device_ignore", "unreachable_after", "pool_threshold")}
            </p>
          )}
        </div>

        <div className={panel}>
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-[13px] font-medium">Email</h3>
            <Button size="sm" variant="ghost" disabled={!m.smtp_configured || !form.email_to.length} loading={test.isPending && test.variables === "email"} onClick={() => test.mutate("email")}>
              Send test
            </Button>
          </div>
          {m.smtp_configured ? (
            <p className="text-xs text-muted">Sent from {m.smtp_from}</p>
          ) : (
            <p className="text-xs text-muted">
              Not configured. Set <code className="addr">LANVEX_SMTP_HOST</code> and <code className="addr">LANVEX_SMTP_FROM</code> in .env.
            </p>
          )}
          <Checkbox id="m-email" label="Send alerts by email" checked={form.email_enabled} disabled={!m.smtp_configured} onChange={(e) => set("email_enabled", e.target.checked)} />
          <div className="grid gap-3 sm:grid-cols-[1fr_14rem]">
            <Field label="Recipients" htmlFor="m-to" error={fieldError(save.error, "email_to")}>
              <Input id="m-to" value={emails} onChange={(e) => setEmails(e.target.value)} placeholder="ops@example.net" />
            </Field>
            <Field label="Severity" htmlFor="m-sev">
              <Select id="m-sev" value={form.email_min_severity} onChange={(e) => set("email_min_severity", e.target.value as Severity)}>
                {SEVERITIES.map((s) => (
                  <option key={s.v} value={s.v}>
                    {s.label}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <Checkbox id="m-resolved" label="Also notify when alerts resolve" checked={form.notify_resolved} onChange={(e) => set("notify_resolved", e.target.checked)} />
        </div>
      </Section>
      <WebhooksSection />
    </div>
  );
}

function WebhooksSection() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["monitoring", "webhooks"], queryFn: () => api<Webhook[]>("/monitoring/webhooks") });
  const [editing, setEditing] = React.useState<Webhook | "new" | null>(null);
  const test = useTest();
  const remove = useMutation({
    mutationFn: (w: Webhook) => api(`/monitoring/webhooks/${w.id}`, { method: "DELETE" }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["monitoring", "webhooks"] }),
    onError: (e) => toast.error(errorMessage(e)),
  });
  const columns: Column<Webhook>[] = [
    { key: "name", header: "Name", cell: (w) => <span className="font-medium">{w.name}</span> },
    { key: "format", header: "Format", cell: (w) => FORMATS.find((f) => f.v === w.format)?.label ?? w.format },
    { key: "url", header: "URL", cell: (w) => <span className="addr text-muted">{w.url_hint}</span> },
    { key: "sev", header: "Severity", cell: (w) => SEVERITIES.find((s) => s.v === w.min_severity)?.label },
    { key: "on", header: "Status", cell: (w) => (w.enabled ? <Badge tone="ok">On</Badge> : <Badge>Off</Badge>) },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      className: "w-px whitespace-nowrap text-right",
      cell: (w) => (
        <div className="flex justify-end gap-1">
          <Button size="sm" variant="ghost" loading={test.isPending && test.variables === `webhook:${w.id}`} onClick={() => test.mutate(`webhook:${w.id}`)}>
            Test
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setEditing(w)}>
            Edit
          </Button>
          <Button size="sm" variant="ghost" loading={remove.isPending && remove.variables?.id === w.id} onClick={() => remove.mutate(w)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];
  return (
    <Section
      title="Webhooks"
      actions={
        <Button size="sm" onClick={() => setEditing("new")}>
          <Plus /> Add webhook
        </Button>
      }
    >
      <DataTable
        columns={columns}
        rows={q.data}
        rowKey={(w) => w.id}
        loading={q.isPending}
        error={q.error}
        onRetry={() => void q.refetch()}
        empty={<p className="px-3 py-6 text-center text-[13px] text-muted">No webhooks</p>}
        dense
      />
      {editing && <WebhookDialog webhook={editing === "new" ? null : editing} onClose={() => setEditing(null)} />}
    </Section>
  );
}

function WebhookDialog({ webhook, onClose }: { webhook: Webhook | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = React.useState(webhook?.name ?? "");
  const [format, setFormat] = React.useState<WebhookFormat>(webhook?.format ?? "generic");
  const [url, setUrl] = React.useState("");
  const [minSeverity, setMinSeverity] = React.useState<Severity>(webhook?.min_severity ?? "warning");
  const [enabled, setEnabled] = React.useState(webhook?.enabled ?? true);
  const save = useMutation({
    mutationFn: () =>
      api<Webhook>(webhook ? `/monitoring/webhooks/${webhook.id}` : "/monitoring/webhooks", {
        method: webhook ? "PUT" : "POST",
        body: { name, format, url: url || null, min_severity: minSeverity, enabled },
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["monitoring", "webhooks"] });
      onClose();
    },
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={webhook ? "Edit webhook" : "Add webhook"}>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <DialogBody>
            {save.error && !fieldError(save.error, "name", "url") && <Notice tone="danger">{errorMessage(save.error)}</Notice>}
            <Field label="Name" htmlFor="wh-name" error={fieldError(save.error, "name")}>
              <Input id="wh-name" value={name} onChange={(e) => setName(e.target.value)} required maxLength={100} />
            </Field>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Format" htmlFor="wh-format">
                <Select id="wh-format" value={format} onChange={(e) => setFormat(e.target.value as WebhookFormat)}>
                  {FORMATS.map((f) => (
                    <option key={f.v} value={f.v}>
                      {f.label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Severity" htmlFor="wh-sev">
                <Select id="wh-sev" value={minSeverity} onChange={(e) => setMinSeverity(e.target.value as Severity)}>
                  {SEVERITIES.map((s) => (
                    <option key={s.v} value={s.v}>
                      {s.label}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <Field label="URL" htmlFor="wh-url" error={fieldError(save.error, "url")} hint={webhook ? `${webhook.url_hint} · leave empty to keep` : undefined}>
              <Input id="wh-url" type="url" value={url} onChange={(e) => setUrl(e.target.value)} required={!webhook} placeholder="https://" autoComplete="off" />
            </Field>
            <Checkbox id="wh-on" label="Enabled" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={save.isPending}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

