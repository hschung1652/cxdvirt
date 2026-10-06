#!/usr/bin/env bash
# capture_mio_cylon.sh -- Fig. 5, Cylon side: the same four MIO cells as
# drivers/cxdvirt/run_mio_cxdvirt.sh, run inside the Cylon guest.
#
#   ./capture_mio_cylon.sh            all four cells
#   ./capture_mio_cylon.sh 1thr|8thr  one half
#   ./capture_mio_cylon.sh 8thr_rnd   one cell (1thr_rnd, 1thr_seq, 8thr_rnd, 8thr_seq)
#
# The VM must be up on the 96 GiB profile with every setting at the launch
# script's default -- 4915 MB buffer (ssd_size/20, matching CXDVirt's 4914 MB),
# CLOCK, 3 us tR, 3232 ns channel transfer, eviction write-back ON:
#
#   cd <cylon>/CylonFEMU/build-femu && ./run-cxlssd.sh 98304
#   sudo drivers/cylon/pin_threads.sh               # once the guest is up
#   drivers/cylon/guest_setup_cxl.sh            # region, devdax, warm-up
#
# and the guest must hold the -C MIO build (drivers/cylon/guest_build_mio.sh).
# The script checks the buffer, policy and tR on QEMU's command line and
# refuses a VM that has already logged write-back as disabled (a Fig. 1 launch).
#
# Footprints are the CXDVirt side's: 1 thread -m 8390 (1.7x of 4914 MiB),
# 8 threads -m 1534 each (8 private windows, 2.5x).  Measured wall time: about
# 35 minutes for all four cells, 17 of them the 1-thread random one.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${CXDVIRT_ROOT:-$(cd "$HERE/../.." && pwd)}
ONLY=${1:-all}
EXPECT_BUF=${EXPECT_BUF:-4915}
M1=${M1:-8390}; M8=${M8:-1534}
FEMU=${FEMU:-$ROOT/cylon-tree/CylonFEMU/build-femu}
LOGDIR=${LOGDIR:-$ROOT/cylon-tree/CylonLogs}
OUT=${OUT:-$ROOT/results/fig5_4914/cylon}
GUEST_DIR=${GUEST_DIR:-'~/Cylon/Cylon-scripts/eval-mio/src'}
G=(ssh -p 8080 -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@localhost)
D="cd $GUEST_DIR && ulimit -c 0 && numactl -N0 --"
mkdir -p "$OUT"

"${G[@]}" true || { echo "REFUSING: guest not reachable on :8080" >&2; exit 1; }
"${G[@]}" '[ -c /dev/dax0.0 ] && [ -f /run/cxdvirt_cxl_ready ]' ||
  { echo "REFUSING: guest device not prepared in this boot (run drivers/cylon/guest_setup_cxl.sh)" >&2; exit 1; }
"${G[@]}" 'fuser /dev/dax0.0' >/dev/null 2>&1 && { echo "REFUSING: guest device in use" >&2; exit 1; }
"${G[@]}" "cd $GUEST_DIR && grep -a -q 't:m:i:r:I:T:P:c:RC' bench512_W" ||
  { echo "REFUSING: guest has no bench512_W with -C (run drivers/cylon/guest_build_mio.sh)" >&2; exit 1; }
