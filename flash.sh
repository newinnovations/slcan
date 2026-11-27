#!/bin/bash

SCRIPT_DIR=$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")

cd "$SCRIPT_DIR"

. .venv/bin/activate

if [ -f ~/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI ]; then
    echo "Flashing with STM32CubeProgrammer..."
    west flash
else
    echo "STM32CubeProgrammer not found, needed for flashing STM32C0"
fi
