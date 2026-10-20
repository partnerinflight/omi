# Installer Phase 1: Shared Install Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reinstalling the Windows service keeps every setting the owner or a later feature added to `service.json`, and the service can write meeting notes. The install logic lives in one shared core that the future wizard will call.

**Architecture:**
- **Merge in Python.** A small, unit-tested module, `second_brain.install_config`, merges `service.json` in three layers: defaults < existing < installer-managed. It validates the result with the real `Config.load` and writes it atomically.
- **PowerShell calls the merge.** The install script calls it through the installed worker Python, instead of regenerating the file from a fixed list.
- **Shared core.** The install logic moves to `installer/install-core.ps1`; `windows/install.ps1` becomes a wrapper with the same parameters, so the source path, the bundle and the future wizard share one implementation.
- **Meetings grant.** The core grants the service Modify on the configured `meetings_vault_folder`.

**Tech Stack:** Python 3.12 stdlib, unittest, PowerShell 7 / Windows PowerShell 5.1, icacls, GitHub Actions `windows-latest`.

Spec: `docs/superpowers/specs/2026-10-20-windows-installer-design.md`, "Implementation phases" item 1, and the two bugs in "Problem".

## The two bugs, precisely

1. **Settings lost on reinstall.** `windows/install.ps1` builds `$cfg` from a fixed `[ordered]@{...}` list and writes it with `ConvertTo-Json | Set-Content "$DataRoot\config\service.json"`. Every key outside that list is silently deleted on reinstall. That includes `meetings_enabled`, `meetings_vault_folder`, `owner_name`, `bridge_url`, `bridge_token_file`, `bridge_seconds`, `speaker_match_threshold`, `speaker_match_margin` and `delete_audio_after_processing`. Tuning values inside the list (`poll_seconds`, `retry_seconds`, `max_attempts`, `job_timeout_seconds`, `vault_folder`, `host`, `no_hermes`) are reset to defaults.
2. **Meeting notes can't be published.** The script grants the service Modify on `$Vault\Omi\Conversations` and the router folders only. The vault itself gets RX. Publishing a note into `$Vault\Omi\Meetings` (default `meetings_vault_folder`) fails with `PermissionError`.

## Key classes

- **Managed** (always set by the installer, from its parameters): `data_dir`, `incoming_dir`, `secret_file`, `pipeline_config`, `status_file`, `vault_path`, `port`, `ffmpeg_dir`, `skip_vibe7`.
- **Defaults** (written only when the key is absent): `vault_folder` (`Omi/Conversations`), `host` (`0.0.0.0`), `poll_seconds` (2), `retry_seconds` (60), `max_attempts` (5), `job_timeout_seconds` (14400), `no_hermes` (false).
- **Everything else** in an existing `service.json` is preserved as is.

PowerShell does not run on the owner's Mac or on the Linux CI job. The PowerShell changes are verified only by the real-SCM smoke test on the Windows CI job (Task 4). Every PowerShell step below says so.

---

## File map

| File | Responsibility |
|---|---|
| `src/second_brain/install_config.py` (create) | `merge_service_config()` + `main()` CLI: layered merge, validation via `Config.load`, atomic write |
| `tests/test_install_config.py` (create) | Unit tests for the merge, the CLI, and validation failures |
| `installer/install-core.ps1` (create) | The full install logic moved from `windows/install.ps1`, with the merge and the meetings grant |
| `windows/install.ps1` (modify) | Thin wrapper: same parameters, forwards to the core |
| `windows/build.ps1` (modify) | Copies `installer/install-core.ps1` into `dist/windows` |
| `windows/smoke-test.ps1` (modify) | Reinstall step: settings survive, meetings folder writable by the service |
| `README.md`, `docs/install-windows.md` (modify) | Reinstall now preserves settings; the meetings folder is granted |

