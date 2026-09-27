# Omi CV1 on macOS

Build the consumer **nRF5340 + nRF7002** device with Nordic's native macOS
toolchain for **nRF Connect SDK 2.9.0**. The board is `omi/nrf5340/cpuapp`.
The upstream release workflow and this fork's build script use the same SDK.

## Install and initialize

Download the macOS `nrfutil` executable from [Nordic](https://www.nordicsemi.com/Products/Development-tools/nRF-Util/Download),
make it executable, and put it on `PATH`. Then:

```bash
nrfutil install toolchain-manager
nrfutil toolchain-manager install --ncs-version v2.9.0
nrfutil toolchain-manager launch --ncs-version v2.9.0 --shell
```

In that toolchain shell, choose an SDK directory **outside the Git checkout**:

```bash
mkdir -p "$HOME/ncs/v2.9.0"
cd "$HOME/ncs/v2.9.0"
west init -m https://github.com/nrfconnect/sdk-nrf --mr v2.9.0 .
# Activate only CV1 build dependencies (west still resolves imported manifests).
west config manifest.project-filter '-.*,+zephyr,+mcuboot,+mbedtls,+oberon-psa-crypto,+nrfxlib,+trusted-firmware-m,+bsim,+nanopb,+cmsis,+cmsis-dsp,+hal_nordic,+hal_st,+hostap,+littlefs,+nrf_wifi,+segger,+tinycrypt,+zcbor,+libmetal,+open-amp'
west update -o=--depth=1 -n
west blobs fetch nrf_wifi
west zephyr-export
```

The compiler, CMake, Ninja, Python, and west come from Nordic's bundle. Do not
substitute a Homebrew ARM compiler. Use `python` inside the toolchain shell.
SDK source installation and toolchain installation are separate steps.

## Native protobuf generator on Apple Silicon

The NCS 2.9.0 macOS bundle's nanopb `protoc` is Intel-only. The SoftAP build
needs a native generator. Outside the Nordic shell, use a native Python 3.12:

```bash
python -m venv "$HOME/ncs/protoc-venv"
"$HOME/ncs/protoc-venv/bin/python" -m pip install grpcio-tools==1.62.3
export OMI_PROTOC_PYTHON="$HOME/ncs/protoc-venv/bin/python"
export OMI_PROTOC="/absolute/path/to/repo/omi/firmware/scripts/protoc-native"
```

These variables survive the toolchain launcher. Only protobuf generation uses
this venv; compiler, SDK libraries, CMake and build Python remain Nordic's.
The override is optional on platforms with a working bundled generator.

## Build

From the repository root, outside or inside a toolchain shell:

```bash
NCS_ROOT="$HOME/ncs/v2.9.0" \
  bash omi/firmware/scripts/build-cv1-macos.sh --wifi
```

Set `NRFUTIL=/absolute/path/to/nrfutil` if it is not on `PATH`. Set `BUILD_DIR`
to override the output directory. The wrapper checks the SDK Git revision and
launches the existing cross-platform build script in Nordic's environment.
Omit `--wifi` to build the BLE-only offline recorder.

The underlying Wi-Fi build is:

```bash
west build -b omi/nrf5340/cpuapp /absolute/path/to/omi/firmware/omi \
  --sysbuild -d /absolute/path/to/omi/firmware/build/local-wifi --pristine always \
  -- -DBOARD_ROOT=/absolute/path/to/omi/firmware -DCONF_FILE=omi.conf \
     -DEXTRA_CONF_FILE=overlay-wifi-upload.conf -DSB_EXTRA_CONF_FILE=sysbuild-wifi.conf
```

Run it from the SDK workspace. `sysbuild-wifi.conf` enables nRF7002 at the
sysbuild level; the application overlay alone is insufficient.

The script requires all three outputs to exist: `dfu_application.zip`,
`merged.hex`, and `merged_CPUNET.hex`. Use the ZIP for BLE OTA. The HEX files
are separate application-core and network-core recovery images.

## Host receiver tests

With a Python 3.10+ environment active:

```bash
cd omi/firmware/scripts/omi-local
python -m pip install -e .
python -m unittest discover -s tests -t .
```

These tests exercise the receiver and a software model of the device protocol.
They do not validate the microphone, SD card, radio, bootloader compatibility,
or battery behavior on a physical Omi.

Setup, button gestures, receiver protocol and credential storage: [PROVISIONING.md](PROVISIONING.md).
Exact local build and hardware results: [VALIDATION_MAC.md](VALIDATION_MAC.md).
