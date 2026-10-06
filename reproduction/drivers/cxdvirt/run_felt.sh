#!/bin/bash
# run_felt.sh — capture CXDVirt's felt (application-visible) miss latency and
# decompose it, the way the motivation experiments decompose Cylon's.
#
# The Option-B dump alone only sees inside the handler.  What an application
# waits for also includes fault entry/exit, TLB work, resume, and any refault
# after a VM_FAULT_RETRY.  felt_probe brackets each page touch with rdtscp;
# join_felt.py matches those windows against the handler's own records and
# splits the difference out as the re-entry gap.
#
# Per run:  reset optb4k -> felt_probe over the DAX device -> dump optb4k ->
#           join -> per-percentile stage table (+ optional per-access CSV)
#
# Requires 4K mode (page_granularity=4k) — the record is 4K-mode only.  Runs as
# a normal user: /dev/dax0.0 is 0666, /proc/nvmev/optb4k is 0666 (read = dump,
# any write = reset).
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
FELT=$(cd "$HERE/../../tools/felt" && pwd)
PROBE=${PROBE:-$FELT/felt_probe}
JOIN=${JOIN:-$FELT/join_felt.py}
PY=${PY:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/plots/.venv/bin/python}
OPTB=${OPTB:-/proc/nvmev/optb4k}
DEBUG=${DEBUG:-/proc/nvmev/debug}
OUTDIR=${OUTDIR:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/results/nvmev}
TAG=${TAG:-felt1}

MB=${MB:-512}                     # footprint, MB.  > dram_cache_mb forces evict+refault
PASSES=${PASSES:-2}               # passes over the footprint
ORDER=${ORDER:-seq}               # seq | rand  (pass 1 is always sequential: cold fill)
# w  = write always (write-allocate: skips the NAND read, charges tPROG at evict)
# r  = read always  (a never-written page is zero-filled and pays NO tR)
# wr = write pass 0, read after — the refault that actually pays tR.  This is
#      the mode that corresponds to what the Cylon motivation runs measure.
MODE=${MODE:-wr}
# `wait` (primary-fill stall) and RETRY refaults (multi-exit refault) require two
# threads faulting the SAME page at once, so they are 0 unless SHARE=share.
THREADS=${THREADS:-1}
SHARE=${SHARE:-split}             # split = disjoint ranges | share = all sweep everything
# Leading passes that run but are not recorded.  MIO writes the whole buffer and
# does one untimed chase before its timed loop, so Cylon's felt is over READS
# only; WARMUP=1 with MODE=wr matches that population.
WARMUP=${WARMUP:-0}
# Bytes between consecutive accesses.  4096 (default) = every access lands on a
# new page and faults, so the run is ~100% miss.  64 walks cache lines within a
# page like MIO's pointer chase: 1 fault then 63 hits, which is what exposes the
# bimodal hit/miss distribution.  SAMPLE records 1 in N accesses (the CDF is
# unbiased under uniform subsampling) to bound the output size at small strides.
STRIDE=${STRIDE:-4096}
SAMPLE=${SAMPLE:-1}
# Accesses per recorded sample, mirroring MIO's -I <chase_interval>: one timing
# bracket spans GROUP accesses and the row stores the per-access mean.  Cylon's
# figure3.sh uses -I 8, so 1 of every 8 samples contains a page's fault (a 4 KiB
# page holds 8 groups at a 64 B stride) rather than 1 in 64.
GROUP=${GROUP:-1}
# 1 = follow a dependent pointer ring (MIO's op_ptr_chase) instead of issuing
# independent strided loads.  Without it the CPU pipelines a whole group and
# the prefetcher streams it, so "hits" measure L1 (~3 ns) rather than the DRAM
# cache.  The warm-up pass builds the ring.
CHASE=${CHASE:-0}
BAND=${BAND:-0.05}                # percentile band half-width for the decomposition
# CACHED re-faults (outcome=2) are not demand fills; Cylon's per-fill breakdown
# counts fills only, so drop them by default.  DROP="" keeps everything.
DROP=${DROP:-2}
KEEP=${KEEP:-0}                   # 1 = keep the raw per-access CSV (it is large)
# Pin the probe.  The reserved region lives on node1 (the memmap= carve-out is
# visible as ~97 GiB missing from node1's MemTotal), and the module's workers
# hold isolated cores 20-23, so a node1 core outside those is the right place:
# measured p50 gap 810 -> 720 ns, p99 3029 -> ~1600 ns.  Prefer a core that is
# ALSO in isolcpus, otherwise normal system work moves onto it and p99.9 gets
# worse than leaving the scheduler free to migrate.
CPU=${CPU:-}