Run from the repo root. Single module: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_install_config -v`. Full suite: `python3 scripts/test.py`. Its trailing `swift test` step fails in a sandboxed shell, which is pre-existing; both Python suites must print `OK`.

---

### Task 1: Layered config merge

**Files:**
- Create: `src/second_brain/install_config.py`
- Test: `tests/test_install_config.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_install_config.py`:

```python
"""service.json survives reinstall: installer-managed keys update, everything else is kept."""

from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MergeTests(unittest.TestCase):
    def merge(self, existing, managed, defaults):
        from second_brain.install_config import merge_service_config
        return merge_service_config(existing, managed, defaults)

    def test_installer_managed_keys_always_win(self):
        merged = self.merge({"port": 7331, "vault_path": "C:/old"}, {"port": 17331, "vault_path": "C:/new"}, {})
        self.assertEqual((merged["port"], merged["vault_path"]), (17331, "C:/new"))

    def test_settings_the_installer_does_not_manage_are_kept(self):
        existing = {"meetings_enabled": True, "owner_name": "Eugene", "bridge_url": "https://sb.example",
                    "speaker_match_threshold": 0.45}
        merged = self.merge(existing, {"port": 7331}, {})
        for key, value in existing.items():
            self.assertEqual(merged[key], value, key)

    def test_defaults_fill_only_missing_keys(self):
        merged = self.merge({"poll_seconds": 5}, {}, {"poll_seconds": 2, "retry_seconds": 60})
        self.assertEqual((merged["poll_seconds"], merged["retry_seconds"]), (5, 60))

    def test_a_fresh_install_is_defaults_plus_managed(self):
        merged = self.merge(None, {"port": 7331}, {"poll_seconds": 2})
        self.assertEqual(merged, {"poll_seconds": 2, "port": 7331})

    def test_inputs_are_not_mutated(self):
        existing, managed, defaults = {"a": 1}, {"b": 2}, {"c": 3}
        self.merge(existing, managed, defaults)
        self.assertEqual((existing, managed, defaults), ({"a": 1}, {"b": 2}, {"c": 3}))


def service_paths(root: Path) -> dict:
    for name in ("data", "incoming", "vault", "status"):
        (root / name).mkdir(exist_ok=True)
    (root / "secret.hex").write_text("0" * 64)
    (root / "pipeline.json").write_text("{}")
    return {"data_dir": str(root / "data"), "incoming_dir": str(root / "incoming"),
            "secret_file": str(root / "secret.hex"), "pipeline_config": str(root / "pipeline.json"),
            "vault_path": str(root / "vault"), "status_file": str(root / "status" / "status.json"),
            "port": 7331, "ffmpeg_dir": "", "skip_vibe7": False}