# Buffer and policy come off QEMU's command line.  (read-labels -s 1 prints them
# too, but it also resets Cylon's counters and rewrites its record file; MIO
# makes those calls itself around its timed section.  -s 2 only flushes the
# buffer, which is what each cell does first.)
QCMD=$(tr '\0' ' ' < /proc/$(pgrep -f 'qemu-system-x86_64.*femu-cxlssd' | head -1)/cmdline 2>/dev/null)
BUF=$(grep -oE 'bufsz_mb=[0-9]+' <<<"$QCMD" | head -1 | cut -d= -f2)
POL=$(grep -oE 'replacement=[0-9]+' <<<"$QCMD" | head -1 | cut -d= -f2)
[ "${BUF:-}" = "$EXPECT_BUF" ] || { echo "REFUSING: Cylon buffer is ${BUF:-unknown} MB, expected $EXPECT_BUF" >&2; exit 1; }
[ "${POL:-}" = 3 ] || { echo "REFUSING: replacement=${POL:-unknown}, need 3 (CLOCK)" >&2; exit 1; }
grep -qE 'pg_rd_lat=3000([, ]|$)' <<<"$QCMD" || { echo "REFUSING: pg_rd_lat is not 3000" >&2; exit 1; }
grep -qE 'ch_xfer_lat=3232([, ]|$)' <<<"$QCMD" || { echo "REFUSING: ch_xfer_lat is not 3232 (not the parity launch script)" >&2; exit 1; }
# FEMU logs the write-back mode at the first eviction, so this only catches a VM
# that has already evicted -- e.g. one left up from a Fig. 1 capture.
grep -q 'eviction writeback DISABLED' "$FEMU/log" 2>/dev/null &&
  { echo "REFUSING: this VM runs with CYLON_WB_OFF=1; Fig. 5 needs write-back on" >&2; exit 1; }

{ echo "cylon capture  start $(date -Is)  buf ${BUF}MB  M1=$M1 M8=$M8"
  echo "qemu $(stat -c '%y' "$FEMU/qemu-system-x86_64" 2>/dev/null || stat -c '%y' "$FEMU/x86_64-softmmu/qemu-system-x86_64" 2>/dev/null)"
  grep -oE '(devsz_mb|bufsz_mb|replacement|pg_rd_lat|pg_wr_lat|ch_xfer_lat|luns_per_ch|nchs|buffer_way)=[0-9]+' <<<"$QCMD" | sort -u | tr '\n' ' '; echo
  grep -E 'PATCH|CYLON_|WB-OFF' "$FEMU/log" 2>/dev/null | sort -u
  echo "uname $(uname -r)"; } > "$OUT/meta.$ONLY"

cell() { local name=$1 threads=$2 m=$3; shift 3
  "${G[@]}" 'cxl read-labels mem0 -s 2 >/dev/null 2>&1'
  local t0; t0=$(date +%s)
  "${G[@]}" "$D ./bench512_W -t$threads -r1 -i1 -I1 -T0 $* -c 0 -m $m -P1 2>/dev/null" > "$OUT/$name.txt"
  local rc=$?
  # MIO's thread 0 runs `cxl read-labels mem0 -s 1` just before and just after
  # its timed section; each call makes FEMU rewrite the per-request record file
  # below, so after the cell it holds the timed section's requests (warm-up
  # chase + recorded pass, capped at 32M).  Keep a copy per cell; no figure
  # reads it.
  cp -p "$LOGDIR/optb_stages_CLOCK_way32.csv" "$OUT/$name.optb.csv" 2>/dev/null
  echo "$name rc=$rc wall=$(( $(date +%s) - t0 ))s lines=$(wc -l < "$OUT/$name.txt")" >> "$OUT/progress.log"
  echo "  $name rc=$rc $(wc -l < "$OUT/$name.txt") samples"
}
want_cell() { case "$ONLY" in all) return 0 ;; 1thr|8thr) [ "${1%%_*}" = "$ONLY" ] ;; *) [ "$1" = "$ONLY" ] ;; esac; }
want_cell 1thr_rnd && cell 1thr_rnd 1 "$M1" -R -C
want_cell 1thr_seq && cell 1thr_seq 1 "$M1"    -C
want_cell 8thr_rnd && cell 8thr_rnd 8 "$M8" -R -C
want_cell 8thr_seq && cell 8thr_seq 8 "$M8"    -C
grep -h 'WB-OFF' "$FEMU/log" 2>/dev/null | sort -u >> "$OUT/meta.$ONLY"
grep -q 'eviction writeback enabled' "$FEMU/log" 2>/dev/null ||
  echo "WARNING: FEMU's log has no '[WB-OFF] eviction writeback enabled' line" >&2
echo "done $(date -Is)" >> "$OUT/progress.log"
echo "wrote $OUT"
