#!/bin/bash

SCRIPT_DIR=$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")

rm -fr \
    "$SCRIPT_DIR/.venv" \
    "$SCRIPT_DIR/.west" \
    "$SCRIPT_DIR/deps" \
    "$SCRIPT_DIR/build" \
    "$SCRIPT_DIR"/zephyr-sdk-*

# Drop the CMake user package registry entries the SDK's setup.sh -c created.
for entry in "$HOME"/.cmake/packages/Zephyr-sdk/*; do
    [ -f "$entry" ] || continue
    case "$(< "$entry")" in
        "$SCRIPT_DIR"/zephyr-sdk-*/cmake) rm -f "$entry" ;;
    esac
done