def run_cli(*args):
    env = {"PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, "-m", "second_brain.install_config", *args],
                          capture_output=True, text=True, env=env)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "service.json"
        self.installer = self.root / "installer.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_installer(self, managed, defaults=None):
        self.installer.write_text(json.dumps({"managed": managed, "defaults": defaults or {}}))

    def test_reinstall_keeps_meetings_and_bridge_settings(self):
        managed = service_paths(self.root)
        self.target.write_text(json.dumps({**managed, "meetings_enabled": True, "owner_name": "Eugene",
                                           "poll_seconds": 5}), encoding="utf-8-sig")   # BOM: PowerShell 5.1
        self.write_installer({**managed, "port": 17331}, {"poll_seconds": 2, "vault_folder": "Omi/Conversations"})
        proc = run_cli("--target", str(self.target), "--installer", str(self.installer))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        saved = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual((saved["meetings_enabled"], saved["owner_name"]), (True, "Eugene"))
        self.assertEqual((saved["port"], saved["poll_seconds"], saved["vault_folder"]), (17331, 5, "Omi/Conversations"))

    def test_a_fresh_install_creates_the_file(self):
        self.write_installer(service_paths(self.root), {"poll_seconds": 2})
        proc = run_cli("--target", str(self.target), "--installer", str(self.installer))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(self.target.read_text())["poll_seconds"], 2)

    def test_an_invalid_result_is_refused_and_the_old_file_is_untouched(self):
        managed = service_paths(self.root)
        original = json.dumps({**managed, "no_such_setting": 1})
        self.target.write_text(original)
        self.write_installer(managed)
        proc = run_cli("--target", str(self.target), "--installer", str(self.installer))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Unknown service configuration keys: no_such_setting", proc.stderr)
        self.assertEqual(self.target.read_text(), original)
        self.assertEqual(sorted(p.name for p in self.root.glob("service.json*")), ["service.json"])

    def test_an_unreadable_existing_file_is_refused_rather_than_replaced(self):
        self.target.write_text("{not json")
        self.write_installer(service_paths(self.root))
        proc = run_cli("--target", str(self.target), "--installer", str(self.installer))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.target.read_text(), "{not json")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_install_config -v`
Expected: FAIL. The merge tests raise `ModuleNotFoundError: No module named 'second_brain.install_config'`, and the CLI tests fail because the process exits non-zero.

- [ ] **Step 3: Implement**

Create `src/second_brain/install_config.py`:

```python
"""Merge the service configuration on install and reinstall.

Three layers: defaults (only when a key is missing) < the existing file < installer-managed keys
(paths and ports the installer owns). Anything else the owner or a later feature added survives a
reinstall. The merged result is validated with the service's own Config.load before it replaces
the file, so an invalid merge never reaches the service."""

from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .config import Config


