"""Production microphone owner transitions, pause privacy and wake feedback."""
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
class ManualRecordingTests(unittest.TestCase):
    def compile_run(self,source):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'test.c').write_text(source)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-I',str(SRC),str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True)
    def test_owner_serializes_pause_wake_resume_and_failures(self):
        names=['int mic_toggle_manual_pause(void)','bool mic_is_manually_paused(void)',
               'bool mic_manual_pause_led_on(void)','static void exit_hw_aad(void)',
               'static void enter_manual_pause(void)','static void exit_manual_pause(void)',
               'static void aad_thread_fn(void *p1, void *p2, void *p3)\n{']
        self.compile_run(r'''
#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
#include <errno.h>
#include <setjmp.h>
#include <stddef.h>
#define CONFIG_OMI_ENABLE_T5838_AAD 1
#define CONFIG_OMI_ENABLE_HAPTIC 1
#define K_FOREVER 0
#define AAD_PDM_SETTLE_MS 20
#define CONFIG_OMI_AAD_WAKE_HAPTIC_MIN_SLEEP_MS 300000
#define MIC_START_DISCARD_BLOCKS 5
#define ARG_UNUSED(x) ((void)(x))
#define LOG_INF(...) ((void)0)
#define LOG_ERR(...) ((void)0)
static int aad_sem,aad_in_sleep,aad_woke,aad_wake_pending,aad_req_sleep;
static int manual_pause_requested,manual_paused,mic_discard_blocks;
static uint32_t manual_pause_started_ms,aad_sleep_started_ms,now;
static bool manual_end_pending,aad_thread_started=true,is_connected,upload;
static bool running=true,rail=true,irq,sd=true,race_pause;
static int flushes,flush_error,resume_error,pause_error,buzzes,disabled;
static jmp_buf stop;static int steps;
int atomic_get(int *p){return *p;}
void atomic_set(int *p,int v){*p=v;}
void atomic_clear(int *p){*p=0;}
void atomic_xor(int *p,int v){*p^=v;}
bool atomic_cas(int *p,int a,int b){if(*p!=a)return false;*p=b;return true;}
void k_sem_give(int *s){(void)s;}
void k_sem_take(int *s,int t){(void)s;(void)t;if(steps++)longjmp(stop,1);}
void k_msleep(int ms){assert(ms==20);}
uint32_t k_uptime_get_32(void){return now;}
void aad_wake_irq(bool on){irq=on;}
void pdm_hw_disable(void){assert(!running);disabled++;}
void t5838_aad_power(bool on){rail=on;}
void t5838_aad_release_clk(void){}
void t5838_aad_enter(void){assert(!running);}
void sd_request_power(bool on){sd=on;}
bool wifi_upload_active(void){return upload;}
int mic_pause(void){running=false;return pause_error;}
int codec_end_recording(void){assert(!running);flushes++;return flush_error;}
int mic_resume(void){assert(mic_discard_blocks==5);mic_discard_blocks=0;if(resume_error)return resume_error;assert(rail);running=true;return 0;}
void play_haptic_milli(int ms){assert(ms==80 && running);buzzes++;}
void enter_hw_aad(void){running=false;aad_in_sleep=1;if(race_pause)manual_pause_requested=1;}
''' + '\n'.join(function(SRC/'mic.c',n) for n in names) + r'''
void step(void){steps=0;if(!setjmp(stop))aad_thread_fn(NULL,NULL,NULL);}
int main(void){
 aad_thread_started=false;assert(mic_toggle_manual_pause()==-EAGAIN);aad_thread_started=true;
 now=1000;aad_wake_pending=1;
 assert(!mic_toggle_manual_pause());step();
 assert(manual_paused && !running && !rail && !irq && flushes==1 && !sd && !buzzes);
 for(int i=0;i<3;i++){aad_wake_pending=1;aad_req_sleep=1;step();assert(!running && !rail && !buzzes);}
 assert(mic_manual_pause_led_on());now=1199;assert(mic_manual_pause_led_on());
 now=1200;assert(!mic_manual_pause_led_on());now=3999;assert(!mic_manual_pause_led_on());
 now=4000;assert(mic_manual_pause_led_on());now=4200;assert(!mic_manual_pause_led_on());
 manual_pause_started_ms=UINT32_MAX-100;now=99;assert(!mic_manual_pause_led_on());
 now=2899;assert(mic_manual_pause_led_on());
 assert(!mic_toggle_manual_pause());step();
 assert(!manual_paused && running && rail && sd && aad_woke && !buzzes);
 /* A short acoustic sleep resumes silently. */
 aad_sleep_started_ms=now;now+=299999;
 running=false;aad_in_sleep=1;aad_wake_pending=1;step();assert(running && !aad_in_sleep && !buzzes);
 /* A long sleep vibrates exactly once, only after a successful start, across uptime wrap. */
 aad_sleep_started_ms=UINT32_MAX-1000;now=298999;
 running=false;aad_in_sleep=1;aad_wake_pending=1;step();assert(running && !aad_in_sleep && buzzes==1);
 aad_wake_pending=1;step();assert(buzzes==1);
 /* A pause from acoustic sleep doesn't emit an extra end marker. */
 running=false;aad_in_sleep=1;upload=true;
 assert(!mic_toggle_manual_pause());step();assert(manual_paused && !rail && flushes==1 && sd);
 assert(!mic_toggle_manual_pause());resume_error=-EIO;step();
 assert(manual_paused && manual_pause_requested && !running && !rail && buzzes==1);
 resume_error=0;assert(!mic_toggle_manual_pause());step();assert(running && !manual_paused);
 /* Failed boundary: stay off and retry it before accepting new audio. */
 flush_error=-EIO;assert(!mic_toggle_manual_pause());step();assert(manual_paused && !rail && manual_end_pending);
 assert(!mic_toggle_manual_pause());step();assert(manual_paused && !rail && manual_pause_requested);
 flush_error=0;assert(!mic_toggle_manual_pause());step();assert(!manual_paused && running && !manual_end_pending);
 /* Pause racing with sleep wins over a queued acoustic wake. */
 race_pause=true;aad_req_sleep=1;aad_wake_pending=1;step();assert(!running && buzzes==1);
 step();assert(manual_paused && !rail);race_pause=false;
 /* Two clicks before the owner runs cancel each other. */
 mic_toggle_manual_pause();mic_toggle_manual_pause();step();assert(manual_paused && !rail);
 /* Failed acoustic start must not claim a vibration or active recording. */
 mic_toggle_manual_pause();step();running=false;aad_in_sleep=1;aad_wake_pending=1;resume_error=-EIO;
 step();assert(!running && aad_in_sleep && buzzes==1 && irq);
 return 0;
}
''')
    def test_pcm_is_not_forwarded_while_pause_requested_or_applied(self):
        self.compile_run(r'''
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <assert.h>
#define CONFIG_OMI_ENABLE_T5838_AAD 1
#define BYTES_PER_SAMPLE 2
#define CHANNELS 2
#define MAX_FRAMES 1600
#define __ASSERT_NO_MSG(x) assert(x)
#define LOG_ERR(...) ((void)0)
static int manual_pause_requested,manual_paused,mic_discard_blocks,mem_slab,forwarded,tracked,freed;
static int16_t mono_buffer[MAX_FRAMES];
int atomic_get(int *p){return *p;}
void atomic_dec(int *p){(*p)--;}
void k_mem_slab_free(int *p,void *b){(void)p;(void)b;freed++;}
void interleaved_stereo_to_mono(int16_t *i,size_t n,int16_t *out){(void)i;(void)n;(void)out;}
void aad_track_silence(const int16_t *b,size_t n){(void)b;(void)n;tracked++;}
void callback(int16_t *b){(void)b;forwarded++;}
static void (*callback_func)(int16_t *)=callback;
''' + function(SRC/'mic.c','static void process_audio_buffer(') + r'''
int main(void){int16_t samples[4]={0};
 manual_pause_requested=1;process_audio_buffer(samples,sizeof(samples));
 manual_pause_requested=0;manual_paused=1;process_audio_buffer(samples,sizeof(samples));
 assert(!forwarded && !tracked && freed==2);
 manual_paused=0;process_audio_buffer(samples,sizeof(samples));assert(forwarded==1 && tracked==1 && freed==3);
 /* Startup blocks after a restart are neither recorded nor VOX-tracked. */
 mic_discard_blocks=2;process_audio_buffer(samples,sizeof(samples));process_audio_buffer(samples,sizeof(samples));
 assert(forwarded==1 && tracked==1 && freed==5 && !mic_discard_blocks);
 process_audio_buffer(samples,sizeof(samples));assert(forwarded==2 && tracked==2 && freed==6);return 0;}
''')

    def test_button_release_toggles_once_and_preserves_long_holds(self):
        self.compile_run(r"""
#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
#include <stddef.h>
#include "lib/core/button_hold.h"
#define CONFIG_OMI_WIFI_UPLOAD 1
#define CONFIG_OMI_ENABLE_HAPTIC 1
#define IS_ENABLED(x) (x)
#define LOG_INF(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define LOG_PRINTK(...) ((void)0)
#define K_MSEC(x) (x)
#define BUTTON_CHECK_INTERVAL 40
#define BUTTON_PRESSED 1
#define BUTTON_RELEASED 0
#define TAP_THRESHOLD 300
#define DOUBLE_TAP_WINDOW 600
#define GRACE 1
typedef uint8_t u_int8_t;
typedef enum { BUTTON_EVENT_NONE, BUTTON_EVENT_SINGLE_TAP, BUTTON_EVENT_DOUBLE_TAP,
 BUTTON_EVENT_LONG_PRESS, BUTTON_EVENT_RELEASE } ButtonEvent;
struct k_work {int unused;};
static bool was_pressed,btn_is_pressed,hold_handled,is_off;
static uint32_t now,current_time,btn_press_start_time,btn_release_time,btn_last_tap_time;
static u_int8_t btn_last_event;
static int button_work,current_button_state,toggles,off,setup;
uint32_t k_uptime_get_32(void){return now;}
int mic_toggle_manual_pause(void){toggles++;return 0;}
void turnoff_all(void){off++;}
void wifi_upload_request_provisioning(void){setup++;}
void notify_long_tap(void){} void notify_tap(void){}
void notify_double_tap(void){} void notify_unpress(void){}
void play_haptic_milli(int ms){assert(ms==100);}
void k_work_reschedule(int *w,int ms){(void)w;assert(ms==40);}
""" + function(SRC/'lib/core/button.c','void check_button_level(struct k_work *work_item)\n{') + r"""
void poll(uint32_t t,bool down){now=t;was_pressed=down;check_button_level(NULL);}
int main(void){
 poll(1000,true);poll(1040,true);assert(!toggles);
 poll(1120,false);assert(toggles==1);poll(1160,false);assert(toggles==1);
 poll(1200,true);poll(1320,false);assert(toggles==2);
 poll(2000,false);assert(toggles==2);
 poll(3000,true);poll(3400,false);assert(toggles==3);
 poll(4000,true);poll(5000,false);assert(toggles==3);
 poll(6000,true);poll(9200,false);assert(off==1 && !setup && toggles==3);
 /* A five-second hold and an abandoned long hold must do nothing. */
 poll(10000,true);poll(15000,true);assert(!setup && off==1);
 poll(15040,false);assert(!setup && off==1 && toggles==3);
 poll(20000,true);poll(39999,true);assert(!setup && off==1);
 poll(40000,true);assert(setup==1);
 poll(41000,true);poll(41100,false);assert(setup==1 && off==1 && toggles==3);
 is_off=true;poll(42000,true);poll(42120,false);assert(toggles==3);
 return 0;
}
""")