[ -x "$PROBE" ] || { echo "ERROR: build the probe first: (cd $FELT && gcc -O2 -pthread -o felt_probe felt_probe.c)"; exit 1; }
[ -e "$OPTB" ]  || { echo "ERROR: $OPTB missing — is nvmev.ko loaded with enable_cxl and page_granularity=4k?"; exit 1; }
[ -x "$PY" ]    || { echo "ERROR: $PY missing (the plots venv provides numpy)"; exit 1; }
mkdir -p "$OUTDIR"

FELT="$OUTDIR/felt_${TAG}_${ORDER}.csv"
OPTBCSV="$OUTDIR/optb4k_felt_${TAG}_${ORDER}.csv"
JOINED="$OUTDIR/felt_joined_${TAG}_${ORDER}.csv"

dbg() { awk -v k="$1" 'index($0,k)==1 {print $2; exit}' "$DEBUG"; }

echo "=== felt capture (tag=$TAG order=$ORDER mode=$MODE ${MB}MB x${PASSES} t=$THREADS $SHARE warm=$WARMUP stride=$STRIDE group=$GROUP chase=$CHASE cpu=${CPU:-any}) ==="
if [ "$(cat /sys/module/nvmev/parameters/optb_tsc 2>/dev/null)" = "N" ]; then
	echo "ERROR: optb_tsc is off, so tsc_in/tsc_dur are 0 and the join cannot run."
	echo "       enable it:  echo 1 | sudo tee /sys/module/nvmev/parameters/optb_tsc"
	exit 1
fi

echo reset > "$OPTB"                        # zero the record index for this run
m0=$(dbg 'cache_misses:')

# stdout is the TSC calibration; stderr is the human-readable progress
PIN=""; [ -n "$CPU" ] && PIN="taskset -c $CPU"
TSC=$($PIN "$PROBE" "$MB" "$PASSES" "$ORDER" "$FELT" "$MODE" "$THREADS" "$SHARE" "$WARMUP" "$STRIDE" "$SAMPLE" "$GROUP" "$CHASE") || exit 1
m1=$(dbg 'cache_misses:'); occ=$(awk '/dram_occupancy:/{print $2; exit}' "$DEBUG")

ncol=$(head -1 "$OPTB" | awk -F, '{print NF}')
[ "$ncol" = 11 ] || {
	echo "ERROR: $OPTB has $ncol columns, expected 11 — load the nvmev.ko rebuilt"
	echo "       with the felt instrumentation (tid,page_idx,outcome,tsc_in,tsc_dur,...)."
	exit 1
}
{ echo "tid,page_idx,outcome,tsc_in,tsc_dur,dispatch,wait,evict,modeled,install,total"
  cat "$OPTB"; } > "$OPTBCSV"
echo "   misses(debug)=$((m1-m0))  occ=$occ  optb_records=$(($(wc -l < "$OPTBCSV") - 1))"
echo "   tsc_per_ns=$TSC"
echo

"$PY" "$JOIN" --felt "$FELT" --optb "$OPTBCSV" --tsc-per-ns "$TSC" \
	--band "$BAND" --drop-outcome "$DROP" --out "$JOINED" || exit 1

[ "$KEEP" = 1 ] || rm -f "$FELT"
echo
echo "kept: $OPTBCSV"
echo "      $JOINED   (one row per faulting access: felt_ns,gap_ns,n_faults,stages)"
