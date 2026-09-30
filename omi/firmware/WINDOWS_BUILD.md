# Omi CV1 on Windows

Build and flash the Wi-Fi firmware from Windows with Nordic's toolchain for
**nRF Connect SDK 2.9.0**, equivalent to [MAC_BUILD.md](MAC_BUILD.md). Keep the
toolchain, SDK and build output **outside the Git checkout** (examples use `C:\ncs`).
Verified 2026-09-29 by building and flashing `3.0.22-localwifi.15`.

## Install

Download Nordic's signed `nrfutil.exe`
(`https://files.nordicsemi.com/artifactory/swtools/external/nrfutil/executables/x86_64-pc-windows-msvc/nrfutil.exe`)
to `C:\ncs`, then:

```powershell
C:\ncs\nrfutil.exe install toolchain-manager
C:\ncs\nrfutil.exe toolchain-manager config --set install-dir=C:\ncs\toolchains
C:\ncs\nrfutil.exe toolchain-manager install --ncs-version v2.9.0
```

Initialize the SDK inside the toolchain (`nrfutil toolchain-manager launch
--ncs-version v2.9.0 -- cmd /c ...`) with the same commands and project filter as
MAC_BUILD.md, in `C:\ncs\v2.9.0`. Check `git -C C:\ncs\v2.9.0\nrf rev-parse HEAD`
equals the `v2.9.0` tag.

## Three Windows-specific steps

1. **LF line endings in the SDK.** With Git's common Windows setting
   `core.autocrlf=true`, SDK sources check out with CRLF and the reviewed DHCP
   patch refuses them (`Could not apply the reviewed NCS 2.9 DHCP DNS fix`): it
   pins the source's SHA-256. Re-checkout only the SDK repositories with LF:

   ```bat
   cd /d C:\ncs\v2.9.0
   west forall -c "git config core.autocrlf false && git rm --cached -r -q . && git reset -q --hard"
   cd nrf && git config core.autocrlf false && git rm --cached -r -q . && git reset -q --hard
   ```

   Afterwards `zephyr\subsys\net\lib\dhcpv4\dhcpv4.c` hashes to the pinned
   `1e2d73c2…` value. Your global Git configuration is unchanged.

2. **Same drive for SDK and firmware.** `west build` computes the source path
   relative to the SDK and fails across drives (`path is on mount 'F:', start on
   mount 'C:'`). If the checkout is on another drive, create a junction (no admin
   needed): `mklink /J C:\ncs\omi-fw <repo>\omi\firmware`.

3. **Protobuf generator.** The toolchain's bundled nanopb `protoc` generates code
   its protobuf 5.27 cannot import (`'FileOptions' has no attribute
   'RegisterExtension'`, then `Plugin output is unparseable`). The build script sets
   `OMI_PROTOC` to [scripts/windows/protoc-grpc.cmd](scripts/windows/protoc-grpc.cmd),
   which runs the toolchain Python's grpcio-tools `protoc` (libprotoc 26.1), the
   Windows counterpart of `scripts/protoc-native`.

## Build

```powershell
C:\ncs\nrfutil.exe toolchain-manager launch --ncs-version v2.9.0 -- `
  cmd /c <repo>\omi\firmware\scripts\windows\build-cv1-wifi.cmd
```

`NCS_ROOT`, `FW_ROOT` (the junction) and `BUILD_DIR` override the `C:\ncs`
defaults. Outputs: `dfu_application.zip`, `merged.hex`, `merged_CPUNET.hex`.
Verify both OTA images against the repository's MCUboot key:

```powershell
python -m venv C:\ncs\flash-venv
C:\ncs\flash-venv\Scripts\python -m pip install "smpclient[ble]" imgtool
C:\ncs\flash-venv\Scripts\python <repo>\omi\firmware\scripts\windows\verify_zip.py C:\ncs\build\wifi\dfu_application.zip
```

Rebuilding from the same sources and paths reproduces the application digest
exactly. The network-core image embeds build-directory paths, so its digest
changes with the output folder even when its code does not.

## Flash over BLE (deployment is a separate, explicit step)

[scripts/windows/flash_omi.py](scripts/windows/flash_omi.py) uses MCUmgr/SMP over
the PC's Bluetooth and finds the Omi by the same service `omi-local` uses:

```powershell
$py = 'C:\ncs\flash-venv\Scripts\python.exe'; $fl = '<repo>\omi\firmware\scripts\windows\flash_omi.py'
& $py $fl status                                  # slots, digests, active/confirmed
& $py $fl stage C:\ncs\build\wifi\dfu_application.zip `
     --key <repo>\omi\firmware\bootloader\mcuboot\root-rsa-2048.pem `
     --net-digest-installed <installed network digest>
& $py $fl reset
```

`stage` verifies signatures, uploads the application image, checks the digest the
device reports and marks it pending. The network-core image has no fallback: it is
skipped unless its digest matches the installed one or `--allow-net` is given; only
update it when its code actually changed. After `reset` the swap and boot take
about 2–3 minutes; poll `status` until slot 0 reports the new digest, then check
`omi-local info` for the firmware revision.

On this device the new image came up **active and confirmed** without a separate
confirm step, and the previous image was no longer listed: do not rely on a
test-boot revert. Flash only images whose build and signatures verified. Recordings
and settings survived (`omi-local info`: ring intact, zero dropped packets).
