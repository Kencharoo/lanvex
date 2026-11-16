# Changing server configuration

Lanvex can change the IPv4 configuration of one interface on a Linux server: its static
addresses (or switching to DHCP), its default gateway, and optionally its DNS servers and search
domains. It can also give a server VLAN networks with DHCP, from the documentation in Lanvex Core:
see [networks.md](networks.md). Every change is previewed, approved by an administrator, applied by
the server itself, verified, and rolled back automatically if anything fails or Lanvex loses contact.

Everything else on the interface stays as it is: IPv6, routes other than the default route, MTU,
VLAN and bridge settings, and any other key in the file.

It is **off by default**, three times over: for the installation, for each server, and per change
(approval).

## What it works with

| Network setup | Supported | What Lanvex edits |
|---|---|---|
| netplan → systemd-networkd (Ubuntu Server default) | Yes | The netplan file that defines the interface |
| netplan → NetworkManager (Ubuntu Desktop / NM with netplan, 23.10+) | Yes | The netplan file NetworkManager's profile is generated from |
| NetworkManager with keyfiles (Fedora, RHEL, Debian with NM) | Yes | `/etc/NetworkManager/system-connections/<profile>.nmconnection` |
| ifupdown (`/etc/network/interfaces`) | Networks only | Interface settings: no. VLAN networks with DHCP: yes, in `/etc/network/interfaces.d/lanvex` ([networks.md](networks.md)) |
| NetworkManager profiles that exist only in memory | No | Refused with an explanation |
| netplan definitions that match many interfaces (`match: {name: "en*"}`) | No | Refused; only definitions for exactly this interface are edited |

The server needs **systemd** and **Python 3.8 or newer**. On netplan servers, `python3-yaml` is
needed; it is installed with netplan.

Tested end to end on Ubuntu 24.04 with netplan → systemd-networkd, NetworkManager keyfiles, and
NetworkManager-in-netplan. See [status.md](status.md) for what has not been tested.

## How a change runs

1. **Prepare.** An administrator describes the interface's new IPv4 settings. Lanvex reads the server
   again, applies its own safety rules (below), and asks the helper on the server for a plan. The
   helper returns the exact file contents before and after and the commands it would run. **Nothing
   on the server changes.**
2. **Review and approve.** The change page shows the old and new values, the file diff, the commands
   and any warnings. An administrator approves by typing the server's name. Optionally a second
   administrator must approve (`LANVEX_CHANGES_REQUIRE_SECOND_ADMIN=true`).
3. **Apply.** The helper checks the file has not changed since the preview (otherwise it refuses),
   backs it up, **arms a rollback timer** (`systemd-run`, default 120 seconds), and only then writes the
   new file and applies it (`netplan apply`, or `nmcli connection load` + `nmcli device reapply`).
4. **Verify.** Lanvex opens a **new** SSH connection and the helper checks:
   - Lanvex can still sign in;
   - the interface has exactly the requested IPv4 addresses (and a DHCP lease, for DHCP);
   - the default route is as requested, and the gateway is there: it answers ping or, if it drops
     ping (many firewalls do), at least answers ARP on the link;
   - the requested DNS servers are active on the interface (when DNS was changed);
   - a host name you name resolves (optional, e.g. your NetBox).
5. **Confirm or roll back.** If every check passes, Lanvex confirms and the helper cancels the timer:
   the change is permanent. If a check fails, Lanvex has the server restore the previous file
   immediately. If Lanvex cannot reach the server at all, it does nothing: the timer fires on the
   server and restores the previous file by itself.
6. **Read again.** Lanvex reads the server once more, so the server page shows what is really there.

The change page follows each step live. Its status is always one of: *Waiting for approval*,
*Approved, queued*, *Applying*, *Verifying*, *Applied and verified*, *Rolled back*, *Not applied*,
*Cancelled*, or *Needs attention* (below). Nothing is called "applied" before the server confirmed it.

