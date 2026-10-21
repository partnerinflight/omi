# Lecture mode — firmware implementation plan (3 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Firmware `3.0.22-localwifi.20` toggles lecture mode with a triple-click: it closes the current recording, writes a recording-mode marker the receiver understands, vibrates twice, keeps one lecture as one recording (no acoustic sleep), shows dim solid yellow, and exits by itself after 4 hours.

**Architecture:** A pure tap-sequence state machine (`button_taps.h`, next to `button_hold.h`) counts short presses and reports single/double/triple only after the 600 ms window closes. A triple-click flips an atomic (`mic_toggle_lecture`) and gives `aad_sem`, exactly like manual pause; the microphone owner thread (`aad_thread_fn`) does the transition. The mode marker travels the same ordered path as the end marker: `codec_mark_mode()` → codec thread (after any drain) → `transport_mark_mode()` → a TX-queue slot whose 2-byte header is `0xFFFF` → pusher → `record_packer_mode()` writes the 440-byte payload `"\0OMIMOD\1"` + mode byte + zero padding behind the 4-byte timestamp `write_to_file` adds, i.e. the 444-byte record of plan 1. The SD ring format and upload protocol v1 are unchanged.

**Tech Stack:** Zephyr / NCS 2.9.0 C, native host C tests driven by Python `unittest` (`cc -std=c11 -Wall -Wextra -Werror`), Node only for the existing portal script test. Spec: `docs/superpowers/specs/2026-10-21-lecture-mode-design.md` section 1 and section 4.

**Prerequisite:** plan 1 (`2026-10-21-lecture-mode-receiver.md`) is implemented on this branch. Tasks 1 and 2 decode firmware output with the receiver's parser and need `protocol.RECORD_MODE_MAGIC` and `Record.mode`. Check before starting:

```bash
cd omi/firmware/scripts/omi-local
python -c "from omi_local import protocol as P; assert P.RECORD_MODE_MAGIC == b'\x00OMIMOD\x01'; print('receiver ready')"
```

Expected: `receiver ready`. If it raises `AttributeError`, implement plan 1 first.

Run test commands from `omi/firmware/scripts/omi-local`. Suite: `python -m unittest discover -s tests -t .`. File paths below are relative to `omi/firmware/` unless they start with `docs/`, `SYSTEM.md` or `scripts/test.py` (repository root). Line numbers refer to the tree at `d5be19d4b2`; later tasks shift them, so match on the quoted text.

**Do not flash.** This plan ends with a local build and signature check. Deployment is a separate step that waits for the owner's explicit request (root `AGENTS.md`, firmware `AGENTS.md`, spec section 4).

---

### Task 1: Mode marker record in the packer

**Files:**
- Modify: `omi/src/lib/core/record_packer.h` (lines 7–26: end-marker magic and `record_packer_end`)
- Test: `scripts/omi-local/tests/test_record_packer_c.py`

- [ ] **Step 1: Write the failing test** — append this method to `RecordPackerTests` in `tests/test_record_packer_c.py`:

```python
    def test_mode_marker_flushes_partial_record_and_retries_failed_emit(self):
        harness = r'''
#include "lib/core/record_packer.h"
#include <assert.h>
#include <stdio.h>
static int fail=1;
static bool emit(const uint8_t *p,size_t n){if(fail){fail=0;return false;}fwrite("\0\0\0\0",1,4,stdout);fwrite(p,1,n,stdout);return true;}
int main(void){uint8_t record[440]={3,0xb8,1,2};size_t used=4;
 assert(!record_packer_mode(record,440,&used,RECORDING_MODE_LECTURE,emit));assert(used==4);
 assert(record_packer_mode(record,440,&used,RECORDING_MODE_LECTURE,emit));assert(used==0);
 assert(record_packer_mode(record,440,&used,RECORDING_MODE_NORMAL,emit));
 assert(!record_packer_mode(record,8,&used,RECORDING_MODE_NORMAL,emit));
 return 0;}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'main.c').write_text(harness)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-I',str(SRC),str(root/'main.c'),'-o',str(root/'check')],check=True)
            data=subprocess.check_output([str(root/'check')])
        self.assertEqual(len(data), 3*P.RECORD_SIZE)
        # Exact wire format, independent of the receiver: magic, mode byte, zero padding.
        self.assertEqual(data[P.RECORD_SIZE+4:2*P.RECORD_SIZE], b'\x00OMIMOD\x01\x01' + bytes(431))
        self.assertEqual(data[2*P.RECORD_SIZE+4:], b'\x00OMIMOD\x01\x00' + bytes(431))
        records=list(P.iter_records(0,data))
        self.assertEqual(records[0].frames,(b'\xb8\x01\x02',))
        self.assertEqual([r.mode for r in records],[None,'lecture','normal'])
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_record_packer_c -v`
Expected: ERROR — the compiler reports `call to undeclared function 'record_packer_mode'` (or `implicit declaration`) and `use of undeclared identifier 'RECORDING_MODE_LECTURE'`, so `subprocess.CalledProcessError`. The existing test still passes.

- [ ] **Step 3: Implement** — in `omi/src/lib/core/record_packer.h` replace lines 7–26 (from `/* Reserved zero-frame payload.` through the closing `}` of `record_packer_end`) with:

```c
/* Reserved zero-frame payload. Older readers treat it as padding; updated
 * readers close the current file without creating an empty recording. */
#define RECORD_END_MAGIC "\0OMIEND\1"
#define RECORD_END_MAGIC_SIZE 8
/* Recording-mode marker: this magic, one mode byte, zero padding. It starts with 0,
 * so readers older than lecture mode treat it as an empty record. Never append data
 * to the end magic instead: those readers reject it and the upload would stall. */
#define RECORD_MODE_MAGIC "\0OMIMOD\1"
#define RECORD_MODE_MAGIC_SIZE 8
#define RECORDING_MODE_NORMAL 0
#define RECORDING_MODE_LECTURE 1

/* Emit a partly filled record, zero padded, so a marker follows every earlier frame. */
static inline bool
record_packer_flush(uint8_t *record, size_t capacity, size_t *used, bool (*emit)(const uint8_t *, size_t))
{
    if (*used) {
        memset(record + *used, 0, capacity - *used);
        if (!emit(record, capacity))
            return false;
        *used = 0;
    }
    return true;
}

static inline bool
record_packer_end(uint8_t *record, size_t capacity, size_t *used, bool (*emit)(const uint8_t *, size_t))
{
    if (capacity < RECORD_END_MAGIC_SIZE || *used > capacity)
        return false;
    if (!record_packer_flush(record, capacity, used, emit))
        return false;
    memset(record, 0, capacity);
    memcpy(record, RECORD_END_MAGIC, RECORD_END_MAGIC_SIZE);
    return emit(record, capacity);
}

static inline bool
record_packer_mode(uint8_t *record, size_t capacity, size_t *used, uint8_t mode, bool (*emit)(const uint8_t *, size_t))
{
    if (capacity < RECORD_MODE_MAGIC_SIZE + 1 || *used > capacity)
        return false;
    if (!record_packer_flush(record, capacity, used, emit))
        return false;
    memset(record, 0, capacity);
    memcpy(record, RECORD_MODE_MAGIC, RECORD_MODE_MAGIC_SIZE);
    record[RECORD_MODE_MAGIC_SIZE] = mode;
    return emit(record, capacity);
}
```

