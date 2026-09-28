"""Generate the CV1's narrowly patched NCS 2.9 DHCP source; never edit the SDK."""
import hashlib
import sys
from pathlib import Path

SDK_SHA256 = '1e2d73c26e1f71727c78ddb13f40095d62f8ffdcb06cb00a6c7cb833784858c3'
DECLARATION = 'const struct sockaddr *dns_servers[CONFIG_DNS_RESOLVER_MAX_SERVERS];'
CALL = 'status = dns_resolve_reconfigure(ctx, NULL, dns_servers);'
FIXED_CALL = '''/* The resolver counts multicast slots too. Always terminate the
             * DHCP list and keep mDNS alongside the router's DNS servers. */
            const char *multicast_servers[] = {
#if defined(CONFIG_MDNS_RESOLVER) && defined(CONFIG_NET_IPV4)
                "224.0.0.251:5353",
#endif
#if defined(CONFIG_MDNS_RESOLVER) && defined(CONFIG_NET_IPV6)
                "[ff02::fb]:5353",
#endif
                NULL,
            };
            status = dns_resolve_reconfigure(ctx, multicast_servers, dns_servers);'''


def patch_source(source):
    if source.count(DECLARATION) != 1 or source.count(CALL) != 1:
        raise ValueError('NCS DHCP source changed; review the DNS-list fix before building')
    return source.replace(DECLARATION,
        'const struct sockaddr *dns_servers[CONFIG_DNS_RESOLVER_MAX_SERVERS + 1] = { 0 };'
    ).replace(CALL, FIXED_CALL)


if __name__ == '__main__':
    source = Path(sys.argv[1]).read_bytes()
    if hashlib.sha256(source).hexdigest() != SDK_SHA256:
        raise SystemExit('Unsupported DHCP source revision; review the pinned NCS 2.9 patch')
    Path(sys.argv[2]).write_text(patch_source(source.decode()), encoding='utf-8')