**NetBox is not changed by the change itself.** Afterwards, the change page says whether NetBox
documents the interface as the server now has it. If not, **Record in NetBox…** shows exactly which
NetBox updates would make it so (add the address to the interface, correct a prefix length, unassign
an address the server no longer has), and makes those updates, and only those, when you confirm.
The same is available for every difference on the server page (below).

### Starting from a difference

On the server page, the comparison with NetBox lists each address that differs. Its menu offers both
directions:

- **Record in NetBox…** — the server is right: update the documentation (a preview first, then exactly
  the previewed updates; if NetBox or the server changed in between, Lanvex shows the new preview
  instead). Nothing on the server changes. An address recorded on another device is not moved; an
  address that is a device's primary IP is not unassigned. Lanvex says so in the preview.
- **Change the server to match…** — NetBox is right: opens this change form with the difference
  already undone (the documented address added, the undocumented one removed, or the documented prefix
  length), starting from the interface's current addresses. It then goes through the usual preview,
  approval and verification. Offered for static IPv4 addresses, when changes are set up for the server.

### When Lanvex loses contact

If the change cuts the connection Lanvex relies on, verification cannot reach the server. Lanvex
waits until the server's rollback deadline has passed (plus 20 seconds) and then asks the server
what happened. Normally the answer is "rolled back by the timer", and the change is shown as
*Rolled back*.

If the server still cannot be reached, or Lanvex itself was restarted mid-change and cannot find
out, the change is marked **Needs attention**: Lanvex does not know the server's state and says so.
Check the server on its console (`sudo /usr/local/libexec/lanvex/lanvex-netapply status` with
`{"id": "<change id>"}` on stdin, or look in `/var/lib/lanvex/changes/`), then press **Check again**
on the change page. A new change can still be prepared meanwhile; its preview starts with a warning
about the unresolved one, and the helper refuses to start while another change is still in progress
on the server.

## Safety rules

Lanvex refuses a change that would remove the address it connects to. If Lanvex reaches the server at
192.0.2.10 and that address is on the interface, the change must keep 192.0.2.10 as a static address,
and the interface cannot be switched to DHCP. To move a server to a new address: add the new address
in one change, update the server's address in Lanvex, then remove the old address in a second change.

### When Lanvex reaches the server indirectly

The rule above only works when the address Lanvex connects to is one of the server's own. It is not
when Lanvex reaches the server through port forwarding, NAT, or from a container on the server itself
(rootless Podman's `host.containers.internal`, 169.254.1.2, which is forwarded to the server's
loopback). Signing in again after a change would then prove nothing: the forwarded connection keeps
working even if the change cut the server off its network. So Lanvex refuses changes for such a
server until it has a **verification address** (Edit server): one of the server's own addresses that
Lanvex reaches directly. That address is protected like the one above, Lanvex checks it can reach it
before preparing a change, and after every change it must reach the server there too, or the change
is rolled back.

If Lanvex runs on the server it changes, it usually cannot reach that server's own addresses (a
rootless container cannot reach its host's LAN address). For that server, let **your browser confirm
changes** instead (below), or run Lanvex on another machine.

### Confirmed in your browser

For the server Lanvex runs on, turn on **My browser confirms changes** (Edit server; leave the
verification address empty). A change then runs as usual, the helper checks the result on the server,
and if every check passes, the change page asks **Does everything still work?** with *Keep the change*
and *Undo it now*, and the seconds left. Only *Keep* makes Lanvex confirm the change.

Use Lanvex through one of that server's own addresses (`http://192.168.178.2:8100`, not
`localhost`): your Keep reaching Lanvex is then the proof that the server's network still works. The
page says which address your browser uses; when it is not one of the server's, Keep needs an extra
tick that you checked the network yourself. The same goes for the server's VPN addresses (Tailscale,
WireGuard and other tunnels): reaching Lanvex through `tailscale0` shows only that the VPN works, not
the server's LAN and VLANs. If the change cut your browser off, nobody can choose
Keep, Lanvex does not confirm, and the server's timer restores the previous configuration. Every page
shows a change waiting for your decision, and *Keep* and *Undo* are in the audit log with the address
used.

