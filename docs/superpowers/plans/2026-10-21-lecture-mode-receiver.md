# Lecture mode — receiver implementation plan (1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The receiver recognizes the firmware's new mode marker record and records `"mode": "normal" | "lecture"` in every recording's sidecar JSON.

**Architecture:** A mode marker is a 444-byte record whose payload starts with `"\0OMIMOD\1"` followed by one mode byte. `protocol.parse_record` returns it as a frameless `Record` with `mode` set. `SessionWriter` closes any open file on a mode marker, remembers the mode in `DeviceState.mode` (persisted in `state.json`, so it survives uploads that end between the marker and the first audio record), and stamps each new file with it. With no marker, the mode is `normal`, so older firmware is unaffected.

**Tech Stack:** Python 3 stdlib, `unittest`. Spec: `docs/superpowers/specs/2026-10-21-lecture-mode-design.md` section 2.

Run all commands from `omi/firmware/scripts/omi-local`. Suite: `python -m unittest discover -s tests -t .`

---

### Task 1: Parse the mode marker

**Files:**
- Modify: `omi_local/protocol.py` (the `# --- Records` section and `split_sessions`)
- Test: `tests/test_protocol.py`

- [ ] **Step 1: Write the failing tests** — add to `tests/test_protocol.py` before `if __name__`:

```python
def mode_marker(ts: int, mode: int) -> bytes:
    payload = P.RECORD_MODE_MAGIC + bytes([mode])
    return struct.pack(">I", ts) + payload + bytes(P.AUDIO_PAYLOAD_BYTES - len(payload))


class ModeMarkerTests(unittest.TestCase):
    def test_parses_known_modes(self):
        self.assertEqual(P.parse_record(4, mode_marker(100, 1)), P.Record(4, 100, (), mode="lecture"))
        self.assertEqual(P.parse_record(5, mode_marker(0, 0)), P.Record(5, 0, (), mode="normal"))

    def test_unknown_mode_byte_is_processed_as_normal(self):
        # A future firmware mode must not stall uploads to this receiver.
        self.assertEqual(P.parse_record(1, mode_marker(100, 9)).mode, "normal")

    def test_nonzero_padding_is_rejected(self):
        raw = bytearray(mode_marker(100, 1))
        raw[-1] = 1
        with self.assertRaises(P.ProtocolError):
            P.parse_record(1, bytes(raw))

    def test_audio_and_end_records_have_no_mode(self):
        self.assertIsNone(P.parse_record(0, make_record(0, 100)).mode)

    def test_mode_marker_splits_sessions_and_is_not_counted(self):
        data = make_record(0, 100) + mode_marker(100, 1) + make_record(2, 100)
        sessions = P.split_sessions(P.iter_records(0, data))
        self.assertEqual([(s.start_seq, s.end_seq) for s in sessions], [(0, 1), (2, 3)])
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_protocol -v`
Expected: FAIL / ERROR — `AttributeError: module 'omi_local.protocol' has no attribute 'RECORD_MODE_MAGIC'`.

- [ ] **Step 3: Implement** — in `omi_local/protocol.py` replace the block from `RECORD_END_MAGIC = ...` through the end-marker branch of `parse_record`:

