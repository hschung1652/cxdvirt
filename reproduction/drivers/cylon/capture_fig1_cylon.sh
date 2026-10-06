#!/usr/bin/env bash
# capture_fig1_cylon.sh -- one NAND profile of Fig. 1 (and, for 3us, the Cylon
# side of Fig. 6): MIO pointer chase over 7400 MB, one thread, two timed
# passes, with guest-felt latency and host exit windows paired per access.
#
#   ./capture_fig1_cylon.sh 3us     # -> TAG znand3us_parity_4914_chase
#   ./capture_fig1_cylon.sh 40us    # -> TAG stock40us_parity_4914_chase
#
# One VM boot per profile, on the Cylon host kernel (it exports /proc/kvm_optb):
#
#   cd <cylon>/CylonFEMU/build-femu
#   CYLON_WB_OFF=1 ./run-cxlssd.sh 98304                      # 3us
#   CYLON_WB_OFF=1 PG_RD_LAT=40000 ./run-cxlssd.sh 98304      # 40us
#   sudo drivers/cylon/pin_threads.sh                         # once the guest is up
#   drivers/cylon/guest_setup_cxl.sh            # region, devdax, warm-up
#
# CYLON_WB_OFF=1 is stock Cylon's no-op eviction flush.  Fig. 1 decomposes the
# per-miss cost of the VM path, and the patched build's default charges
# write-back, which would put a cost into the tail that stock Cylon does not
# have.  Everything else is at the launch script's default: 96 GiB device,
# 4915 MB buffer, CLOCK, 3232 ns channel transfer (the parity setting).
#
# 7400 MB against the 4915 MB buffer makes every access of a sequential chase
# miss, and -C makes the timed loop follow the ring (bench_4096_tsc_chase has one
# node per page).  Measured: ~7 min at 3us, ~15-20 min at 40us.
#
# Writes <DATA_ROOT>/cylon/{guestfelt,kvmwin,optb_stages,full}_<TAG>_gtsc.* and
# capture_<TAG>_gtsc.log, DATA_ROOT defaulting to the artifact's results/ -- the
# directory make_figures.sh draws from.  The shipped reference captures under
# plots/data/ are never written.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${CXDVIRT_ROOT:-$(cd "$HERE/../.." && pwd)}
FEMU=${FEMU:-$ROOT/cylon-tree/CylonFEMU/build-femu}
DATA=${DATA_ROOT:-$ROOT/results}
case "${1:-}" in
  3us)  TAG=${TAG:-znand3us_parity_4914_chase};  LAT=3000 ;;
  40us) TAG=${TAG:-stock40us_parity_4914_chase}; LAT=40000 ;;
  *) echo "usage: $0 3us|40us" >&2; exit 2 ;;
esac
G=(ssh -p 8080 -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@localhost)

"${G[@]}" true || { echo "REFUSING: guest not reachable on :8080" >&2; exit 1; }
"${G[@]}" '[ -c /dev/dax0.0 ] && [ -f /run/cxdvirt_cxl_ready ]' ||
  { echo "REFUSING: guest device not prepared in this boot (run drivers/cylon/guest_setup_cxl.sh)" >&2; exit 1; }
QCMD=$(tr '\0' ' ' < /proc/$(pgrep -f 'qemu-system-x86_64.*femu-cxlssd' | head -1)/cmdline 2>/dev/null)
chk() { grep -qE "$1=$2([, ]|\$)" <<<"$QCMD" || { echo "REFUSING: QEMU is not running with $1=$2" >&2; exit 1; }; }
chk pg_rd_lat "$LAT"; chk bufsz_mb 4915; chk replacement 3; chk ch_xfer_lat 3232; chk devsz_mb 98304
grep -q 'eviction writeback enabled' "$FEMU/log" 2>/dev/null &&
  { echo "REFUSING: this VM charges eviction write-back; relaunch with CYLON_WB_OFF=1" >&2; exit 1; }

mkdir -p "$DATA/cylon"
DATADIR="$DATA/cylon" TAG=$TAG SUF=_gtsc GUEST_BIN=./bench_4096_tsc_chase MIO_FLAGS=-C \
MISS_M=${MISS_M:-7400} THREADS=1 ITERS=2 \
  "$HERE/capture_guestfelt_paired.sh" 2>&1 | tee "$DATA/cylon/capture_${TAG}_gtsc.log"
rc=${PIPESTATUS[0]}
# FEMU logs the write-back mode at the first eviction, which this run caused.
grep -q 'eviction writeback DISABLED' "$FEMU/log" 2>/dev/null ||
  echo "WARNING: FEMU's log does not say write-back was DISABLED -- not the Fig. 1 configuration" >&2
exit "$rc"
