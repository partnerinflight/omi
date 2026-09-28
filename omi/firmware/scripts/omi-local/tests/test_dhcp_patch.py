"""The generated SDK call must supply bounded DNS lists, including mDNS."""
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'patch-ncs290-dhcp.py'
spec = importlib.util.spec_from_file_location('dhcp_patch', SCRIPT)
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)

class DhcpPatchTests(unittest.TestCase):
    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError): patch.patch_source('unrelated source')
        with self.assertRaises(ValueError): patch.patch_source((patch.DECLARATION + patch.CALL) * 2)

    @unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
    def test_full_dns_list_is_terminated_with_mdns_enabled(self):
        # Exercise the exact generated declaration/call. NCS supplies a full
        # MAX_SERVERS array, while its resolver scans MAX_SERVERS + multicast.
        for count in (1, 2):
            source = r'''
#include <assert.h>
#include <stddef.h>
#include <string.h>
#define CONFIG_NET_IPV4 1
#define CONFIG_MDNS_RESOLVER 1
struct sockaddr { int family; };
static int dns_resolve_reconfigure(void *ctx,const char **names,const struct sockaddr **addresses) {
 (void)ctx;
 assert(!strcmp(names[0],"224.0.0.251:5353") && names[1]==NULL);
 int n=0;
 while(n<CONFIG_DNS_RESOLVER_MAX_SERVERS+1 && addresses[n]) {
  assert(addresses[n]->family==2);n++;
 }
 assert(n==CONFIG_DNS_RESOLVER_MAX_SERVERS);
 assert(addresses[n]==NULL);
 return 0;
}
int main(void) {
 void *ctx=NULL; int status;
 struct sockaddr storage[CONFIG_DNS_RESOLVER_MAX_SERVERS];
''' + patch.DECLARATION + r'''
 for(int i=0;i<CONFIG_DNS_RESOLVER_MAX_SERVERS;i++) {
  storage[i].family=2;dns_servers[i]=&storage[i];
 }
''' + patch.CALL + '\nreturn status;\n}\n'
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp)
                (p/'test.c').write_text(patch.patch_source(source))
                subprocess.run(['cc','-Wall','-Wextra','-fsanitize=address,undefined',
                    '-DCONFIG_DNS_RESOLVER_MAX_SERVERS='+str(count),str(p/'test.c'),'-o',str(p/'test')],check=True)
                subprocess.run([str(p/'test')],check=True)
