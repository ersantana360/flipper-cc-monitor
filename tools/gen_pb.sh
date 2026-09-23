#!/usr/bin/env bash
# Generate tools/pb/*_pb2.py (Flipper RPC protobuf stubs) with the Windows Python.
set -e
cd "$(dirname "$0")"
py.exe -3 -m pip install --user --quiet grpcio-tools pyserial
[ -d flipperzero-protobuf ] || git clone -q --depth 1 https://github.com/flipperdevices/flipperzero-protobuf.git
mkdir -p pb
py.exe -3 -m grpc_tools.protoc -I flipperzero-protobuf --python_out=pb flipperzero-protobuf/*.proto
echo "generated: $(ls pb)"
