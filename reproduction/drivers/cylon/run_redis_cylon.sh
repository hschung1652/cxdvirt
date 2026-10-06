#!/usr/bin/env bash
# run_redis_cylon.sh -- Fig. 7(b), Cylon side: Redis/YCSB-C at 1M, 3M and 6M
# records, 1 run thread, on the HOST, with the VM up.  No root needed.
#
# The VM must be launched with the settings the published points ran with
# (leg 3), from cylon-tree/CylonFEMU/build-femu:
#
#     CYLON_WB_OFF=0 CYLON_EVICT_SYNC=3 CYLON_WM_HIGH=95 CYLON_WM_LOW=85 \
#         CYLON_WM_BATCH=1 ./run-cxlssd.sh 98304
#
# i.e. the 96 GiB device with its default 4915 MB buffer (Cylon's ssd_size/20,
# which CXDVirt's 4914 MB matches), CLOCK, 3 us tR, eviction write-back ON, the
# 95%/85% watermarks CXDVirt drains to with one victim per fill, and evict_sync
# mode 3 -- the evictor never pays, a later touch of an in-flight page waits,
# the analogue of CXDVirt's background drain.  CYLON_FT_PROG is left at its
# default, as it was.  Then, once the guest is up:
#
#     sudo drivers/cylon/pin_threads.sh
#     drivers/cylon/guest_setup_cxl.sh              # region, devdax, warm-up
#     drivers/cylon/guest_stage_redis.sh            # once per guest image
#
# USE A FRESH BOOT, AND DO NOTHING ELSE IN IT.  The guest script onlines the
# device as node-1 system RAM, which cannot be undone without a restart.  All
# three record counts run in the one boot; the script flushes the device buffer
# and restarts Redis between them.
#
# Writes, per record count N, into $OUT (default results/redis_cylon/):
#   cylon_commandstats_N.txt  cylon_latencystats_N.txt  cylon_histogram_N.txt
#   cylon_run_N.out  cylon_load_N.out  cylon_redis_N.log  cylon_console_N.log
#   cylon_buffer_N.txt   FEMU's write-back counters for this point (the dumps
#                        the guest script's markers produce; copy them out
#                        before shutting the VM down -- leg 3's were lost)
#   cylon_meta.txt       the VM's configuration, read off QEMU and FEMU's log
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
SSH_PORT=${SSH_PORT:-8080}
SSH=(ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@localhost)
SCP=(scp -q -P "$SSH_PORT" -o StrictHostKeyChecking=no)
OUT=${OUT:-$ROOT/results/redis_cylon}
FEMU=${FEMU:-$ROOT/cylon-tree/CylonFEMU/build-femu}
BUFLOG=${BUFLOG:-$ROOT/cylon-tree/CylonLogs/cxlssd_buffer.txt}
RECORDS=${RECORDS:-"1000000 3000000 6000000"}

# --- is this the VM the published points ran on? ------------------------------
qpid=$(pgrep -f 'qemu-system-x86_64.*femu-cxlssd' | head -1)
[ -n "$qpid" ] || { echo "ERROR: no Cylon VM running -- see the header" >&2; exit 1; }
QCMD=$(tr '\0' ' ' < "/proc/$qpid/cmdline")
chk() { grep -qE "$1=$2([, ]|\$)" <<<"$QCMD" || { echo "REFUSING: QEMU is not running with $1=$2" >&2; exit 1; }; }
chk bufsz_mb 4915; chk replacement 3; chk pg_rd_lat 3000; chk ch_xfer_lat 3232; chk devsz_mb 98304
grep -qE '\[WM-PATCH\].*\(95%\).*batch=1$' "$FEMU/log" 2>/dev/null || {
  echo "REFUSING: FEMU's log shows no 95% / batch=1 watermark -- relaunch with" >&2
  echo "          CYLON_WM_HIGH=95 CYLON_WM_LOW=85 CYLON_WM_BATCH=1 (see the header)" >&2; exit 1; }
grep -q 'eviction writeback DISABLED' "$FEMU/log" 2>/dev/null && {
  echo "REFUSING: this VM runs with eviction write-back off (a Fig. 1 launch)" >&2; exit 1; }

"${SSH[@]}" true || { echo "ERROR: guest not reachable on :$SSH_PORT" >&2; exit 1; }
"${SSH[@]}" 'test -f /run/cxdvirt_cxl_ready' ||
  { echo "REFUSING: guest device not prepared in this boot (run drivers/cylon/guest_setup_cxl.sh)" >&2; exit 1; }
"${SSH[@]}" 'test -x /root/cylon_redis_cdf.sh && test -x /root/stage/redis/redis-server &&
             test -f /root/pin_binary.so && test -x /root/stage/ycsb/bin/ycsb.sh' || {
  echo "ERROR: guest not staged -- run drivers/cylon/guest_stage_redis.sh once" >&2; exit 1; }
# The guest copy must be this repository's script, not a stale one.
"${SCP[@]}" "$HERE/guest_redis_cdf.sh" root@localhost:/root/cylon_redis_cdf.sh

mkdir -p "$OUT"
{ echo "# Cylon Redis leg, $(date -Is)"
  echo "qemu_device $(grep -oE 'femu-cxlssd[^ ]*' <<<"$QCMD" | head -1)"
  grep -hE '^\[(WM|SYNC|ASYNC|FT|WB-OFF)-PATCH|^\[WB-OFF\]' "$FEMU/log" 2>/dev/null | sort -u
  echo "records $RECORDS"; } > "$OUT/cylon_meta.txt"

for N in $RECORDS; do
  echo; echo "############ Cylon Redis, $N records ############"
  before=$(wc -l < "$BUFLOG" 2>/dev/null || echo 0)
  "${SSH[@]}" "bash /root/cylon_redis_cdf.sh $N" 2>&1 | tee "$OUT/cylon_console_$N.log"
  for f in commandstats latencystats histogram; do
    "${SCP[@]}" "root@localhost:/root/results/${f}_$N.txt" "$OUT/cylon_${f}_$N.txt" || echo "  WARNING: no ${f}_$N.txt"
  done
  "${SCP[@]}" "root@localhost:/root/results/run_$N.out"   "$OUT/cylon_run_$N.out"   || echo "  WARNING: no run_$N.out"
  "${SCP[@]}" "root@localhost:/root/results/load_$N.out"  "$OUT/cylon_load_$N.out"  || true
  "${SCP[@]}" "root@localhost:/root/results/redis_$N.log" "$OUT/cylon_redis_$N.log" || true
  sleep 2   # the last marker's dump lands host-side asynchronously
  tail -n +"$((before + 1))" "$BUFLOG" > "$OUT/cylon_buffer_$N.txt" 2>/dev/null
  grep -h 'cmdstat_hgetall' "$OUT/cylon_commandstats_$N.txt" 2>/dev/null | sed 's/^/  /'
done
# evict_sync is reported at the first eviction, which only the larger points cause.
grep -q 'evict_sync=3' "$FEMU/log" 2>/dev/null ||
  echo "WARNING: FEMU's log does not show evict_sync=3 -- check the launch" >&2
grep -hE '^\[SYNC-PATCH\]' "$FEMU/log" 2>/dev/null | sort -u >> "$OUT/cylon_meta.txt"
echo; echo "done -> $OUT"
