"""Execute production silence timing, codec drain and recording-end packing."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from omi_local import protocol as P
SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
def function(path, name):
    source = path.read_text(); start = source.index(name)
    return source[start:source.index('\n}\n', start) + 3]
@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class VoxTests(unittest.TestCase):
    def compile_run(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'test.c').write_text(source)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-I',str(SRC),str(root/'test.c'),
                            str(SRC/'lib/core/vox_filter.c'),'-o',str(root/'test'),'-lm'],check=True)
            return subprocess.check_output([str(root/'test')])
    def test_speech_band_sustain_hold_and_wake_reset(self):
        self.compile_run(r'''
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <math.h>
#include <assert.h>
#include "lib/core/vox_filter.h"
#define CONFIG_OMI_VAD_ABS_THRESHOLD 150
#define CONFIG_OMI_VAD_SUSTAIN_BLOCKS 3
#define CONFIG_OMI_VAD_WINDOW_BLOCKS 5
#define CONFIG_OMI_VAD_HOLD_MS 30000
#define PI 3.14159265358979
static int aad_woke,aad_in_sleep,aad_req_sleep,aad_sem;
static int64_t aad_last_voice_ms,now;
static bool syncing;
static struct vox_filter vox;
int64_t k_uptime_get(void){return now;}
int atomic_get(int *p){return *p;}
void atomic_set(int *p,int v){*p=v;}
bool atomic_cas(int *p,int a,int b){if(*p!=a)return false;*p=b;return true;}
void k_sem_give(int *p){(*p)++;}
bool storage_transfer_active(void){return syncing;}
''' + function(SRC/'mic.c','static void aad_track_silence(const int16_t *buf, size_t n)\n{') + r'''
static int16_t quiet[1600], speech[1600], hum[1600];
static void block(const int16_t *b, int64_t t){now=t;aad_track_silence(b,1600);}
int main(void){
 for(int i=0;i<1600;i++){
  speech[i]=(int16_t)(600*sin(2*PI*1000*i/16000.0));   /* ~380 after the filter */
  hum[i]=(int16_t)(1000*sin(2*PI*100*i/16000.0));      /* ~100 after the filter */
 }
 for(int k=0;k<10;k++) block(hum,100+k*100);          /* loud low-frequency sound never counts */
 assert(aad_last_voice_ms==0);
 block(speech,2000); block(speech,2100); assert(aad_last_voice_ms==0);   /* 2 of 5 */
 block(speech,2200); assert(aad_last_voice_ms==2200);                    /* 3 of 5 */
 block(quiet,2300); block(quiet,2400); assert(aad_last_voice_ms==2400);  /* still 3 of 5 */
 block(quiet,2500); assert(aad_last_voice_ms==2400);                     /* 2 of 5 */
 now=2400+29999; aad_track_silence(quiet,1600); assert(!aad_req_sleep);
 now=2400+30000; aad_track_silence(quiet,1600); assert(aad_req_sleep && aad_sem==1);
 aad_req_sleep=0;
 block(speech,100000); block(speech,100100);          /* 2 loud blocks in history */
 aad_woke=1; block(speech,100200);                    /* wake: reset -> history 1 of 5, timer refreshed by the wake itself */
 assert(aad_last_voice_ms==100200);
 block(speech,100300); assert(aad_last_voice_ms==100200); /* only 2 of 5 since the reset; without the reset it would be 4 */
 aad_req_sleep=0; /* the first loud block after the long silence asked for sleep */
 syncing=true; block(quiet,200000); assert(!aad_req_sleep);
 syncing=false; block(quiet,200100); assert(aad_req_sleep);
 return 0;
}
''')
    def test_codec_order_partial_padding_and_error(self):
        self.compile_run(r'''
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <string.h>
#include <errno.h>
#include <assert.h>
#include <setjmp.h>
#define CODEC_PACKAGE_SAMPLES 4
#define CODEC_OPUS 1
#define OPUS_RESET_STATE 99
#define K_FOREVER 0
static jmp_buf stop;
static int codec_data_sem,codec_end_done,codec_end_requested,codec_end_result;
static int codec_ring_buf,m_opus_state,resets,callbacks,ends,fail_encode,fail_end,takes;
static uint8_t pcm[12],codec_output_bytes[8];
static size_t left;
static int16_t codec_input_samples[4];
void k_sem_take(int *s,int t){(void)s;(void)t;if(takes++)longjmp(stop,1);}
void k_sem_give(int *s){assert(s==&codec_end_done);}
bool atomic_cas(int *p,int a,int b){if(*p!=a)return false;*p=b;return true;}
size_t ring_buf_size_get(int *r){(void)r;return left;}
size_t ring_buf_get(int *r,uint8_t *out,size_t n){(void)r;assert(n<=left);memcpy(out,pcm+12-left,n);left-=n;return n;}
uint16_t execute_codec(void){if(fail_encode)return 0;if(left==0)assert(codec_input_samples[2]==0 && codec_input_samples[3]==0);return 3;}
int callback(uint8_t *data,size_t len){(void)data;if(len){assert(!ends);callbacks++;}else{assert(left==0);ends++;}return fail_end?-EIO:0;}
static int (*_callback)(uint8_t *,size_t)=callback;
void opus_encoder_ctl(int s,int op){(void)s;assert(op==99);resets++;}
''' + function(SRC/'lib/core/codec.c','void codec_entry()') + r'''
int main(void){
 memset(pcm,1,sizeof(pcm));left=12;codec_end_requested=1;
 if(!setjmp(stop))codec_entry();
 assert(callbacks==2 && ends==1 && resets==1 && codec_end_result==0);
 takes=callbacks=ends=resets=0;left=12;codec_end_requested=1;fail_end=1;
 if(!setjmp(stop))codec_entry();
 assert(callbacks==2 && ends==1 && resets==0 && codec_end_result==-EIO);
 takes=callbacks=ends=0;left=8;codec_end_requested=0;fail_encode=1;
 if(!setjmp(stop))codec_entry();
 assert(callbacks==0 && ends==0 && left==0);return 0;
}
''')
    def test_partial_record_then_marker_and_failed_emit_retry(self):
        data=self.compile_run(r'''
#include "lib/core/record_packer.h"
#include <assert.h>
#include <stdio.h>
static int fail=1;
static bool emit(const uint8_t *p,size_t n){if(fail){fail=0;return false;}fwrite("\0\0\0\0",1,4,stdout);fwrite(p,1,n,stdout);return true;}
int main(void){uint8_t record[440]={3,0xb8,1,2};size_t used=4;
 assert(!record_packer_end(record,440,&used,emit));assert(used==4);
 assert(record_packer_end(record,440,&used,emit));assert(used==0);return 0;}
''')
        records=list(P.iter_records(0,data));self.assertEqual(len(records),2)
        self.assertEqual(records[0].frames,(b'\xb8\x01\x02',));self.assertFalse(records[0].recording_end)
        self.assertTrue(records[1].recording_end)
