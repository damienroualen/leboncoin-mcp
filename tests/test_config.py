from leboncoin_mcp.config import LOCAL_HOSTS, parse_hosts
from leboncoin_mcp.server import transport_security


def test_only_local_hosts_by_default():
    assert parse_hosts(None) == LOCAL_HOSTS
    assert parse_hosts(" , ") == LOCAL_HOSTS


def test_extra_hosts_are_added_to_local_ones():
    assert parse_hosts("mcp.example.org, 10.0.0.5:*") == LOCAL_HOSTS + ("mcp.example.org", "10.0.0.5:*")


def test_transport_security_is_always_enabled():
    security = transport_security(("localhost:*",))
    assert security.enable_dns_rebinding_protection
    assert security.allowed_origins == ["http://localhost:*", "https://localhost:*"]