`record_packer_append` (old lines 28–53) is unchanged. The end marker's bytes and its failed-emit behavior are unchanged; `test_vox_c.py`'s `test_partial_record_then_marker_and_failed_emit_retry` guards that.

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_record_packer_c tests.test_vox_c -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/lib/core/record_packer.h omi/firmware/scripts/omi-local/tests/test_record_packer_c.py
git commit -m "feat(firmware): pack the recording-mode marker record"
```

---

### Task 2: Mode marker entry in the TX queue and pusher

**Files:**
- Modify: `omi/src/lib/core/transport.c` (`write_to_tx_queue` lines 938–965, `pusher` lines 1049–1061, `transport_end_recording` lines 1279–1290)
- Modify: `omi/src/lib/core/transport.h` (lines 33–34)
- Create: `scripts/omi-local/tests/test_pusher_markers_c.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_pusher_markers_c.py`:

```python
"""Execute the production TX queue and pusher: end and mode markers stay in order."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from omi_local import protocol as P

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
def function(path, name):
    text = path.read_text(); start = text.index(name)
    return text[start:text.index('\n}\n', start) + 3]
NAMES = ['static bool put_tx_entry(', 'static bool write_to_tx_queue(', 'static bool read_from_tx_queue()',
         'static void emit_storage_record(', 'static bool emit_recording_end(', 'bool write_to_storage(void)',
         'void pusher(void)']

@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class PusherMarkerTests(unittest.TestCase):
    def test_audio_end_and_mode_markers_reach_the_ring_in_order(self):
        source = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <errno.h>
#include <stdio.h>
#include <assert.h>
#include "lib/core/record_packer.h"
#define CONFIG_OMI_ENABLE_OFFLINE_STORAGE 1
#define CODEC_OUTPUT_MAX_BYTES 80
#define RING_BUFFER_HEADER_SIZE 2
#define MAX_WRITE_SIZE 440U
#define TX_ENTRY_MODE_MARKER 0xFFFFU
#define K_FOREVER 0
#define LOG_WRN(...) ((void)0)
struct ring_buf {uint8_t data[82*16];uint32_t head,tail;};
static struct ring_buf ring_buf;
static uint8_t tx_buffer[82],tx_buffer_2[82],storage_temp_data[MAX_WRITE_SIZE];
static uint32_t tx_buffer_size;
static size_t buffer_offset;
static int tx_queue_sem,recording_end_done,pusher_stop_flag,recording_end_result,takes,results[8],markers;
static bool storage_unavailable_warned;
uint32_t ring_buf_put(struct ring_buf *r,const uint8_t *d,uint32_t n){if(r->head+n>sizeof(r->data))return 0;memcpy(r->data+r->head,d,n);r->head+=n;return n;}
uint32_t ring_buf_get(struct ring_buf *r,uint8_t *d,uint32_t n){if(r->tail+n>r->head)return 0;memcpy(d,r->data+r->tail,n);r->tail+=n;return n;}
void k_sem_give(int *s){if(s==&recording_end_done)results[markers++]=recording_end_result;}
void k_sem_take(int *s,int t){(void)s;(void)t;if(takes++)pusher_stop_flag=1;}
void k_msleep(int ms){(void)ms;}
int atomic_get(int *p){return *p;}
bool is_sd_on(void){return true;}
uint32_t write_to_file(uint8_t *data,uint32_t length){assert(length==MAX_WRITE_SIZE);fwrite("\0\0\0\0",1,4,stdout);fwrite(data,1,length,stdout);return length;}
''' + ''.join(function(SRC/'lib/core/transport.c', n) for n in NAMES) + r'''
int main(void){
 uint8_t a[100],b[40],c[30],none=0,mode;
 memset(a,1,sizeof a);memset(b,2,sizeof b);memset(c,3,sizeof c);
 assert(!write_to_tx_queue(a,81));               /* longer than an Opus slot */
 assert(write_to_tx_queue(a,80) && write_to_tx_queue(b,40));
 assert(put_tx_entry(0,&none,0));                /* end of the normal recording */
 mode=RECORDING_MODE_LECTURE;assert(put_tx_entry(TX_ENTRY_MODE_MARKER,&mode,1));
 assert(write_to_tx_queue(c,30));
 assert(put_tx_entry(0,&none,0));
 mode=RECORDING_MODE_NORMAL;assert(put_tx_entry(TX_ENTRY_MODE_MARKER,&mode,1));
 pusher();
 assert(markers==4 && !buffer_offset);
 for(int i=0;i<markers;i++)assert(results[i]==0);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'test.c').write_text(source)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-I',str(SRC),str(root/'test.c'),'-o',str(root/'test')],check=True)
            data = subprocess.check_output([str(root/'test')])
        records = list(P.iter_records(0, data))
        self.assertEqual([(len(r.frames), r.recording_end, r.mode) for r in records],
                         [(2, False, None), (0, True, None), (0, False, 'lecture'),
                          (1, False, None), (0, True, None), (0, False, 'normal')])
        self.assertEqual(records[0].frames, (b'\x01' * 80, b'\x02' * 40))
        self.assertEqual(records[3].frames, (b'\x03' * 30,))
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_pusher_markers_c -v`
Expected: ERROR — `ValueError: substring not found` (`put_tx_entry` does not exist yet).

- [ ] **Step 3: Implement** — in `omi/src/lib/core/transport.c` replace `write_to_tx_queue` (lines 938–965) with:

```c
/* Each slot is a 2-byte header plus CODEC_OUTPUT_MAX_BYTES of data. The header is
 * the Opus frame length, 0 for a recording-end marker, or TX_ENTRY_MODE_MARKER
 * followed by one recording-mode byte. */
#define TX_ENTRY_MODE_MARKER 0xFFFFU
BUILD_ASSERT(CODEC_OUTPUT_MAX_BYTES < TX_ENTRY_MODE_MARKER, "Opus length collides with the mode marker");

static bool put_tx_entry(uint16_t header, const uint8_t *data, size_t size)
{
    // Copy data (TODO: Avoid this copy)
    tx_buffer_2[0] = header & 0xFF;
    tx_buffer_2[1] = (header >> 8) & 0xFF;
    memcpy(tx_buffer_2 + RING_BUFFER_HEADER_SIZE, data, size);

    // Write to ring buffer
    int written =
        ring_buf_put(&ring_buf,
                     tx_buffer_2,
                     (CODEC_OUTPUT_MAX_BYTES + RING_BUFFER_HEADER_SIZE)); // It always fits completely or not at all
    if (written != CODEC_OUTPUT_MAX_BYTES + RING_BUFFER_HEADER_SIZE) {
        return false;
    }
    k_sem_give(&tx_queue_sem);
    return true;
}

static bool write_to_tx_queue(uint8_t *data, size_t size)
{
#ifdef CONFIG_OMI_ENABLE_MONITOR
    // Increment the counter
    monitor_inc_tx_queue_write();
#endif

    if (size > CODEC_OUTPUT_MAX_BYTES) {
        return false;
    }
    return put_tx_entry((uint16_t) size, data, size);
}
```

In `pusher`, replace the marker branch head (lines 1049–1055, from `if (!tx_buffer_size) {` through `: -EIO;`) with:

```c
            if (!tx_buffer_size || tx_buffer_size == TX_ENTRY_MODE_MARKER) {
#ifdef CONFIG_OMI_ENABLE_OFFLINE_STORAGE
                /* Both markers flush the partial record first, so they follow every earlier frame. */
                bool written = is_sd_on() && (tx_buffer_size ? record_packer_mode(storage_temp_data,
                                                                                  sizeof(storage_temp_data),
                                                                                  &buffer_offset,
                                                                                  tx_buffer[RING_BUFFER_HEADER_SIZE],
                                                                                  emit_recording_end)
                                                             : record_packer_end(storage_temp_data,
                                                                                 sizeof(storage_temp_data),
                                                                                 &buffer_offset,
                                                                                 emit_recording_end));
                recording_end_result = written ? 0 : -EIO;
```

(The following `#else` / `recording_end_result = -ENOTSUP;` / `#endif` / `k_sem_give(&recording_end_done);` / `continue;` lines stay.)

Replace `transport_end_recording` (lines 1279–1290) with:

```c
/* Queue a marker behind every earlier frame and wait until the pusher wrote it. */
static int queue_recording_marker(uint16_t header, uint8_t value)
{
    int64_t deadline = k_uptime_get() + 1000;
    while (!put_tx_entry(header, &value, header == TX_ENTRY_MODE_MARKER ? 1 : 0)) {
        if (k_uptime_get() >= deadline)
            return -ENOBUFS;
        k_sleep(K_MSEC(5));
    }
    k_sem_take(&recording_end_done, K_FOREVER);
    return recording_end_result;
}

int transport_end_recording(void)
{
    return queue_recording_marker(0, 0);
}

int transport_mark_mode(uint8_t mode)
{
    return queue_recording_marker(TX_ENTRY_MODE_MARKER, mode);
}
```

In `omi/src/lib/core/transport.h`, after line 34 (`int transport_end_recording(void);`) add:

```c
/* Ordered recording-mode marker (record_packer.h RECORDING_MODE_*), codec thread only. */
int transport_mark_mode(uint8_t mode);
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_pusher_markers_c tests.test_disconnect_power_c -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/lib/core/transport.c omi/firmware/omi/src/lib/core/transport.h \
        omi/firmware/scripts/omi-local/tests/test_pusher_markers_c.py
git commit -m "feat(firmware): queue recording-mode markers behind audio and end markers"
```

---

### Task 3: `codec_mark_mode` on the codec thread

**Files:**
- Modify: `omi/src/lib/core/codec.c` (lines 22–27 callback, 35–46 end request, 101–118 end handling in `codec_entry`)
- Modify: `omi/src/lib/core/codec.h` (lines 5–14)
- Modify: `omi/src/main.c` (line 391, `set_codec_callback(codec_handler);`)
- Test: `scripts/omi-local/tests/test_vox_c.py`

- [ ] **Step 1: Write the failing tests** — in `tests/test_vox_c.py`, update the existing `test_codec_order_partial_padding_and_error` harness so it still compiles once `codec_entry` reads the mode request:

Replace lines 84–85:

```c
static int codec_data_sem,codec_end_done,codec_end_requested,codec_end_result;
static int codec_ring_buf,m_opus_state,resets,callbacks,ends,fail_encode,fail_end,takes;
```

with:

```c
typedef int atomic_val_t;
static int codec_data_sem,codec_end_done,codec_end_requested,codec_mode_requested,codec_end_result;
static int codec_ring_buf,m_opus_state,resets,callbacks,ends,fail_encode,fail_end,takes;
```

after line 91 (`bool atomic_cas(...)`) add:

```c
atomic_val_t atomic_set(int *p,atomic_val_t v){atomic_val_t o=*p;*p=v;return o;}
```

and after line 96 (`static int (*_callback)(uint8_t *,size_t)=callback;`) add:

```c
static int (*_mode_callback)(uint8_t)=NULL;
```

Then add this method to `VoxTests`, before `test_partial_record_then_marker_and_failed_emit_retry`:

```python
    def test_mode_marker_follows_drained_audio_and_end_marker(self):
        self.compile_run(r'''
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <string.h>
#include <errno.h>
#include <assert.h>
#include <setjmp.h>
#define CODEC_PACKAGE_SAMPLES 4
#define CODEC_OPUS 1
#define OPUS_RESET_STATE 99
#define K_FOREVER 0
typedef int atomic_val_t;
static jmp_buf stop;
static int codec_data_sem,codec_end_done,codec_end_requested,codec_mode_requested,codec_end_result;
static int codec_ring_buf,m_opus_state,takes,gives,fail_mode;
static uint8_t pcm[12],codec_output_bytes[8];
static size_t left;
static int16_t codec_input_samples[4];
static char order[16];
void k_sem_take(int *s,int t){(void)s;(void)t;if(takes++)longjmp(stop,1);}
void k_sem_give(int *s){assert(s==&codec_end_done);gives++;}
bool atomic_cas(int *p,int a,int b){if(*p!=a)return false;*p=b;return true;}
atomic_val_t atomic_set(int *p,atomic_val_t v){atomic_val_t o=*p;*p=v;return o;}
size_t ring_buf_size_get(int *r){(void)r;return left;}
size_t ring_buf_get(int *r,uint8_t *out,size_t n){(void)r;assert(n<=left);memcpy(out,pcm+12-left,n);left-=n;return n;}
uint16_t execute_codec(void){return 3;}
int callback(uint8_t *data,size_t len){(void)data;strcat(order,len?"a":"e");return 0;}
int mode_callback(uint8_t mode){assert(left==0);strcat(order,mode==1?"L":"N");return fail_mode?-EIO:0;}
static int (*_callback)(uint8_t *,size_t)=callback;
static int (*_mode_callback)(uint8_t)=mode_callback;
void opus_encoder_ctl(int s,int op){(void)s;assert(op==99);}
''' + function(SRC/'lib/core/codec.c','void codec_entry()') + r'''
static void run(void){takes=0;if(!setjmp(stop))codec_entry();}
int main(void){
 /* Drained audio, then the end marker, then the lecture marker. */
 memset(pcm,1,sizeof(pcm));left=12;codec_end_requested=1;codec_mode_requested=1+1;
 run();assert(!strcmp(order,"aaeL") && gives==2 && codec_end_result==0 && !codec_mode_requested);
 /* A mode marker alone (resume inside a lecture): no audio, no end marker. */
 order[0]=0;gives=0;codec_mode_requested=0+1;run();
 assert(!strcmp(order,"N") && gives==1 && codec_end_result==0);
 /* Its failure reaches the caller. */
 order[0]=0;gives=0;fail_mode=1;codec_mode_requested=1+1;run();
 assert(!strcmp(order,"L") && gives==1 && codec_end_result==-EIO);
 /* Without a mode callback the request fails instead of hanging. */
 _mode_callback=NULL;gives=0;codec_mode_requested=1+1;run();
 assert(gives==1 && codec_end_result==-ENODEV);
 return 0;
}
''')
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_vox_c -v`
Expected: 2 ERRORS — the old `codec_entry` never reads the request, so the existing codec test fails to compile with `unused variable 'codec_mode_requested'` / `unused variable '_mode_callback'` (`-Werror`), and the new test aborts on `assert(!strcmp(order,"aaeL") ...)` (`died with <Signals.SIGABRT: 6>`).

- [ ] **Step 3: Implement** — in `omi/src/lib/core/codec.c`:

Replace lines 22–27:

```c
static volatile codec_callback _callback = NULL;

void set_codec_callback(codec_callback callback)
{
    _callback = callback;
}
```

with:

```c
static volatile codec_callback _callback = NULL;
static volatile codec_mode_callback _mode_callback = NULL;

void set_codec_callback(codec_callback callback)
{
    _callback = callback;
}

void set_codec_mode_callback(codec_mode_callback callback)
{
    _mode_callback = callback;
}
```

Replace lines 36–46 (from `static atomic_t codec_end_requested;` through the end of `codec_end_recording`) with:

```c
static atomic_t codec_end_requested;
static atomic_t codec_mode_requested; /* mode + 1; 0 = none */
static int codec_end_result;

int codec_end_recording(void)
{
    atomic_set(&codec_end_requested, 1);
    k_sem_give(&codec_data_sem);
    k_sem_take(&codec_end_done, K_FOREVER);
    return codec_end_result;
}

int codec_mark_mode(uint8_t mode)
{
    atomic_set(&codec_mode_requested, (atomic_val_t) mode + 1);
    k_sem_give(&codec_data_sem);
    k_sem_take(&codec_end_done, K_FOREVER);
    return codec_end_result;
}
```

In `codec_entry`, after the end-request block's `k_sem_give(&codec_end_done);` and its closing `}` (line 118), still inside `while (1)`, add:

```c
        /* After any drain above, so the mode marker follows every earlier frame. */
        atomic_val_t mode = atomic_set(&codec_mode_requested, 0);
        if (mode) {
            codec_end_result = _mode_callback ? _mode_callback((uint8_t) (mode - 1)) : -ENODEV;
            k_sem_give(&codec_end_done);
        }
```

In `omi/src/lib/core/codec.h`, after line 8 (`void set_codec_callback(codec_callback callback);`) add:

```c
/* Ordered recording-mode marker (record_packer.h RECORDING_MODE_*). */
typedef int (*codec_mode_callback)(uint8_t mode);
void set_codec_mode_callback(codec_mode_callback callback);
```

and after line 14 (`int codec_end_recording(void);`) add:

```c
/* Same contract: call with the PCM producer paused; returns once the marker is written. */
int codec_mark_mode(uint8_t mode);
```

In `omi/src/main.c`, after line 391 (`set_codec_callback(codec_handler);`) add:

```c
    set_codec_mode_callback(transport_mark_mode);
```

(`main.c` already includes `lib/core/transport.h`.)

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_vox_c -v`
Expected: all 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/lib/core/codec.c omi/firmware/omi/src/lib/core/codec.h omi/firmware/omi/src/main.c \
        omi/firmware/scripts/omi-local/tests/test_vox_c.py
git commit -m "feat(firmware): codec thread orders recording-mode markers after the drain"
```

---

### Task 4: Lecture state on the microphone owner (entry, exit, auto-exit)

**Files:**
- Modify: `omi/Kconfig` (after `OMI_AAD_WAKE_HAPTIC_MIN_SLEEP_MS`, lines 127–134)
- Modify: `omi/src/mic.c` (includes 15–19; owner statics after line 103; after `mic_toggle_manual_pause` line 372; after `#ifdef CONFIG_OMI_ENABLE_T5838_AAD` line 392; before `aad_thread_fn` line 548; `aad_thread_fn` lines 558–573)
- Modify: `omi/src/lib/core/mic.h` (after line 32)
- Create: `scripts/omi-local/tests/test_lecture_mode_c.py`
- Modify: `scripts/omi-local/tests/test_manual_recording_c.py` (owner harness, after line 46)

- [ ] **Step 1: Write the failing test** — create `tests/test_lecture_mode_c.py`:

```python
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
static void note(const char *s){assert(strlen(calls)+strlen(s)<sizeof(calls));strcat(calls,s);}
int atomic_get(int *p){return *p;}
void atomic_set(int *p,int v){*p=v;}
void atomic_clear(int *p){*p=0;}
void atomic_xor(int *p,int v){*p^=v;}
bool atomic_cas(int *p,int a,int b){if(*p!=a)return false;*p=b;return true;}
void k_sem_give(int *s){(void)s;}
void k_sem_take(int *s,int t){(void)s;(void)t;if(steps++)longjmp(stop,1);}
void k_msleep(int ms){assert(ms==150 || ms==20);}
uint32_t k_uptime_get_32(void){return now;}
void k_timer_start(struct k_timer *t,int d,int p){assert(t==&lecture_timer && p==0);timer_on=true;timer_ms=d;}
void k_timer_stop(struct k_timer *t){assert(t==&lecture_timer);timer_on=false;}
void aad_wake_irq(bool on){irq=on;}
void pdm_hw_disable(void){assert(!running);}
void t5838_aad_power(bool on){rail=on;}
void t5838_aad_release_clk(void){}
void t5838_aad_enter(void){assert(!running);}
void sd_request_power(bool on){sd=on;}
bool wifi_upload_active(void){return upload;}
int mic_pause(void){running=false;note("P");return 0;}
int codec_end_recording(void){assert(!running && sd);note("E");return end_error;}
int codec_mark_mode(uint8_t mode){assert(!running && sd);note(mode==RECORDING_MODE_LECTURE?"L":"N");return mark_error;}
int mic_resume(void){assert(mic_discard_blocks==5 && rail);mic_discard_blocks=0;running=true;note("R");return 0;}
void play_haptic_pulses(uint8_t n){assert(n==2 && !running);note("2");}
void play_haptic_milli(int ms){assert(ms==80);buzzes++;}
void enter_hw_aad(void){running=false;aad_in_sleep=1;if(!is_connected && !upload)sd=false;note("Z");}
'''
HELPERS = r'''
static void step(void){steps=0;if(!setjmp(stop))aad_thread_fn(NULL,NULL,NULL);}
static void expect(const char *want){assert(!strcmp(calls,want));calls[0]=0;}
'''

@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class LectureModeTests(unittest.TestCase):
    def run_main(self, body):
        source = (PRELUDE + '\n'.join(function(SRC/'mic.c', n) for n in NAMES) + HELPERS
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


if __name__ == '__main__':
    unittest.main()
```

In `tests/test_manual_recording_c.py`, the owner harness compiles the real `aad_thread_fn`, which will now call the lecture transitions. After line 46 (`static jmp_buf stop;static int steps;`) add:

```c
static int lecture_requested,lecture_active;
static void enter_lecture(void){assert(0);}
static void exit_lecture(void){assert(0);}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_lecture_mode_c tests.test_manual_recording_c -v`
Expected: `test_entry_exit_auto_exit_and_failures` ERROR with `ValueError: substring not found` (`mic_toggle_lecture` does not exist); `test_owner_serializes_pause_wake_resume_and_failures` ERROR with `unused variable 'lecture_requested'` / `unused function 'enter_lecture'` (`-Werror`). The other two manual-recording tests pass.

- [ ] **Step 3: Implement**

In `omi/Kconfig`, after the `OMI_AAD_WAKE_HAPTIC_MIN_SLEEP_MS` entry (after line 134, before `config OMI_MIC_START_DISCARD_MS`) add:

```
config OMI_LECTURE_MAX_MS
    int "Lecture mode automatic exit (ms)"
    range 60000 86400000
    default 14400000
    depends on OMI_ENABLE_T5838_AAD
    help
        "Lecture mode (triple-click) ends by itself after this long, exactly as if
         triple-clicked again: end marker, normal-mode marker and two pulses.
         Default 4 hours."

config OMI_LECTURE_LED_PWM
    int "Lecture mode yellow LED level (percent)"
    range 1 100
    default 10
    help
        "PWM duty of the red and green LEDs that show solid yellow during lecture
         mode. Kept low so hours of solid light cost little battery."
```

In `omi/src/mic.c`:

After line 17 (`#include "lib/core/mic_highpass.h"`) add:

```c
#include "lib/core/record_packer.h"
```

After line 103 (`static bool manual_end_pending; /* owner-only: retry failed boundary before resume */`) add:

```c
/* Lecture mode (triple-click): one recording, no acoustic sleep, ends after
 * CONFIG_OMI_LECTURE_MAX_MS. Not saved; a reboot returns to normal mode. */
static atomic_t lecture_requested;  /* button / timer: wanted state */
static atomic_t lecture_active;     /* owner: applied state */
static bool lecture_marker_pending; /* owner-only: re-mark lecture before resumed audio */
#define LECTURE_EXIT_RETRY_MS 60000 /* a failed exit retries every minute */
BUILD_ASSERT(IS_ENABLED(CONFIG_TIMEOUT_64BIT), "the lecture timer needs 64-bit kernel timeouts");
static void lecture_timer_expired(struct k_timer *timer);
static K_TIMER_DEFINE(lecture_timer, lecture_timer_expired, NULL);
```

After `mic_toggle_manual_pause` (after line 372, before `bool mic_is_manually_paused(void)`) add:

```c
int mic_toggle_lecture(void)
{
#ifdef CONFIG_OMI_ENABLE_T5838_AAD
    if (!aad_thread_started)
        return -EAGAIN;
    atomic_xor(&lecture_requested, 1);
    k_sem_give(&aad_sem);
    return 0;
#else
    return -ENOTSUP;
#endif
}

bool mic_in_lecture(void)
{
#ifdef CONFIG_OMI_ENABLE_T5838_AAD
    return atomic_get(&lecture_active) != 0;
#else
    return false;
#endif
}
```

After line 392 (`#ifdef CONFIG_OMI_ENABLE_T5838_AAD`, before the `pdm_hw_disable` comment) add:

```c

/* Auto-exit: behaves exactly like a triple-click that leaves lecture mode. */
static void lecture_timer_expired(struct k_timer *timer)
{
    ARG_UNUSED(timer);
    atomic_clear(&lecture_requested);
    k_sem_give(&aad_sem);
}
```

Before `static void aad_thread_fn(void *p1, void *p2, void *p3)` (line 548) add:

```c
/* Lecture transitions also run only on the owner. Markers need the SD card even
 * from pause or acoustic sleep; a later POWER_OFF drains queued writes first. */
static void enter_lecture(void)
{
    bool paused = atomic_get(&manual_paused);
    bool asleep = atomic_get(&aad_in_sleep);
    bool recording = !paused && !asleep;
    int ret = 0;

    sd_request_power(true);
    if (recording) {
        ret = mic_pause(); /* stop PCM so the end marker follows every frame */
        if (!ret)
            ret = codec_end_recording();
    } else if (paused && manual_end_pending) {
        ret = codec_end_recording();
        if (!ret)
            manual_end_pending = false;
    }
    if (!ret)
        ret = codec_mark_mode(RECORDING_MODE_LECTURE);
    if (ret) {
        atomic_clear(&lecture_requested); /* no vibration: the user sees it failed */
        LOG_ERR("Lecture mode unavailable (%d)", ret);
    } else {
        atomic_set(&lecture_active, 1);
        atomic_clear(&aad_req_sleep);
        lecture_marker_pending = false;
        k_timer_start(&lecture_timer, K_MSEC(CONFIG_OMI_LECTURE_MAX_MS), K_NO_WAIT);
#ifdef CONFIG_OMI_ENABLE_HAPTIC
        play_haptic_pulses(2); /* mic is stopped; the startup discard covers the rest */
#endif
    }

    if (recording) {
        atomic_set(&aad_woke, 1);
        atomic_set(&mic_discard_blocks, MIC_START_DISCARD_BLOCKS);
        if (mic_resume())
            LOG_ERR("Mic restart after lecture boundary failed");
    } else if (ret) {
        if (!is_connected && !wifi_upload_active())
            sd_request_power(false);
    } else if (paused) {
        atomic_clear(&manual_pause_requested);
        exit_manual_pause();
    } else {
        aad_sleep_started_ms = k_uptime_get_32(); /* the double pulse replaces the wake buzz */
        exit_hw_aad();
    }
}

static void exit_lecture(void)
{
    bool paused = atomic_get(&manual_paused);
    bool asleep = atomic_get(&aad_in_sleep);
    bool recording = !paused && !asleep;
    int ret = 0;

    sd_request_power(true);
    if (recording) {
        ret = mic_pause();
        if (!ret)
            ret = codec_end_recording();
    } else if (paused && manual_end_pending) {
        ret = codec_end_recording();
        if (!ret)
            manual_end_pending = false;
    }
    if (!ret)
        ret = codec_mark_mode(RECORDING_MODE_NORMAL);
    if (ret) {
        /* Stay in lecture mode; another triple-click or the retry timer tries again. */
        atomic_set(&lecture_requested, 1);
        k_timer_start(&lecture_timer, K_MSEC(LECTURE_EXIT_RETRY_MS), K_NO_WAIT);
        LOG_ERR("Lecture exit blocked by recording boundary (%d)", ret);
    } else {
        k_timer_stop(&lecture_timer);
        atomic_clear(&lecture_active);
        lecture_marker_pending = false;
#ifdef CONFIG_OMI_ENABLE_HAPTIC
        play_haptic_pulses(2);
#endif
    }

    if (recording) {
        atomic_set(&aad_woke, 1); /* restart the VOX silence timer */
        atomic_set(&mic_discard_blocks, MIC_START_DISCARD_BLOCKS);
        if (mic_resume())
            LOG_ERR("Mic restart after lecture boundary failed");
    } else if (!is_connected && !wifi_upload_active()) {
        sd_request_power(false); /* paused or asleep: back to idle storage */
    }
}

```

In `aad_thread_fn`, after `k_sem_take(&aad_sem, K_FOREVER);` (line 558) and its blank line, before `bool requested = atomic_get(&manual_pause_requested);`, add:

```c
        bool lecture = atomic_get(&lecture_requested);
        if (lecture && !atomic_get(&lecture_active))
            enter_lecture();
        else if (!lecture && atomic_get(&lecture_active))
            exit_lecture();

```

and replace line 571:

```c
        if (atomic_cas(&aad_req_sleep, 1, 0) && !atomic_get(&aad_in_sleep)) {
```

with:

```c
        if (atomic_cas(&aad_req_sleep, 1, 0) && !atomic_get(&aad_in_sleep) && !atomic_get(&lecture_active)) {
```

In `omi/src/lib/core/mic.h`, after line 32 (`bool mic_manual_pause_led_on(void);`) add:

```c
/* Nonblocking triple-click toggle; the microphone owner writes the markers. */
int mic_toggle_lecture(void);
bool mic_in_lecture(void);
```

Behavior notes, all exercised by the test: entering while recording pauses the PDM producer so the end marker follows every frame, then restarts it with the 500 ms startup discard; the two 80 ms pulses (280 ms total) play while the microphone is stopped and end inside that discard, so they are not recorded. Entering from acoustic sleep writes no second end marker (sleep entry already wrote one) and suppresses the 80 ms wake buzz. A failed entry leaves normal mode with no vibration. A failed exit stays in lecture mode and retries after `LECTURE_EXIT_RETRY_MS`.

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_lecture_mode_c tests.test_manual_recording_c -v`
Expected: all 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/Kconfig omi/firmware/omi/src/mic.c omi/firmware/omi/src/lib/core/mic.h \
        omi/firmware/scripts/omi-local/tests/test_lecture_mode_c.py \
        omi/firmware/scripts/omi-local/tests/test_manual_recording_c.py
git commit -m "feat(firmware): lecture mode transitions on the microphone owner"
```

---

### Task 5: Manual pause inside lecture mode

**Files:**
- Modify: `omi/src/mic.c` (`enter_manual_pause` lines 509–511, `exit_manual_pause` lines 526–528)
- Test: `scripts/omi-local/tests/test_lecture_mode_c.py`, `scripts/omi-local/tests/test_manual_recording_c.py`

- [ ] **Step 1: Write the failing test** — add this method to `LectureModeTests` in `tests/test_lecture_mode_c.py`, after `test_entry_exit_auto_exit_and_failures`:

```python
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
```

In `tests/test_manual_recording_c.py`, the owner harness compiles the real `enter_manual_pause` / `exit_manual_pause`, which will now use the lecture marker. After line 29 (`#include <stddef.h>` in the first harness) add:

```c
#include "lib/core/record_packer.h"
```

and after the three lines added in Task 4 (`static void exit_lecture(void){assert(0);}`) add:

```c
static bool lecture_marker_pending;
int codec_mark_mode(uint8_t mode){(void)mode;assert(0);return 0;}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_lecture_mode_c tests.test_manual_recording_c -v`
Expected: `test_pause_inside_lecture_and_entry_or_exit_from_pause` ERROR — `Assertion failed: (manual_paused && mic_in_lecture() && lecture_marker_pending && ...)` (`died with <Signals.SIGABRT: 6>`); `test_owner_serializes_pause_wake_resume_and_failures` ERROR — `unused variable 'lecture_marker_pending'`. The others pass.

- [ ] **Step 3: Implement** — in `omi/src/mic.c`, `enter_manual_pause`, between `atomic_clear(&aad_req_sleep);` (line 509) and `manual_pause_started_ms = k_uptime_get_32();` (line 510) add:

```c
    lecture_marker_pending = atomic_get(&lecture_active); /* resume stays a lecture */
```

In `exit_manual_pause`, between the `manual_end_pending` block's closing `}` (line 527) and `k_msleep(MANUAL_RESUME_MOTOR_SETTLE_MS);` (line 528) add:

```c
    if (lecture_marker_pending) {
        /* Mark the lecture again so the resumed recording is a lecture too. */
        int ret = codec_mark_mode(RECORDING_MODE_LECTURE);
        if (ret) {
            atomic_set(&manual_pause_requested, 1);
            LOG_ERR("Manual resume blocked by lecture marker (%d)", ret);
            return;
        }
        lecture_marker_pending = false;
    }
```

A failed re-mark keeps the device paused, like a failed end-marker retry; another 3–5 s hold retries.

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_lecture_mode_c tests.test_manual_recording_c -v`
Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/mic.c omi/firmware/scripts/omi-local/tests/test_lecture_mode_c.py \
        omi/firmware/scripts/omi-local/tests/test_manual_recording_c.py
git commit -m "feat(firmware): pause inside a lecture resumes as a lecture"
```

---

### Task 6: VOX never requests acoustic sleep during a lecture

**Files:**
- Modify: `omi/src/mic.c` (`aad_track_silence`, lines 605–606)
- Test: `scripts/omi-local/tests/test_vox_c.py` (`test_speech_band_sustain_hold_and_wake_reset`, lines 33 and 66–67)

- [ ] **Step 1: Write the failing test** — in `tests/test_vox_c.py` replace line 33:

```c
static int aad_woke,aad_in_sleep,aad_req_sleep,aad_sem;
```

with:

```c
static int aad_woke,aad_in_sleep,aad_req_sleep,aad_sem,lecture_active;
```

and after line 66 (` syncing=false; block(quiet,200100); assert(aad_req_sleep);`) add:

```c
 /* Lecture mode never asks for acoustic sleep, even after hours of silence. */
 aad_req_sleep=0; lecture_active=1; block(quiet,300000); block(quiet,14400000); assert(!aad_req_sleep);
 lecture_active=0; block(quiet,14400100); assert(aad_req_sleep);
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_vox_c -v`
Expected: `test_speech_band_sustain_hold_and_wake_reset` FAILS — `died with <Signals.SIGABRT: 6>` on `assert(!aad_req_sleep)`.

- [ ] **Step 3: Implement** — in `omi/src/mic.c`, `aad_track_silence`, replace lines 605–606:

```c
    if (!atomic_get(&aad_in_sleep) && !storage_transfer_active() &&
        (now - aad_last_voice_ms) >= CONFIG_OMI_VAD_HOLD_MS) {
```

with:

```c
    /* Lecture mode never sleeps: one lecture is one recording. */
    if (!atomic_get(&aad_in_sleep) && !atomic_get(&lecture_active) && !storage_transfer_active() &&
        (now - aad_last_voice_ms) >= CONFIG_OMI_VAD_HOLD_MS) {
```

The owner-side guard from Task 4 also drops a sleep request that was already queued when the lecture started.

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_vox_c tests.test_mic_highpass_c -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/mic.c omi/firmware/scripts/omi-local/tests/test_vox_c.py
git commit -m "feat(firmware): no acoustic sleep while lecture mode is on"
```

---

### Task 7: Tap-sequence logic (`button_taps.h`)

**Files:**
- Create: `omi/src/lib/core/button_taps.h`
- Create: `scripts/omi-local/tests/test_button_taps_c.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_button_taps_c.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_button_taps_c -v`
Expected: ERROR — `fatal error: 'lib/core/button_taps.h' file not found`.

- [ ] **Step 3: Implement** — create `omi/src/lib/core/button_taps.h`:

```c
#ifndef OMI_BUTTON_TAPS_H
#define OMI_BUTTON_TAPS_H
#include <stdbool.h>
#include <stdint.h>

/*
 * Short-press sequences. A tap is a press released within BUTTON_TAP_MAX_MS; the next tap
 * must start at most BUTTON_TAP_GAP_MS after the previous release. A sequence is reported
 * once, when that window closes, so a triple-click never reports a double tap first. A
 * fourth tap cancels the sequence; a longer press (a hold) ends it without a result, and
 * button_hold.h decides what the hold does.
 */
enum button_tap_result { TAPS_NONE = 0, TAPS_SINGLE = 1, TAPS_DOUBLE = 2, TAPS_TRIPLE = 3 };

#define BUTTON_TAP_MAX_MS 300U
#define BUTTON_TAP_GAP_MS 600U
#define BUTTON_TAPS_CANCELLED 0xFFU

struct button_taps {
    bool pressed;
    uint8_t count; /* taps in the open sequence, or BUTTON_TAPS_CANCELLED */
    uint32_t press_ms;
    uint32_t release_ms;
};

static inline enum button_tap_result button_taps_close(struct button_taps *t)
{
    enum button_tap_result done = t->count == BUTTON_TAPS_CANCELLED ? TAPS_NONE : (enum button_tap_result) t->count;
    t->count = 0;
    return done;
}

/* Feed every button poll. Returns a sequence's result once, when its window has closed. */
static inline enum button_tap_result button_taps_update(struct button_taps *t, uint32_t now, bool pressed)
{
    enum button_tap_result done = TAPS_NONE;

    if (pressed && !t->pressed) {
        if (t->count && now - t->release_ms > BUTTON_TAP_GAP_MS)
            done = button_taps_close(t); /* this press starts a new sequence */
        t->press_ms = now;
    } else if (!pressed && t->pressed) {
        if (now - t->press_ms < BUTTON_TAP_MAX_MS) {
            if (t->count != BUTTON_TAPS_CANCELLED)
                t->count = t->count == 3 ? BUTTON_TAPS_CANCELLED : (uint8_t) (t->count + 1);
            t->release_ms = now;
        } else {
            t->count = 0; /* a hold cancels the tap count */
        }
    } else if (!pressed && t->count && now - t->release_ms > BUTTON_TAP_GAP_MS) {
        done = button_taps_close(t);
    }
    t->pressed = pressed;
    return done;
}

static inline bool button_taps_pending(const struct button_taps *t)
{
    return t->count != 0;
}
#endif
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_button_taps_c -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/lib/core/button_taps.h omi/firmware/scripts/omi-local/tests/test_button_taps_c.py
git commit -m "feat(firmware): tap-sequence logic for the triple-click"
```

---

### Task 8: Triple-click in the button handler

**Files:**
- Modify: `omi/src/lib/core/button.c` (include line 15; defines lines 153–154; state lines 166–168; `check_button_level` lines 174–284)
- Test: `scripts/omi-local/tests/test_button_taps_c.py`, `scripts/omi-local/tests/test_manual_recording_c.py` (hold harness, lines 150–178)

- [ ] **Step 1: Write the failing tests** — add this method to `ButtonTapTests` in `tests/test_button_taps_c.py`, after `test_triple_click_timings_cancel_and_delayed_reports`:

```python
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
```

In `tests/test_manual_recording_c.py`, update the hold harness of `test_hold_windows_vibrate_while_held_and_act_on_release` (its assertions are unchanged; holds must behave exactly as before):

After line 150 (`#include "lib/core/button_hold.h"`) add:

```c
#include "lib/core/button_taps.h"
```

Delete lines 161–162:

```c
#define TAP_THRESHOLD 300
#define DOUBLE_TAP_WINDOW 600
```

Replace line 170:

```c
static uint32_t now,current_time,btn_last_pressed_time,btn_press_start_time,btn_release_time,btn_last_tap_time;
```

with:

```c
static uint32_t now,current_time,btn_last_pressed_time,btn_press_start_time;
```

after line 171 (`static u_int8_t btn_last_event;`) add:

```c
static struct button_taps btn_taps;
```

and after line 174 (`int mic_toggle_manual_pause(void){toggles++;return 0;}`) add:

```c
int mic_toggle_lecture(void){assert(0);return 0;}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_button_taps_c tests.test_manual_recording_c -v`
Expected: `test_button_handler_toggles_lecture_and_delays_tap_notifications` and `test_hold_windows_vibrate_while_held_and_act_on_release` ERROR — `use of undeclared identifier 'btn_release_time'` / `'btn_last_tap_time'` / `'TAP_THRESHOLD'` (the old handler still uses them). The others pass.

- [ ] **Step 3: Implement** — in `omi/src/lib/core/button.c`:

After line 15 (`#include "button_hold.h"`) add:

```c
#include "button_taps.h"
```

Delete lines 153–154 (and the blank line after them):

```c
#define TAP_THRESHOLD 300     // 300 ms for single tap
#define DOUBLE_TAP_WINDOW 600 // 600 ms maximum for double-tap
```

Replace lines 167–168:

```c
static uint32_t btn_release_time;
static uint32_t btn_last_tap_time;
```

with:

```c
static struct button_taps btn_taps; /* short-press sequence: tap, double tap, lecture triple-click */
```

Replace the whole of `check_button_level` (lines 174–284) with:

```c
void check_button_level(struct k_work *work_item)
{
    current_time = k_uptime_get_32();

    u_int8_t btn_state = was_pressed ? BUTTON_PRESSED : BUTTON_RELEASED;

    ButtonEvent event = BUTTON_EVENT_NONE;

    if (btn_state == BUTTON_PRESSED)
        btn_last_pressed_time = current_time;

    /* Taps are reported only once their 600 ms window closes: a triple-click never
     * sends a double tap first, and a fourth tap or a hold cancels the sequence. */
    switch (button_taps_update(&btn_taps, current_time, btn_state == BUTTON_PRESSED)) {
    case TAPS_SINGLE:
        event = BUTTON_EVENT_SINGLE_TAP;
        break;
    case TAPS_DOUBLE:
        event = BUTTON_EVENT_DOUBLE_TAP;
        break;
    case TAPS_TRIPLE:
        if (!is_off) {
            int ret = mic_toggle_lecture();
            if (ret)
                LOG_WRN("Lecture toggle unavailable (%d)", ret);
        }
        break;
    case TAPS_NONE:
        break;
    }

    // Debouncing pressed state
    if (btn_state == BUTTON_PRESSED && !btn_is_pressed) {
        btn_is_pressed = true;
        btn_press_start_time = current_time;
        hold_level_played = 0;
    } else if (btn_state == BUTTON_RELEASED && btn_is_pressed) {
        btn_is_pressed = false;

        /* Every gesture is hold-then-release; the vibration while held said which. */
        /* Act on the last hold the pulses could have signalled, not the release poll. */
        uint32_t felt_duration = btn_last_pressed_time - btn_press_start_time;
        switch (button_hold_action(felt_duration, IS_ENABLED(CONFIG_OMI_WIFI_UPLOAD))) {
        case HOLD_PAUSE_TOGGLE:
            if (!is_off) {
                int ret = mic_toggle_manual_pause();
                if (ret)
                    LOG_WRN("Recording toggle unavailable (%d)", ret);
            }
            break;
        case HOLD_POWER_OFF:
            turnoff_all();
            break;
        case HOLD_SETUP:
#ifdef CONFIG_OMI_WIFI_UPLOAD
            wifi_upload_request_provisioning();
            notify_long_tap();
#endif
            break;
        case HOLD_NONE:
            break;
        }
    }

    /* One release notification once a gesture, including any tap sequence, is over. */
    if (event == BUTTON_EVENT_NONE && !btn_is_pressed && !button_taps_pending(&btn_taps) &&
        (current_time - btn_press_start_time) > BUTTON_TAP_MAX_MS) {
        event = BUTTON_EVENT_RELEASE;
    }

#ifdef CONFIG_OMI_ENABLE_HAPTIC
    if (btn_is_pressed) {
        uint8_t level = button_hold_level(current_time - btn_press_start_time, IS_ENABLED(CONFIG_OMI_WIFI_UPLOAD));
        if (level > hold_level_played) {
            hold_level_played = level;
            play_haptic_pulses(level);
        }
    }
#endif

    // Single tap
    if (event == BUTTON_EVENT_SINGLE_TAP) {
        LOG_INF("single tap detected\n");
        btn_last_event = event;

        notify_tap();
    }

    // Double tap
    if (event == BUTTON_EVENT_DOUBLE_TAP) {
        LOG_INF("double tap detected\n");
        btn_last_event = event;
        notify_double_tap();
    }

    // Releases, one time event
    if (event == BUTTON_EVENT_RELEASE && btn_last_event != BUTTON_EVENT_RELEASE) {
        LOG_PRINTK("release detected\n");
        btn_last_event = event;
        notify_unpress();

        // Reset
        btn_press_start_time = 0;
    }
    if (event == BUTTON_EVENT_RELEASE) {
        current_button_state = GRACE;
    }

    k_work_reschedule(&button_work, K_MSEC(BUTTON_CHECK_INTERVAL));
    return;
}
```

Hold handling, hold pulses and the hold-length rule are unchanged. The only BLE-visible change is the spec's: single/double tap notifications now arrive after the 600 ms window instead of early, and a triple-click sends none.

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_button_taps_c tests.test_manual_recording_c tests.test_wifi_config_c -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/lib/core/button.c omi/firmware/scripts/omi-local/tests/test_button_taps_c.py \
        omi/firmware/scripts/omi-local/tests/test_manual_recording_c.py
git commit -m "feat(firmware): triple-click toggles lecture mode"
```

---

### Task 9: Dim solid yellow LED while in lecture mode

**Files:**
- Modify: `omi/src/main.c` (`set_led_state`, before the comment at line 201)
- Test: `scripts/omi-local/tests/test_led_state_c.py`

- [ ] **Step 1: Write the failing test** — in `tests/test_led_state_c.py`:

After line 21 (`#define BATTERY_FULL_THRESHOLD_PERCENT 100`) add:

```c
#define CONFIG_OMI_LECTURE_LED_PWM 10
typedef enum { LED_RED, LED_GREEN, LED_BLUE } led_color_t;
```

Replace line 25:

```c
static bool manual_pause,pause_flash;
```

with:

```c
static bool manual_pause,pause_flash,lecture;
static int red_level,green_level;
```

Replace lines 28–31:

```c
void set_led_red(bool v){red=v;}
void set_led_green(bool v){green=v;}
void set_led_blue(bool v){blue=v;}
void led_off(void){red=green=blue=false;}
```

with:

```c
void set_led_red(bool v){red=v;red_level=v?100:0;}
void set_led_green(bool v){green=v;green_level=v?100:0;}
void set_led_blue(bool v){blue=v;}
void set_led_pwm(led_color_t c,uint8_t level){
 if(c==LED_RED){red=level>0;red_level=level;}else if(c==LED_GREEN){green=level>0;green_level=level;}else blue=level>0;}
void led_off(void){red=green=blue=false;red_level=green_level=0;}
bool mic_in_lecture(void){return lecture;}
```

After line 75 (` is_off=true;pause_flash=true;set_led_state();assert(!red && !green && !blue);`), before ` return 0;`, add:

```c
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_led_state_c -v`
Expected: FAIL — `died with <Signals.SIGABRT: 6>` on `assert(red && green && !blue && red_level==10 && green_level==10)`.

- [ ] **Step 3: Implement** — in `omi/src/main.c`, `set_led_state`, insert before line 201 (`/* Normal recorder indication overrides charging/connection colours.`):

```c
    /* Lecture mode: dim solid yellow, in place of the normal recorder indication.
     * Pause feedback and the warnings above stay visible; a low PWM duty keeps
     * hours of light cheap. */
    if (mic_in_lecture()) {
        set_led_blue(false);
        set_led_pwm(LED_RED, CONFIG_OMI_LECTURE_LED_PWM);
        set_led_pwm(LED_GREEN, CONFIG_OMI_LECTURE_LED_PWM);
        return;
    }

```

`set_led_pwm` takes a percent of the PWM period (`led.c` lines 59–89); every other state sets the same channels through `set_led_red/green`, which overwrite the duty, so no state leaks between them.

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_led_state_c -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/main.c omi/firmware/scripts/omi-local/tests/test_led_state_c.py
git commit -m "feat(firmware): dim solid yellow LED during lecture mode"
```

---

### Task 10: Version, portal help and docs

**Files:**
- Modify: `omi/omi.conf` (line 112), `omi/overlay-wifi-upload.conf` (line 87)
- Modify: `omi/src/portal.html` (line 3)
- Test: `scripts/omi-local/tests/test_portal_page.py`
- Modify: `AGENTS.md` (firmware component guide, lines 35–57), `PROVISIONING.md` (lines 49–54)
- Modify: `SYSTEM.md` (repository root, "Firmware and transport", after the Button bullet ending at line 79)

- [ ] **Step 1: Write the failing test** — append to `tests/test_portal_page.py` (this class needs no Node, so it runs everywhere):

```python


class PortalHelpTests(unittest.TestCase):
    def test_help_describes_every_button_gesture_it_mentions(self):
        text = PAGE.read_text()
        self.assertIn('three times (20 seconds)', text)
        self.assertIn('Triple-click the button to start or stop lecture mode', text)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_portal_page -v`
Expected: `test_help_describes_every_button_gesture_it_mentions` FAILS — `AssertionError: 'Triple-click the button to start or stop lecture mode' not found in ...`.

- [ ] **Step 3: Implement**

`omi/src/portal.html` line 3: replace

```html
hold the button until it buzzes three times (20 seconds), then release.</p>
```

with

```html
hold the button until it buzzes three times (20 seconds), then release. Triple-click the button to start or stop lecture mode: two buzzes confirm it, and the light stays yellow while a lecture records.</p>
```

Version: in `omi/omi.conf` line 112 and `omi/overlay-wifi-upload.conf` line 87 change `CONFIG_BT_DIS_FW_REV_STR="3.0.22-localwifi.19"` to:

```
CONFIG_BT_DIS_FW_REV_STR="3.0.22-localwifi.20"
```

Firmware `AGENTS.md`:

- Line 35, replace `Normal recording uses no LEDs; acoustic sleep uses solid red. Preserve the` with `Normal recording uses no LEDs; acoustic sleep uses solid red; lecture mode uses dim solid yellow (`CONFIG_OMI_LECTURE_LED_PWM`). Preserve the`.
- Line 42, replace `pulses). Short clicks and releases in the 5–10 s / 15–20 s dead bands do nothing. The` with `pulses). A triple-click toggles lecture mode (below); single and double clicks and releases in the 5–10 s / 15–20 s dead bands do nothing. The`.
- Line 54, replace `Short taps still send the BLE tap/double-tap notifications. `test_wifi_config_c.py` and` with `Single and double taps still send the BLE tap/double-tap notifications, but only after the 600 ms tap window closes (`button_taps.h`). `test_wifi_config_c.py` and`.
- After line 57 (`pause. Keep the portal help in sync.`) add a blank line and:

```markdown
Lecture mode (`.20`): a triple-click (three presses, each released within 300 ms, at
most 600 ms from a release to the next press; a fourth tap or a hold cancels) flips an
atomic for the microphone owner, like manual pause. Entry drains an end marker for an
open recording, writes a recording-mode marker (`"\0OMIMOD\1"` + mode byte, zero padding,
`record_packer.h`) through codec → TX queue (header `0xFFFF`) → pusher, plays 2 pulses
while the mic is stopped, and starts the mic from pause or acoustic sleep; the 500 ms
startup discard applies. Exit writes an end marker, the normal-mode marker and 2 pulses.
During a lecture VOX never requests acoustic sleep; manual pause still ends the recording
and resume re-marks the lecture before audio. `CONFIG_OMI_LECTURE_MAX_MS` (4 h) exits the
same way; a failed exit retries every minute. Not persistent across reboot.
`test_button_taps_c.py`, `test_lecture_mode_c.py`, `test_pusher_markers_c.py`,
`test_record_packer_c.py` and `test_led_state_c.py` execute the production code.
```

`PROVISIONING.md`:

- Line 49, replace `Short clicks and releases between windows do nothing. From `3.0.22-localwifi.15`.` with `Single and double clicks and releases between windows do nothing. From `3.0.22-localwifi.15`.`
- After line 54 (`the selected configuration; reboot or hold 20 seconds and release to start setup again.`) add a blank line and:

```markdown
**Triple-click** (three quick presses, each shorter than 0.3 s and at most 0.6 s apart)
toggles **lecture mode**, from `3.0.22-localwifi.20`. Two vibration pulses confirm entry
and exit, and the LED stays dim yellow while a lecture records. Entry ends the current
recording; everything until the next triple-click is one lecture recording, because the
30-second silence timer does not stop it. A 3–5 second hold still pauses, and resuming
continues the lecture. Lecture mode ends by itself after 4 hours and is not kept across a
restart. A fourth press cancels the gesture. Update the receiver first: older receivers
ignore the marker and treat lectures as normal recordings.
```

`SYSTEM.md` (repository root), after the Button bullet (the one ending `**The 20-second action opens setup; it is not a reboot.**`, line 79) add:

```markdown
- Lecture mode, prepared in `.20` (built, not flashed): a triple-click (three presses
  under 300 ms, at most 600 ms apart; a fourth tap or a hold cancels) toggles it.
  Single/double BLE tap notifications now wait for the tap window. Entry and exit write
  an end marker and a recording-mode marker (`"\0OMIMOD\1"` + mode byte) and vibrate
  twice; VOX never sleeps during a lecture; dim solid yellow LED below setup, transfer,
  pause and warning indications; auto-exit after `CONFIG_OMI_LECTURE_MAX_MS` (4 h); not
  persistent. Triple-click reliability on the real button, motor feel, LED colour and
  brightness, and the battery cost of 4 hours without acoustic sleep are not validated
  on hardware.
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_portal_page -v`
Expected: both PASS (the Node test is skipped where Node is missing).

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/omi.conf omi/firmware/omi/overlay-wifi-upload.conf omi/firmware/omi/src/portal.html \
        omi/firmware/scripts/omi-local/tests/test_portal_page.py omi/firmware/AGENTS.md \
        omi/firmware/PROVISIONING.md SYSTEM.md
git commit -m "docs(firmware): lecture mode gesture, LED and version 3.0.22-localwifi.20"
```

---

### Task 11: Format, full suite, local build, signature check (no flashing)

**Files:**
- Modify: `VALIDATION_MAC.md` (append a section)

- [ ] **Step 1: Format** — from `omi/firmware`:

```bash
clang-format -i omi/src/mic.c omi/src/main.c omi/src/lib/core/button.c omi/src/lib/core/button_taps.h \
  omi/src/lib/core/codec.c omi/src/lib/core/codec.h omi/src/lib/core/mic.h \
  omi/src/lib/core/record_packer.h omi/src/lib/core/transport.c omi/src/lib/core/transport.h
git diff --stat
```

Expected: no further changes. These files were clang-format clean at `d5be19d4b2` and the code above is already formatted with `omi/firmware/.clang-format`; if anything changed, it is only in lines this plan added. Review and keep it.

- [ ] **Step 2: Full host suite**

Run (from `omi/firmware/scripts/omi-local`): `python -m unittest discover -s tests -t .`
Expected: `OK`, with 8 more tests than before this plan (Task 1: 1, Task 2: 1, Task 3: 1, Tasks 4–5: 2, Tasks 7–8: 2, Task 10: 1).

Then from the repository root: `python scripts/test.py`
Expected: pipeline, receiver/native and (on macOS with Swift) the Mac app suites all pass.

- [ ] **Step 3: Local Wi-Fi build** (per `MAC_BUILD.md`; SDK outside the checkout)

```bash
NCS_ROOT="$HOME/ncs/v2.9.0" \
OMI_PROTOC_PYTHON="$HOME/ncs/protoc-venv/bin/python" \
OMI_PROTOC="$PWD/omi/firmware/scripts/protoc-native" \
  bash omi/firmware/scripts/build-cv1-macos.sh --wifi > /tmp/omi-build-20.log 2>&1; echo "exit $?"
tail -n 5 /tmp/omi-build-20.log
grep -A2 "Memory region" /tmp/omi-build-20.log | grep -E "FLASH|RAM"
grep -E "warning:.*(mic|button|codec|transport|main)\.c" /tmp/omi-build-20.log
```

Run from the repository root. The `OMI_PROTOC*` variables are only needed on Apple Silicon (see `MAC_BUILD.md`).
Expected: `exit 0`; the script lists `dfu_application.zip`, `merged.hex` and `merged_CPUNET.hex` in `omi/firmware/build/local-wifi`. The application image is about 874.7 KB of 949,760 flash bytes (≈92.1 %) and ≈429 KB RAM. The warnings grep shows only warnings that `.19` already had (`reset_count`/`threshold` in `button.c`, opus size checks in `codec.c`, unused boot sequences in `main.c`, battery prototypes in `transport.c`); none in the new code.

- [ ] **Step 4: Verify both OTA image signatures**

```bash
nrfutil toolchain-manager launch --ncs-version v2.9.0 -- \
  env PYTHONPATH="$HOME/ncs/v2.9.0/bootloader/mcuboot/scripts" \
  python "$PWD/omi/firmware/scripts/windows/verify_zip.py" "$PWD/omi/firmware/build/local-wifi/dfu_application.zip"
mkdir -p ~/omi-firmware
cp omi/firmware/build/local-wifi/dfu_application.zip ~/omi-firmware/Omi_CV1_OTA_3.0.22-localwifi.20.zip
shasum -a 256 ~/omi-firmware/Omi_CV1_OTA_3.0.22-localwifi.20.zip
```

Expected: `image 0 omi.signed.bin: OK ...` and `image 1 ipc_radio.bin: OK ...`, each with a digest. Use Nordic's toolchain Python: the protoc venv lacks `cbor`, which `imgtool` imports.

**Do not flash.** Do not run `flash_omi.py`, `nrfutil device`, SMP or any other upload to the device. Flashing waits for the owner's explicit request.

- [ ] **Step 5: Record the evidence** — append to `omi/firmware/VALIDATION_MAC.md`, using the numbers printed by Steps 2–4:

```markdown
## Firmware 3.0.22-localwifi.20: lecture mode — 2026-10-21

Triple-click toggles lecture mode: end marker, recording-mode marker
(`"\0OMIMOD\1"` + mode byte), two pulses, dim solid yellow LED, no acoustic sleep,
auto-exit after `CONFIG_OMI_LECTURE_MAX_MS` (4 h). Single/double BLE tap notifications
now wait for the 600 ms tap window. Hold gestures are unchanged.

Native C tests execute the production tap logic and button handler, the owner
transitions (entry/exit from recording, pause and acoustic sleep; pause inside a
lecture; auto-exit; failed markers), the codec/TX-queue/pusher marker order, VOX
suppression and the LED selector. Host suite: <the "Ran N tests" line from Step 2>, OK.
NCS 2.9.0 Wi-Fi sysbuild: app <FLASH line> / <RAM line> from Step 3.
Both OTA images pass MCUboot signature verification (`verify_zip.py`).
Archive: `~/omi-firmware/Omi_CV1_OTA_3.0.22-localwifi.20.zip`.
ZIP SHA-256: <shasum output from Step 4>.
App digest: <image 0 digest from Step 4>.
Network digest: <image 1 digest from Step 4>.

Not flashed. Not validated on hardware: triple-click reliability on the real button,
motor feel, yellow colour and brightness at 10 % PWM, and battery cost of a 4-hour
lecture without acoustic sleep.
```

Replace each `<...>` with the value printed by the named step before committing; leave none in the file.

- [ ] **Step 6: Commit**

```bash
git add omi/firmware/VALIDATION_MAC.md
git commit -m "docs(firmware): record the 3.0.22-localwifi.20 build (not flashed)"
```

---

## Self-review against the spec

Checked against spec section 1 and section 4; issues found while writing were fixed inline above.

| Spec requirement | Where |
|---|---|
| Triple-click: 3 presses < 300 ms, ≤ 600 ms release → press | Task 7 (`button_taps.h`, exact 299/300 ms and 600/601 ms boundaries), Task 8 at 40 ms polling |
| Fourth tap cancels | Task 7 (4 and 5 taps), Task 8 |
| Single/double notifications delayed until the window closes | Task 7, Task 8 (`taps`/`doubles` counters before and after the window) |
| Holds unchanged; a hold cancels the count | Task 8 keeps the hold block verbatim; existing hold test passes unchanged; taps + 3 s hold test |
| Button only flips an atomic and gives `aad_sem` | Task 4 `mic_toggle_lecture` |
| Entry: end marker if a recording is open, lecture marker, 2 pulses, start mic if paused/asleep, 500 ms discard | Task 4 (`PEL2R`, `L2R` from sleep), Task 5 (`L2R` from pause, `EL2R` with a pending end) |
| Exit: end marker, normal marker, 2 pulses, VOX resumes | Task 4 (`PEN2R`, then `Z` on the next silence request) |
| VOX never requests sleep during a lecture | Task 6 (`aad_track_silence`), Task 4 (owner drops a stale request) |
| Pause ends the recording; resume stays lecture and re-marks before audio | Task 5 (`PE`, `LR`) |
| Auto-exit after `CONFIG_OMI_LECTURE_MAX_MS` (default 4 h) like a triple-click | Task 4 (`lecture_timer_expired` → `PEN2R`), Kconfig default 14400000 |
| Not persistent across reboot | Lecture state lives in zero-initialised RAM statics only; nothing is written to settings/NVS |
| 444-byte record, `"\0OMIMOD\1"` + mode byte + zero padding; end marker unchanged; ring and protocol v1 unchanged | Task 1 (byte-exact assertion), Task 2 (through the real pusher), receiver parser decodes both |
| Marker order (end, then mode) on entry and exit | Tasks 2, 3, 4 |
| LED solid yellow, low PWM, recording slot below setup/transfer/pause/SD-full/RTC | Task 9 |
| Version `3.0.22-localwifi.20` in both conf files | Task 10 |
| Firmware `AGENTS.md`, portal help, `SYSTEM.md` | Task 10 (`PROVISIONING.md` too, because it holds the gesture table) |
| Rollout: receiver first, firmware afterwards, flashing on request | Prerequisite check; Task 11 explicitly stops before flashing |

Decisions the spec leaves open, made here:

- **Vibration is not recorded.** The spec accepts the vibration in the audio. With the producer paused for an exact boundary, the 280 ms of pulses play while the microphone is stopped and end inside the 500 ms startup discard. What is recorded is the mechanical click of the triple-click itself, at the end of the previous recording (entry) or of the lecture (exit).
- **SD power first.** From pause or acoustic sleep the card may be off. Both transitions call `sd_request_power(true)` before writing markers, and return it to idle afterwards when paused/asleep, offline and not uploading. `REQ_POWER_OFF` drains queued writes before unmounting (`sd_card.c` lines 1079–1093).
- **Exit while paused** writes the normal marker and pulses, and stays paused.
- **Failures.** A failed entry stays in normal mode with no pulses. A failed exit stays in lecture mode and retries every 60 s.
- **Timer.** The 4 h runs from entry, including any pause.
- **Release notification.** The BLE "release" notification now also waits for the tap window. Nothing in this fork consumes it.

## Risks that tests cannot cover

- **AAD owner stack (1024 bytes).** `enter_lecture` → `exit_manual_pause` / `exit_hw_aad` → `codec_mark_mode` → `LOG_ERR` nests one level deeper than before. The build links, but stack use was not measured. Check on the device (thread analyzer or `CONFIG_THREAD_STACK_INFO`) before the owner relies on it. If needed, raise `AAD_THREAD_STACK_SIZE`; RAM is at about 95 %.
- **Button bounce.** `was_pressed` comes from an undebounced GPIO ISR, sampled every 40 ms. A bounce caught by a poll can add a tap. That makes a real triple-click a cancelled "fourth tap", or turns a double click into a triple. Spec: triple-click reliability is not validated.
- **Motor and audio.** Hold pulses (button work queue) and lecture pulses (owner thread) share `haptic.c`'s non-atomic pulse state. Overlap is unlikely, because a triple-click and a hold cannot coincide, but it is not locked.
- **LED.** At 10 % PWM, red and green differ in efficiency, so the light may look orange or green and be hard to see in daylight. `set_led_pwm` ignores the `dim_ratio` setting. Tune `CONFIG_OMI_LECTURE_LED_PWM` on hardware.
- **Battery.** A lecture keeps the PDM microphone, codec and SD active for up to 4 hours, plus a solid LED. This is not measured.
- **Flash.** At about 92.1 % flash, headroom is small for later features.
