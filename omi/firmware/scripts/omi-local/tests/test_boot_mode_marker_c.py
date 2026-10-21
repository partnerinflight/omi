"""Execute the production boot sequence: a normal-mode marker precedes the first audio record."""
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
TRANSPORT = ['static bool put_tx_entry(', 'static bool write_to_tx_queue(', 'static bool read_from_tx_queue()',
             'static void emit_storage_record(', 'static bool emit_recording_end(', 'bool write_to_storage(void)',
             'void pusher(void)', 'static int queue_recording_marker(', 'int transport_end_recording(void)\n{',
             'int transport_mark_mode(uint8_t mode)\n{']
MAIN = ['static int mark_boot_mode(void)\n{', 'static int start_audio_pipeline(void)\n{']
# Single-threaded stand-in for the codec, pusher and SD threads: codec_mark_mode runs the
# mode callback the boot code registered (the real transport_mark_mode), and waiting for
# the marker runs the real pusher, which packs records into write_to_file.
PRELUDE = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <assert.h>
#include "lib/core/record_packer.h"
#define CONFIG_OMI_ENABLE_OFFLINE_STORAGE 1
#define CODEC_OUTPUT_MAX_BYTES 80
#define RING_BUFFER_HEADER_SIZE 2
#define MAX_WRITE_SIZE 440U
#define TX_ENTRY_MODE_MARKER 0xFFFFU
#define BOOT_MODE_MARKER_ATTEMPTS 50
#define BOOT_MODE_MARKER_RETRY_MS 100
#define K_FOREVER 0
#define K_MSEC(x) (x)
#define LOG_INF(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define LOG_ERR(...) ((void)0)
struct ring_buf {uint8_t data[82*16];uint32_t head,tail;};
static struct ring_buf ring_buf;
static uint8_t tx_buffer[82],tx_buffer_2[82],storage_temp_data[MAX_WRITE_SIZE];
static uint32_t tx_buffer_size;
static size_t buffer_offset;
static int tx_queue_sem,recording_end_done,pusher_stop_flag,recording_end_result,takes;
static bool storage_unavailable_warned,mic_started;
static int sd_not_ready,marks,retries,codec_errors,mic_errors,end_result=1;
typedef int (*codec_mode_callback)(uint8_t mode);
static codec_mode_callback mode_callback;
void pusher(void);
uint32_t ring_buf_put(struct ring_buf *r,const uint8_t *d,uint32_t n){if(r->head+n>sizeof(r->data))return 0;memcpy(r->data+r->head,d,n);r->head+=n;return n;}
uint32_t ring_buf_get(struct ring_buf *r,uint8_t *d,uint32_t n){if(r->tail+n>r->head)return 0;memcpy(d,r->data+r->tail,n);r->tail+=n;
 if(r->tail==r->head)r->tail=r->head=0;return n;}  /* drained: reuse the space */
void k_sem_give(int *s){(void)s;}
void k_sem_take(int *s,int t){(void)t;
 if(s==&recording_end_done){pusher_stop_flag=0;takes=0;pusher();return;}  /* the pusher writes the marker */
 if(takes++)pusher_stop_flag=1;}
void k_msleep(int ms){if(ms==BOOT_MODE_MARKER_RETRY_MS)retries++;else assert(ms==500);}
void k_sleep(int ms){(void)ms;assert(0);}  /* the TX queue never fills here */
int64_t k_uptime_get(void){return 0;}
int atomic_get(int *p){return *p;}
bool is_sd_on(void){return true;}
/* SD mounts asynchronously at boot; until then write_to_file drops records. */
uint32_t write_to_file(uint8_t *data,uint32_t length){assert(length==MAX_WRITE_SIZE);
 if(sd_not_ready){sd_not_ready--;return 0;}
 fwrite("\0\0\0\0",1,4,stdout);fwrite(data,1,length,stdout);return length;}
static int codec_handler(uint8_t *d,size_t n){(void)d;(void)n;return 0;}
static void mic_handler(int16_t *b){(void)b;}
void set_codec_callback(int (*cb)(uint8_t *,size_t)){assert(cb==codec_handler);}
void set_codec_mode_callback(codec_mode_callback cb){mode_callback=cb;}
int codec_start(void){return 0;}
int codec_mark_mode(uint8_t mode){assert(!mic_started && mode_callback);marks++;return mode_callback(mode);}
void set_mic_callback(void (*cb)(int16_t *)){assert(cb==mic_handler);}
void error_codec(void){codec_errors++;}
void error_microphone(void){mic_errors++;}
static bool write_to_tx_queue(uint8_t *data, size_t size);
int transport_end_recording(void);
/* The microphone produces one Opus frame, then VOX ends the recording. */
int mic_start(void){uint8_t a[50];memset(a,7,sizeof a);mic_started=true;
 assert(write_to_tx_queue(a,sizeof a));end_result=transport_end_recording();return 0;}
'''
HELPERS = r'''
int main(int argc,char **argv){
 assert(argc==2);sd_not_ready=atoi(argv[1]);
 assert(!start_audio_pipeline());
 assert(mic_started && !codec_errors && !mic_errors);
 fprintf(stderr,"%d %d %d\n",marks,retries,end_result);
 return 0;
}
'''

@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class BootModeMarkerTests(unittest.TestCase):
    def run_boot(self, sd_not_ready):
        source = (PRELUDE + ''.join(function(SRC/'lib/core/transport.c', n) for n in TRANSPORT)
                  + ''.join(function(SRC/'main.c', n) for n in MAIN) + HELPERS)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'test.c').write_text(source)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-function','-I',str(SRC),
                            str(root/'test.c'),'-o',str(root/'test')],check=True)
            done = subprocess.run([str(root/'test'), str(sd_not_ready)], check=True, capture_output=True)
        marks, retries, end_result = (int(v) for v in done.stderr.split())
        return list(P.iter_records(0, done.stdout)), marks, retries, end_result

    def test_normal_marker_precedes_first_audio_record_after_sd_mounts(self):
        records, marks, retries, end_result = self.run_boot(sd_not_ready=3)
        # Three attempts before the SD ring was ready, then the marker; audio only after it.
        self.assertEqual((marks, retries, end_result), (4, 3, 0))
        self.assertEqual([(r.mode, len(r.frames), r.recording_end) for r in records],
                         [('normal', 0, False), (None, 1, False), (None, 0, True)])
        self.assertEqual(records[1].frames, (b'\x07' * 50,))

    def test_boot_continues_when_the_sd_card_never_accepts_the_marker(self):
        records, marks, retries, _ = self.run_boot(sd_not_ready=1000)
        self.assertEqual((records, marks, retries), ([], 50, 50))


if __name__ == '__main__':
    unittest.main()
