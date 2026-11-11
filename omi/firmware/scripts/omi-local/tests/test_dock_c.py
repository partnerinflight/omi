"""Execute the production dock-power detection from dock.c.

CV1 mainboard v1.2 routes the dock's VIN through R39/R41 over R38/R40 (about 0.6 x VIN)
to VIN_DETECT on P1.06. Unlike the charger's CHG output, it stays high when a docked
battery is full. An active charge signal also proves dock power.
"""
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
class DockTests(unittest.TestCase):
    def test_debounced_dock_power_follows_vin_detect_or_charging(self):
        names = ['bool dock_vin_detect_raw(void)', 'bool dock_power_present(void)',
                 'static void dock_settle(struct k_work *work)\n{', 'void dock_recheck(void)', 'void dock_poll(void)']
        source = r'''
#include <stdbool.h>
#include <stdint.h>
#include <assert.h>
#include "lib/core/dock.h"
#define ARG_UNUSED(x) ((void)(x))
#define LOG_INF(...) ((void)0)
#define K_MSEC(x) (x)
#define CONFIG_OMI_DOCK_DEBOUNCE_MS 1000
struct k_work {int unused;};
struct gpio_dt_spec {int pin;};
static struct gpio_dt_spec vin_detect;
static struct k_work dock_work;
static bool ready=true,is_charging;
static int present,pin_level,docked_calls,last_docked=-1,reschedules,schedules,last_delay;
int atomic_get(int *p){return *p;}
int atomic_set(int *p,int v){int old=*p;*p=v;return old;}
int gpio_pin_get_dt(const struct gpio_dt_spec *s){assert(s==&vin_detect);return pin_level;}
void k_work_reschedule(struct k_work *w,int ms){assert(w==&dock_work);reschedules++;last_delay=ms;}
void k_work_schedule(struct k_work *w,int ms){assert(w==&dock_work);schedules++;last_delay=ms;}
void mic_set_docked(bool docked){docked_calls++;last_docked=docked;}
''' + '\n'.join(function(SRC/'dock.c', n) for n in names) + r'''
int main(void){
 /* Policy: dock VIN, or current flowing into the battery, means docked. */
 assert(!dock_present_from(false,false));
 assert(dock_present_from(true,false));   /* full battery on the dock: CHG released */
 assert(dock_present_from(false,true));   /* VIN_DETECT below the logic threshold */
 assert(dock_present_from(true,true));
 /* An edge restarts the debounce; the settled level decides. */
 dock_recheck();assert(reschedules==1 && last_delay==1000);
 pin_level=1;dock_settle(&dock_work);
 assert(dock_power_present() && docked_calls==1 && last_docked==1 && dock_vin_detect_raw());
 /* Settling at an unchanged level does not repeat the transition. */
 dock_settle(&dock_work);assert(docked_calls==1);
 /* A battery that fills releases CHG; VIN_DETECT keeps the dock state. */
 is_charging=true;dock_settle(&dock_work);is_charging=false;dock_settle(&dock_work);
 assert(dock_power_present() && docked_calls==1);
 /* Undocking: the poll notices a missed edge without restarting a pending debounce. */
 pin_level=0;dock_poll();dock_poll();assert(schedules==2 && last_delay==1000 && dock_power_present());
 dock_settle(&dock_work);assert(!dock_power_present() && docked_calls==2 && last_docked==0);
 dock_poll();assert(schedules==2);         /* agreed: nothing to do */
 /* The charge signal alone counts while VIN_DETECT reads low. */
 is_charging=true;dock_poll();assert(schedules==3);dock_settle(&dock_work);
 assert(dock_power_present() && last_docked==1);
 /* Without the pin (board lacks it) only the charge signal is used. */
 ready=false;pin_level=1;is_charging=false;assert(!dock_vin_detect_raw());
 dock_settle(&dock_work);assert(!dock_power_present() && last_docked==0);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p/'test.c').write_text(source)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-I',str(SRC),str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True)

    def test_board_wires_vin_detect_to_p1_06_without_a_pull(self):
        dts = (SRC.parent.parent/'boards'/'omi'/'omi_nrf5340_cpuapp.dts').read_text()
        node = dts[dts.index('vin_detect_pin: vin_detect_pin {'):]
        node = node[:node.index('};')]
        self.assertIn('<&gpio1 6 GPIO_ACTIVE_HIGH>', node)
        code = (SRC/'dock.c').read_text()
        # An internal pull would load the divider and pull a docked level under the threshold.
        self.assertIn('gpio_pin_configure_dt(&vin_detect, GPIO_INPUT)', code)
        self.assertIn('GPIO_INT_EDGE_BOTH', code)


if __name__ == '__main__':
    unittest.main()
