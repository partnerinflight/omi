@echo off
rem Windows counterpart of scripts/protoc-native: run the toolchain Python's grpcio-tools protoc.
"%OMI_PROTOC_PYTHON%" -m grpc_tools.protoc %*
