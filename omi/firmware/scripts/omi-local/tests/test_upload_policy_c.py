"""Execute the real upload-abort policy from wifi_upload.c.

A full battery on the charger stops charging, which drops the CHG signal; automatic
uploads used to abort mid-transfer every time that happened (seen live on .14:
'aborted (charger removed / busy)'). Once started, an upload now runs to completion;
only a Wi-Fi setup request, which needs the radio, interrupts it.
"""
import shutil
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
CC = os.environ.get('CC') or shutil.which('cc')


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


@unittest.skipUnless(CC, 'native C compiler required')
class UploadPolicyTests(unittest.TestCase):
    def test_interrupted_upload_retries_without_charging_and_stops_at_budget(self):
        source = (SRC / 'wifi_upload.c').read_text()
        helpers = ''
        for signature in ('static bool upload_retryable(', 'static bool upload_ready(', 'static unsigned int upload_retry_budget('):
            start = source.index(signature)
            helpers += source[start:source.index('\n}\n', start) + 3]
        header = (SRC / 'lib/core/wifi_upload.h').read_text()
        start = header.index('enum wifi_upload_result {')
        result_enum = header[start:header.index('};', start) + 2]
        harness = '#include <stdbool.h>\n#include <stdint.h>\n#include <assert.h>\n#define UP_MIN_UNREAD_PACKETS 600U\n#define UP_RETRY_ATTEMPTS 6U\n'
        harness += result_enum + helpers + r'''
int main(void) {
 assert(upload_ready(true, 0, 600));
 assert(!upload_ready(false, 0, 600));
 assert(!upload_ready(true, 0, 599));
 unsigned int retries = upload_retry_budget(WIFI_UPLOAD_ERR_LINK_LOST, false, 0);
 assert(retries == 6);
 unsigned int attempts = 0;
 while (retries) {
   assert(upload_ready(false, retries, 1)); /* retry even a sub-minute tail */
   assert(upload_retryable(WIFI_UPLOAD_ERR_LINK_LOST));
   retries = upload_retry_budget(WIFI_UPLOAD_ERR_LINK_LOST, false, retries);
   ++attempts;
 }
 assert(attempts == 6);
 assert(upload_retry_budget(WIFI_UPLOAD_OK, false, 4) == 0);
 assert(upload_retry_budget(WIFI_UPLOAD_ERR_ABORTED, false, 4) == 0);
 assert(upload_retry_budget(WIFI_UPLOAD_ERR_LINK_LOST, true, 1) == 6);
 assert(!upload_ready(false, retries, 10000));
 assert(!upload_ready(false, 6, 0));
 assert(upload_retryable(WIFI_UPLOAD_ERR_TCP_CONNECT));
 assert(upload_retryable(WIFI_UPLOAD_ERR_DHCP));
 assert(!upload_retryable(WIFI_UPLOAD_ERR_AUTH));
 assert(!upload_retryable(WIFI_UPLOAD_ERR_PROTOCOL));
 assert(!upload_retryable(WIFI_UPLOAD_ERR_ABORTED)); /* setup cancels the transfer */
 assert(!upload_retryable(WIFI_UPLOAD_OK));
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'test.c').write_text(harness)
            exe = root / 'test.exe'
            subprocess.run([CC, '-std=c11', '-Wall', '-Wextra', '-Werror', str(root / 'test.c'), '-o', str(exe)], check=True)
            subprocess.run([str(exe)], check=True)

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
            subprocess.run([CC, '-std=c11', '-Wall', '-Wextra', '-Werror', str(root / 'test.c'), '-o', str(exe)], check=True)
            subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    unittest.main()
