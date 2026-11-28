#!/bin/bash

# Usage: ./make.sh [board]   (default: nucleo_c092rc; also: nucleo_h7s3l8)
# Output goes to build/<board>.

SCRIPT_DIR=$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")

cd "$SCRIPT_DIR"

. .venv/bin/activate

BOARD=${1:-nucleo_c092rc}

# Prefer the in-repo SDK over any other one registered with CMake.
export ZEPHYR_SDK_INSTALL_DIR="$SCRIPT_DIR/zephyr-sdk-$(< deps/zephyr/SDK_VERSION)"

west build -p -b "$BOARD" -d "build/$BOARD" slcan_bridge
