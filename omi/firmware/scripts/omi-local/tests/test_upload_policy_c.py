"""Execute the real upload-abort policy from wifi_upload.c.

A full battery on the charger stops charging, which drops the CHG signal; automatic
uploads used to abort mid-transfer every time that happened (seen live on .14:
'aborted (charger removed / busy)'). Once started, an upload now runs to completion;
only a Wi-Fi setup request, which needs the radio, interrupts it.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'


def policy_source():
    source = (SRC / 'wifi_upload.c').read_text()
    start = source.index('static bool upload_should_abort(')
    return source, source[start:source.index('\n}\n', start) + 3]


class UploadPolicyWiringTests(unittest.TestCase):
    def test_upload_loop_uses_the_policy_not_the_charging_signal(self):
        source, _ = policy_source()
        loop = source[source.index('static enum wifi_upload_result upload_records('):]
        loop = loop[:loop.index('\n}\n')]
        self.assertIn('upload_should_abort(', loop)
        self.assertNotIn('is_charging', loop, 'the charging signal must not abort a running upload')


@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class UploadPolicyTests(unittest.TestCase):
    def test_charging_drop_never_aborts_and_setup_always_does(self):
        _, policy = policy_source()
        harness = r'''
#include <stdbool.h>
#include <assert.h>
''' + policy + r'''
int main(void){
 assert(!upload_should_abort(false));  /* charger/CHG state is not an input at all */
 assert(upload_should_abort(true));    /* Wi-Fi setup needs the radio */
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'test.c').write_text(harness)
            exe = root / 'test.exe'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', str(root / 'test.c'), '-o', str(exe)], check=True)
            subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    unittest.main()
