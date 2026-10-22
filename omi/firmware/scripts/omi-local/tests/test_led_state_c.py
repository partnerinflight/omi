"""Execute the real LED selector across recorder and existing status states."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class LedStateTests(unittest.TestCase):
    def test_recording_dark_silence_red_and_existing_priorities(self):
        source = (SRC/'main.c').read_text()
        start = source.index('void set_led_state()')
        handler = source[start:source.index('\n}\n', start)+3]
        low = next(l for l in source.splitlines() if l.startswith('#define BATTERY_LOW_THRESHOLD_PERCENT'))
        harness = low + r'''
#include <stdbool.h>
#include <stdint.h>
#include <assert.h>
#define CONFIG_OMI_ENABLE_OFFLINE_STORAGE 1
#define CONFIG_OMI_ENABLE_BATTERY 1
#define CONFIG_OMI_ENABLE_HAPTIC 1
#define BATTERY_FULL_THRESHOLD_PERCENT 100
#define CONFIG_OMI_LECTURE_LED_PWM 10
typedef enum { LED_RED, LED_GREEN, LED_BLUE } led_color_t;
static bool is_off,is_charging,is_connected,blink_toggle,storage_full_notified;
static bool setup,upload,ble_transfer,full,clock_valid=true,recording,silent;
static bool red,green,blue;
static bool manual_pause,pause_flash,lecture;
static int red_level,green_level;
static uint8_t battery_percentage;
static bool sampled=true,low_storage,fast_on,led_fast_poll;
bool battery_sampled(void){return sampled;}
bool sd_ring_is_low(void){return low_storage;}
static bool fast_blink_on(void){return fast_on;}
static int buzzes;
void set_led_red(bool v){red=v;red_level=v?100:0;}
void set_led_green(bool v){green=v;green_level=v?100:0;}
void set_led_blue(bool v){blue=v;}
void set_led_pwm(led_color_t c,uint8_t level){
 if(c==LED_RED){red=level>0;red_level=level;}else if(c==LED_GREEN){green=level>0;green_level=level;}else blue=level>0;}
void led_off(void){red=green=blue=false;red_level=green_level=0;}
bool mic_in_lecture(void){return lecture;}
bool wifi_upload_provisioning(void){return setup;}
bool wifi_upload_active(void){return upload;}
bool storage_transfer_active(void){return ble_transfer;}
bool sd_ring_is_full(void){return full;}
bool rtc_is_valid(void){return clock_valid;}
bool mic_is_running(void){return recording;}
bool mic_in_aad_sleep(void){return silent;}
bool mic_is_manually_paused(void){return manual_pause;}
bool mic_manual_pause_led_on(void){return pause_flash;}
void play_haptic_milli(int ms){assert(ms==300);buzzes++;}
''' + handler + r'''
int main(void){
 for(int charging=0;charging<2;charging++)for(int connected=0;connected<2;connected++)
 for(int battery=10;battery<=100;battery+=90){ // below 10% is the low-battery blink, tested below
  is_charging=charging;is_connected=connected;battery_percentage=battery;
  silent=false;recording=true;red=green=blue=true;set_led_state();
  assert(!red && !green && !blue);
  silent=true;recording=false;set_led_state();assert(red && !green && !blue);
  silent=false;recording=true;set_led_state();assert(!red && !green && !blue);
 }
 silent=true;recording=false;is_off=true;set_led_state();assert(!red && !green && !blue);is_off=false;
 setup=true;blink_toggle=true;set_led_state();assert(blue && !red && !green);setup=false;
 upload=true;blink_toggle=true;set_led_state();assert(green && blue && !red);upload=false;
 full=true;blink_toggle=true;set_led_state();assert(red && !blue && green && buzzes==1);
 set_led_state();assert(!red && blue && buzzes==1);full=false;
 clock_valid=false;blink_toggle=true;set_led_state();assert(red && green && !blue);clock_valid=true;
 silent=false;recording=false;battery_percentage=100;set_led_state();assert(green && !red && !blue);
 manual_pause=true;full=true;clock_valid=false;
 // Each transport overrides both pause-flash phases, even after CHG drops.
 for(int charging=0;charging<2;charging++)for(int transport=0;transport<2;transport++)
 for(int flash=0;flash<2;flash++){
  is_charging=charging;pause_flash=flash;
  upload=transport==0;ble_transfer=transport==1;blink_toggle=true;
  set_led_state();assert(green && blue && !red && manual_pause);
  set_led_state();assert(green && !blue && !red && manual_pause);
  is_off=true;set_led_state();assert(!red && !green && !blue);is_off=false;
  upload=ble_transfer=false;
  set_led_state();assert(red==pause_flash && !green && !blue && manual_pause);
 }
 setup=true;blink_toggle=true;set_led_state();assert(blue && !red && !green);
 setup=false;
 pause_flash=true;set_led_state();assert(red && !green && !blue);
 pause_flash=false;set_led_state();assert(!red && !green && !blue);
 is_off=true;pause_flash=true;set_led_state();assert(!red && !green && !blue);
 /* Lecture: dim solid yellow in the recording slot, whatever charging/connection say. */
 is_off=false;manual_pause=pause_flash=false;full=false;clock_valid=true;lecture=true;
 for(int charging=0;charging<2;charging++)for(int connected=0;connected<2;connected++)
 for(int state=0;state<3;state++){
  is_charging=charging;is_connected=connected;battery_percentage=100;
  recording=state==0;silent=state==1;   /* state 2: mic stopped, still in lecture mode */
  blue=true;set_led_state();assert(red && green && !blue && red_level==10 && green_level==10);
 }
 recording=true;silent=false;
 /* Pause feedback stays above lecture mode. */
 manual_pause=pause_flash=true;set_led_state();assert(red && !green && !blue && red_level==100);
 pause_flash=false;set_led_state();assert(!red && !green && !blue);manual_pause=false;
 /* Setup, transfers, storage-full and clock warnings stay visible during a lecture. */
 setup=true;blink_toggle=true;set_led_state();assert(blue && !red && !green);setup=false;
 upload=true;blink_toggle=true;set_led_state();assert(green && blue && !red);upload=false;
 ble_transfer=true;blink_toggle=true;set_led_state();assert(green && blue && !red);ble_transfer=false;
 full=true;blink_toggle=true;set_led_state();assert(red && !blue && red_level==100);full=false;
 clock_valid=false;blink_toggle=true;set_led_state();assert(red && !blue && red_level==100);clock_valid=true;
 /* Leaving lecture mode returns to dark recording; off stays dark. */
 lecture=false;set_led_state();assert(!red && !green && !blue);
 lecture=true;is_off=true;set_led_state();assert(!red && !green && !blue);
 is_off=false;lecture=false;
 manual_pause=false;full=false;clock_valid=true;silent=true;battery_percentage=50;
 // Low battery: 500 ms red blink while not charging; wins over low storage and the clock warning.
 for(int on=0;on<2;on++){
  fast_on=on;is_charging=false;battery_percentage=9;low_storage=true;clock_valid=false;
  set_led_state();assert(red==on && !green && !blue && led_fast_poll);
  low_storage=false;clock_valid=true;set_led_state();assert(red==on && !green && !blue && led_fast_poll);
 }
 is_charging=true;set_led_state();assert(!led_fast_poll);is_charging=false;
 sampled=false;battery_percentage=0;set_led_state();assert(red && !green && !blue && !led_fast_poll);
 sampled=true;battery_percentage=10;set_led_state();assert(!led_fast_poll);
 // Low storage: 500 ms blue blink; storage full, pause and transfers keep their priority.
 for(int on=0;on<2;on++){
  fast_on=on;low_storage=true;set_led_state();assert(blue==on && !red && !green && led_fast_poll);
 }
 full=true;blink_toggle=true;set_led_state();assert(!led_fast_poll && red && !blue);full=false;
 manual_pause=true;battery_percentage=5;pause_flash=false;set_led_state();assert(!red && !blue && !led_fast_poll);manual_pause=false;
 upload=true;set_led_state();assert(green && !led_fast_poll);upload=false;
 low_storage=false;battery_percentage=50;set_led_state();assert(red && !green && !blue && !led_fast_poll);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'test.c').write_text(harness)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror',str(root/'test.c'),'-o',str(root/'test')],check=True)
            subprocess.run([str(root/'test')],check=True)

    def test_storage_low_below_ten_percent_free(self):
        source = (SRC/'sd_card.c').read_text()
        start = source.index('bool sd_ring_is_low(void)')
        harness = r'''
#include <stdbool.h>
#include <stdint.h>
#include <assert.h>
#define MIN(a,b) ((a)<(b)?(a):(b))
static struct { uint32_t capacity_packets; } ring_state;
static uint64_t used;
static uint64_t ring_used_packets(void){return used;}
''' + source[start:source.index('\n}\n', start)+3] + r'''
int main(void){
 assert(!sd_ring_is_low()); // not mounted yet
 ring_state.capacity_packets=1000;
 used=900;assert(!sd_ring_is_low()); // exactly 10% free
 used=901;assert(sd_ring_is_low());
 used=1000;assert(sd_ring_is_low());
 used=1200;assert(sd_ring_is_low()); // a partial batch past capacity never wraps
 used=0;assert(!sd_ring_is_low());
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'test.c').write_text(harness)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror',str(root/'test.c'),'-o',str(root/'test')],check=True)
            subprocess.run([str(root/'test')],check=True)