```python
RECORD_END_MAGIC = b"\x00OMIEND\x01"
# Recording-mode marker: magic, then one mode byte. The leading 0 makes
# receivers older than lecture mode treat it as an empty record.
RECORD_MODE_MAGIC = b"\x00OMIMOD\x01"
RECORD_MODES = {0: "normal", 1: "lecture"}

@dataclass(frozen=True)
class Record:
    seq: int
    timestamp: int  # UTC epoch seconds, 0 = clock was not set when recorded
    frames: tuple[bytes, ...]
    recording_end: bool = False
    mode: str | None = None  # set only on a mode marker record

    @property
    def has_time(self) -> bool:
        return self.timestamp != 0


def parse_record(seq: int, raw: bytes) -> Record:
    """Parse timestamp + packed Opus frames, or a reserved zero-frame marker."""
    if len(raw) != RECORD_SIZE:
        raise ProtocolError(f"record {seq}: expected {RECORD_SIZE} bytes, got {len(raw)}")
    timestamp = struct.unpack_from(">I", raw, 0)[0]
    if raw[TIMESTAMP_BYTES:].startswith(RECORD_END_MAGIC):
        if any(raw[TIMESTAMP_BYTES + len(RECORD_END_MAGIC):]):
            raise ProtocolError(f"record {seq}: invalid recording-end padding")
        return Record(seq, timestamp, (), recording_end=True)
    if raw[TIMESTAMP_BYTES:].startswith(RECORD_MODE_MAGIC):
        mode_at = TIMESTAMP_BYTES + len(RECORD_MODE_MAGIC)
        if any(raw[mode_at + 1:]):
            raise ProtocolError(f"record {seq}: invalid recording-mode padding")
        # An unknown (future) mode is processed as normal rather than refused.
        return Record(seq, timestamp, (), mode=RECORD_MODES.get(raw[mode_at], "normal"))
```

(The frame-parsing loop below it is unchanged.)

In `split_sessions`, change the marker check:

```python
        if rec.recording_end or rec.mode is not None:
            prev = None
            continue
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_protocol -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add omi_local/protocol.py tests/test_protocol.py
git commit -m "feat(receiver): parse the recording-mode marker record"
```

---

### Task 2: Persist the device mode in receiver state

**Files:**
- Modify: `omi_local/state.py` (`DeviceState`, `StateStore.get`)
- Test: `tests/test_checkpoint.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_checkpoint.py` (add `import tempfile`, `from pathlib import Path`, `from omi_local.state import StateStore` if missing):

```python
class DeviceModeStateTests(unittest.TestCase):
    def test_mode_defaults_to_normal_and_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            store = StateStore(Path(d))
            st = store.get("dev")
            self.assertEqual(st.mode, "normal")
            st.mode = "lecture"
            store.put(st)
            self.assertEqual(StateStore(Path(d)).get("dev").mode, "lecture")

    def test_state_written_before_lecture_mode_loads_as_normal(self):
        with tempfile.TemporaryDirectory() as d:
            store = StateStore(Path(d))
            store.dir.mkdir(parents=True)
            store.path.write_text('{"dev": {"downloaded_through": 7}}')
            st = StateStore(Path(d)).get("dev")
            self.assertEqual((st.downloaded_through, st.mode), (7, "normal"))
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_checkpoint -v`
Expected: FAIL — `AttributeError: 'DeviceState' object has no attribute 'mode'`.

- [ ] **Step 3: Implement** — in `omi_local/state.py` add the field at the end of `DeviceState`:

```python
    files: list[dict] = field(default_factory=list)
    mode: str = "normal"  # last recording-mode marker seen from this device
```

and in `StateStore.get` add the keyword to the returned `DeviceState(...)`:

```python
            files=list(d.get("files", [])),
            mode=str(d.get("mode", "normal")),
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_checkpoint -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omi_local/state.py tests/test_checkpoint.py
git commit -m "feat(receiver): persist each device's recording mode"
```

---

### Task 3: Stamp recordings with their mode

**Files:**
- Modify: `omi_local/cli.py` (`SessionWriter._open`, `SessionWriter.add`)
- Test: `tests/test_cli_writer.py`

- [ ] **Step 1: Write the failing tests** — add to `SessionWriterTests` in `tests/test_cli_writer.py`:

