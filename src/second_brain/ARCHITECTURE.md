# Service architecture and ownership

`Config` resolves absolute machine paths. `Runtime` holds an exclusive process
lock for the data directory, a receiver listener, one processing worker, and a
heartbeat writer. The receiver remains responsive while model subprocesses run.

## Receipt → job → note

1. `omi_local.SessionWriter` writes packed audio, fsyncs it, then atomically
   publishes its resume checkpoint. New checkpoints include exact file length
   and metadata. Resume truncates an uncommitted tail; a failed write cannot
   advance the cursor in shutdown cleanup. Old receiver checkpoints are readable.
2. `ReceiverFactory` honors existing root receiver state; new devices receive
   independent directories. `DurableWriter.fsync` emits immutable ready receipts
   only for closed recordings after durable checkpoint state, before ACK.
   Startup reconstructs any missing receipts from committed completed files.
3. Discovery places each recording in SQLite with source identity and SHA-256.
   One worker atomically claims jobs. Attempts use separate directories. The
   source hash is checked before processing, and a saved complete manifest is
   the replay boundary between ASR and vault publication.
4. The adaptive v3 gate retains its supplied policy tests. Progress is an atomic
   small JSON file, independent of transcript logs. Model commands are argument
   lists (no shell); the supervising worker bounds total runtime. Failures and
   heuristic/ASR fallbacks are visible in job results. Vibe padding is filtered
   to the selected window and speaker labels are locally scoped.
5. The vault writer creates deterministic names from job and window identity.
   A flushed temporary file is linked into place without replacing an existing
   note. Retrying identical output is a no-op; different existing content is a
   conflict. Only kept windows are published. SQLite completion follows note
   publication; a crash in between safely repeats it.

## Windows ownership

The .NET service runs under a virtual service account in Session 0. It starts
Python with explicit paths, waits for a graceful stdin stop, and kills the
process tree if it cannot stop in time. A Job Object kills descendants even if
the supervisor dies; abnormal exits trigger SCM recovery. The service does not
rely on the desktop, current user PATH, GUI, model downloads, or mapped drives.

The tray is a separate WinForms process. Its read-only status file contains
counts, stage names and sanitized operations. It tolerates replacement while
reading and reports a stale heartbeat after 15 seconds. Detailed transcripts,
keys and private logs stay behind service/admin filesystem ACLs.

## Verification boundaries

`python scripts/test.py`: original policy tests, state transitions, source
validation, receiver authentication/resume, real ffmpeg decoding, fixture-ASR
orchestration, vault idempotency and recovery, plus native firmware tests.

`dotnet run --project windows/SecondBrain.Status.Tests`: shared status presenter.
`windows/smoke-test.ps1`: installs a separately named service, asserts Session 0,
sends synthetic audio, checks a note and restart recovery, then removes only the
test service. It retains its uniquely named test directory as evidence. CI runs
this against the actual Windows SCM; it never connects to a real Omi or vault.

Model recognition quality, the user's exact engine installations, physical Omi
capture and the live vault need separate real-world checks. Test fixtures must
never be installed as production model configuration.
