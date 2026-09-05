"""Compile the firmware's SHA-256/HMAC (src/lib/core/local_auth.c) natively and
cross-check it against Python's hashlib/hmac. Skipped when no C compiler is
available."""

import hashlib
import hmac
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[3] / "omi" / "src" / "lib" / "core"

HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "local_auth.h"
static size_t unhex(const char *s, unsigned char *out) {
    size_t n = strlen(s) / 2;
    for (size_t i = 0; i < n; i++) { unsigned v; sscanf(s + 2 * i, "%2x", &v); out[i] = (unsigned char) v; }
    return n;
}
int main(int argc, char **argv) {
    static unsigned char a[8192], b[8192], c[64];
    unsigned char out[32];
    size_t la = argc > 2 ? unhex(argv[2], a) : 0;
    size_t lb = argc > 3 ? unhex(argv[3], b) : 0;
    size_t lc = argc > 4 ? unhex(argv[4], c) : 0;
    (void) lc;
    if (!strcmp(argv[1], "sha")) local_sha256(a, la, out);
    else if (!strcmp(argv[1], "hmac")) local_hmac_sha256(a, la, b, lb, out);
    else if (!strcmp(argv[1], "tag")) local_auth_tag(a, argv[5], b, c, out);
    else return 2;
    for (int i = 0; i < 32; i++) printf("%02x", out[i]);
    printf("\n");
    return 0;
}
'''


@unittest.skipUnless(shutil.which("cc") or shutil.which("gcc"), "no C compiler")
@unittest.skipUnless((CORE / "local_auth.c").exists(), "firmware sources not present")
class LocalAuthCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        # Copy into a clean dir: the firmware's core dir has its own features.h
        # which would shadow glibc's <features.h> if used as an include path.
        for name in ("local_auth.c", "local_auth.h"):
            (d / name).write_bytes((CORE / name).read_bytes())
        (d / "harness.c").write_text(HARNESS)
        cc = shutil.which("cc") or shutil.which("gcc")
        subprocess.check_call([cc, "-O2", "-Wall", "-Wextra", "-o", str(d / "harness"),
                               str(d / "harness.c"), str(d / "local_auth.c")])
        cls.exe = str(d / "harness")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_c(self, *args) -> str:
        return subprocess.check_output([self.exe, *args]).decode().strip()

    def test_sha256_vectors(self):
        for msg in (b"", b"abc", b"a" * 55, b"a" * 56, b"a" * 64, b"a" * 119, os.urandom(1000)):
            self.assertEqual(self.run_c("sha", msg.hex()), hashlib.sha256(msg).hexdigest(), msg[:8])

    def test_hmac_vectors(self):
        for key, msg in ((b"k", b"m"), (b"x" * 32, b"hello"), (b"y" * 64, b"z" * 200), (b"long" * 40, b"msg"),
                         (os.urandom(32), os.urandom(3000))):
            self.assertEqual(self.run_c("hmac", key.hex(), msg.hex()), hmac.new(key, msg, hashlib.sha256).hexdigest())

    def test_handshake_tags_match_host_definition(self):
        from omi_local import upload_protocol as U

        sec, cn, sn = os.urandom(32), os.urandom(16), os.urandom(16)
        for label in (U.LABEL_SERVER, U.LABEL_CLIENT):
            self.assertEqual(self.run_c("tag", sec.hex(), cn.hex(), sn.hex(), label.decode()),
                             U.auth_tag(sec, label, cn, sn).hex())
