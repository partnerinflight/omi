"""Run production socket writes against bounded memory and partial-send seams."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
SRC=Path(__file__).resolve().parents[3]/'omi'/'src'

@unittest.skipUnless(shutil.which('cc'),'native C compiler required')
class SocketTests(unittest.TestCase):
    def test_bounded_writes_backpressure_timeout_and_disconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/'zephyr/net').mkdir(parents=True)
            (p/'zephyr/kernel.h').write_text('#include <stdint.h>\n#define K_MSEC(x) (x)\nint64_t k_uptime_get(void);\nvoid k_sleep(int ms);\n')
            (p/'zephyr/net/socket.h').write_text('#include <sys/types.h>\nssize_t zsock_send(int,const void *,size_t,int);\n')
            (p/'test.c').write_text(r'''
#include <assert.h>
#include <string.h>
#include "lib/core/wifi_socket.h"
static int64_t now;
static int calls, mode;
static size_t received;
static unsigned char payload[15984];
int64_t k_uptime_get(void) {return now;}
void k_sleep(int ms) {now+=ms;}
ssize_t zsock_send(int fd,const void *data,size_t len,int flags) {
 (void)fd;(void)flags;calls++;
 assert(len<=1024);
 if(mode==1) {errno=ENOBUFS;return -1;}
 if(mode==2) return 0;
 if(mode==3) {errno=EPIPE;return -1;}
 if(calls%3==1) {errno=ENOBUFS;return -1;}
 size_t n=len>333?333:len;
 assert(memcmp(data,payload+received,n)==0);received+=n;
 return n;
}
int main(void) {
 for(size_t i=0;i<sizeof(payload);i++)payload[i]=(unsigned char)i;
 assert(wifi_socket_send_all(0,payload,sizeof(payload))==0);
 assert(received==sizeof(payload));
 mode=1;now=0;
 assert(wifi_socket_send_all(0,payload,1)==-ETIMEDOUT);
 assert(now==WIFI_SOCKET_PROGRESS_TIMEOUT_MS);
 mode=2;assert(wifi_socket_send_all(0,payload,1)==-ECONNRESET);
 mode=3;assert(wifi_socket_send_all(0,payload,1)==-EPIPE);
 return 0;
}''')
            subprocess.run(['cc','-Wall','-Wextra','-I',str(p),'-I',str(SRC),str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True)
