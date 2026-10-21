"""Execute the production tap-sequence logic and its use in the button handler."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
def function(path, name):
    text = path.read_text(); start = text.index(name)
    return text[start:text.index('\n}\n', start) + 3]

@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class ButtonTapTests(unittest.TestCase):
    def compile_run(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p/'test.c').write_text(source)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-I',str(SRC),
                            str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True)

    def test_triple_click_timings_cancel_and_delayed_reports(self):
        self.compile_run(r'''
#include <assert.h>
#include "lib/core/button_taps.h"
static struct button_taps t;
static enum button_tap_result at(uint32_t ms,bool down){return button_taps_update(&t,ms,down);}
static void tap(uint32_t press,uint32_t release){assert(at(press,true)==TAPS_NONE);assert(at(release,false)==TAPS_NONE);}
int main(void){
 /* Accept: 299 ms presses and 600 ms gaps; reported only after the window closes. */
 tap(0,299);tap(899,1198);tap(1798,2097);
 assert(at(2697,false)==TAPS_NONE);                 /* exactly 600 ms: still open */
 assert(at(2698,false)==TAPS_TRIPLE);
 assert(!button_taps_pending(&t) && at(5000,false)==TAPS_NONE);
 /* A 300 ms press is a hold: it ends the sequence without a result. */
 tap(10000,10100);tap(10300,10400);assert(at(10600,true)==TAPS_NONE);assert(at(10900,false)==TAPS_NONE);
 assert(!button_taps_pending(&t) && at(20000,false)==TAPS_NONE);
 /* A 601 ms gap splits the clicks: a single, then a double, never a triple. */
 tap(30000,30100);assert(at(30701,true)==TAPS_SINGLE);assert(at(30800,false)==TAPS_NONE);
 tap(31000,31100);assert(at(31701,false)==TAPS_DOUBLE);
 /* A fourth tap cancels; further taps in the window stay cancelled. */
 tap(40000,40100);tap(40300,40400);tap(40600,40700);tap(40900,41000);
 assert(at(41601,false)==TAPS_NONE && !button_taps_pending(&t));
 tap(50000,50100);tap(50300,50400);tap(50600,50700);tap(50900,51000);tap(51200,51300);
 assert(at(51901,false)==TAPS_NONE);
 /* A hold after three taps cancels the count. */
 tap(60000,60100);tap(60300,60400);tap(60600,60700);assert(at(61000,true)==TAPS_NONE);
 assert(at(64000,false)==TAPS_NONE);assert(at(65000,false)==TAPS_NONE);
 /* Single and double taps are also reported only once the window closes. */
 tap(70000,70100);assert(at(70700,false)==TAPS_NONE);assert(at(70701,false)==TAPS_SINGLE);
 tap(80000,80100);tap(80300,80400);assert(at(81000,false)==TAPS_NONE);assert(at(81001,false)==TAPS_DOUBLE);
 /* Uptime wrap. */
 tap(UINT32_MAX-500,UINT32_MAX-400);tap(UINT32_MAX-200,UINT32_MAX-100);tap(100,200);
 assert(at(800,false)==TAPS_NONE);assert(at(801,false)==TAPS_TRIPLE);
 return 0;
}
''')

    def test_button_handler_toggles_lecture_and_delays_tap_notifications(self):
        self.compile_run(r'''
#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
#include <stddef.h>
#include "lib/core/button_hold.h"
#include "lib/core/button_taps.h"
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
#define GRACE 1
typedef uint8_t u_int8_t;
typedef enum { BUTTON_EVENT_NONE, BUTTON_EVENT_SINGLE_TAP, BUTTON_EVENT_DOUBLE_TAP,
 BUTTON_EVENT_LONG_PRESS, BUTTON_EVENT_RELEASE } ButtonEvent;
struct k_work {int unused;};
static bool was_pressed,btn_is_pressed,is_off;
static uint8_t hold_level_played;
static uint32_t now,current_time,btn_last_pressed_time,btn_press_start_time;
static u_int8_t btn_last_event;
static struct button_taps btn_taps;
static int button_work,current_button_state,toggles,lectures,taps,doubles,releases,off,setup;
uint32_t k_uptime_get_32(void){return now;}
int mic_toggle_manual_pause(void){toggles++;return 0;}
int mic_toggle_lecture(void){lectures++;return 0;}
void turnoff_all(void){off++;}
void wifi_upload_request_provisioning(void){setup++;}
void notify_long_tap(void){}
void notify_tap(void){taps++;}
void notify_double_tap(void){doubles++;}
void notify_unpress(void){releases++;}
void play_haptic_pulses(uint8_t n){(void)n;}
void k_work_reschedule(int *w,int ms){(void)w;assert(ms==40);}
''' + function(SRC/'lib/core/button.c', 'void check_button_level(struct k_work *work_item)\n{') + r'''
static void poll(uint32_t t,bool down){now=t;was_pressed=down;check_button_level(NULL);}
/* Hold for `down` ms, polled every 40 ms like the work item; returns the release poll. */
static uint32_t click(uint32_t at,uint32_t down){uint32_t t=at;for(;t<at+down;t+=40)poll(t,true);poll(t,false);return t;}
static void idle(uint32_t from,uint32_t to){for(uint32_t t=from;t<=to;t+=40)poll(t,false);}
int main(void){
 /* Triple-click toggles lecture mode once, only after the 600 ms window closes. */
 uint32_t r=click(1000,120);r=click(r+200,120);r=click(r+200,120);
 idle(r+40,r+600);assert(!lectures && !taps && !doubles && !releases);
 poll(r+640,false);assert(lectures==1 && !taps && !doubles && !toggles && releases==1);
 /* Single and double taps are notified only once their window has closed. */
 r=click(5000,120);idle(r+40,r+600);assert(!taps);poll(r+640,false);assert(taps==1 && !doubles);
 r=click(8000,120);r=click(r+200,120);idle(r+40,r+600);assert(!doubles);
 poll(r+640,false);assert(doubles==1 && taps==1 && lectures==1);
 /* A fourth click cancels: no lecture toggle, no tap notification. */
 r=click(12000,120);for(int i=0;i<3;i++)r=click(r+200,120);idle(r+40,r+2000);
 assert(lectures==1 && taps==1 && doubles==1);
 /* Clicks then a 3 s hold: the hold pauses and the clicks are dropped. */
 r=click(20000,120);r=click(r+200,120);r=click(r+200,3200);idle(r+40,r+2000);
 assert(toggles==1 && lectures==1 && doubles==1 && taps==1);
 /* A slow third click (680 ms gap) is a double then a single. */
 r=click(30000,120);r=click(r+200,120);idle(r+40,r+640);r=click(r+680,120);idle(r+40,r+2000);
 assert(lectures==1 && doubles==2 && taps==2);
 /* A 320 ms press inside the sequence is not a tap. */
 r=click(40000,120);r=click(r+200,320);r=click(r+200,120);idle(r+40,r+2000);
 assert(lectures==1 && taps==3 && doubles==2 && toggles==1);
 /* No lecture toggle while powered off. */
 is_off=true;r=click(50000,120);r=click(r+200,120);r=click(r+200,120);idle(r+40,r+2000);
 assert(lectures==1);is_off=false;
 assert(!off && !setup);
 return 0;
}
''')


if __name__ == '__main__':
    unittest.main()