```python
    @staticmethod
    def _end(ts):
        return struct.pack('>I', ts) + P.RECORD_END_MAGIC + bytes(P.AUDIO_PAYLOAD_BYTES - len(P.RECORD_END_MAGIC))

    @staticmethod
    def _mode(ts, mode):
        payload = P.RECORD_MODE_MAGIC + bytes([mode])
        return struct.pack('>I', ts) + payload + bytes(P.AUDIO_PAYLOAD_BYTES - len(payload))

    def _sidecars(self, dest):
        return [json.loads(p.read_text()) for p in sorted(dest.glob('*.json'))]

    def test_mode_marker_tags_following_recordings(self):
        ts = 1_700_000_000
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            store = StateStore(dest)
            w = SessionWriter(dest, 'dev', store.get('dev'), store)
            # normal audio, end, lecture marker, lecture audio, end, normal marker, audio
            w.add(0, make_record(0, ts) + self._end(ts) + self._mode(ts, 1) + make_record(3, ts + 1)
                  + self._end(ts + 1) + self._mode(ts + 1, 0) + make_record(6, ts + 2))
            w.finish(final=True)
            self.assertEqual([(m['start_seq'], m['mode']) for m in self._sidecars(dest)],
                             [(0, 'normal'), (3, 'lecture'), (6, 'normal')])

    def test_mode_marker_closes_an_open_recording(self):
        ts = 1_700_000_000
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            store = StateStore(dest)
            w = SessionWriter(dest, 'dev', store.get('dev'), store)
            w.add(0, make_record(0, ts) + self._mode(ts, 1) + make_record(2, ts))
            w.finish(final=True)
            metas = self._sidecars(dest)
            self.assertEqual([(m['start_seq'], m['complete'], m['mode']) for m in metas],
                             [(0, True, 'normal'), (2, True, 'lecture')])

    def test_mode_survives_an_upload_that_ends_after_the_marker(self):
        ts = 1_700_000_000
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            store = StateStore(dest)
            w = SessionWriter(dest, 'dev', store.get('dev'), store)
            w.add(0, self._end(ts) + self._mode(ts, 1))
            w.finish(final=False)
            store = StateStore(dest)
            w = SessionWriter(dest, 'dev', store.get('dev'), store)
            w.add(2, make_record(2, ts))
            w.finish(final=False)  # file stays open across uploads
            store = StateStore(dest)
            w = SessionWriter(dest, 'dev', store.get('dev'), store)
            w.add(3, make_record(3, ts) + self._end(ts))
            w.finish(final=False)
            metas = self._sidecars(dest)
            self.assertEqual([(m['start_seq'], m['end_seq'], m['mode']) for m in metas], [(2, 4, 'lecture')])

    def test_ogg_comment_carries_mode(self):
        ts = 1_700_000_000
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            store = StateStore(dest)
            w = SessionWriter(dest, 'dev', store.get('dev'), store)
            w.add(0, self._mode(ts, 1) + make_record(1, ts))
            w.finish(final=True)
            self.assertIn(b'OMI_MODE=lecture', next(dest.glob('*.opus')).read_bytes())
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_cli_writer -v`
Expected: the four new tests FAIL (`KeyError: 'mode'` / missing comment); existing tests still pass.

- [ ] **Step 3: Implement** — in `omi_local/cli.py`, `SessionWriter._open`, add the comment and the metadata key:

```python
            comments=[f"ENCODER=omi-local {__version__}", f"OMI_DEVICE={self.device_id}",
                      f"OMI_START_SEQ={rec.seq}", f"OMI_START_UTC={fmt_ts(rec.timestamp)}",
                      f"OMI_MODE={self.state.mode}"],
        )
        self._cur = {
            "file": str(path), "device": self.device_id, "start_seq": rec.seq, "end_seq": rec.seq,
            "first_timestamp": rec.timestamp, "last_timestamp": rec.timestamp,
            "first_utc": fmt_ts(rec.timestamp), "last_utc": fmt_ts(rec.timestamp),
            "packets": 0, "frames": 0, "audio_seconds": 0.0, "complete": False,
            "mode": self.state.mode,
        }
```

In `SessionWriter.add`, handle the marker right after the end-marker branch:

```python
            if rec.recording_end:
                self._close_file(eos=True)
                self.state.open_file = None
                self._prev = None
                continue
            if rec.mode is not None:
                # A mode change always starts a new recording.
                self._close_file(eos=True)
                self.state.open_file = None
                self.state.mode = rec.mode
                self._prev = None
                continue
```

