#!/usr/bin/env bash
# teardown.sh -- reverse of setup.sh.
#
# ORDER MATTERS.  The DAX device holds a reference on the CXL region and the
# region holds one on the memdev, so rmmod fails unless the stack is unwound in
# reverse.  NVMeV_exit() then drains inflight NAND operations itself, which can
# take ~10 s after a write-heavy run.  Do not shorten the waits.
set -u
CXL=${CXL:-$(command -v cxl)}
DAXCTL=${DAXCTL:-$(command -v daxctl)}

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

if [ -e /dev/dax0.0 ] || "$CXL" list -R 2>/dev/null | grep -q region; then
	"$DAXCTL" offline-memory dax0.0 >/dev/null 2>&1 || true
	for r in $("$CXL" list -R 2>/dev/null | grep -oE '"region[0-9]+"' | tr -d '"'); do
		"$CXL" disable-region "$r" >/dev/null 2>&1 || true
		"$CXL" destroy-region "$r" >/dev/null 2>&1 || true
	done
	sleep 1
fi

if lsmod | grep -q '^nvmev '; then
	echo "rmmod nvmev (draining inflight I/O, up to ~10 s)"
	rmmod nvmev || { echo "rmmod failed; device still referenced" >&2; exit 1; }
fi
for _ in $(seq 1 30); do lsmod | grep -q '^nvmev ' || break; sleep 1; done
lsmod | grep -q '^nvmev ' && { echo "nvmev still loaded" >&2; exit 1; }
echo "device down"