This is how Netzverwaltung confirms its changes too, with the same limit: it proves the network your
browser is on, not every network the change touched. The helper's own checks (addresses, routes,
gateway, DHCP) still run before you are asked.

### Protected interfaces

Interfaces that another tool manages (a network-management script, Ansible, VLAN interfaces generated
from a file) can be listed under **Protected interfaces** (Edit server), as names or patterns such as
`vlan-*`. Lanvex never offers or prepares a change for them, so the two tools never fight over the
same profile. This includes [networks](networks.md): a Lanvex network whose name is protected stays
exactly as the server has it.

### Warnings

Lanvex warns before approval when a change touches the interface it connects through, adds, removes
or replaces the default gateway, or turns DHCP off. Loopback interfaces and interfaces that are part
of a bridge or bond cannot be changed. Only one change per server can be open at a time.

The helper adds its own checks: addresses, gateways and DNS servers must be valid IPv4 values; the
gateway must be inside one of the interface's subnets; the file must not have changed since the
preview; only one change runs at a time on a server.

## Turning it on

### 1. For the Lanvex installation

In `.env`, then restart Lanvex (`docker compose up -d`):

```
LANVEX_AUTOMATION_ENABLED=true
# optional
LANVEX_CHANGE_ROLLBACK_SECONDS=120          # 30–900
LANVEX_CHANGES_REQUIRE_SECOND_ADMIN=false
```

Pick a rollback time comfortably longer than your slowest server takes to apply a network change
and accept a new SSH connection. Lanvex needs to finish verifying within that time.

### 2. On each server: install the helper

Lanvex never runs commands as root except one: the change helper, a single Python file installed
at `/usr/local/libexec/lanvex/lanvex-netapply`. The user Lanvex signs in as may run exactly that
program with sudo, nothing else. Installing it needs root, so an administrator does it once.

On the server page, under **Configuration changes**, choose **Install the helper**:

1. **Copy to the server** puts the file in the Lanvex user's home directory over SFTP. (Or
   **Download** it and copy it yourself.)
2. Run the commands shown, on the server, as an administrator. They look like this:

   ```bash
   echo '<sha256>  /home/lanvex/lanvex-netapply' | sha256sum -c -
   sudo install -D -m 0755 -o root -g root /home/lanvex/lanvex-netapply /usr/local/libexec/lanvex/lanvex-netapply
   echo 'lanvex ALL=(root) NOPASSWD: /usr/local/libexec/lanvex/lanvex-netapply' | sudo tee /etc/sudoers.d/lanvex >/dev/null
   sudo chmod 0440 /etc/sudoers.d/lanvex && sudo visudo -cf /etc/sudoers.d/lanvex
   ```

   The first line checks the file is exactly the one your Lanvex version ships. **Stop if it does not
   print `OK`.** The checksum is also shown by `GET /api/v1/changes/helper` (header `X-Lanvex-SHA256`).
3. **Check now.** Lanvex asks the helper for its version through `sudo -n`. The checklist shows
   *Installed* when it works, or what is missing: *Not installed*, *Not allowed through sudo*,
   *Python 3 missing*, *Outdated version* (after a Lanvex upgrade: install the new file the same way).

**After a Lanvex update** that ships a new helper, every server's helper shows as *Outdated version*.
Lanvex cannot replace it by itself: its account may run the helper through sudo, nothing else. Repeat
steps 1–3: **Copy to the server**, then an administrator runs the commands shown (the sudoers line is
already there and may stay).

### 3. For the server

Under **Configuration changes**, choose **Turn on**. Administrators can now prepare changes for
this server. **Turn off** again at any time: changes still waiting for approval can then not be
approved, and approved ones that have not started end as *Not applied*. A change already being
applied finishes, including its verification and rollback.

