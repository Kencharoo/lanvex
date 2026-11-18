"""TEST DOUBLE: an SNMP agent in this process, answering GET, GETNEXT, GETBULK and SET from a table.

Built on pysnmp's own command responder, so requests and answers (v2c communities, SNMPv3 users with
authentication and privacy) go over UDP exactly as with a switch. It is NOT a switch: it answers what
the table holds. Per-VLAN tables (Cisco "community@vlan", SNMPv3 context "vlan-<id>") are separate tables.
"""

import bisect
import socket
from typing import Any

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.proto import rfc1902, rfc1905
from pysnmp.proto.api import v2c
from pysnmp.smi import error as smi_error
from pysnmp.smi import instrum

COMMUNITY = "lanvex-test"
USER, AUTH_KEY, PRIV_KEY = "lanvex", "auth-pass-123", "priv-pass-456"


def snmp_value(v: Any) -> Any:
    if isinstance(v, bool) or v is None:
        raise TypeError(v)
    if isinstance(v, int):
        return rfc1902.Integer32(v) if -(2**31) <= v < 2**31 else rfc1902.Counter64(v)
    if isinstance(v, str):
        return rfc1902.OctetString(v.encode())
    if isinstance(v, bytes):
        return rfc1902.OctetString(v)
    if isinstance(v, tuple) and v and v[0] == "oid":
        return rfc1902.ObjectIdentifier(v[1])
    if isinstance(v, tuple) and v and v[0] == "ticks":
        return rfc1902.TimeTicks(v[1])
    if isinstance(v, tuple) and v and v[0] == "gauge":
        return rfc1902.Gauge32(v[1])
    return v


# Q-BRIDGE-MIB dot1qVlanStaticEgressPorts / UntaggedPorts -> dot1qVlanCurrentEgressPorts / UntaggedPorts
CURRENT = {
    (1, 3, 6, 1, 2, 1, 17, 7, 1, 4, 3, 1, 2): (1, 3, 6, 1, 2, 1, 17, 7, 1, 4, 2, 1, 4),
    (1, 3, 6, 1, 2, 1, 17, 7, 1, 4, 3, 1, 4): (1, 3, 6, 1, 2, 1, 17, 7, 1, 4, 2, 1, 5),
}


class Table(instrum.AbstractMibInstrumController):
    """Answers from `table`. SET changes existing objects when `writable`: `refuse` lists OIDs it refuses
    (inconsistentValue), `ignore` OIDs it acknowledges without changing, like a switch that does not
    really take a setting."""

    def __init__(self, table: dict[str, Any], writable: bool = True) -> None:
        self.table = {tuple(int(x) for x in k.split(".")): snmp_value(v) for k, v in table.items()}
        self.oids = sorted(self.table)
        self.writable = writable
        self.refuse: set[str] = set()
        self.ignore: set[str] = set()
        self.writes: list[tuple[str, Any]] = []

    def write_variables(self, *binds: Any, **ctx: Any) -> list[Any]:
        if not self.writable:
            raise smi_error.NotWritableError(name=binds[0][0])
        for oid, _ in binds:
            if tuple(oid) not in self.table:
                raise smi_error.NotWritableError(name=oid)
            if str(oid) in self.refuse:
                raise smi_error.InconsistentValueError(name=oid)
        for oid, v in binds:
            self.writes.append((str(oid), v))
            if str(oid) not in self.ignore:
                self.table[tuple(oid)] = v
                # As on a switch, the configured (static) VLAN membership is also the current one.
                key = tuple(oid)
                if key[:-1] in CURRENT and CURRENT[key[:-1]] + (0, key[-1]) in self.table:
                    self.table[CURRENT[key[:-1]] + (0, key[-1])] = v
        return list(binds)

    def __getitem__(self, oid: str) -> Any:
        return self.table[tuple(int(x) for x in oid.split("."))]

    def read_variables(self, *binds: Any, **ctx: Any) -> list[Any]:
        return [(o, self.table.get(tuple(o), rfc1905.noSuchInstance)) for o, _ in binds]

    def read_next_variables(self, *binds: Any, **ctx: Any) -> list[Any]:
        out = []
        for o, _ in binds:
            i = bisect.bisect_right(self.oids, tuple(o))
            if i < len(self.oids):
                out.append((v2c.ObjectIdentifier(self.oids[i]), self.table[self.oids[i]]))
            else:
                out.append((o, rfc1905.endOfMibView))
        return out


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Agent:
    """Start inside a running event loop; stop when done."""

    def __init__(
        self,
        table: dict[str, Any],
        vlans: dict[int, dict[str, Any]] | None = None,
        port: int | None = None,
        writable: bool = True,
    ) -> None:
        self.port = port or free_port()
        self.engine = e = engine.SnmpEngine()
        config.add_transport(
            e, udp.DOMAIN_NAME, udp.UdpTransport().open_server_mode(("127.0.0.1", self.port))
        )
        config.add_v3_user(
            e, USER, config.USM_AUTH_HMAC96_SHA, AUTH_KEY, config.USM_PRIV_CFB128_AES, PRIV_KEY
        )
        ctx = context.SnmpContext(e)
        contexts = {"": table} | {f"vlan-{v}": t for v, t in (vlans or {}).items()}
        for i, (name, rows) in enumerate(contexts.items()):
            community = COMMUNITY if not name else f"{COMMUNITY}@{name.removeprefix('vlan-')}"
            config.add_v1_system(e, f"area-{i}", community, contextName=name)
            config.add_vacm_user(e, 2, f"area-{i}", "noAuthNoPriv", (1,), (1,), contextName=name)
            config.add_vacm_user(e, 3, USER, "authPriv", (1,), (1,), contextName=name)
            if not name:
                ctx.unregister_context_name(v2c.OctetString(""))
            table_ = Table(rows, writable)
            if not name:
                self.table = table_
            ctx.register_context_name(v2c.OctetString(name), table_)
        cmdrsp.GetCommandResponder(e, ctx)
        cmdrsp.SetCommandResponder(e, ctx)
        cmdrsp.NextCommandResponder(e, ctx)
        cmdrsp.BulkCommandResponder(e, ctx)
        e.transport_dispatcher.job_started(1)

    def stop(self) -> None:
        self.engine.transport_dispatcher.job_finished(1)
        self.engine.close_dispatcher()
