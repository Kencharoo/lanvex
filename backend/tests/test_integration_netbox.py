"""Integration tests against a REAL, DISPOSABLE NetBox. Skipped unless explicitly configured.

    LANVEX_TEST_NETBOX_URL=http://localhost:8000 LANVEX_TEST_NETBOX_TOKEN=nbt_... uv run pytest -m netbox

These create and delete objects under 198.51.100.0/24 (TEST-NET-2) and VLAN 4001. Never point them
at a production NetBox.
"""

import os
import uuid

import pytest

from lanvex.ipam.prefixes import PrefixService
from lanvex.ipam.schemas import PrefixCreate, PrefixValidateRequest
from lanvex.netbox.client import NetBoxClient

URL = os.environ.get("LANVEX_TEST_NETBOX_URL")
TOKEN = os.environ.get("LANVEX_TEST_NETBOX_TOKEN")

pytestmark = [
    pytest.mark.netbox,
    pytest.mark.skipif(not (URL and TOKEN), reason="LANVEX_TEST_NETBOX_URL / _TOKEN not set"),
]


@pytest.fixture
async def nb():
    client = NetBoxClient(URL or "", TOKEN or "")
    yield client
    await client.aclose()


async def test_status(nb: NetBoxClient) -> None:
    s = await nb.status()
    assert s.reachable and s.authenticated and s.supported, s.message


async def test_prefix_round_trip(nb: NetBoxClient) -> None:
    svc = PrefixService(nb)
    cidr = "198.51.100.0/26"
    for leftover in (await nb.page("ipam/prefixes/", {"prefix": cidr, "vrf_id": "null"})).results:
        await nb.delete(f"ipam/prefixes/{leftover['id']}/")
    for leftover in (await nb.page("ipam/ip-addresses/", {"parent": cidr, "vrf_id": "null"})).results:
        await nb.delete(f"ipam/ip-addresses/{leftover['id']}/")

    name = f"lanvex-it-{uuid.uuid4().hex[:6]}"
    v = await svc.validate(PrefixValidateRequest(prefix=cidr, gateway="198.51.100.1/26"))
    assert v.valid, v.findings

    created, warnings = await svc.create(PrefixCreate(prefix=cidr, name=name, gateway="198.51.100.1/26"))
    try:
        assert warnings == []
        assert created.prefix == cidr and created.name == name
        assert created.gateway and created.gateway.address == "198.51.100.1/26"
        assert created.utilization and created.utilization.used == 1 and created.utilization.total == 62

        dup = await svc.validate(PrefixValidateRequest(prefix=cidr))
        assert not dup.valid and any(f.code == "duplicate_prefix" for f in dup.findings)

        free = await svc.available_ips(created.id, limit=2)
        assert [a.address for a in free] == ["198.51.100.2/26", "198.51.100.3/26"]
    finally:
        if created.gateway:
            await nb.delete(f"ipam/ip-addresses/{created.gateway.ip_address_id}/")
        await nb.delete(f"ipam/prefixes/{created.id}/")
