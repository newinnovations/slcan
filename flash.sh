#!/bin/bash

# Usage: ./flash.sh [board]   (default: nucleo_c092rc; also: nucleo_h7s3l8)
# Flashes build/<board>, built by ./make.sh [board].

SCRIPT_DIR=$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")

cd "$SCRIPT_DIR"

. .venv/bin/activate

BOARD=${1:-nucleo_c092rc}

if [ -f ~/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI ]; then
    echo "Flashing $BOARD with STM32CubeProgrammer..."
    west flash -d "build/$BOARD"
else
    echo "STM32CubeProgrammer not found, needed for flashing STM32C0 and STM32H7S"
fi
