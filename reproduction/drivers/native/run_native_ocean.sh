#!/usr/bin/env bash
# run_native_ocean.sh -- Splash-4 OCEAN with no device in the path.
#
# This is the dashed line in Fig. 7(a).  Same binary, same -n and -p, same
# machine; the only difference is that the heap is plain DRAM instead of the
# emulated device.  The `heap` arm (default) is the one the figure draws; the
# `remote` and `local` arms are the earlier --membind baselines, kept for
# context.
#
# THE MEMBIND ARMS.  `remote` runs node-0 CPUs against node-1
# memory, which is the NUMA distance both emulators pay: CXDVirt's carve-out and
# FEMU's backing file are both on node 1, so a node-local baseline would charge
# the emulators for distance they did not cause.  `local` is collected as well,
# because `--membind 1` binds EVERY allocation -- binary, stack, libc and page
# cache -- while daxmalloc redirects only malloc.  The remote arm is therefore
# over-penalised and the local arm brackets it.
#
# `heap` is the placement-matched arm: launched exactly as sweep_ocean_wss.sh
# launches OCEAN on CXDVirt (node-0 CPUs, --membind 0, daxmalloc preloaded), with
# /dev/dax0.0 swapped for a /dev/shm file whose pages are pre-faulted on node 1.
# The heap sits on node 1 in the same dlmalloc arena, everything else on node 0,
# and only the device is missing.  Run it on the kernel the CXDVirt runs used
# (6.18.5).  Against it CXDVirt at 0.35x is +4.8%, all in init; the remote arm
# read 4.7% slow and had hidden that.
#
# NO MODULE LOADED AND NO VM RUNNING, for every arm.  Native OCEAN differs 4%
# between 6.4.6 and 6.18.5, almost all of it in the page-fault path, so every
# output file names its kernel (k6185_ / k646_).
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${CXDVIRT_ROOT:-$(cd "$HERE/../.." && pwd)}
OCEAN=${OCEAN:-$ROOT/bench/Splash-4/Splash-4/ocean-contiguous_partitions/OCEAN-CONT}
OUT=${OUT:-$ROOT/results/ocean_native}
OCEAN_N=${OCEAN_N:-8194}
OCEAN_P=${OCEAN_P:-8}
REPS=${REPS:-3}
ARMS=${ARMS:-heap}
DAXMALLOC=${DAXMALLOC:-$(cd "$ROOT/.." && pwd)/emulator/tools/daxmalloc/daxmalloc.so}
HEAPFILE=${HEAPFILE:-/dev/shm/cxdvirt_native_heap}
HEAP_GB=${HEAP_GB:-16}   # peak_live is 13.86 GiB at -n8194

[ -x "$OCEAN" ] || { echo "ERROR: $OCEAN missing" >&2; exit 1; }
lsmod | grep -q '^nvmev ' && { echo "ERROR: nvmev loaded; the baseline must have no device." >&2; exit 1; }
pgrep -f 'qemu-system' >/dev/null && { echo "ERROR: QEMU running; stop the VM first." >&2; exit 1; }
command -v numactl >/dev/null || { echo "ERROR: numactl not found" >&2; exit 1; }

mkdir -p "$OUT"
KV=$(uname -r | cut -d- -f1 | tr -d .)
for arm in $ARMS; do
  case "$arm" in
    remote) BIND="--cpunodebind 0 --membind 1" ;;
    local)  BIND="--cpunodebind 0 --membind 0" ;;
    heap)   BIND="--cpunodebind 0 --membind 0"
            [ -f "$DAXMALLOC" ] || { echo "ERROR: $DAXMALLOC missing" >&2; exit 1; } ;;
    *) echo "unknown arm $arm" >&2; exit 1 ;;
  esac
  for r in $(seq 1 "$REPS"); do
    f="$OUT/k${KV}_s4_${arm}_r${r}.out"
    echo "--- $arm rep $r -> $f"
    if [ "$arm" = heap ]; then
      # A fresh arena per rep, pre-faulted on node 1: a tmpfs page stays on the
      # node it was allocated on, so OCEAN's --membind 0 cannot pull it local.
      rm -f "$HEAPFILE"
      numactl --membind 1 -- fallocate -l "${HEAP_GB}G" "$HEAPFILE" ||
        { echo "ERROR: could not pre-fault $HEAPFILE on node 1" >&2; exit 1; }
      { echo "# numactl $BIND env LD_PRELOAD=$DAXMALLOC DAXMALLOC_PATH=$HEAPFILE $OCEAN -n$OCEAN_N -p$OCEAN_P -s"
        echo "# kernel $(uname -r)   $(date -Is)   heap ${HEAP_GB} GiB pre-faulted on node 1"
        # shellcheck disable=SC2086
        numactl $BIND -- env LD_PRELOAD="$DAXMALLOC" DAXMALLOC_PATH="$HEAPFILE" \
          DAXMALLOC_REQUIRE=1 DAXMALLOC_STATS=1 "$OCEAN" -n"$OCEAN_N" -p"$OCEAN_P" -s
        # Where the arena's pages ended up.  Every one must be on node 1.
        python3 - "$HEAPFILE" <<'PY'
import mmap, os, re, sys
f = sys.argv[1]
m = mmap.mmap(os.open(f, os.O_RDONLY), 0, prot=mmap.PROT_READ)
for i in range(0, len(m), 4096):
    m[i]
line = next(l for l in open("/proc/self/numa_maps") if f in l)
n = dict(re.findall(r"\bN(\d+)=(\d+)", line))
print(f"# heap pages: node0 {n.get('0', 0)}  node1 {n.get('1', 0)}")
PY
      } > "$f" 2>&1
      rm -f "$HEAPFILE"
      grep -q "^# heap pages: node0 0 " "$f" ||
        echo "WARNING: $f: heap not entirely on node 1 -- $(grep '^# heap pages' "$f")" >&2
      continue
    fi
    { echo "# numactl $BIND $OCEAN -n$OCEAN_N -p$OCEAN_P -s"
      echo "# kernel $(uname -r)   $(date -Is)"
      # shellcheck disable=SC2086
      numactl $BIND "$OCEAN" -n"$OCEAN_N" -p"$OCEAN_P" -s
    } > "$f" 2>&1
  done
done
echo "done -> $OUT"
