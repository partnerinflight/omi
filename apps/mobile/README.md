# Second Brain mobile (milestone 0 spike)

Expo SDK 57 / React Native 0.86 / TypeScript. Proves the native boundaries in
[implementation plan](../../docs/mobile/implementation-plan.md) milestone 0 with
synthetic data. There is no server yet, so every edit stays "Queued".

```sh
npm install
npm test && npm run typecheck && npx expo-doctor
npx expo run:ios            # simulator
npx expo run:ios --device   # physical iPhone (needs a signing team in Xcode)
```

Mac prerequisites: `sudo xcodebuild -runFirstLaunch` and the iOS simulator runtime
matching Xcode (`xcodebuild -downloadPlatform iOS`).

iOS 27 requires the UIScene life cycle; without it UIKit traps at launch.
`plugins/withSceneLifecycle.js` wires Expo's `ExpoAppSceneDelegate` into the
generated project; keep it until Expo's template does this itself.

If `expo run:ios` fails with an `osascript ... System Events` error after
"Build Succeeded", the terminal lacks macOS Automation permission. The app is
installed anyway: run `npx expo start --dev-client` and launch it from the simulator.

## Simulator results (2026-10-10, Xcode 27.0, iPhone 18 Pro, iOS 27.0)

- Launches; SQLite migrates; clip asset loads (3.1 s); Keychain read succeeds.
- Dark mode and accessibility-XXXL text render without clipping.
- Not yet exercised: tapping through todo/clip/Keychain flows, VoiceOver, physical device.

## Device checklist (record results, device and iOS version here)

- [ ] Airplane mode: add, complete, reopen, dismiss todos; force-quit; relaunch; all persist and show Queued
- [ ] Clip plays with ringer switch on silent; pauses on incoming call/Siri; resumes by tap; unplugging headphones pauses
- [ ] Keychain token survives relaunch; delete removes it
- [ ] VoiceOver reads headers, checkbox state, Queued count; all buttons reachable
- [ ] Largest Dynamic Type size: no clipped text; dark mode legible
- [ ] `npx expo run:android` builds (Android path feasibility)
