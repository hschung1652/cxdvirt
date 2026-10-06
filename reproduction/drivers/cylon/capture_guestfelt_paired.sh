#!/usr/bin/env bash
# capture_guestfelt_paired.sh — same-run capture of GUEST-felt per-access latency
# (rdtscp) AND the HOST window (/proc/kvm_optb) so the two can be paired per miss
# for the VM-transition decomposition (Path A).
#
# WHY same-run: transition_i = guest_latency_i - host_window_i needs the i-th
# guest access matched to its host window.  At ~100% miss + single thread the two
# streams are strict 1:1 in time order, so we pair by SEQUENCE (guest[i] <->
# host[i + (N_h - N_g)]) and validate with the containment constraint
# host_window <= guest_latency.  Durations only -> no cross-clock alignment.
#
# /proc/kvm_optb is mode 0666 -> NO sudo needed.  FEMU must be the desired config
# (pg_rd_lat = 0 / 3000 / 40000); set TAG to match (h0 / znand3us / stock40us).
#
# Usage:  TAG=stock40us ./capture_guestfelt_paired.sh
# Tunables: MISS_M=612 THREADS=1 ITERS=2
set -u

TAG="${TAG:-h0}"
MISS_M="${MISS_M:-612}"          # per-thread footprint (>cache=409MB -> 0%-hit churn)
THREADS="${THREADS:-1}"
ITERS="${ITERS:-2}"
# bench_4096      prints one ns latency per access  -> pair by SEQUENCE   (SUF=_seq)
# bench_4096_tsc  prints "<guest_tsc> <cycles>"     -> exact containment  (SUF=_gtsc)
# The _gtsc pair is what refault_breakdown.py / felt_cmp_plot.py consume.
GUEST_BIN="${GUEST_BIN:-./bench_4096}"
SUF="${SUF:-_seq}"
# Extra MIO flags.  MIO_FLAGS=-C makes the timed loop follow the ring (a real
# pointer chase); without it MIO -I1 times one load per node in ADDRESS order,
# i.e. a strided scan.  Needs a binary built from MIO with -C, e.g.
# eval-mio/src-tsc-chase/build.sh -> bench_4096_tsc_chase (the June
# bench_4096_tsc predates -C and would silently ignore it).
MIO_FLAGS="${MIO_FLAGS:-}"

GUEST_SSH=( ssh -p 8080 -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@localhost )
GUEST_DIR='~/Cylon/Cylon-scripts/eval-mio/src'
KVMOPTB=/proc/kvm_optb
P=/mnt/nvme/cxdvirt/cxdvirt/reproduction/plots
# Where the capture lands.  The artifact passes its results/cylon here, so a
# re-collection never mixes with the shipped reference captures.
DATADIR="${DATADIR:-$P/data/cylon}"
mkdir -p "$DATADIR"

gf="$DATADIR/guestfelt_${TAG}${SUF}.txt"     # per-access guest timing (rdtscp)
kvm="$DATADIR/kvmwin_${TAG}${SUF}.csv"       # host windows from /proc/kvm_optb

[ -w "$KVMOPTB" ] || { echo "ERROR: $KVMOPTB not writable (load kvm.ko w/ Option-B patch)." >&2; exit 1; }
"${GUEST_SSH[@]}" true 2>/dev/null || { echo "ERROR: guest not reachable on :8080 (VM up?)." >&2; exit 1; }

# confirm live FEMU latency matches TAG
QPID=$(pgrep -f 'qemu-system-x86_64.*femu-cxlssd' | head -1)
lat=$(tr '\0' ' ' < /proc/$QPID/cmdline | grep -o 'pg_rd_lat=[0-9]*' | cut -d= -f2)
case "$TAG" in h0) want=0;; znand3us) want=3000;; stock40us) want=40000;; *) want="$lat";; esac
echo "=== paired capture: TAG=$TAG  pg_rd_lat=$lat (want $want)  miss=${MISS_M}MB  t=$THREADS i=$ITERS  bin=$GUEST_BIN flags=[$MIO_FLAGS] suf=$SUF ==="
[ "$lat" = "$want" ] || echo "  WARN: live pg_rd_lat=$lat != expected $want for TAG=$TAG"

