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
        harness = r'''
#include <stdbool.h>
#include <stdint.h>
#include <assert.h>
#define CONFIG_OMI_ENABLE_OFFLINE_STORAGE 1
#define CONFIG_OMI_ENABLE_BATTERY 1
#define CONFIG_OMI_ENABLE_HAPTIC 1
#define BATTERY_FULL_THRESHOLD_PERCENT 100
static bool is_off,is_charging,is_connected,blink_toggle,storage_full_notified;
static bool setup,upload,full,clock_valid=true,recording,silent;
static bool red,green,blue;
static bool manual_pause,pause_flash;
static uint8_t battery_percentage;
static int buzzes;
void set_led_red(bool v){red=v;}
void set_led_green(bool v){green=v;}
void set_led_blue(bool v){blue=v;}
void led_off(void){red=green=blue=false;}
bool wifi_upload_provisioning(void){return setup;}
bool wifi_upload_active(void){return upload;}
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
 for(int battery=1;battery<=100;battery+=99){
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
 manual_pause=true;upload=true;setup=true;full=true;clock_valid=false;
 pause_flash=true;set_led_state();assert(red && !green && !blue);
 pause_flash=false;set_led_state();assert(!red && !green && !blue);
 is_off=true;pause_flash=true;set_led_state();assert(!red && !green && !blue);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'test.c').write_text(harness)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror',str(root/'test.c'),'-o',str(root/'test')],check=True)
            subprocess.run([str(root/'test')],check=True)
