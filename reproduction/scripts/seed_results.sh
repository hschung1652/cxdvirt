#!/usr/bin/env bash
# seed_results.sh -- copy the paper's Cylon series into results/, for a run that
# re-collects only the CXDVirt side.
#
#   ./scripts/seed_results.sh cylon
#
# Every figure but Fig. 8 compares CXDVirt against Cylon, and the Cylon side
# needs its own host kernel, a FEMU build and a guest image.  A reviewer who
# re-runs only the CXDVirt targets can seed results/ with the paper's Cylon
# measurements for Figs. 5 and 7 (the per-access traces of Figs. 1 and 6 are
# not included), so that make_figures.sh draws YOUR CXDVirt runs against OUR
# Cylon runs.  What was copied is listed in results/SEEDED_FROM_REFERENCE.txt,
# and make_figures.sh prints that list with every figure it draws, so a figure
# made this way can never be mistaken for a complete re-collection.
#
# Never overwrites: a Cylon file already in results/ (your own run) is kept.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
REF=$ROOT/plots/data
RES=${DATA_ROOT:-$ROOT/results}
LOG=$RES/SEEDED_FROM_REFERENCE.txt
[ "${1:-}" = cylon ] || { echo "usage: $0 cylon" >&2; exit 1; }

seed() {   # source, destination
  [ -e "$1" ] || return 0
  if [ -e "$2" ]; then echo "  kept (yours): ${2#$RES/}"; return 0; fi
  mkdir -p "$(dirname "$2")"; cp -p "$1" "$2"
  echo "${2#$RES/}  <-  plots/data/${1#$REF/}" >> "$LOG"
  echo "  seeded: ${2#$RES/}"
}
mkdir -p "$RES"
[ -f "$LOG" ] || echo "# Reference (paper) data copied into results/ by seed_results.sh $(date -Is)" > "$LOG"

echo "--- Fig. 5: Cylon's four cells (8-thread random: the third capture, as plotted)"
for c in 1thr_seq 1thr_rnd 8thr_seq; do
  for z in .txt .lat.gz; do seed "$REF/fig5_4914/cylon/$c$z" "$RES/fig5_4914/cylon/$c$z"; done
done
for z in .txt .lat.gz; do
  seed "$REF/fig5_4914/cylon_8thr_rnd_run3/8thr_rnd$z" "$RES/fig5_4914/cylon/8thr_rnd$z"
done
echo "--- Fig. 7(a): Cylon OCEAN"
for f in "$REF"/ocean_wss_cylon_s4/*; do seed "$f" "$RES/ocean_wss_cylon_s4/$(basename "$f")"; done
echo "--- Fig. 7(b): Cylon Redis (leg 3), under the names run_redis_cylon.sh writes"
for n in 1000000 3000000 6000000; do
  for k in commandstats latencystats; do
    seed "$REF/redis/cylon_leg3/cylon_leg3_${k}_$n.txt" "$RES/redis_cylon/cylon_${k}_$n.txt"
  done
  seed "$REF/redis/cylon_leg3/cylon_leg3_run_$n.out" "$RES/redis_cylon/cylon_run_$n.out"
done
echo; echo "listed in ${LOG#$ROOT/}"
