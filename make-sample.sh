#!/bin/bash

SCRIPT_DIR=$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")

cd "$SCRIPT_DIR"

. .venv/bin/activate

# Prefer the in-repo SDK over any other one registered with CMake.
export ZEPHYR_SDK_INSTALL_DIR="$SCRIPT_DIR/zephyr-sdk-$(< deps/zephyr/SDK_VERSION)"

west build -p -b nucleo_c092rc deps/zephyr/samples/drivers/can/counter