## Making a change

1. Server page → **Change IP settings**.
2. Choose the interface. The form starts with its current settings, as last read.
3. Static: edit the address list (CIDR, e.g. `192.0.2.10/24`) and the default gateway (empty for none).
   Or choose **From DHCP**.
4. DNS stays untouched unless you tick **Also change DNS for this interface**. Then the servers and
   search domains you enter replace the interface's current ones (empty fields remove them).
5. Optionally name a host that must still resolve afterwards.
6. **Prepare change.** Review the change page, then **Approve and apply** and type the server's name.

## What the helper does as root

- Reads the network configuration and runs `netplan generate --root-dir` (validation in a temporary
  directory), `netplan generate`, `netplan apply`, `nmcli` (connection list, load, reapply, up),
  `ip -json`, `ping`, `resolvectl`, `getent`, and `systemd-run`/`systemctl` for its own timer units.
  For networks also `ifup`/`ifdown`, `ip link add`/`delete` for its own VLAN interfaces,
  `dnsmasq --test`, `firewall-cmd --check-config` and `--reload`, `ufw` (its own marked rules),
  `nft -c`/`-f` for its own table and `nft delete table inet lanvex`, `nft -j list chains` (read
  only), `ip route` (read only), `sysctl -w net.ipv4.ip_forward`, `ip link add lxdns type dummy`
  and the DNS filter's address on it, and `systemctl` for dnsmasq, `lanvex-firewall.service` and the
  DNS filters `lanvex-dns@<network>.service`. When taking over from Netzverwaltung, also `systemctl`
  for `netzverwaltung.service` and `dnsmasq-filter.service` (stop, disable, and back on rollback),
  and `nmcli connection delete` for its profiles `vlan-<name>`.
- Reads Netzverwaltung's `/etc/netzverwaltung/netz.yaml`, its `nv-…` files and policies for the import
  (*inspect*; read only).
- Writes only the files shown in the preview, owned by root (0600 for netplan and NetworkManager
  files, which refuse anything else).
- Keeps a backup and a state file per change in `/var/lib/lanvex/changes/<id>/` (the last 20
  changes), and a lock in `/run/lanvex-netapply.lock`.
- Takes JSON on stdin; nothing from the request is passed to a shell. It validates every value
  itself, independently of Lanvex.

The preview is generated by the helper from the server's real files. Values that look like secrets
(Wi-Fi keys, 802.1X passwords) are shown as `[redacted]` and never leave the server in clear text.

## Removing the helper

```bash
sudo rm /usr/local/libexec/lanvex/lanvex-netapply /etc/sudoers.d/lanvex
sudo rm -rf /var/lib/lanvex      # backups and state of past changes
```

## Troubleshooting

| Message | Meaning and fix |
|---|---|
| *Not allowed through sudo* | The sudoers rule is missing, names another path or user, or `sudo` asks for a password. Re-run the install commands |
| *Python 3 missing* | Install `python3` (and `python3-yaml` on netplan servers) |
| *Outdated version* | Lanvex was upgraded, or the file on the server is not exactly the one Lanvex ships (the helper reports its own SHA-256). Copy the new file to the server from the server page and have an administrator run the commands shown ([installing the helper](#2-on-each-server-install-the-helper)) |
| "The configuration file changed since the preview" | Someone edited the file after the change was prepared. Cancel it and prepare a new one |
| "Another change is in progress on this server" | A change is still running or awaiting its timer. Wait, or check `/var/lib/lanvex/changes/` |
| Rolled back, "Gateway … answers" failed | Nothing on that interface's link answered ping or ARP for the gateway address: a typo, the wrong subnet, or the gateway is down |
| Rolled back, "Lanvex can still sign in" failed | The new configuration cut the connection. The server restored its previous configuration on its own |
| *Needs attention* | See "When Lanvex loses contact" above |
