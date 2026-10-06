#!/usr/bin/env bash
# guest_setup_cxl.sh -- prepare the Cylon guest's CXL device after every VM boot.
# Run on the HOST once the guest is up (no root needed).
#
# Performs, in the guest, the sequence the paper's runs used after each boot:
#
#   cxl create-region -m -t ram -d decoder0.0 -w 1 -g 4096 mem0
#   echo dax0.0 > /sys/bus/dax/drivers/kmem/unbind          # take it from kmem
#   echo dax0.0 > /sys/bus/dax/drivers/device_dax/new_id    # bind it to device_dax
#   daxctl reconfigure-device --mode=devdax --force dax0.0
#   ./cxl_warmup <device size in GiB>
#
# The warm-up touches every page of the device once, so that each EPT entry is
# populated before a measurement; it spans the whole emulated device, whose size
# is read from QEMU's command line (devsz_mb).  cxl_warmup is built in the guest
# from cylon-tree/tools/cxl_warmup.c when absent or out of date.
#
# The device is left in devdax mode, which the MIO, Fig. 1 and OCEAN targets use;
# the Redis target's guest script onlines it as system RAM itself.  A marker,
# /run/cxdvirt_cxl_ready, records the warmed size; tmpfs clears it at the next
# boot, and the capture drivers refuse a guest without it.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
SSH_PORT=${SSH_PORT:-8080}
G=(ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@localhost)
WARMUP_SRC=$ROOT/cylon-tree/tools/cxl_warmup.c
GUEST_TOOLS=${GUEST_TOOLS:-'~/Cylon/tools'}

qpid=$(pgrep -f 'qemu-system-x86_64.*femu-cxlssd' | head -1)
[ -n "$qpid" ] || { echo "ERROR: no Cylon VM running" >&2; exit 1; }
DEVSZ_MB=$(tr '\0' '\n' < "/proc/$qpid/cmdline" | tr ',' '\n' | grep -oP '^devsz_mb=\K[0-9]+' | head -1)
[ -n "$DEVSZ_MB" ] || { echo "ERROR: no devsz_mb on the QEMU command line" >&2; exit 1; }
GB=$((DEVSZ_MB / 1024))
[ -f "$WARMUP_SRC" ] || { echo "ERROR: $WARMUP_SRC missing -- scripts/fetch.sh --cylon" >&2; exit 1; }
"${G[@]}" true || { echo "ERROR: guest not reachable on :$SSH_PORT" >&2; exit 1; }

if "${G[@]}" "test -f /run/cxdvirt_cxl_ready && test -c /dev/dax0.0"; then
  echo "device already prepared in this boot: $("${G[@]}" cat /run/cxdvirt_cxl_ready)"
  exit 0
fi

echo "--- cxl_warmup in the guest ($GUEST_TOOLS)"
"${G[@]}" "mkdir -p $GUEST_TOOLS" || exit 1
want=$(md5sum "$WARMUP_SRC" | cut -d' ' -f1)
have=$("${G[@]}" "cd $GUEST_TOOLS && md5sum cxl_warmup.c 2>/dev/null | cut -d' ' -f1")
if [ "$want" != "$have" ] || ! "${G[@]}" "test -x $GUEST_TOOLS/cxl_warmup"; then
  scp -q -P "$SSH_PORT" -o StrictHostKeyChecking=no "$WARMUP_SRC" "root@localhost:$GUEST_TOOLS/cxl_warmup.c" &&
  "${G[@]}" "cd $GUEST_TOOLS && gcc -o cxl_warmup cxl_warmup.c -fopenmp" ||
    { echo "ERROR: building cxl_warmup in the guest failed (gcc with OpenMP needed)" >&2; exit 1; }
fi

echo "--- CXL region -> devdax, then warm-up over the whole ${GB} GiB device"
"${G[@]}" "set -e
  cxl create-region -m -t ram -d decoder0.0 -w 1 -g 4096 mem0
  echo dax0.0 > /sys/bus/dax/drivers/kmem/unbind 2>/dev/null || true
  echo dax0.0 > /sys/bus/dax/drivers/device_dax/new_id 2>/dev/null || true
  daxctl reconfigure-device --mode=devdax --force dax0.0
  test -c /dev/dax0.0
  cd $GUEST_TOOLS && ./cxl_warmup $GB
  echo 'devsz_gib=$GB' > /run/cxdvirt_cxl_ready" ||
  { echo "ERROR: device setup failed in the guest" >&2; exit 1; }
echo "device ready: /dev/dax0.0, ${GB} GiB warmed"
