#!/usr/bin/env bash
# run_mio_cxdvirt.sh -- Fig. 5, CXDVirt side: MIO pointer chase at 1 and 8
# threads, sequential and random, against a device that is already up.
#
#   ./run_mio_cxdvirt.sh [all|1thr|8thr]
#
# Brings nothing up itself.  run_experiment.sh fig5-mio loads the module with
#
#   DRAM_CACHE_MB=4914 CLOCK_HW_YOUNG=0 BG_DRAIN_4K=1 BG_DRAIN_BATCH=64 BG_DRAIN_MS=10
#
# and this script refuses to run against any other configuration, because each
# of those settings moves the curves:
#
#   - 4914 MB is the paper's 4.8 GiB cache, Cylon's ssd_size/20 at 96 GiB.
#   - CLOCK_HW_YOUNG=0 makes CLOCK use only its software reference bit, which a
#     hit never sets -- the way Cylon's CLOCK behaves under EPT remapping.
#   - 64 victims per 10 ms is the SLOW drain, a ceiling of 6.4K evictions/s.
#     One MIO thread misses ~100K times a second, so most evictions happen
#     inside the fault handler, as they did in the published capture.  1024/1
#     (the Fig. 7(b) and Fig. 8(a) setting) moves nearly all of them to the
#     background and is a different regime.
#
# Measured wall time at 4914 MB: 1thr_rnd 48 min, 1thr_seq 11, 8thr_rnd 25,
# 8thr_seq 5 -- about 90 minutes for all four cells.
#
# MIO is Cylon's microbenchmark, built by scripts/build_mio.sh with 512 B chase
# nodes (8 per page) and the -C flag: without -C the timed loop restarts every
# sample at the next node in ADDRESS order, so -R shuffles a ring that is never
# followed and "random" is a sequential scan.
#
#     bench512_W -tN -r1 -i1 -I1 -T0 [-R] -C -c 0 -m M -P1     (numactl -N0)
#
# Footprints hold the paper's normalized WSS against the 4914 MiB cache:
#   1 thread   -m 8390          8390 / 4914      = 1.7x
#   8 threads  -m 1534 each     8 x 1534 / 4914  = 2.5x  (8 private windows)
#
# MIO's thread 0 runs `cxl read-labels mem0 -s 1` twice just before its timed
# section and once just after.  bin/cxl stands in for `cxl` on PATH: calls 1-2
# reset /proc/nvmev/optb4k and call 3 saves it, so each cell also leaves its
# per-miss stage records (<cell>.optb4k.csv; no figure reads them).  The
# published captures ran with no `cxl` on PATH at all, so the stand-in also
# keeps a real `cxl` from doing mailbox work around the timed section.
#
# Runs as the login user: /dev/dax0.0 and /proc/nvmev/optb4k are 0666.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${CXDVIRT_ROOT:-$(cd "$HERE/../.." && pwd)}
ONLY=${1:-all}
MIO=${MIO:-$ROOT/cylon-tree/Cylon-scripts/eval-mio/src/bench512_W}
CACHE=${CACHE:-4914}
M1=${M1:-8390}
M8=${M8:-1534}
OUT=${OUT:-$ROOT/results/fig5_4914/cxdvirt/c4914_d64}
P=/sys/module/nvmev/parameters

case "$ONLY" in all|1thr|8thr) ;; *) echo "usage: $0 [all|1thr|8thr]" >&2; exit 2 ;; esac
[ -x "$MIO" ] || { echo "ERROR: $MIO missing -- scripts/fetch.sh, then scripts/build_mio.sh" >&2; exit 1; }
grep -a -q 't:m:i:r:I:T:P:c:RC' "$MIO" ||
  { echo "REFUSING: $MIO has no -C option (stock MIO; rebuild with scripts/build_mio.sh)" >&2; exit 1; }
[ -e /dev/dax0.0 ] || { echo "REFUSING: no /dev/dax0.0 -- bring the device up first" >&2; exit 1; }
want() { local got; got=$(cat "$P/$1" 2>/dev/null)
  [ "$got" = "$2" ] || { echo "REFUSING: $1=$got, Fig. 5 needs $2" >&2; exit 1; }; }
want dram_cache_mb "$CACHE"; want cache_policy clock; want cxl_clock_hw_young 0
want page_granularity 4k; want prefetch_mode 0; want cxl_wr_alloc 0; want nand_tr_ns 3000
want bg_drain_4k 1; want bg_drain_batch 64; want bg_drain_ms 10; want optb_tsc Y
fuser /dev/dax0.0 >/dev/null 2>&1 && { echo "REFUSING: /dev/dax0.0 is in use" >&2; exit 1; }

mkdir -p "$OUT"
{ echo "fig5 cxdvirt  start $(date -Is)  only=$ONLY  M1=$M1 M8=$M8"
  for p in "$P"/*; do echo "$(basename "$p") $(cat "$p" 2>/dev/null)"; done
  echo "srcversion $(cat /sys/module/nvmev/srcversion 2>/dev/null)"
  echo "uname $(uname -r)"; echo "cmdline $(cat /proc/cmdline)"
  echo "governor $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
  echo "bench512_W md5 $(md5sum "$MIO" | cut -d' ' -f1)"; } > "$OUT/meta.$ONLY"

cell() { local name=$1; shift
  cat /proc/nvmev/debug > "$OUT/$name.dbg0"
  echo 0 > "$OUT/.lsa_calls"
  local t0; t0=$(date +%s)
  ( cd "$(dirname "$MIO")" && ulimit -c 0 &&
    PATH="$HERE/bin:$PATH" OPTB_OUT="$OUT/$name.optb4k.csv" LSA_CALLS="$OUT/.lsa_calls" \
    numactl -N0 -- "./$(basename "$MIO")" -r1 -i1 -I1 -T0 "$@" -c 0 -P1 ) \
      2> "$OUT/$name.err" > "$OUT/$name.txt"
  local rc=$?
  cat /proc/nvmev/debug > "$OUT/$name.dbg1"
  echo "$name rc=$rc wall=$(( $(date +%s) - t0 ))s lines=$(wc -l < "$OUT/$name.txt")" >> "$OUT/progress.log"
  echo "  $name rc=$rc $(wc -l < "$OUT/$name.txt") samples"
}
if [ "$ONLY" != 8thr ]; then
  cell 1thr_rnd -t1 -m "$M1" -R -C
  cell 1thr_seq -t1 -m "$M1"    -C
fi
if [ "$ONLY" != 1thr ]; then
  cell 8thr_rnd -t8 -m "$M8" -R -C
  cell 8thr_seq -t8 -m "$M8"    -C
fi
rm -f "$OUT/.lsa_calls"
echo "done $(date -Is)" >> "$OUT/progress.log"
echo "wrote $OUT"
