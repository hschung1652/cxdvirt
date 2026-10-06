#!/usr/bin/env bash
# teardown_device.sh -- reverse of init_device.sh, so a sweep can reload the
# module between points.
#
# The unwinding itself is the emulator's (../emulator/bin/teardown.sh: DAX device,
# CXL region, then rmmod, which drains inflight NAND operations for up to
# ~10 s).  This wrapper first stops a benchmark server still holding the device
# -- a Redis left running by an aborted point keeps /dev/dax0.0 open and makes
# the rmmod fail.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
EMU=${CXDVIRT_EMU:-$(cd "$ROOT/.." && pwd)/emulator}
CXL=${CXL:-$ROOT/ndctl/build/cxl/cxl}
DAXCTL=${DAXCTL:-$ROOT/ndctl/build/daxctl/daxctl}
[ -x "$EMU/bin/teardown.sh" ] || { echo "ERROR: no emulator at $EMU -- run scripts/fetch.sh." >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root." >&2; exit 1; }

echo "--- stopping any redis on the DAX device"
pkill -f 'redis-server .*6379' 2>/dev/null
for _ in $(seq 1 20); do pgrep -f 'redis-server .*6379' >/dev/null || break; sleep 1; done
pkill -9 -f 'redis-server .*6379' 2>/dev/null
sleep 1

CXL="$CXL" DAXCTL="$DAXCTL" "$EMU/bin/teardown.sh"
