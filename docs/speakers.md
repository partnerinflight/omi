# Speaker review and voice memory

Right-click the tray icon and choose **Speakers — listen and name**. Each row is
a speaker within one recording/chunk, initially **Speaker 1**, **Speaker 2**, etc.
The same number in another recording does not imply the same person.

Select a row, choose one of up to five clips, and click **Play clip** (or
double-click the clip). Clips are at most 12 seconds; long turns can provide
several clips. The accompanying text is the turn transcript, which may extend
beyond that particular clip. Choose an existing person or type a name and click
**Assign name**. Existing people are offered so references from multiple
recordings accumulate under one stable identity.

**Rename person** updates that person's name throughout the review catalog.
**Clear assignment** makes the selected row explicitly unidentified and removes
its contribution to voice matching. **Forget person** removes their profile and
all assignments. Other automatic assignments are recalculated when confirmed
references change. Automatically matched clips never enroll themselves; only
human confirmations contribute reference voices.

Names appear immediately in review after the service acknowledges the change.
Changes made while the service is offline remain queued. Request IDs are durable
and replay-safe. Voice profiles and assignments survive service/PC restarts.

## Enable recognition across recordings

Manual review works without another model. Automatic recognition requires a
local speaker encoder, separate from ASR and diarization. Before installing the
service, run this once on Windows:

```powershell
.\windows\setup-speakers.ps1 `
  -Python 'C:\Program Files\Python312\python.exe' `
  -PipelineConfig 'C:\SecondBrainConfig\pipeline.json'
```

For a prebuilt bundle, the script is `setup-speakers.ps1` in the extracted folder.
It creates a separate CPU Python environment, downloads the public
[SpeechBrain ECAPA speaker model](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb),
and sets `speaker_python` and `speaker_model` in the private pipeline config.
The default revision is pinned in `config/engines.lock.json`; optional `-Revision`
accepts a full Hugging Face commit SHA. Runtime inference uses local
weights with downloads disabled. Model-file hashes identify the embedding space;
embeddings from different model versions are never compared. The encoder uses
SpeechBrain's [speaker embedding interface](https://speechbrain.readthedocs.io/en/latest/tutorials/tasks/speech-classification-from-scratch.html).

Install with `-ReviewUser 'YOUR-PC\Eugene'` to grant your normal login access to
speaker review. The default is the account running the elevated installer; set
this explicitly if you elevate using a different administrator account. Reinstall
after changing model/interpreter paths so the service receives the required ACLs.

## Matching behavior

Clean, non-overlapping, precisely timed turns of at least two seconds can become
reference clips. Silence and approximately timed Streaming VibeVoice turns are
excluded from voice enrollment. Those clips remain playable and manually
nameable. If there is no usable reference, the catalog says so instead of implying
recognition is active. Set up the encoder before naming recordings that should
become reusable voice references; already completed jobs are not automatically
re-encoded after enabling/changing models.

A future occurrence needs at least two usable clips whose embeddings all favor
the same confirmed person. Every clip must exceed `speaker_match_threshold`
(default cosine similarity 0.80) and beat the next person by
`speaker_match_margin` (default 0.10). These are conservative starting values,
not calibrated probabilities. Unknown, short, ambiguous and conflicting evidence
stays unidentified. Voice recognition is an estimate and can be corrected in the
UI; Omi microphone/noise conditions need real-world threshold validation.

Named future Obsidian notes include stable person IDs and whether each identity
was confirmed or voice-matched. Already published notes are preserved, including
after a correction; the app's catalog always reflects current mappings. A saved
publication manifest freezes names for crash recovery so late naming cannot
overwrite an existing note or turn a retry into a conflict.

## Local storage and access

- `ProgramData\SecondBrain\data\speakers.sqlite3`: private identities,
  embeddings, assignments and idempotent command results.
- `ProgramData\SecondBrain\review\catalog.json` and `clips\`: names,
  turn excerpts and PCM playback audio, readable only by the configured review
  user, service and administrators.
- `review\requests\`: the review user's narrowly scoped write access; the
  service validates assignment commands and owns all authoritative state.
- `review\responses\`: read-only acknowledgments for the app.

No listener, browser endpoint, cloud recognition service, or pairing-key access
is added. Public tray status remains operational metadata only. The installer
keeps the audio/identity review area separate from status readable by all users.
Back up private service data alongside recordings if voice memories must survive
a reinstall on another machine. Uninstall retains these files.
