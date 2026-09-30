# SecondBrainCapture (macOS)

Menu-bar app that records allowlisted meeting apps while they use the microphone —
your mic on the left channel, the app's audio (other participants) on the right — and
uploads each meeting to the Second Brain receiver (protocol v2) as stereo Opus.
Needs macOS 14.4+ and Xcode (for `swift`).

## Build and install

    cd mac/SecondBrainCapture
    swift test
    scripts/build-app.sh
    ditto build/SecondBrainCapture.app ~/Applications/SecondBrainCapture.app
    open ~/Applications/SecondBrainCapture.app

On first launch approve **Microphone** and, at the first capture, **System Audio
Recording Only**. On its first launch the app registers itself as a login item (System
Settings → General → Login Items); if you remove it there, it stays removed. To update,
quit it from the menu, rebuild and repeat the `ditto`/`open`.

The app is signed ad hoc, so macOS may ask for Microphone and System Audio permission again
after each rebuild. After installing or updating, make one short test call to grant it.

## Configure

`~/Library/Application Support/SecondBrainCapture/config.json` is created on first run:

    {"host": "192.168.1.85", "port": 7331}

Other keys (defaults): `allowlist` (Chime, Zoom, Teams, Slack, Webex bundle IDs; browsers
are not included because a tap records every tab), `secretPath`
(`~/.omi-local/upload-secret.hex`), `minCaptureSeconds` (60), `releaseGraceSeconds` (20),
`spoolWarnBytes` (5 GB). Restart the app after editing.

Copy the receiver's pairing secret to this Mac: the same 64-hex-character file the Omi
uses: the Windows service's secret is `C:\ProgramData\SecondBrain\config\upload-secret.hex`
(or the `upload-secret.hex` in `%USERPROFILE%\.omi-local\` if you pair that way). Save it
as `~/.omi-local/upload-secret.hex` with `chmod 600`.

Until `host` is set (and the app restarted), captures are recorded but wait in the spool.

The Windows service must run a build that includes receiver protocol v2
(`windows/install.ps1` from a checkout containing it); older receivers reject the app.

## Behavior

- Starts when an allowlisted app (or its helper process) holds the mic; stops 20 s after
  it lets go. Captures under 60 s are discarded (the receiver is told they were cancelled).
- A capture also ends when the Mac goes to sleep, when the audio device configuration
  changes (for example headphones are connected), or when you quit the app. If the call
  continues, a new capture starts, so each file's time span is real. If devices keep
  changing (3 times within 10 s of a capture starting), the app stops, notifies you, and
  does not record again until the app releases the mic.
- Spool: `~/Library/Application Support/SecondBrainCapture/spool/` (0700). Audio is written
  as raw PCM with an fsync every 5 s, encoded to stereo Opus CAF (L mic, R app) when the
  meeting ends, and deleted only after the receiver confirms a verified commit. Captures
  interrupted by a crash or quit are finished on the next launch.
- If the app's audio stays silent for 20 s, the app notifies you and shows the warning in
  the menu ("No audio from ... yet"); check System Settings → Privacy & Security → Screen &
  System Audio Recording. It keeps recording, since the other side may really be silent.
- Uploads retry with backoff (30 s → 15 min) while the receiver is unreachable.
- Menu: Skip this meeting (discard it and tell the receiver), Pause for 1 hour / Resume,
  Open spool folder, Quit.

## Diagnose

    open -W build/SecondBrainCapture.app --args --probe --probe-tap us.zoom.xos 10
    cat ~/Library/Application\ Support/SecondBrainCapture/probe.txt

Lists audio processes (`MIC` = currently using input) and taps the given app for 10 s.
`peak=0.0` while the other side is audibly talking means System Audio permission is missing.
macOS gives no API to check that permission directly, so a silent capture cannot be told
apart from a silent meeting.
