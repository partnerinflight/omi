"""Execute the production TX queue and pusher: end and mode markers stay in order."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from omi_local import protocol as P

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
def function(path, name):
    text = path.read_text(); start = text.index(name)
    return text[start:text.index('\n}\n', start) + 3]
NAMES = ['static bool put_tx_entry(', 'static bool write_to_tx_queue(', 'static bool read_from_tx_queue()',
         'static void emit_storage_record(', 'static bool emit_recording_end(', 'bool write_to_storage(void)',
         'void pusher(void)']

@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class PusherMarkerTests(unittest.TestCase):
    def test_audio_end_and_mode_markers_reach_the_ring_in_order(self):
        source = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
#include <stdio.h>
#include <assert.h>
#include "lib/core/record_packer.h"
#define CONFIG_OMI_ENABLE_OFFLINE_STORAGE 1
#define CODEC_OUTPUT_MAX_BYTES 80
#define RING_BUFFER_HEADER_SIZE 2
#define MAX_WRITE_SIZE 440U
#define TX_ENTRY_MODE_MARKER 0xFFFFU
#define K_FOREVER 0
#define LOG_WRN(...) ((void)0)
struct ring_buf {uint8_t data[82*16];uint32_t head,tail;};
static struct ring_buf ring_buf;
static uint8_t tx_buffer[82],tx_buffer_2[82],storage_temp_data[MAX_WRITE_SIZE];
static uint32_t tx_buffer_size;
static size_t buffer_offset;
static int tx_queue_sem,recording_end_done,pusher_stop_flag,recording_end_result,takes,results[8],markers;
static bool storage_unavailable_warned;
uint32_t ring_buf_put(struct ring_buf *r,const uint8_t *d,uint32_t n){if(r->head+n>sizeof(r->data))return 0;memcpy(r->data+r->head,d,n);r->head+=n;return n;}
uint32_t ring_buf_get(struct ring_buf *r,uint8_t *d,uint32_t n){if(r->tail+n>r->head)return 0;memcpy(d,r->data+r->tail,n);r->tail+=n;return n;}
void k_sem_give(int *s){if(s==&recording_end_done)results[markers++]=recording_end_result;}
void k_sem_take(int *s,int t){(void)s;(void)t;if(takes++)pusher_stop_flag=1;}
void k_msleep(int ms){(void)ms;}
int atomic_get(int *p){return *p;}
bool is_sd_on(void){return true;}
uint32_t write_to_file(uint8_t *data,uint32_t length){assert(length==MAX_WRITE_SIZE);fwrite("\0\0\0\0",1,4,stdout);fwrite(data,1,length,stdout);return length;}
''' + ''.join(function(SRC/'lib/core/transport.c', n) for n in NAMES) + r'''
int main(void){
 uint8_t a[100],b[40],c[30],none=0,mode;
 memset(a,1,sizeof a);memset(b,2,sizeof b);memset(c,3,sizeof c);
 assert(!write_to_tx_queue(a,81));               /* longer than an Opus slot */
 assert(write_to_tx_queue(a,80) && write_to_tx_queue(b,40));
 assert(put_tx_entry(0,&none,0));                /* end of the normal recording */
 mode=RECORDING_MODE_LECTURE;assert(put_tx_entry(TX_ENTRY_MODE_MARKER,&mode,1));
 assert(write_to_tx_queue(c,30));
 assert(put_tx_entry(0,&none,0));
 mode=RECORDING_MODE_NORMAL;assert(put_tx_entry(TX_ENTRY_MODE_MARKER,&mode,1));
 pusher();
 assert(markers==4 && !buffer_offset);
 for(int i=0;i<markers;i++)assert(results[i]==0);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'test.c').write_text(source)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-I',str(SRC),str(root/'test.c'),'-o',str(root/'test')],check=True)
            data = subprocess.check_output([str(root/'test')])
        records = list(P.iter_records(0, data))
        self.assertEqual([(len(r.frames), r.recording_end, r.mode) for r in records],
                         [(2, False, None), (0, True, None), (0, False, 'lecture'),
                          (1, False, None), (0, True, None), (0, False, 'normal')])
        self.assertEqual(records[0].frames, (b'\x01' * 80, b'\x02' * 40))
        self.assertEqual(records[3].frames, (b'\x03' * 30,))
