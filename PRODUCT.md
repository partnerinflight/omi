# Second Brain local product

Omi records locally and uploads over the user's LAN to one paired receiver.
A Windows service receives durable audio, performs adaptive speech recognition
and speaker diarization, filters ephemeral conversation, and publishes durable
conversation notes into the user's Obsidian vault. No login or tray process is
required for the service. Models run locally; optional Hermes scoring uses an
explicitly configured endpoint and is disabled in the example configuration.

Original recordings and rejected-window transcripts remain private archives.
Novelty alone is not a reason to keep content. Speaker labels are local to an
ASR chunk, not identified people. Generated notes carry source and timestamp
provenance, and never overwrite human edits. A tray app reports current work,
recent successes/failures, queue counts and stale service status.
