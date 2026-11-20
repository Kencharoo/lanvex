# Monitoring and alerts

Lanvex watches every server it reads, of any kind, and opens an alert when something needs attention.
The checks rest only on what Lanvex reads from the server and what the documentation (Lanvex Core or
NetBox) says, so they work on any server Lanvex can read over SSH. Switches get the *Unreachable* and
*Read failed* alerts too: Lanvex asks each one for its uptime over SNMP every two minutes
([switches.md](switches.md)).

## Checks

| Alert | When | Ends |
|---|---|---|
| **Unreachable** (critical) | The server's SSH port did not answer the last *n* checks (default 3). Lanvex checks every server every two minutes with a TCP connection only: no sign-in, so nothing counts against the server's protection against failed sign-ins | When the port answers again |
| **Read failed** (warning) | A read was refused: sign-in rejected, host key changed, address not allowed, connections refused | With the next successful read |
| **Drift** (warning) | The server's addresses, default gateway, DNS servers, search domains or static routes differ from what its device or virtual machine documents ([changes.md](changes.md#gateway-dns-and-static-routes)), or its Lanvex networks (VLAN, DHCP, access rules) are not as documented. Settings nothing documents are no drift. Interfaces of containers, VMs and VPNs (`docker*`, `veth*`, `tailscale*`, …) are ignored; the list is editable | When server and documentation match. More differences while open send a new message |
| **DHCP pool** (warning; critical when full) | Leases fill a Lanvex DHCP pool to the threshold (default 90%) | When usage drops below it |
| **Unknown device** (info) | A device holds a lease and its MAC address is documented nowhere: not on a device's or virtual machine's interface, not as a fixed address. Networks, prefixes or switch VLANs (`vlan:30`) can be excluded (guest networks) | When the device is documented, or acknowledged. A device is reported once per server |
| **Change failed** (warning) | A change was rolled back, failed, or needs attention | When acknowledged |
| **Port down**, **Device moved** | On switches: [switches.md](switches.md#monitoring). Unknown devices on switch ports are reported too | |

Checks that need a read run after every read, scheduled or by hand: with the default schedule, once an
hour per server (`LANVEX_SERVER_READ_INTERVAL_MINUTES`, or per server). A failed read keeps what the last
successful read found: nothing is resolved by a read that saw nothing.

Turning a check off ends its open alerts without a message.

## Alerts

*Alerts* lists open, resolved and all alerts; the navigation shows how many open alerts nobody has
acknowledged, and the overview lists the open ones. Acknowledging marks an alert as seen; alerts about
events (unknown devices, failed changes) end with it.

## Messages

Each alert is sent when it opens, when it changes (drift), and when it ends (can be turned off). What
is due at the same time goes out as one message per channel; failed deliveries are retried after 1, 2,
5, 15 and 60 minutes.

**Email** needs an SMTP server in `.env`, then recipients and the lowest severity under *Settings,
Monitoring*:

```bash
LANVEX_SMTP_HOST=mail.example.net
LANVEX_SMTP_PORT=587
LANVEX_SMTP_SECURITY=starttls        # or tls (port 465), or none
LANVEX_SMTP_USERNAME=lanvex@example.net
LANVEX_SMTP_PASSWORD=…
LANVEX_SMTP_FROM=Lanvex <lanvex@example.net>
LANVEX_PUBLIC_URL=https://lanvex.example.net   # links in messages
```

**Webhooks** (*Settings, Webhooks*) post to any URL, each with the lowest severity it receives:

| Format | For |
|---|---|
| JSON | Your own receivers: `{"source": "lanvex", "events": [{"event": "opened", "alert": {…}}]}` |
| ntfy | A topic URL (`https://ntfy.sh/your-topic`); priority and tags follow the severity |
| Gotify | `https://gotify.example.net/message?token=…` |
| Slack / Mattermost | An incoming webhook URL |
| Discord | A channel webhook URL |
| Microsoft Teams | A Workflows webhook URL (adaptive card) |

Webhook URLs usually contain a token, so Lanvex stores them encrypted (with `LANVEX_SECRET_KEY`) and
shows only their host. *Test* sends a test message to one channel.

## Running

The monitor (checks of the SSH ports, sending messages) runs in one Lanvex process at a time; the
others wait to take over. `LANVEX_MONITOR_ENABLED=false` turns monitoring off,
`LANVEX_MONITOR_PROBE_SECONDS` sets how often ports are checked (30 to 3600).
