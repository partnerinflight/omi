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


if __name__ == '__main__':
    unittest.main()
