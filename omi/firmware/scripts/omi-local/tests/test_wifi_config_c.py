"""Execute the firmware parser and hold policy natively; no model of firmware behavior."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from omi_local import upload_protocol as U

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
HARNESS = r'''
#include "lib/core/wifi_config.h"
#include "lib/core/button_hold.h"
#include "lib/core/bulk_owner.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(int argc, char **argv) {
 if (!strcmp(argv[1], "owner")) {
  atomic_int owner = 0;
  if (!bulk_claim(&owner, BULK_BLE) || bulk_claim(&owner, BULK_WIFI)) return 3;
  bulk_release(&owner, BULK_WIFI);
  if (atomic_load(&owner) != BULK_BLE) return 4;
  bulk_release(&owner, BULK_BLE);
  if (!bulk_claim(&owner, BULK_WIFI) || bulk_claim(&owner, BULK_BLE)) return 5;
  bulk_release(&owner, BULK_WIFI);
  if (!bulk_claim(&owner, BULK_BLE)) return 6;
  puts("ok"); return 0;
 }
 if (!strcmp(argv[1], "hold")) {
  printf("%d", button_hold_action(atoi(argv[2]), atoi(argv[3]), atoi(argv[4]))); return 0;
 }
 struct wifi_upload_config c = {0}, old;
 unsigned char buf[1024]; size_t len = strlen(argv[2])/2;
 if (len > sizeof(buf)) return 2;
 for(size_t i=0;i<len;i++){unsigned v;sscanf(argv[2]+i*2,"%2x",&v);buf[i]=v;}
 old=c; int ret=wifi_config_parse(&c,buf,len);
 printf("%d %d %d %s",ret,wifi_config_valid(&c),memcmp(&c,&old,sizeof(c))==0,c.hostname);
 return 0;
}
'''

@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class WifiConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        (d/'lib/core').mkdir(parents=True)
        (d/'zephyr').mkdir()
        (d/'zephyr/toolchain.h').write_text('#define __packed __attribute__((packed))\n')
        for name in ['wifi_config.h','wifi_upload.h','button_hold.h','bulk_owner.h']:
            shutil.copy(SRC/'lib/core'/name, d/'lib/core'/name)
        shutil.copy(SRC/'wifi_config.c', d/'wifi_config.c')
        (d/'harness.c').write_text(HARNESS)
        cls.exe = str(d/'check')
        subprocess.run(['cc','-Wall','-Wextra','-I',str(d),str(d/'harness.c'),str(d/'wifi_config.c'),'-o',cls.exe],check=True)
    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()
    def run_c(self, *args):
        return subprocess.check_output([self.exe,*args],text=True).strip()
    def config(self, host='secondbrain.local'):
        return U.encode_wifi_config(ssid='Home',password='abcdefgh',host=host,port=7331,secret=bytes(range(32)),enabled=True)
    def test_hostname_and_ip_roundtrip(self):
        for host in ['secondbrain.local','192.168.1.50']:
            self.assertEqual(self.run_c('config',self.config(host).hex()),'0 1 0 '+host)
    def test_reject_entire_malformed_update(self):
        for suffix in [b'\x07\x01/',b'\x04\x02\x01',b'\xff\x00',b'\x06\x01\x02']:
            self.assertEqual(self.run_c('config',(self.config()+suffix).hex()),'-22 0 1')
    def test_invalid_hostname_and_missing_secret(self):
        for host in [b'-bad.local',b'a..local',b'a'*64+b'.local',b'a\x00.local']:
            self.assertEqual(self.run_c('config',(bytes([7,len(host)])+host).hex()),'-22 0 1')
        self.assertTrue(self.run_c('config',U.encode_wifi_config(host='valid.local').hex()).startswith('0 0 '))
    def test_deliberate_setup_and_power_off(self):
        for ms, released, wifi, action in [(100,1,1,0),(2999,0,1,0),(3000,0,1,0),
                (3000,1,1,1),(4999,1,1,1),(5000,0,1,2),(5000,1,1,0),(9000,0,1,2),(3000,0,0,1)]:
            self.assertEqual(self.run_c('hold',str(ms),str(released),str(wifi)),str(action))

    def test_bulk_ownership_survives_rejected_claim(self):
        self.assertEqual(self.run_c('owner'), 'ok')
