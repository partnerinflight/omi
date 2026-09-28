# Second Brain Adaptive Audio v3

v3 adds a **durable-memory gate** so ordinary family chatter, small talk,
entertainment chatter, immediate household coordination, and other ephemeral
conversation do not pollute Second Brain.

The architecture is now:

```text
Omi audio
  ↓
MOSS C++ Q5_K
  ↓
natural conversation windows
  ↓
first-pass scoring
  ├─ importance
  ├─ novelty
  ├─ ASR uncertainty
  └─ durable-memory classification
       ├─ conversation type
       ├─ durability
       ├─ retrieval value
       ├─ actionability
       └─ durable-information signals
  ↓
clearly ephemeral + low value
  └─ keep only in transcript/archive; DO NOT run 7B; DO NOT send to router

potentially durable + ASR needs help
  └─ VibeVoice-ASR-Streaming-7B + vault hotwords

catastrophically broken first-pass ASR
  └─ VibeVoice 7B rescue even if the first-pass memory gate is uncertain

  ↓
final durable-memory gate
  ↓
KEEP → router_queue → Knowledge Router → Obsidian
DROP → filtered_out audit record only
```

## Core rule

**Novelty is not durability.**

A conversation can be completely new and still be worthless to remember.

Examples that should normally be filtered:

```text
"Want pancakes?"
"Put your shoes on."
"What are you doing?"
"Did you see that?"
"I played some Helldivers."
```

Examples that should pass even though they occur during family conversation:

```text
"Daniel's audition is October 3 at 4 PM."
"We decided to switch soccer teams."
"Remember to schedule Mia's dentist appointment."
"I think DeepQuill should launch at $39."
```

## Conversation types

The gate classifies each window as one of:

```text
decision
task_commitment
project_work
idea
planning_logistics
personal_durable_fact
family_chatter
small_talk
entertainment_media
background_audio
other_ephemeral
other
```

## Durable signals

A real durable signal normally passes the gate:

```text
decision
task
commitment
reusable idea
project information
date / future event
durable personal fact
```

Family chatter is therefore not dropped merely because family members are
speaking. It is dropped because it lacks durable content.

## 7B compute filtering

v3 also avoids wasting 7B on irrelevant chatter.

If a window looks ephemeral and low-value, even moderate ASR uncertainty does
not cause an expensive 7B pass.

There is one safety valve:

```text
vibe7_rescue_uncertainty_threshold = 92
```

If MOSS output looks catastrophically broken, 7B can still rescue the window
because the bad transcript itself may have hidden durable information.

## Install

Extract to:

```text
C:\second-brain-adaptive-audio-v3
```

Then:

```powershell
cd C:\second-brain-adaptive-audio-v3
Copy-Item config.example.json config.json
notepad config.json
.\setup_engines.ps1
```

All commands use `python`, never `py`.

## Recommended calibration

First run 30–60 minutes with 7B disabled:

```powershell
.\run_adaptive.ps1 `
  -Audio "D:\SecondBrain-Audio\incoming\sample.m4a" `
  -SkipVibe7 `
  -NoHermes
```

Inspect:

```text
C:\second-brain-adaptive\results\<timestamp>\report.md
C:\second-brain-adaptive\results\<timestamp>\manifest.json
C:\second-brain-adaptive\results\<timestamp>\filtered_out\
C:\second-brain-adaptive\results\<timestamp>\router_queue\
```

The key thing to evaluate is not transcription quality yet. Check whether:

- routine family chatter lands in `filtered_out`;
- actual decisions/tasks/events survive into `router_queue`;
- interesting-but-ephemeral content stays out;
- project/work conversations survive.

Only after the gate looks right should you enable 7B.

## Final-memory gate output

Every window gets:

```json
{
  "conversation_type": "family_chatter",
  "durability": 12,
  "actionability": 5,
  "retrieval_value": 16,
  "contains": {
    "decision": false,
    "task": false,
    "commitment": false,
    "idea": false,
    "project_information": false,
    "date_or_event": false,
    "personal_durable_fact": false
  },
  "durable_signal": false,
  "reasons": [
    "ephemeral type: family_chatter"
  ]
}
```

The manifest also records:

```text
memory_keep
memory_reason
route_to_knowledge_router
```

## Hermes

Hermes is optional. With Hermes disabled, the gate uses deterministic
heuristics.

When Hermes is enabled, its existing scoring call now also returns:

```text
conversation_type
durability
actionability
retrieval_value
durable-information flags
memory_keep
memory_reason
```

Those values are blended with deterministic scoring. The deterministic policy
still makes the final keep/drop decision.

This gives the model useful semantic judgment without allowing it to directly
write arbitrary content into the vault.

## Archive behavior

Filtered information is **not deleted**.

For every window:

```text
windows\wXXXX.final.txt
```

remains as the transcript archive.

Rejected windows additionally get:

```text
filtered_out\wXXXX.json
```

which records why the window was rejected and points back to its transcript.

Only durable windows get files in:

```text
router_queue\
```

So Second Brain stays curated without throwing away the underlying evidence.
