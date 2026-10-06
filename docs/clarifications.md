# Clarifying incomplete notes

The tray's **Needs clarification** page lists useful notes that lack the person,
team, meeting, or project context needed to understand them. A count appears in
the navigation and a tray notification announces increases; notifications show
only the count, never note text. Click the notification or use the tray menu.

Select an entry, read its question and source excerpt, then edit the prefilled
sentence to include the missing name/context. Keep the date if useful. **Save
corrected note** updates that one entry in Obsidian. **Keep note as is** dismisses
the question without changing the note. These corrections do not label a voice
or train speaker recognition.

The service keeps a private SQLite review queue, original source excerpt, saved
answer, and note backup. Save verifies the original entry is unchanged before
replacing it; other prose remains intact. If you edited/moved the entry yourself,
the app refuses to overwrite it: finish editing in Obsidian and dismiss the
review. Requests use the existing restricted review mailbox; service responses
confirm completion. Interrupted saves can safely replay after restart.

The extraction prompt asks for complete context from the source before asking
the user. Explicit anonymous speaker references also receive a deterministic
check. This does not guarantee detection of every omission: semantic detection
depends on Hermes. The adaptive v3 memory gate is unchanged. Previously routed
notes are not automatically re-extracted. `scripts/flag-clarification.py` can
register a specific existing entry using its event ID and retained source.

Validation: filesystem/mailbox tests cover targeted updates, preserving human
edits, backup creation, dismissal, invalid answers, source replay, and recovery
after interruption between note write and database commit. Windows status tests
cover the catalog/mailbox and backward-compatible count; these are workflow
tests, not measurements of model detection accuracy.

Oct 4 validation: 35 Python tests passed initially, followed by two additional
clarification edge cases (seven clarification tests total). Windows status/review
tests pass and the Release tray publish succeeds. The Wednesday meeting entry
is registered locally. Activation is awaiting the Windows administrator prompt
from `scripts/install-clarification-update.ps1`; no live UI verification yet.
A check against the real meeting transcript through Hermes was blocked by
automatic approval review pending explicit permission to send that transcript;
no real-model detection accuracy is claimed.
