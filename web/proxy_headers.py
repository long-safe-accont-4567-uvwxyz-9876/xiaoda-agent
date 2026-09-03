"""Resolve client IPs across explicitly trusted reverse proxies."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable

from loguru import logger

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Preserve the local reverse-proxy default. Other private networks must be
# explicitly configured through RATE_LIMIT_TRUSTED_NETWORKS.
DEFAULT_TRUSTED_PROXY_NETWORKS = ("127.0.0.0/8", "::1/128")


def parse_trusted_proxy_networks(raw: str | None) -> list[IPNetwork]:
    """Parse a comma-separated trusted proxy IP/CIDR allowlist."""
    items: Iterable[str]
    if raw is None:
        items = DEFAULT_TRUSTED_PROXY_NETWORKS
    else:
        items = raw.split(",")

    networks: list[IPNetwork] = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("proxy_headers.trusted_network_invalid item={}", item)
    return networks


def _is_in_trusted_networks(address: IPAddress, networks: Iterable[IPNetwork]) -> bool:
    return any(address in network for network in networks)


def peer_is_trusted_proxy(
    peer: str,
    trusted_networks: Iterable[IPNetwork] | None = None,
) -> bool:
    """Return whether the socket peer belongs to a configured proxy CIDR."""
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    networks = trusted_networks
    if networks is None:
        networks = parse_trusted_proxy_networks(
            os.environ.get("RATE_LIMIT_TRUSTED_NETWORKS")
        )
    return _is_in_trusted_networks(address, networks)


def trust_forwarded_for() -> bool:
    """Return whether reverse-proxy forwarding headers are enabled."""
    env_val = os.getenv("TRUST_FORWARDED_FOR", "").strip().lower()
    if env_val in ("1", "true", "yes", "on"):
        return True
    try:
        from config import TRUST_FORWARDED_FOR as config_value

        return bool(config_value)
    except (ImportError, RuntimeError, ValueError):
        logger.debug("proxy_headers.trust_config_read_failed", exc_info=True)
        return False


def resolve_client_ip(
    peer: str,
    forwarded_for: str,
    *,
    trusted_networks: Iterable[IPNetwork] | None = None,
    trust_forwarded: bool | None = None,
) -> str:
    """Resolve the first untrusted address walking an XFF chain right-to-left.

    Forwarded headers are ignored unless the socket peer itself is in the
    configured proxy allowlist. Once trusted, only addresses in that same
    allowlist are skipped; private, loopback, or reserved status alone never
    makes an XFF hop a proxy.
    """
    networks = trusted_networks
    if networks is None:
        networks = parse_trusted_proxy_networks(
            os.environ.get("RATE_LIMIT_TRUSTED_NETWORKS")
        )

    enabled = trust_forwarded_for() if trust_forwarded is None else trust_forwarded
    if not enabled or not peer_is_trusted_proxy(peer, networks):
        return peer

    candidates = [item.strip() for item in forwarded_for.split(",") if item.strip()]
    for candidate in reversed(candidates):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            # A malformed hop on the trusted side makes the chain ambiguous.
            return peer
        if _is_in_trusted_networks(address, networks):
            continue
        return str(address)

    # An empty/all-proxy chain has no attributable client; use the socket peer.
    return peer
