"""Execute the production TX queue and pusher: markers stay in order and every
recording starts with its mode marker."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from omi_local import protocol as P

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
TRANSPORT = SRC / 'lib/core/transport.c'
def function(path, name):
    text = path.read_text(); start = text.index(name)
    return text[start:text.index('\n}\n', start) + 3]
def mode_state():
    # The pusher's mode state, with its production initial values (NORMAL, owed at boot).
    text = TRANSPORT.read_text(); start = text.index('static uint8_t current_mode')
    end = text.index('\n', text.index('static bool mode_owed', start))
    return text[start:end + 1]
NAMES = ['static bool put_tx_entry(', 'static bool write_to_tx_queue(', 'static bool read_from_tx_queue()',
         'static void emit_storage_record(', 'static bool emit_recording_end(', 'bool write_to_storage(void)',
         'static bool write_owed_mode(void)', 'void pusher(void)']
PRELUDE = r'''
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
static int tx_queue_sem,recording_end_done,pusher_stop_flag,recording_end_result,takes,results[16],markers;
static bool storage_unavailable_warned,sd_on=true;
static int sd_not_ready,lose;  /* writes refused (SD ring not mounted), writes accepted but lost */
uint32_t ring_buf_put(struct ring_buf *r,const uint8_t *d,uint32_t n){if(r->head+n>sizeof(r->data))return 0;memcpy(r->data+r->head,d,n);r->head+=n;return n;}
uint32_t ring_buf_get(struct ring_buf *r,uint8_t *d,uint32_t n){if(r->tail+n>r->head)return 0;memcpy(d,r->data+r->tail,n);r->tail+=n;
 if(r->tail==r->head)r->tail=r->head=0;return n;}
void k_sem_give(int *s){if(s==&recording_end_done)results[markers++]=recording_end_result;}
void k_sem_take(int *s,int t){(void)s;(void)t;if(takes++)pusher_stop_flag=1;}
void k_msleep(int ms){(void)ms;}
int atomic_get(int *p){return *p;}
bool is_sd_on(void){return sd_on;}
uint32_t write_to_file(uint8_t *data,uint32_t length){assert(length==MAX_WRITE_SIZE);
 if(sd_not_ready){sd_not_ready--;return 0;}
 if(lose){lose--;return length;}
 fwrite("\0\0\0\0",1,4,stdout);fwrite(data,1,length,stdout);return length;}
'''
HELPERS = r'''
static void A(size_t n,uint8_t fill){uint8_t f[80];memset(f,fill,n);assert(write_to_tx_queue(f,n));}
static void END(void){uint8_t none=0;assert(put_tx_entry(0,&none,0));}
static void MODE(uint8_t m){assert(put_tx_entry(TX_ENTRY_MODE_MARKER,&m,1));}
static void RUN(void){takes=0;pusher_stop_flag=0;pusher();}
'''

@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class PusherMarkerTests(unittest.TestCase):
    def run_pusher(self, body):
        source = (PRELUDE + mode_state() + ''.join(function(TRANSPORT, n) for n in NAMES) + HELPERS
                  + 'int main(void){\n' + body + '\n return 0;\n}\n')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'test.c').write_text(source)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-function','-I',str(SRC),str(root/'test.c'),'-o',str(root/'test')],check=True)
            data = subprocess.check_output([str(root/'test')])
        return list(P.iter_records(0, data))

    def shape(self, records):
        """'N'/'L' mode marker, 'E' end marker, 'a<frames>' audio record."""
        return [{'normal': 'N', 'lecture': 'L'}[r.mode] if r.mode else 'E' if r.recording_end
                else 'a%d' % len(r.frames) for r in records]

    def test_audio_end_and_mode_markers_reach_the_ring_in_order(self):
        records = self.run_pusher(r'''
 uint8_t a[100];memset(a,1,sizeof a);
 assert(!write_to_tx_queue(a,81));               /* longer than an Opus slot */
 A(80,1);A(40,2);END();MODE(RECORDING_MODE_LECTURE);A(30,3);END();MODE(RECORDING_MODE_NORMAL);
 RUN();
 assert(markers==4 && !buffer_offset);
 for(int i=0;i<markers;i++)assert(results[i]==0);
''')
        # Boot owes the normal marker, so the first recording starts with it; the owner's
        # explicit markers are written where they were queued, without duplicates.
        self.assertEqual(self.shape(records), ['N', 'a2', 'E', 'L', 'a1', 'E', 'N'])
        self.assertEqual(records[1].frames, (b'\x01' * 80, b'\x02' * 40))
        self.assertEqual(records[4].frames, (b'\x03' * 30,))

    def test_boot_marker_precedes_first_audio_even_while_sd_mounts(self):
        records = self.run_pusher(r'''
 sd_not_ready=3;                                 /* three refused marker attempts */
 for(uint8_t i=1;i<=5;i++)A(30,i);END();RUN();
 assert(markers==1 && !results[0] && !sd_not_ready);
''')
        # Frames 1-3 are dropped with their failed marker attempts; nothing precedes the marker.
        self.assertEqual(self.shape(records), ['N', 'a2', 'E'])
        self.assertEqual(records[1].frames, (b'\x04' * 30, b'\x05' * 30))

    def test_lost_marker_is_reemitted_at_the_next_recording_start(self):
        records = self.run_pusher(r'''
 A(30,1);END();RUN();
 lose=1;MODE(RECORDING_MODE_LECTURE);RUN();      /* accepted by write_to_file, then lost */
 assert(markers==2 && !results[1]);
 A(30,2);END();A(30,3);END();RUN();
''')
        self.assertEqual(self.shape(records), ['N', 'a1', 'E', 'a1', 'E', 'L', 'a1', 'E'])
        self.assertEqual(records[6].frames, (b'\x03' * 30,))

    def test_each_recording_after_an_end_marker_starts_with_the_current_mode(self):
        records = self.run_pusher(r'''
 /* A lecture paused and resumed without a fresh marker is still a lecture. */
 MODE(RECORDING_MODE_LECTURE);A(30,1);END();A(30,2);END();
 /* Exit: the explicit normal marker is not duplicated; later recordings stay normal. */
 MODE(RECORDING_MODE_NORMAL);A(30,3);END();A(30,4);END();RUN();
 /* A refused lecture marker leaves the device, and the next recording, in normal mode. */
 sd_on=false;MODE(RECORDING_MODE_LECTURE);RUN();assert(results[markers-1]==-EIO);sd_on=true;
 A(30,5);END();RUN();
''')
        self.assertEqual(self.shape(records),
                         ['L', 'a1', 'E', 'L', 'a1', 'E', 'N', 'a1', 'E', 'N', 'a1', 'E', 'N', 'a1', 'E'])
