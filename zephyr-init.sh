#!/bin/bash

set -euo pipefail

SCRIPT_DIR=$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")
cd "$SCRIPT_DIR"

if [ ! -d .venv ]; then
    uv venv --seed --link-mode=copy
fi
. ./.venv/bin/activate
uv pip install --link-mode=copy west

# A local manifest at the workspace root makes `west init -l .` put .west in
# the parent. Configure this directory as the workspace root instead.
mkdir -p .west
west config --local manifest.path .
west config --local manifest.file west.yml
west config --local zephyr.base deps/zephyr

west update --narrow --fetch-opt=--depth=1
west packages pip --install

# west reuses an SDK of the same version found anywhere CMake looks (package
# registry, ~, /opt, ...) and then ignores -b. Require the in-repo copy.
SDK_DIR="$SCRIPT_DIR/zephyr-sdk-$(< deps/zephyr/SDK_VERSION)"
west sdk install -t arm-zephyr-eabi -b "$SCRIPT_DIR"
if [ ! -f "$SDK_DIR/sdk_version" ]; then
    echo "error: west used a Zephyr SDK installed elsewhere; $SDK_DIR was not created." >&2
    echo "Remove or unregister the other SDK of the same version and rerun." >&2
    exit 1
fi