Update the class docstring's first sentence to: `A session ends on an explicit recording-end or recording-mode marker, when ...`.

A resumed file keeps the mode from its checkpointed metadata (`OpenFile.metadata`), so `_resume_open_file` needs no change. Files from before this change have no `mode` key; the service treats a missing key as `normal`.

- [ ] **Step 4: Run to verify pass**

Run: `python -m unittest tests.test_cli_writer -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add omi_local/cli.py tests/test_cli_writer.py
git commit -m "feat(receiver): tag each recording with its device mode"
```

---

### Task 4: Wi-Fi upload end to end

**Files:**
- Test: `tests/test_upload.py`

- [ ] **Step 1: Write the test** — add to `ServerTests` in `tests/test_upload.py` (its `setUp` fills `self.ring` with 300 audio records at seq 0–299; `struct`, `json`, `P`, `FakeUploader`, `run` are already imported there):

```python
    def test_lecture_marker_reaches_sidecar_over_wifi(self):
        ts = 1_700_000_030
        end = struct.pack('>I', ts) + P.RECORD_END_MAGIC + bytes(P.AUDIO_PAYLOAD_BYTES - len(P.RECORD_END_MAGIC))
        payload = P.RECORD_MODE_MAGIC + b'\x01'
        lecture = struct.pack('>I', ts) + payload + bytes(P.AUDIO_PAYLOAD_BYTES - len(payload))
        self.ring.records[300] = end
        self.ring.records[301] = lecture
        self.ring.write_seq = 302
        self.ring.record_session(ts, 20)  # seq 302-321, lecture audio
        self.ring.records[self.ring.write_seq] = end
        self.ring.write_seq += 1
        up = FakeUploader(self.ring, self.secret)

        async def go(server):
            await up.run('127.0.0.1', server.bound_port)

        run(self._with_server(go))
        self.assertEqual(up.result, 'ok')
        metas = [json.loads(p.with_suffix('.json').read_text()) for p in sorted(self.dest.glob('*.opus'))]
        self.assertEqual([(m['start_seq'], m['mode'], m['complete']) for m in metas],
                         [(0, 'normal', True), (302, 'lecture', True)])
```

- [ ] **Step 2: Run**

Run: `python -m unittest tests.test_upload -v`
Expected: PASS (Tasks 1–3 already implement the behavior; this guards the server's validation path, which parses every record before ACK).

- [ ] **Step 3: Run the full suite**

Run: `python -m unittest discover -s tests -t .` then, from the repo root, `python scripts/test.py`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_upload.py
git commit -m "test(receiver): lecture mode marker survives a Wi-Fi upload"
```

---

### Task 5: Docs

**Files:**
- Modify: `omi/firmware/scripts/omi-local/README.md` (record-format / markers section near line 155)
- Modify: `SYSTEM.md` ("Firmware and transport" record-format bullet; "Receiver, queue, and crash boundaries" step 1)

- [ ] **Step 1:** In the README markers section add:

```markdown
A recording-mode marker (`"\0OMIMOD\1"` + mode byte: 0 normal, 1 lecture, zero padding)
ends any open recording and sets the mode of the recordings that follow. The sidecar
JSON carries `"mode"`, and the Ogg file an `OMI_MODE` comment. The last mode seen is kept
per device in `state.json`. Unknown mode bytes are treated as normal; receivers older
than this change ignore the marker.
```

- [ ] **Step 2:** In `SYSTEM.md`, after the end-marker sentence of the record-format bullet, add: `A recording-mode marker ("\0OMIMOD\1" + mode byte) starts lecture or normal recordings; the receiver writes "mode" into the sidecar.`

- [ ] **Step 3: Commit**

```bash
git add omi/firmware/scripts/omi-local/README.md SYSTEM.md
git commit -m "docs(receiver): document the recording-mode marker"
```
