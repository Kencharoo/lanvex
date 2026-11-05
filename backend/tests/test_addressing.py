import ipaddress

import pytest

from lanvex.ipam import addressing as a

N = ipaddress.ip_network
I = ipaddress.ip_interface  # noqa: E741


class TestParseNetwork:
    @pytest.mark.parametrize("value", ["10.0.0.0/24", "192.168.1.0/30", "2001:db8::/48", "0.0.0.0/0", "::/0"])
    def test_valid(self, value: str) -> None:
        assert str(a.parse_network(value)) == str(N(value))

    def test_strips_whitespace(self) -> None:
        assert str(a.parse_network("  10.1.0.0/16 ")) == "10.1.0.0/16"

    def test_host_bits_set_suggests_network(self) -> None:
        with pytest.raises(a.AddressError) as e:
            a.parse_network("10.0.0.5/24")
        assert e.value.suggestion == "10.0.0.0/24"
        assert "host bits" in e.value.message

    def test_ipv6_host_bits(self) -> None:
        with pytest.raises(a.AddressError) as e:
            a.parse_network("2001:db8::1/64")
        assert e.value.suggestion == "2001:db8::/64"

    @pytest.mark.parametrize(
        "value", ["", "10.0.0.0", "10.0.0.0/33", "300.0.0.0/8", "hello/24", "2001:db8::/129"]
    )
    def test_invalid(self, value: str) -> None:
        with pytest.raises(a.AddressError):
            a.parse_network(value)


class TestUsableBounds:
    def test_ipv4_24_excludes_network_and_broadcast(self) -> None:
        first, last = a.usable_bounds(N("10.0.0.0/24"))
        assert (str(ipaddress.ip_address(first)), str(ipaddress.ip_address(last))) == (
            "10.0.0.1",
            "10.0.0.254",
        )

    @pytest.mark.parametrize("cidr,count", [("10.0.0.0/31", 2), ("10.0.0.7/32", 1)])
    def test_ipv4_point_to_point_and_host(self, cidr: str, count: int) -> None:
        first, last = a.usable_bounds(N(cidr))
        assert last - first + 1 == count

    def test_ipv4_pool_is_fully_usable(self) -> None:
        first, last = a.usable_bounds(N("10.0.0.0/24"), is_pool=True)
        assert last - first + 1 == 256

    def test_ipv6_64_excludes_only_subnet_router_anycast(self) -> None:
        net = N("2001:db8::/64")
        first, last = a.usable_bounds(net)
        assert first == int(net.network_address) + 1
        assert last == int(net.broadcast_address)

    @pytest.mark.parametrize("cidr,count", [("2001:db8::/127", 2), ("2001:db8::1/128", 1)])
    def test_ipv6_small(self, cidr: str, count: int) -> None:
        first, last = a.usable_bounds(N(cidr))
        assert last - first + 1 == count


class TestUtilization:
    def test_denominator_matches_netbox(self) -> None:
        assert a.utilization_denominator(N("10.0.0.0/24")) == 254
        assert a.utilization_denominator(N("10.0.0.0/24"), is_pool=True) == 256
        assert a.utilization_denominator(N("10.0.0.0/31")) == 2
        assert a.utilization_denominator(N("2001:db8::/120")) == 256  # IPv6 uses full size

    def test_address_utilization(self) -> None:
        u = a.address_utilization(N("10.0.0.0/24"), False, ip_count=127)
        assert u.used == 127 and u.total == 254 and u.percent == pytest.approx(50.0)
        assert u.basis == "ip_addresses"

    def test_address_utilization_caps_at_100(self) -> None:
        u = a.address_utilization(N("10.0.0.0/30"), False, ip_count=10)
        assert u.percent == 100.0 and u.used == u.total == 2

    def test_utilized_ranges_are_added(self) -> None:
        u = a.address_utilization(N("10.0.0.0/24"), False, ip_count=4, utilized_range_size=50)
        assert u.used == 54

    def test_container_counts_overlapping_children_once(self) -> None:
        children = [N("10.0.0.0/25"), N("10.0.0.0/26"), N("10.0.0.128/26")]
        u = a.container_utilization(N("10.0.0.0/24"), children)
        assert u.used == 192 and u.percent == pytest.approx(75.0)

    def test_container_ignores_self_other_family_and_outside(self) -> None:
        children = [N("10.0.0.0/24"), N("2001:db8::/64"), N("10.9.0.0/24"), N("10.0.0.0/26")]
        u = a.container_utilization(N("10.0.0.0/24"), children)
        assert u.used == 64

    def test_marked_utilized(self) -> None:
        u = a.marked_utilized(N("10.0.0.0/24"), False)
        assert u.percent == 100.0 and u.basis == "marked_utilized"


class TestOverlap:
    @pytest.mark.parametrize(
        "candidate,existing,expected",
        [
            ("10.0.0.0/24", "10.0.0.0/24", "duplicate"),
            ("10.0.0.0/24", "10.0.0.0/16", "parent"),
            ("10.0.0.0/16", "10.0.5.0/24", "child"),
            ("10.0.0.0/24", "10.0.1.0/24", None),
            ("10.0.0.0/24", "2001:db8::/32", None),
            ("2001:db8:1::/48", "2001:db8::/32", "parent"),
        ],
    )
    def test_classify(self, candidate: str, existing: str, expected: str | None) -> None:
        assert a.classify_overlap(N(candidate), N(existing)) == expected


class TestGateway:
    def test_suggest(self) -> None:
        assert a.suggest_gateway(N("10.0.0.0/24")) == "10.0.0.1/24"
        assert a.suggest_gateway(N("2001:db8::/64")) == "2001:db8::1/64"
        assert a.suggest_gateway(N("10.0.0.0/31")) is None

    def test_valid_host(self) -> None:
        assert a.address_in_prefix(I("10.0.0.1/24"), N("10.0.0.0/24")) is None

    @pytest.mark.parametrize(
        "address,fragment",
        [
            ("10.0.0.0/24", "network address"),
            ("10.0.0.255/24", "broadcast address"),
            ("10.0.1.1/24", "outside"),
            ("10.0.0.1/16", "mask length"),
            ("2001:db8::1/64", "IPv6"),
        ],
    )
    def test_invalid_host(self, address: str, fragment: str) -> None:
        problem = a.address_in_prefix(I(address), N("10.0.0.0/24"))
        assert problem and fragment in problem

    def test_pool_allows_network_address(self) -> None:
        assert a.address_in_prefix(I("10.0.0.0/24"), N("10.0.0.0/24"), is_pool=True) is None

    def test_parse_interface_requires_mask(self) -> None:
        with pytest.raises(a.AddressError):
            a.parse_interface("10.0.0.1")
