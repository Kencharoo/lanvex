# SSH lab for server management

Three disposable Ubuntu 24.04 hosts, each booting systemd and running a different network stack, so
the Linux adapter is tested against real tools rather than sample text:

| Container | Address | Network stack | Notes |
|---|---|---|---|
| `lab-netplan` | 172.30.0.11 | netplan → systemd-networkd, systemd-resolved | Ubuntu Server default. Has a bridge `br0` with no carrier |
| `lab-nm` | 172.30.0.12 (+ 172.31.0.12) | NetworkManager | Fedora Server's stack. `eth1` is "connected (externally)": its profile never activated |
| `lab-ifup` | 172.30.0.13 | ifupdown (`/etc/network/interfaces`) | Classic Debian stack |

`lab-nm` and `lab-ifup` run lldpd and announce themselves with LLDP; `up.sh` lets the lab bridge
forward LLDP. `lab-netplan` receives it through systemd-networkd; on `lab-ifup`, `lanvex` is in the
group that may run `lldpcli` (`adm`) and asks lldpd; on `lab-nm` it is not, so Lanvex reports that.

Each has an unprivileged `lanvex` user (key login) and an `ops` user with password `ops-lab-password-1`.
Host keys are generated on first boot, so each container has its own.

```bash
sudo ./build.sh   # debootstrap + docker build; needs archive.ubuntu.com (or LANVEX_LAB_MIRROR), no registry
sudo ./up.sh      # start the three hosts on the lanvex-lab network
```

Then run the tests that use them:

```bash
cd backend && LANVEX_TEST_LAB=1 uv run pytest -m lab
cd frontend && LANVEX_E2E_LAB=1 npx playwright test e2e/servers.lab.spec.ts e2e/changes.lab.spec.ts
```

The change tests (`test_changes_lab.py`, `changes.lab.spec.ts`) **really change** `lab-netplan` and
`lab-nm`: they install the change helper and a sudoers rule for `lanvex`, apply, verify and roll back
changes, and put the shipped network configuration back afterwards. One test briefly attaches
`lab-ifup` to `lanvex-lab-b` as a gateway that ignores ping. Point them only at these containers.

To refresh the parser fixtures in `backend/tests/fixtures/linux`, run every command from
`lanvex/integrations/linux/commands.py` as `lanvex` on each host (`docker exec -u lanvex <host> sh -c '<command>'`).

## Limits

- The containers share the build machine's kernel. If it lacks IPv6, VLAN (8021q), bonding or dummy
  interfaces, those cannot appear in the lab; the parsers cover them with constructed samples instead.
- Distribution coverage is Ubuntu only: Debian and Fedora package mirrors were not reachable where
  the lab was built. Their network tools are the same upstream projects (ifupdown, NetworkManager),
  and os-release detection is unit-tested, but neither distribution has been tested end to end.
- `--privileged` is required for systemd as PID 1. Run the lab only on a development machine.
