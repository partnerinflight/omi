"""Execute the production frame packer and decode its records with the real reader."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from omi_local import protocol as P

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
HARNESS = r'''
#include "lib/core/record_packer.h"
#include <stdio.h>
static void emit(const uint8_t *data, size_t size) { fwrite("\0\0\0\0",1,4,stdout); fwrite(data,1,size,stdout); }
int main(void) {
 uint8_t record[440], frame[255]; size_t used=0;
 memset(record,0xad,sizeof(record));
 int len; unsigned index=0;
 while ((len=getchar())!=EOF) {
  memset(frame,++index,len);
  if (!record_packer_append(record,sizeof(record),&used,frame,len,emit)) return 2;
 }
 if (used) {memset(record+used,0,sizeof(record)-used);emit(record,sizeof(record));}
 return 0;
}
'''

@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class RecordPackerTests(unittest.TestCase):
    def test_overflow_exact_fit_and_reused_tail(self):
        # Hardware regression: five 89-byte frames caused a bogus sixth length
        # at offset 360. Also exercise an exact 440-byte payload and stale tails.
        lengths = [89]*10 + [219,219,255,183,1,88,255,255,80,80,80,80,80,80]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'main.c').write_text(HARNESS)
            subprocess.run(['cc','-Wall','-Wextra','-Werror','-I',str(SRC),str(root/'main.c'),'-o',str(root/'check')],check=True)
            data=subprocess.check_output([str(root/'check')],input=bytes(lengths))
        self.assertEqual(len(data) % P.RECORD_SIZE,0)
        frames=[f for r in P.iter_records(0,data) for f in r.frames]
        self.assertEqual(frames,[bytes([i+1])*n for i,n in enumerate(lengths)])
