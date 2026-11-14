"""Execute the production Wi-Fi teardown hang watch from wifi_upload.c.

Seen live on .22 (2026-11-13): a session that lost its link mid-upload never finished
teardown, so the uploader stayed "active" for ~14 hours, no later session ran and the
transfer LED stayed on. Each teardown call now records its step, and a hang on the dock
reboots the device after recording that step in GPREGRET[1].
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'


def function(path, name):
    text = path.read_text(); start = text.index(name)
    return text[start:text.index('\n}\n', start) + 3]


class TeardownWiringTests(unittest.TestCase):
    def test_every_teardown_call_records_its_step_inside_the_watch(self):
        source = (SRC / 'wifi_upload.c').read_text()
        out = source[source.index('\nout:\n'):]
        out = out[:out.index('\n}\n')]
        order = ['k_timer_start(&teardown_watch', 'TEARDOWN_STEP_SOCKET_CLOSE', 'zsock_close(',
                 'TEARDOWN_STEP_DHCP_STOP', 'net_dhcpv4_stop(', 'TEARDOWN_STEP_WIFI_DISCONNECT',
                 'NET_REQUEST_WIFI_DISCONNECT', 'TEARDOWN_STEP_RADIO_STOP', 'wifi_radio_stop(',
                 'TEARDOWN_STEP_RELEASE', 'storage_release_upload(', 'k_timer_stop(&teardown_watch)',
                 'TEARDOWN_STEP_NONE', 'atomic_clear(&active)']
        positions = [out.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions), 'teardown steps out of order')


@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class TeardownWatchTests(unittest.TestCase):
    def test_hang_reboots_only_on_the_dock_and_records_the_step(self):
        header = (SRC / 'lib/core/wifi_upload.h').read_text()
        start = header.index('enum wifi_teardown_step {')
        steps = header[start:header.index('};', start) + 2]
        names = ['static uint8_t hang_record_next(', 'static void teardown_watch_expired(struct k_timer *timer)\n{']
        source = r'''
#include <stdbool.h>
#include <stdint.h>
#include <assert.h>
#define ARG_UNUSED(x) ((void)(x))
#define LOG_ERR(...) ((void)0)
#define SYS_REBOOT_COLD 1
struct k_timer {int unused;};
static struct k_timer teardown_watch;
static struct { uint8_t teardown_step; } status;
static bool docked;
static int reboots;static uint8_t gpregret;
bool dock_power_present(void){return docked;}
uint8_t hang_record_read(void){return gpregret;}
void hang_record_write(uint8_t v){gpregret=v;}
void sys_reboot(int type){assert(type==SYS_REBOOT_COLD);reboots++;}
''' + steps + '\n'.join(function(SRC / 'wifi_upload.c', n) for n in names) + r'''
int main(void){
 /* Record: low nibble the hung step, high nibble a saturating count. */
 assert(hang_record_next(0,TEARDOWN_STEP_WIFI_DISCONNECT)==0x13);
 assert(hang_record_next(0x13,TEARDOWN_STEP_RADIO_STOP)==0x24);
 assert(hang_record_next(0xF3,TEARDOWN_STEP_DHCP_STOP)==0xF2);
 /* A teardown that finished just before expiry does nothing. */
 status.teardown_step=TEARDOWN_STEP_NONE;docked=true;teardown_watch_expired(&teardown_watch);
 assert(!reboots && !gpregret);
 /* Off the dock the mic may be recording (or a manual pause would be lost): keep waiting. */
 status.teardown_step=TEARDOWN_STEP_WIFI_DISCONNECT;docked=false;teardown_watch_expired(&teardown_watch);
 assert(!reboots && !gpregret);
 /* On the dock: record the step, then reboot. */
 docked=true;teardown_watch_expired(&teardown_watch);
 assert(reboots==1 && gpregret==0x13);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p / 'test.c').write_text(source)
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', str(p / 'test.c'), '-o', str(p / 'test')],
                           check=True)
            subprocess.run([str(p / 'test')], check=True)


if __name__ == '__main__':
    unittest.main()