def merge_service_config(existing, managed, defaults):
    return {**defaults, **(existing or {}), **managed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", required=True, type=Path, help="service.json to create or update")
    parser.add_argument("--installer", required=True, type=Path,
                        help='JSON file: {"managed": {...}, "defaults": {...}}')
    args = parser.parse_args(argv)
    installer = json.loads(args.installer.read_text(encoding="utf-8-sig"))
    existing = None
    if args.target.exists():
        try:
            existing = json.loads(args.target.read_text(encoding="utf-8-sig"))
        except ValueError as error:
            print(f"Existing {args.target} is not valid JSON ({error}); fix or remove it first.", file=sys.stderr)
            return 2
    merged = merge_service_config(existing, installer["managed"], installer.get("defaults", {}))
    args.target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=args.target.parent, prefix=args.target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(merged, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        try:
            Config.load(Path(temporary))
        except (ValueError, KeyError, TypeError) as error:
            print(f"Merged configuration is invalid: {error}", file=sys.stderr)
            return 1
        os.replace(temporary, args.target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps({"ok": True, "kept": sorted(set(existing or {}) - set(installer["managed"]))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Before relying on the validation test, read `src/second_brain/config.py` `Config.load`. It must raise `ValueError("Unknown service configuration keys: ...")` for unknown keys and accept the `service_paths` fixture. If `Config.load` needs more keys than the fixture provides, add them to `service_paths`, not to the assertions.

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_install_config -v`
Expected: 9 tests PASS.
Run: `python3 scripts/test.py`
Expected: both Python suites `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/install_config.py tests/test_install_config.py
git commit -m "feat(install): merge service.json on reinstall instead of regenerating it"
```

---

### Task 2: Move the install logic into the shared core

**Files:**
- Create: `installer/install-core.ps1`
- Modify: `windows/install.ps1`, `windows/build.ps1`

This is a pure move. Behaviour changes come in Task 3.

- [ ] **Step 1: Move the file**

```bash
mkdir -p installer
git mv windows/install.ps1 installer/install-core.ps1
```

- [ ] **Step 2: Fix the core's bundle default**

In `installer/install-core.ps1`, the bundle parameter defaults to the script's own folder (`[string]$Bundle = $PSScriptRoot,`). Leave it. In a built bundle, `install-core.ps1` sits in `dist/windows` next to `service\`, `tray\` and `python\` (Step 4), so `$PSScriptRoot` is still the bundle. Read the whole file and confirm that no other line assumes the file lives in `windows/`. Search for `$PSScriptRoot` and `..`. Report anything you find.

- [ ] **Step 3: Create the wrapper**

Create `windows/install.ps1`:

```powershell
#Requires -RunAsAdministrator
# Same parameters as before; the implementation is shared with the installer wizard.
# Run from a built bundle (dist\windows) or from the repository's windows\ folder.
$core = Join-Path $PSScriptRoot 'install-core.ps1'
if (-not (Test-Path $core)) { $core = Join-Path (Split-Path $PSScriptRoot -Parent) 'installer\install-core.ps1' }
if (-not (Test-Path $core)) { throw "install-core.ps1 not found next to $PSCommandPath or in installer\" }
& $core @args
if (-not $?) { throw 'Installation failed' }
```

`@args` splats every named and positional argument unchanged, so existing callers like `windows/smoke-test.ps1`, `windows/setup.ps1` and the docs keep working. Before relying on that, check two things: `windows/setup.ps1` calls `"$repo/dist/windows/install.ps1"`, and `smoke-test.ps1` calls `"$PSScriptRoot\install.ps1"` with `-Bundle`.

There is one subtlety. When the wrapper runs from `windows\` in a source checkout, the core's `$Bundle` default would be `installer\`, not a bundle. Every source-checkout caller already passes `-Bundle` (smoke test) or runs the copy in `dist\windows` (setup), so check both. If either does not, make the wrapper pass `-Bundle` explicitly when the caller didn't.

- [ ] **Step 4: Ship the core in the bundle**

In `windows/build.ps1`, after the line `Copy-Item (Join-Path $PSScriptRoot 'install.ps1') 'dist/windows/install.ps1' -Force`, add:

```powershell
    Copy-Item (Join-Path $repo 'installer/install-core.ps1') 'dist/windows/install-core.ps1' -Force
```

- [ ] **Step 5: Verify and commit**

PowerShell cannot run on the Mac or the Linux CI job. Check syntax by reading. The Windows CI job runs `build.ps1` and `smoke-test.ps1`, which exercise the wrapper and the core. Run `python3 scripts/test.py`; both Python suites must print `OK`.

```bash
git add installer/install-core.ps1 windows/install.ps1 windows/build.ps1
git commit -m "refactor(install): shared install core with install.ps1 as a wrapper"
```

---

### Task 3: Merge instead of overwrite; grant the meetings folder

**Files:**
- Modify: `installer/install-core.ps1`

- [ ] **Step 1: Replace the overwrite with the merge**

In `installer/install-core.ps1`, replace the whole `$cfg = [ordered]@{ ... }` block and the following `$cfg | ConvertTo-Json | Set-Content "$DataRoot\config\service.json" -Encoding UTF8` line with:

```powershell
# Reinstall keeps every setting the owner or a later feature added; only installer-owned keys
# change (src/second_brain/install_config.py). Defaults fill keys that are missing.
$installerKeys = [ordered]@{
    managed = [ordered]@{
        data_dir = "$DataRoot\data"; incoming_dir = [IO.Path]::GetFullPath($IncomingDir)
        secret_file = $targetKey; pipeline_config = $pipelineTarget
        vault_path = $Vault; status_file = "$DataRoot\status\status.json"; port = $Port
        ffmpeg_dir = $FfmpegDir; skip_vibe7 = [bool]$SkipVibe7
    }
    defaults = [ordered]@{
        vault_folder = 'Omi/Conversations'; host = '0.0.0.0'
        poll_seconds = 2; retry_seconds = 60; max_attempts = 5; job_timeout_seconds = 14400
        no_hermes = $false
    }
}
$installerFile = Join-Path $env:TEMP ('secondbrain-install-' + [guid]::NewGuid().ToString('N') + '.json')
try {
    $installerKeys | ConvertTo-Json -Depth 4 | Set-Content $installerFile -Encoding UTF8
    Run $workerPython @('-I', '-m', 'second_brain.install_config', '--target', "$DataRoot\config\service.json", '--installer', $installerFile)
} finally { Remove-Item $installerFile -ErrorAction SilentlyContinue }
$serviceConfig = Get-Content "$DataRoot\config\service.json" -Raw | ConvertFrom-Json
```

The managed and default values must be exactly the ones the removed block wrote. Diff the old block against this one and report any difference.

- [ ] **Step 2: Grant the meetings folder**

In `installer/install-core.ps1`, find the line that grants the service Modify on `"$Vault\Omi\Conversations"`. Directly after it, add:

```powershell
    # Meeting notes publish here (meetings_vault_folder). Granted even while meetings are off, so
    # turning them on later needs no reinstall. Config.load already rejects unsafe folder values.
    $meetingsFolder = if ($serviceConfig.meetings_vault_folder) { $serviceConfig.meetings_vault_folder } else { 'Omi/Meetings' }
    $meetingsPath = Join-Path $Vault ($meetingsFolder -replace '/', '\')
    New-Item -ItemType Directory -Force $meetingsPath | Out-Null
    Run icacls.exe @($meetingsPath, '/grant', "${principal}:(OI)(CI)M", '/T', '/Q')
```

Also make the Conversations grant use the configured `vault_folder`. The service publishes Omi notes to `vault_folder`, so an owner who changed it today loses write access on reinstall. Replace the literal `"$Vault\Omi\Conversations"` in that grant, and in the `$paths` directory list near the top, with a `$conversationsPath` computed from `$serviceConfig.vault_folder` the same way. Because the merge must happen before that path is known, move the directory creation for it to after the merge, and say what moved.

- [ ] **Step 3: Verify and commit**

PowerShell is not executable here, so verification is by reading plus the Windows CI smoke test (Task 4). Re-read the edited region top to bottom and check:
- `$workerPython`, `$targetKey` and `$pipelineTarget` are defined before the merge call.
- `$principal` is defined before the meetings grant (it is set inside the later `try` block, so the meetings grant must also be inside it).
- `$serviceConfig` is defined before both grants.

Run `python3 scripts/test.py`; both Python suites must print `OK`.

```bash
git add installer/install-core.ps1
git commit -m "fix(install): keep service settings on reinstall; let the service write meeting notes"
```

---

### Task 4: Prove it on a real Windows service in CI

**Files:**
- Modify: `windows/smoke-test.ps1`

The smoke test already installs a real service on the Windows runner, uploads synthetic audio and checks the note. This task adds a reinstall round trip.

- [ ] **Step 1: Add the reinstall assertions**

In `windows/smoke-test.ps1`, directly before the line that starts `Write-Host "PASS:`, insert:

```powershell
    # Reinstall must keep settings the installer does not own, and the service must be able to
    # write meeting notes (installer/install-core.ps1, src/second_brain/install_config.py).
    $serviceJson = "$root\machine\config\service.json"
    $edited = Get-Content $serviceJson -Raw | ConvertFrom-Json
    $edited | Add-Member -NotePropertyName meetings_enabled -NotePropertyValue $true -Force
    $edited | Add-Member -NotePropertyName owner_name -NotePropertyValue 'Smoke Owner' -Force
    $edited | Add-Member -NotePropertyName poll_seconds -NotePropertyValue 3 -Force
    $edited | ConvertTo-Json | Set-Content $serviceJson -Encoding UTF8
    & "$PSScriptRoot\uninstall.ps1" -ServiceName $name
    & "$PSScriptRoot\install.ps1" -Python $pythonPath -PipelineConfig "$root\pipeline.json" -SecretFile "$root\secret.hex" -Vault "$root\vault" -IncomingDir "$root\incoming" -DataRoot "$root\machine" -InstallDir "$root\app" -Bundle $Bundle -FfmpegDir $FfmpegDir -SkipVibe7 -ServiceName $name -Port 17331
    $kept = Get-Content $serviceJson -Raw | ConvertFrom-Json
    if (-not $kept.meetings_enabled -or $kept.owner_name -ne 'Smoke Owner' -or $kept.poll_seconds -ne 3) {
        throw "Reinstall lost settings: $(Get-Content $serviceJson -Raw)"
    }
    $sid = (New-Object Security.Principal.NTAccount(('NT SERVICE\' + $name))).Translate([Security.Principal.SecurityIdentifier]).Value
    $acl = Get-Acl "$root\vault\Omi\Meetings"
    $writable = $acl.Access | Where-Object {
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $sid -and
        ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::Modify) -eq [Security.AccessControl.FileSystemRights]::Modify
    }
    if (-not $writable) { throw 'The service cannot write meeting notes (no Modify on Omi\Meetings)' }
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 500
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
    } until (($status.service -eq 'running' -and $status.started -gt $previousStart) -or (Get-Date) -gt $deadline)
    if ($status.service -ne 'running') { throw 'Service did not come back after reinstall' }
```

Then extend the `PASS` message with `, reinstall keeps settings, meetings folder writable`.

Read the surrounding script before inserting. `$previousStart` is set before the restart check, and `$statusPath` is defined earlier. The `finally` block's `uninstall.ps1` still runs after the reinstalled service, which is correct. Check whether the meetings preflight would now refuse to start because `meetings_enabled` is true with no ffmpeg Opus decoder. The CI runner installs ffmpeg with Opus through choco, so it shouldn't. If preflight fails for another reason, report it rather than switching meetings off.

- [ ] **Step 2: Verify and commit**

This runs only on the Windows CI job (`./windows/smoke-test.ps1` after `build.ps1`). Push the branch and check that job before calling the task done, and report its result. If CI cannot be triggered from here, say so explicitly.

```bash
git add windows/smoke-test.ps1
git commit -m "test(windows): reinstall keeps settings and the meetings folder is writable"
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md`, `docs/install-windows.md`, `SYSTEM.md`

- [ ] **Step 1: Write it down**

- `docs/install-windows.md`, "Existing installations and individual steps": replace any instruction or implication that a reinstall needs every setting re-entered. Say that reinstall keeps every `service.json` setting the installer does not own. List the installer-owned keys (from this plan's "Key classes" section), and say that the service is granted write access to `vault_folder` and `meetings_vault_folder`. Remove the advice to edit `service.json` and then reinstall if it exists, or keep it now that it is safe.
- `README.md`: wherever `meetings_enabled` or `owner_name` setup is described, say that the values survive a reinstall.
- `SYSTEM.md`: add a dated entry (2026-10-20) under known gaps / fixed issues. Reinstall used to regenerate `service.json` and drop meetings, bridge and tuning settings, and the service could not write `Omi\Meetings`. Both are fixed by `installer/install-core.ps1` and `second_brain.install_config`, and proven by the Windows smoke test.

Find the exact places with `grep -n -i "service.json\|reinstall\|meetings_enabled" README.md docs/install-windows.md SYSTEM.md`, and match the surrounding density.

- [ ] **Step 2: Verify and commit**

Run `python3 scripts/test.py`; both Python suites must print `OK`.

```bash
git add README.md docs/install-windows.md SYSTEM.md
git commit -m "docs(install): reinstall keeps settings; meetings folder granted"
```

---

### Task 6: Final verification

- [ ] **Step 1:** Run `python3 scripts/test.py` twice. Both Python suites must print `OK`.
- [ ] **Step 2:** Push, and confirm the Windows CI job passes `build.ps1` and `smoke-test.ps1`. Report the run URL and result.
- [ ] **Step 3:** In the handoff, state:
  - **Not covered by CI:** only the synthetic service runs in CI. Nothing in CI touches the owner's real installation.
  - **The owner's existing install:** it already lost its meetings and bridge settings at the last reinstall, if any. After this lands, the owner should re-enter them once in `C:\ProgramData\SecondBrain\config\service.json`, then upgrade with `uninstall.ps1` + `install.ps1`. From then on they survive.
