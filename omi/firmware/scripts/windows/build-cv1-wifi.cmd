@echo off
rem Omi CV1 Wi-Fi firmware build on Windows; same flags as scripts/build-cv1-local.sh --wifi.
rem Run inside the toolchain:  nrfutil toolchain-manager launch --ncs-version v2.9.0 -- cmd /c build-cv1-wifi.cmd
rem   NCS_ROOT   NCS v2.9.0 workspace with LF line endings (default C:\ncs\v2.9.0)
rem   FW_ROOT    omi/firmware on the SAME drive as NCS_ROOT, e.g. a junction (default C:\ncs\omi-fw)
rem   BUILD_DIR  output directory (default C:\ncs\build\wifi)
rem See omi/firmware/WINDOWS_BUILD.md.
setlocal
if "%NCS_ROOT%"=="" set NCS_ROOT=C:\ncs\v2.9.0
if "%FW_ROOT%"=="" set FW_ROOT=C:\ncs\omi-fw
if "%BUILD_DIR%"=="" set BUILD_DIR=C:\ncs\build\wifi
rem The bundled nanopb protoc is too old for the toolchain's protobuf 5.x; use grpcio-tools' protoc.
set OMI_PROTOC=%~dp0protoc-grpc.cmd
for /f "delims=" %%p in ('where python') do if not defined OMI_PROTOC_PYTHON set OMI_PROTOC_PYTHON=%%p
cd /d "%NCS_ROOT%" || exit /b 1
west build -b omi/nrf5340/cpuapp "%FW_ROOT%/omi" --sysbuild -d "%BUILD_DIR%" --pristine always -- -DBOARD_ROOT="%FW_ROOT%" -DCONF_FILE=omi.conf -DEXTRA_CONF_FILE=overlay-wifi-upload.conf -DSB_EXTRA_CONF_FILE=sysbuild-wifi.conf || exit /b 1
for %%f in (dfu_application.zip merged.hex merged_CPUNET.hex) do if not exist "%BUILD_DIR%\%%f" (echo missing %%f & exit /b 1)
echo BUILD-OK: %BUILD_DIR%
