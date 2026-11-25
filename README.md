# Zephyr project setup

## Requirements

Run this setup on Linux with a supported host architecture: `x86_64` or `aarch64`. The setup script requires Bash, `uv`, Git, CMake (3.20 or newer), and Ninja, plus internet access. Device tree compiler and other host tools come from the Zephyr SDK.

## Install

From the repository root, run:

```sh
./zephyr-init.sh
```

The script:

1. Creates a Python virtual environment (with pip, which `west packages pip` needs) and installs West into it.
2. Configures this directory as the West workspace root and shallowly updates the projects pinned by `west.yml`.
3. Installs the Python requirements of those projects.
4. Runs `west sdk install`, which downloads the Zephyr SDK version named in `deps/zephyr/SDK_VERSION`, checks it against the release `sha256.sum`, extracts it into this directory, and runs its `setup.sh` to install the `arm-zephyr-eabi` GNU toolchain and the host tools.

The script is safe to rerun. If an SDK of the same version is already installed, West skips the download but runs `setup.sh` again. West looks for an installed SDK wherever CMake finds one (the user package registry, `~`, `/opt`, ...). If it picks one outside this directory, the script stops with an error; remove or unregister that SDK and rerun.

Files created in this repository directory:

```text
.venv/                    Python environment with West
.west/config              West workspace configuration
deps/zephyr/              Zephyr
deps/modules/             Modules imported from Zephyr's manifest
zephyr-sdk-<version>/     Zephyr SDK
build/                    Build output of make-sample.sh
```

Outside it, the SDK's `setup.sh` registers the SDK in the CMake user package registry under `~/.cmake/packages/Zephyr-sdk/`.

## Build

To build the configured sample after installation, run:

```sh
./make-sample.sh
```

It sets `ZEPHYR_SDK_INSTALL_DIR` so the build prefers the SDK in this directory over any other registered one.

## Clean

To remove the generated local environment, workspace, build output, SDK, and the SDK's CMake package registry entry, run:

```sh
./clean-all.sh
```
