#!/usr/bin/env bash
# init_device.sh -- bring the emulator up for one experiment point.
#
# The reproduction does not carry its own device bring-up: the emulator's
# bin/setup.sh (../emulator) is the one bring-up there is.  This wrapper adds
# only what an experiment needs around it:
#
#   * the paper's defaults where a caller sets nothing -- in particular the SLOW
#     background drain (64 victims per 10 ms), which Figs. 5 and 6 ran with;
#     the emulator's own default is the fast 1024 per 1 ms
#   * ndctl from this repository's fetch (ndctl/build), since the CXL region
#     commands need v78+ and distro packages are older
#   * a refusal to load over a live device, and a record of the live module
#     parameters at the end, for the run's log
#
# Every module knob is the emulator's; see ../emulator/docs/PARAMETERS.md.  They are
# environment variables, which sudo strips, hence `sudo env`:
#
#     sudo ./scripts/init_device.sh
#     sudo env DRAM_CACHE_MB=6456 CXL_WR_ALLOC=2 ZERO_ON_INIT=true ./scripts/init_device.sh
#
# PREREQUISITE: the physical carve-out and the isolated cores on the kernel
# command line -- ../emulator/bin/setup.sh and ../emulator/kernel/README.md give the
# exact line.  Without the isolation, the tail percentiles this artifact reports
# move by more than the effects it reports.
#
# Reverse with teardown_device.sh.  dram_cache_mb, prefetch_*, bg_drain_4k,
# lifo_stage and optb4k_max are insmod-time only, so any sweep over them tears
# the device down and rebuilds it per point, as the drivers do.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
EMU=${CXDVIRT_EMU:-$(cd "$ROOT/.." && pwd)/emulator}
CXL=${CXL:-$ROOT/ndctl/build/cxl/cxl}
DAXCTL=${DAXCTL:-$ROOT/ndctl/build/daxctl/daxctl}

[ -x "$EMU/bin/setup.sh" ] || { echo "ERROR: no emulator at $EMU -- run scripts/fetch.sh, then scripts/build.sh." >&2; exit 1; }
[ -f "$EMU/nvmevirt/nvmev.ko" ] || { echo "ERROR: $EMU/nvmevirt/nvmev.ko missing -- run scripts/build.sh." >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root (insmod)." >&2; exit 1; }
lsmod | grep -q '^nvmev ' && { echo "ERROR: nvmev already loaded -- run teardown_device.sh." >&2; exit 1; }
grep -q 'memmap=' /proc/cmdline || echo "WARNING: no memmap= on /proc/cmdline; insmod will fail." >&2

# The paper's defaults for anything the caller leaves unset.  Every
# run_experiment.sh target sets the drain explicitly; these matter only for a
# bare `init_device.sh`.
export DRAM_CACHE_MB=${DRAM_CACHE_MB:-4914}
export BG_DRAIN_4K=${BG_DRAIN_4K:-1}
export BG_DRAIN_BATCH=${BG_DRAIN_BATCH:-64}
export BG_DRAIN_MS=${BG_DRAIN_MS:-10}

CXL="$CXL" DAXCTL="$DAXCTL" CXDVIRT_ROOT="$EMU" "$EMU/bin/setup.sh" || {
  echo "ERROR: emulator/bin/setup.sh failed" >&2; exit 1; }

echo
echo "--- live module parameters"
for p in /sys/module/nvmev/parameters/*; do printf '  %-22s %s\n' "$(basename "$p")" "$(cat "$p" 2>/dev/null)"; done
printf '  %-22s %s\n' srcversion "$(cat /sys/module/nvmev/srcversion 2>/dev/null)"
echo
echo "Device ready.  Record these parameters with any result you keep."
