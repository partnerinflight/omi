"""SD power ownership across Bluetooth disconnects."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
SRC=Path(__file__).resolve().parents[3]/'omi'/'src'
def function(path,name):
    text=path.read_text();start=text.index(name)
    return text[start:text.index('\n}\n',start)+3]
@unittest.skipUnless(shutil.which('cc'),'native C compiler required')
class DisconnectPowerTests(unittest.TestCase):
    def compile_run(self,source):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'test.c').write_text(source)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-I',str(SRC),str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True)
    def test_ble_disconnect_preserves_sd_for_upload(self):
        self.compile_run(r"""
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <assert.h>
#define CONFIG_OMI_ENABLE_OFFLINE_STORAGE 1
#define CONFIG_SHELL_BT_NUS 0
#define CONFIG_BT_CONN_TX_MAX 10
#define BULK_TX_RESERVED_SLOTS 2
#define IS_ENABLED(x) (x)
#define LOG_INF(...) ((void)0)
struct bt_conn {int unused;};
static bool is_connected,storage_is_on,asleep,paused,upload,sd=true;
static int mtu_recheck_work,mtu_recheck_attempts,current_mtu,charging_status_last_notified,bulk_tx_sem;
static struct bt_conn *current_connection;
void k_work_cancel_delayable(int *p){(void)p;}
void shell_bt_nus_disable(void){}
void sd_notify_ble_state(bool on){assert(!on);}
bool mic_in_aad_sleep(void){return asleep;}
bool mic_is_manually_paused(void){return paused;}
bool wifi_upload_active(void){return upload;}
void sd_request_power(bool on){sd=on;}
void bt_conn_unref(struct bt_conn *c){(void)c;}
void k_sem_init(int *p,int initial,int limit){(void)p;assert(initial==8 && limit==8);}
""" + function(SRC/'lib/core/transport.c','static void _transport_disconnected(') + r"""
int main(void){
 for(int a=0;a<2;a++){for(int p=0;p<2;p++){for(int u=0;u<2;u++){
 asleep=a;paused=p;upload=u;sd=true;is_connected=true;
 _transport_disconnected(NULL,0);assert(!is_connected);
 assert(sd==(!(a||p)||u));
 }}}
 return 0;}
""")
