"""Execute the production lecture-mode transitions on the microphone owner thread."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
def function(path, name):
    text = path.read_text(); start = text.index(name)
    return text[start:text.index('\n}\n', start) + 3]
# The production pulse timing, so the motor-before-rail check uses the real pattern length.
HAPTIC = ''.join(l + '\n' for l in (SRC/'lib/core/haptic.h').read_text().splitlines()
                 if l.startswith('#define HAPTIC_PULSE'))
NAMES = ['int mic_toggle_manual_pause(void)', 'int mic_toggle_lecture(void)', 'bool mic_in_lecture(void)',
         'static void lecture_timer_expired(struct k_timer *timer)\n{', 'static void exit_hw_aad(void)',
         'static void enter_manual_pause(void)', 'static void exit_manual_pause(void)',
         'static void enter_lecture(void)\n{', 'static void exit_lecture(void)\n{',
         'static void aad_thread_fn(void *p1, void *p2, void *p3)\n{']
# Stubs log every boundary call: P mic_pause, E end marker, L/N lecture/normal marker,
# 2 double pulse, R mic running again, Z acoustic sleep entered.
PRELUDE = r'''
#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
#include <errno.h>
#include <setjmp.h>
#include <stddef.h>
#include <string.h>
#include "lib/core/record_packer.h"
#define CONFIG_OMI_ENABLE_T5838_AAD 1
#define CONFIG_OMI_ENABLE_HAPTIC 1
#define CONFIG_OMI_LECTURE_MAX_MS 14400000
#define LECTURE_EXIT_RETRY_MS 60000
#define K_FOREVER 0
#define K_NO_WAIT 0
#define K_MSEC(x) (x)
#define AAD_PDM_SETTLE_MS 20
#define MANUAL_RESUME_MOTOR_SETTLE_MS 150
#define CONFIG_OMI_AAD_WAKE_HAPTIC_MIN_SLEEP_MS 300000
#define MIC_START_DISCARD_BLOCKS 5
#define ARG_UNUSED(x) ((void)(x))
#define LOG_INF(...) ((void)0)
#define LOG_ERR(...) ((void)0)
struct k_timer {int unused;};
static struct k_timer lecture_timer;
static int aad_sem,aad_in_sleep,aad_woke,aad_wake_pending,aad_req_sleep;
static int manual_pause_requested,manual_paused,mic_discard_blocks,lecture_requested,lecture_active;
static uint32_t manual_pause_started_ms,aad_sleep_started_ms,now;
static bool manual_end_pending,lecture_marker_pending,aad_thread_started=true,is_connected,upload;
static bool running=true,rail=true,irq,sd=true,timer_on;
static int end_error,mark_error,buzzes,timer_ms;
static char calls[64];
static jmp_buf stop;static int steps;
static bool motor_pending;static int motor_wait;  /* owner sleeps since the last pulses */
static void note(const char *s){assert(strlen(calls)+strlen(s)<sizeof(calls));strcat(calls,s);}
int atomic_get(int *p){return *p;}
void atomic_set(int *p,int v){*p=v;}
void atomic_clear(int *p){*p=0;}
void atomic_xor(int *p,int v){*p^=v;}
bool atomic_cas(int *p,int a,int b){if(*p!=a)return false;*p=b;return true;}
void k_sem_give(int *s){(void)s;}
void k_sem_take(int *s,int t){(void)s;(void)t;if(steps++)longjmp(stop,1);}
void k_msleep(int ms){assert(ms==150 || ms==20 || ms==HAPTIC_PULSES_MS(2));if(motor_pending)motor_wait+=ms;}
uint32_t k_uptime_get_32(void){return now;}
void k_timer_start(struct k_timer *t,int d,int p){assert(t==&lecture_timer && p==0);timer_on=true;timer_ms=d;}
void k_timer_stop(struct k_timer *t){assert(t==&lecture_timer);timer_on=false;}
void aad_wake_irq(bool on){irq=on;}
void pdm_hw_disable(void){assert(!running);}
/* The mic rail never powers while the motor runs: wait out the pulses plus the settle margin. */
void t5838_aad_power(bool on){if(on && motor_pending)assert(motor_wait>=HAPTIC_PULSES_MS(2)+MANUAL_RESUME_MOTOR_SETTLE_MS);rail=on;}
void t5838_aad_release_clk(void){}
void t5838_aad_enter(void){assert(!running);}
void sd_request_power(bool on){sd=on;}
bool wifi_upload_active(void){return upload;}
int mic_pause(void){running=false;note("P");return 0;}
int codec_end_recording(void){assert(!running && sd);note("E");return end_error;}
int codec_mark_mode(uint8_t mode){assert(!running && sd);note(mode==RECORDING_MODE_LECTURE?"L":"N");return mark_error;}
int mic_resume(void){assert(mic_discard_blocks==5 && rail);mic_discard_blocks=0;running=true;note("R");return 0;}
void play_haptic_pulses(uint8_t n){assert(n==2 && !running);note("2");motor_pending=true;motor_wait=0;}
void play_haptic_milli(int ms){assert(ms==80);buzzes++;}
void enter_hw_aad(void){running=false;aad_in_sleep=1;if(!is_connected && !upload)sd=false;note("Z");}
'''
HELPERS = r'''
static void step(void){steps=0;motor_pending=false;  /* owner wakes are user-paced */
 if(!setjmp(stop))aad_thread_fn(NULL,NULL,NULL);}
static void expect(const char *want){assert(!strcmp(calls,want));calls[0]=0;}
'''

@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class LectureModeTests(unittest.TestCase):
    def run_main(self, body):
        source = (HAPTIC + PRELUDE + '\n'.join(function(SRC/'mic.c', n) for n in NAMES) + HELPERS
                  + 'int main(void){\n' + body + '\n return 0;\n}\n')
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p/'test.c').write_text(source)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-unused-function','-I',str(SRC),
                            str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True)

    def test_entry_exit_auto_exit_and_failures(self):
        self.run_main(r'''
 aad_thread_started=false;assert(mic_toggle_lecture()==-EAGAIN);aad_thread_started=true;
 /* Entry while recording: end marker, lecture marker, double pulse, then audio. */
 now=1000;assert(!mic_toggle_lecture());step();expect("PEL2R");
 assert(mic_in_lecture() && timer_on && timer_ms==14400000 && running && aad_woke && sd);
 /* A stale silence request never puts a lecture to sleep. */
 aad_req_sleep=1;step();expect("");assert(!aad_in_sleep && running && !aad_req_sleep);
 /* Triple-click exit: end marker, normal marker, double pulse, then normal VOX. */
 now=2000;assert(!mic_toggle_lecture());step();expect("PEN2R");
 assert(!mic_in_lecture() && !timer_on && running);
 aad_req_sleep=1;step();expect("Z");assert(aad_in_sleep && !running && !sd);
 /* Entry from acoustic sleep: no second end marker and no 80 ms wake buzz. */
 aad_sleep_started_ms=now;now+=400000;assert(!mic_toggle_lecture());step();expect("L2R");
 assert(mic_in_lecture() && running && !aad_in_sleep && !buzzes && timer_on && !irq);
 /* Auto-exit behaves like a triple-click. */
 lecture_timer_expired(&lecture_timer);step();expect("PEN2R");
 assert(!mic_in_lecture() && !timer_on && !lecture_requested);
 /* A failed lecture marker stays in normal mode without the confirmation. */
 mark_error=-EIO;assert(!mic_toggle_lecture());step();expect("PELR");
 assert(!mic_in_lecture() && !lecture_requested && running && !timer_on);
 /* A failed end marker does not write the lecture marker. */
 mark_error=0;end_error=-EIO;assert(!mic_toggle_lecture());step();expect("PER");
 assert(!mic_in_lecture() && !lecture_requested);end_error=0;
 /* A failed exit stays in lecture mode and retries a minute later. */
 assert(!mic_toggle_lecture());step();expect("PEL2R");
 mark_error=-EIO;assert(!mic_toggle_lecture());step();expect("PENR");
 assert(mic_in_lecture() && lecture_requested && timer_on && timer_ms==60000 && running);
 mark_error=0;lecture_timer_expired(&lecture_timer);step();expect("PEN2R");
 assert(!mic_in_lecture() && !timer_on);
 /* Two triple-clicks before the owner runs cancel each other. */
 mic_toggle_lecture();mic_toggle_lecture();step();expect("");assert(!mic_in_lecture());
''')

    def test_pause_inside_lecture_and_entry_or_exit_from_pause(self):
        self.run_main(r'''
 now=1000;assert(!mic_toggle_lecture());step();expect("PEL2R");
 /* Pause inside a lecture ends the recording but keeps lecture mode. */
 assert(!mic_toggle_manual_pause());step();expect("PE");
 assert(manual_paused && mic_in_lecture() && lecture_marker_pending && !rail && !sd && !irq);
 /* Resume re-marks the lecture before any audio. */
 assert(!mic_toggle_manual_pause());step();expect("LR");
 assert(!manual_paused && running && mic_in_lecture() && !lecture_marker_pending);
 /* Exit while paused: normal marker and pulses only; the mic stays paused. */
 assert(!mic_toggle_manual_pause());step();expect("PE");
 assert(!mic_toggle_lecture());step();expect("N2");
 assert(manual_paused && !rail && !sd && !mic_in_lecture() && !lecture_marker_pending && !timer_on);
 /* The next resume is normal: no marker. */
 assert(!mic_toggle_manual_pause());step();expect("R");assert(running && !manual_paused);
 /* Entry from pause starts the mic after the marker and the pulses. */
 assert(!mic_toggle_manual_pause());step();expect("PE");
 assert(!mic_toggle_lecture());step();expect("L2R");
 assert(!manual_paused && !manual_pause_requested && running && rail && mic_in_lecture() && timer_on);
 assert(!mic_toggle_lecture());step();expect("PEN2R");
 /* A pause whose end marker failed retries it before the lecture marker. */
 end_error=-EIO;assert(!mic_toggle_manual_pause());step();expect("PE");assert(manual_end_pending);
 end_error=0;assert(!mic_toggle_lecture());step();expect("EL2R");
 assert(!manual_end_pending && mic_in_lecture() && running);
''')


if __name__ == '__main__':
    unittest.main()