case " $MIO_FLAGS " in *" -C "*)
  "${GUEST_SSH[@]}" "cd $GUEST_DIR && grep -a -q 't:m:i:r:I:T:P:c:RC' $GUEST_BIN" \
    || { echo "REFUSING: $GUEST_BIN has no -C option (built before MIO grew it)" >&2; exit 1; } ;;
esac
# 1) isolate this run's host windows
echo reset > "$KVMOPTB"

# 2) cold-start flush + timed run with per-access guest timing (-P1 -I1)
"${GUEST_SSH[@]}" "cd $GUEST_DIR && \
  cxl read-labels mem0 -s 2 >/dev/null 2>&1 ; \
  numactl -N0 -- $GUEST_BIN -t$THREADS -r1 -i$ITERS -I1 -T0 $MIO_FLAGS -m$MISS_M -P1 \
    > /tmp/gf_${TAG}.txt 2>/dev/null ; echo guest_exit=\$?"

# 3) snapshot host windows + pull guest log
# header must match kvm_optb_seq_show() in CylonLinux arch/x86/kvm/x86.c:10604.
# The patch grew gtsc_exit/gtsc_entry (guest TSC, for the containment join) and
# cpu_ns/rundelay_ns (on-CPU work vs runqueue wait) after this script was written.
{ echo "vcpu,t_exit_ns,t_entry_ns,gtsc_exit,gtsc_entry,cpu_ns,rundelay_ns"
  cat "$KVMOPTB"; } > "$kvm"
scp -q -P 8080 -o StrictHostKeyChecking=no root@localhost:/tmp/gf_${TAG}.txt "$gf"

ng=$(wc -l < "$gf"); nh=$(( $(wc -l < "$kvm") - 1 ))
echo "  guest accesses N_g=$ng   host windows N_h=$nh   warmup offset k≈$((nh-ng))"
echo "  -> $gf"
echo "  -> $kvm"

# 4) FEMU per-miss stage dump, joined to the SAME run's host windows.
#
# Why this belongs here and not in a separate capture: the stage split
# (KVM/QEMU/FTL/NAND) and the guest-felt latency have to describe the SAME
# accesses for the breakdown to be measured per percentile rather than frozen
# at a p50 body.  Historically full_*.csv came from a `_seq` run while
# kvmwin_*/guestfelt_* came from a `_gtsc` run, so no per-access join was
# possible at all.  MIO self-fires `cxl read-labels mem0 -s 1` at the end of
# its timed loop, so FEMU has already written the dump by the time we get here.
OUTDIR="${OUTDIR:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/cylon-tree/CylonLogs}"
PY="${PY:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/plots/.venv/bin/python}"
JOIN="${JOIN:-$P/tools/optb_kvm_join.py}"
optb="$DATADIR/optb_stages_${TAG}${SUF}.csv"
full="$DATADIR/full_${TAG}${SUF}.csv"

# newest optb_stages_*.csv that is not one of our own tagged snapshots
base=$(ls -t "$OUTDIR"/optb_stages_*.csv 2>/dev/null | grep -v "_${TAG}${SUF}\.csv$" | head -1)
if [ -n "$base" ] && [ -s "$base" ]; then
  cp -f "$base" "$optb"
  echo "  optb records=$(( $(wc -l < "$optb") - 1 ))  (from $(basename "$base"))"
  if [ -x "$PY" ] && [ -f "$JOIN" ]; then
    "$PY" "$JOIN" --kvmcsv "$kvm" --optb "$optb" --out "$full" 2>&1 | sed 's/^/  /'
    [ -s "$full" ] && echo "  -> $full" \
                   || echo "  WARN: join produced no rows"
  else
    echo "  WARN: $PY or $JOIN missing — join skipped; run it by hand:"
    echo "    $PY $JOIN --kvmcsv $kvm --optb $optb --out $full"
  fi
else
  echo "  WARN: no optb_stages_*.csv in $OUTDIR — is FEMU built with LSA_TROLL"
  echo "        and did MIO fire its -s 1 marker?  full_${TAG}${SUF}.csv NOT written."
fi
