"""Execute the production gauge and battery work handler through native seams."""
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from omi_local import protocol as P, upload_protocol as U

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'

@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class BatteryTests(unittest.TestCase):
    def compile_run(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p/'test.c').write_text(source)
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-I', str(SRC), str(p/'test.c'), '-o', str(p/'test')], check=True)
            subprocess.run([str(p/'test')], check=True)

    def test_small_changes_converge_and_do_not_reverse(self):
        self.compile_run(r'''
#include "lib/core/battery_filter.h"
#include <assert.h>
int main(void) {
 for (int raw=2; raw<=100; raw++) {
  struct battery_filter f={0};
  assert(battery_filter_update(&f,1,true)==1);
  for(int i=0;i<100;i++) battery_filter_update(&f,raw,true);
  assert(battery_filter_update(&f,raw,true)==raw);
  assert(battery_filter_update(&f,0,true)==raw);
  for(int i=0;i<100;i++) battery_filter_update(&f,0,false);
  assert(battery_filter_update(&f,0,false)==0);
  assert(battery_filter_update(&f,100,false)==0);
 }
 return 0;
}''')

    def test_bluetooth_value_updates_while_disconnected(self):
        source = (SRC/'lib/core/transport.c').read_text()
        start = source.index('void broadcast_battery_level(struct k_work *work_item)\n{')
        end = source.index('\n}\n', start) + 3
        self.compile_run(r'''
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <assert.h>
struct k_work {int unused;};
typedef int k_spinlock_key_t;
static int battery_snapshot_lock, battery_work;
static uint8_t battery_percentage, bas, charging_notifications;
static uint16_t battery_last_mv;
static int64_t battery_sample_ms;
static int32_t battery_sample_error;
static bool is_connected;
static void *current_connection;
#define BATTERY_REFRESH_INTERVAL_CONNECTED 5000
#define BATTERY_REFRESH_INTERVAL_DISCONNECTED 10000
#define CONFIG_OMI_BATTERY_CRITICAL_MV 3500
#define LOG_PRINTK(...) ((void)0)
#define LOG_ERR(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define K_MSEC(x) (x)
static int refresh_ms;
static int sample_error;
int battery_get_millivolt(uint16_t *v) {*v=4100;return sample_error;}
int battery_get_percentage(uint8_t *p,uint16_t v) {(void)v;*p=80;return 0;}
int k_spin_lock(int *l) {(void)l;return 0;}
void k_spin_unlock(int *l,int k) {(void)l;(void)k;}
int64_t k_uptime_get(void) {return 12345;}
int bt_bas_set_battery_level(uint8_t p) {bas=p;return 0;}
int notify_charging_status(void *c,bool f) {(void)c;(void)f;charging_notifications++;return 0;}
void turnoff_all(void) {assert(0);}
void k_work_reschedule(int *w,int ms) {(void)w;refresh_ms=ms;}
''' + source[start:end] + r'''
int main(void) {
 broadcast_battery_level(0);
 assert(bas==80 && battery_last_mv==4100 && battery_sample_ms==12345);
 assert(!battery_sample_error && !charging_notifications && refresh_ms==10000);
 is_connected=true;current_connection=&battery_work;
 broadcast_battery_level(0);
 assert(bas==80 && charging_notifications==1 && refresh_ms==5000);
 sample_error=-5;bas=77;
 broadcast_battery_level(0);
 assert(bas==77 && battery_sample_error==-5 && battery_sample_ms==12345);
 return 0;
}''')

class DiagnosticsTests(unittest.TestCase):
    def test_battery_diagnostics_and_invalid_length(self):
        st=P.parse_battery_diagnostics(struct.pack('<HBBIi',4100,80,1,250,-5))
        self.assertEqual((st.millivolts,st.percentage,st.charging,st.sample_age_ms,st.error),(4100,80,True,250,-5))
        with self.assertRaises(P.ProtocolError): P.parse_battery_diagnostics(b'\0'*11)

    def test_extended_dhcp_status(self):
        base=struct.pack('<BBBbiIIIII',1,4,4,0,-116,0,0,10,50000,70000)
        self.assertIsNone(U.parse_upload_status(base).dhcp_state)
        st=U.parse_upload_status(base+struct.pack('<BBH4B',3,2,0,192,168,1,42))
        self.assertEqual((st.dhcp_state,st.dhcp_attempts,st.ipv4),(3,2,'192.168.1.42'))
