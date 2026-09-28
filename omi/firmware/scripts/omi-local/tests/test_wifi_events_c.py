"""Execute the production callback across old and new Wi-Fi associations."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'


@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class WifiEventTests(unittest.TestCase):
    def test_new_connection_clears_only_previous_disconnect(self):
        source = (SRC / 'wifi_upload.c').read_text()
        start = source.index('static void wifi_mgmt_event_handler(')
        end = source.index('\n}\n', start) + 3
        harness = r'''
#include <stdint.h>
#include <stddef.h>
#include <assert.h>
#define ARG_UNUSED(x) ((void)(x))
#define NET_EVENT_WIFI_CONNECT_RESULT 1
#define NET_EVENT_WIFI_DISCONNECT_RESULT 2
struct net_if {int unused;};
struct net_mgmt_event_callback {void *info;};
struct wifi_status {int status;};
static int link_lost, connect_status, connect_sem;
void atomic_clear(int *p) {*p=0;}
void atomic_set(int *p,int v) {*p=v;}
void k_sem_give(int *p) {(*p)++;}
''' + source[start:end] + r'''
int main(void) {
 struct wifi_status status={0};
 struct net_mgmt_event_callback cb={&status};
 wifi_mgmt_event_handler(&cb,NET_EVENT_WIFI_DISCONNECT_RESULT,NULL);
 assert(link_lost);
 wifi_mgmt_event_handler(&cb,NET_EVENT_WIFI_CONNECT_RESULT,NULL);
 assert(!link_lost && connect_status==0 && connect_sem==1);
 wifi_mgmt_event_handler(&cb,NET_EVENT_WIFI_DISCONNECT_RESULT,NULL);
 assert(link_lost);
 status.status=-1;
 wifi_mgmt_event_handler(&cb,NET_EVENT_WIFI_CONNECT_RESULT,NULL);
 assert(link_lost && connect_status==-1);
 cb.info=NULL;
 wifi_mgmt_event_handler(&cb,NET_EVENT_WIFI_CONNECT_RESULT,NULL);
 assert(link_lost && connect_status==-1);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'test.c').write_text(harness)
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', str(root / 'test.c'),
                            '-o', str(root / 'test')], check=True)
            subprocess.run([str(root / 'test')], check=True)
