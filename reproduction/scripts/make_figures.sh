#!/usr/bin/env bash
# make_figures.sh -- draw the paper's figures from YOUR runs.
#
#   ./scripts/make_figures.sh [all|fig1|fig5|fig6|fig7|fig8]
#         from results/, where every run_experiment.sh target writes;
#         figures land in figures/
#   ./scripts/make_figures.sh --reference [all|fig1|...]
#         from plots/data/, the measurements the paper's figures were drawn
#         from; figures land in figures/reference/, for comparison
#
# Re-ran only the CXDVirt side?  ./scripts/seed_results.sh cylon copies the
# paper's Cylon series into results/ (and lists them), so the comparison
# figures draw your CXDVirt runs against the paper's Cylon runs.
#
# Needs no device, no VM and no root.  A figure whose runs are not in results/
# yet is skipped with the target that produces them, and the others still draw.
# Fig. 7 also prints every number its paragraph quotes, recomputed from the
# same data, next to the paper's value (plots/tools/fig7_numbers.py).
#
# What the plotting scripts are passed is not incidental -- running them bare
# does not reproduce the paper's figures:
#   RB_CFG / --cylon-tag ..._parity_4914_chase   the parity captures, in which
#       FEMU charges the 4 KiB channel transfer CXDVirt charges (stock Cylon
#       skips it, a free 3.2 us per miss)
#   CH_XFER=3232 / --cylon-ch-xfer 3232          draw that transfer as its own
#       segment instead of folding it into FTL+NAND
#   measured / --cylon-measured / --cxd-measured take each percentile's
#       composition from the per-access join, not a p50 body plus a rescaled tail
#   --cxd-read-pass                              CXDVirt's second recorded pass,
#       which writes nothing back, matching Cylon's CYLON_WB_OFF=1 capture
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
TOOLS=$ROOT/plots/tools
# A tree cloned to a path other than the one it was staged at: rewrite the
# scripts' absolute paths first.  Idempotent, and silent when nothing moved.
[ -x "$HERE/relocate.sh" ] && "$HERE/relocate.sh" | sed -n '/^rewriting/p'
REF=0; WHICH=all
for a in "$@"; do
  case "$a" in
    --reference) REF=1 ;;
    -*) echo "unknown option $a" >&2; exit 1 ;;
    *) WHICH=$a ;;
  esac
done
if [ "$REF" = 1 ]; then
  DATA=${DATA:-$ROOT/plots/data};  OUT=${FIGDIR:-$ROOT/figures/reference}
else
  DATA=${DATA:-$ROOT/results};     OUT=${FIGDIR:-$ROOT/figures}
fi
# The paper's figures were drawn with the pinned venv; plots/requirements.txt
# records it.  Any matplotlib >= 3.7 with numpy and scienceplots renders them,
# but tick placement can shift between matplotlib minors.
PY=${PY:-$ROOT/plots/.venv/bin/python}
[ -x "$PY" ] || PY=python3
"$PY" -c 'import matplotlib, numpy' 2>/dev/null || {
  echo "ERROR: $PY has no matplotlib/numpy.  python3 -m venv plots/.venv && plots/.venv/bin/pip install -r plots/requirements.txt" >&2
  exit 1; }
mkdir -p "$OUT"
echo "drawing from $DATA  ->  $OUT"
if [ "$REF" = 0 ] && [ -f "$DATA/SEEDED_FROM_REFERENCE.txt" ]; then
  echo "NOTE: $(grep -vc '^#' "$DATA/SEEDED_FROM_REFERENCE.txt") files under results/ are the PAPER's data, copied by"
  echo "      seed_results.sh (listed in results/SEEDED_FROM_REFERENCE.txt), not your runs."
fi
FAILED=""

# The capture tags the drivers write under (run_experiment.sh uses the same).
CYLON_TAG_3US=${CYLON_TAG_3US:-znand3us_parity_4914_chase}
CYLON_TAG_40US=${CYLON_TAG_40US:-stock40us_parity_4914_chase}
FELT_TAG=${FELT_TAG:-iso1_4914_chase}

skip() {   # figure, then the lines saying how to get its data
  local fig=$1; shift
  echo "### $fig: skipped -- no data under $DATA" >&2
  printf '    %s\n' "$@" >&2
  FAILED="$FAILED $fig"
}
have() { [ -f "$1" ] || [ -f "$1.xz" ] || [ -f "$1.gz" ]; }
have_cylon() {
  local t
  for t in "$@"; do
    have "$DATA/cylon/guestfelt_${t}_gtsc.txt" && have "$DATA/cylon/kvmwin_${t}_gtsc.csv" &&
      have "$DATA/cylon/full_${t}_gtsc.csv" || return 1
  done
}
# Figs. 1 and 6 are drawn from per-access traces, which are not included for
# the paper's runs; the targets regenerate them under results/.  Under
# --reference their absence is reported, not counted as a failure.
traces_skip() {   # figure, then the commands that produce its traces
  local fig=$1; shift
  if [ "$REF" = 1 ]; then
    echo "### $fig: not drawn -- the paper's per-access traces are not included;" >&2
    echo "    the following regenerate them under results/:" >&2
    printf '    %s\n' "$@" >&2
  else
    skip "$fig" "$@"
  fi
}

fig1() {   # Motivation: Cylon's per-miss latency, decomposed by percentile.
  echo "### Fig. 1  breakdown_refault_measured"
  have_cylon "$CYLON_TAG_3US" "$CYLON_TAG_40US" || {
    traces_skip "Fig. 1" "PROFILE=3us  ./scripts/run_experiment.sh fig1-cylon-breakdown   (read --dry-run first)" \
                         "PROFILE=40us ./scripts/run_experiment.sh fig1-cylon-breakdown"
    return 0; }
  CYLON_DATA="$DATA/cylon" CH_XFER=3232 OUTDIR="$OUT" \
  RB_CFG="${RB_CFG:-3us:$CYLON_TAG_3US,40us:$CYLON_TAG_40US}" \
    "$PY" "$TOOLS/refault_breakdown.py" measured
}

fig5() {   # Access-latency CDF with a tail inset, MIO, 1 and 8 threads.
  echo "### Fig. 5  fig5_4914_cdf_tail"
  local c missing="" use=()
  for c in cxdvirt/c4914_d64/{1,8}thr_{seq,rnd} cylon/{1,8}thr_{seq,rnd}; do
    have "$DATA/fig5_4914/$c.txt" || have "$DATA/fig5_4914/$c.lat" || missing="$missing $c"
  done
  [ -n "$missing" ] && {
    skip "Fig. 5" "missing:$missing" "sudo ./scripts/run_experiment.sh fig5-mio    (CXDVirt)" \
                  "./scripts/run_experiment.sh fig5-mio-cylon   (Cylon)"
    return 0; }
  # The paper plots the third capture of Cylon's 8-thread random cell.
  [ "$REF" = 1 ] && use=(--use "Cylon:8thr_rnd=fig5_4914/cylon_8thr_rnd_run3/8thr_rnd.txt")
  "$PY" "$TOOLS/fig5_4914_cdf_tail.py" --data "$DATA" "${use[@]}" --out "$OUT/fig5_4914_cdf_tail"
}

fig6() {   # CXDVirt vs Cylon, same decomposition, at parity.
  echo "### Fig. 6  felt_cmp_3us_parity_measured"
  have_cylon "$CYLON_TAG_3US" && have "$DATA/nvmev/felt_joined_${FELT_TAG}_seq.csv" || {
    traces_skip "Fig. 6" "sudo ./scripts/run_experiment.sh fig6-felt-cxdvirt         (CXDVirt)" \
                         "PROFILE=3us ./scripts/run_experiment.sh fig1-cylon-breakdown   (Cylon, shared with Fig. 1)"
    return 0; }
  CYLON_DATA="$DATA/cylon" NVMEV_DATA="$DATA/nvmev" \
  "$PY" "$TOOLS/felt_cmp_plot.py" \
      --cylon-tag "$CYLON_TAG_3US" --cylon-ch-xfer 3232 \
      --cxd "$DATA/nvmev/felt_joined_${FELT_TAG}_seq.csv" --cxd-read-pass \
      --cylon-measured --cxd-measured \
      --out "$OUT/felt_cmp_3us_parity_measured.png"
}

fig7() {   # (a) Splash-4 OCEAN runtime, (b) Redis read latency, both vs remote DRAM.
  echo "### Fig. 7  macro_rw_cxdvirt_vs_cylon"
  if [ "$REF" = 0 ]; then
    # Your runs: extract_fig7.py's custom mode, which discovers what the drivers
    # wrote (newest stamp per point, printed) and drops the check that the
    # numbers equal the paper's.
    export FIG7_DATA="$DATA"
    "$PY" "$TOOLS/extract_fig7.py" >/dev/null 2>"$OUT/.fig7_err" || {
      skip "Fig. 7" "$(tail -3 "$OUT/.fig7_err")" \
           "./scripts/run_experiment.sh fig7a-ocean / fig7a-ocean-cylon / fig7b-redis /" \
           "                            fig7b-redis-cylon / fig7-native"
      rm -f "$OUT/.fig7_err"; return 0; }
    rm -f "$OUT/.fig7_err"
  fi
  "$PY" "$TOOLS/macro_rw_combined.py" --out "$OUT/macro_rw_cxdvirt_vs_cylon.png" &&
    "$PY" "$TOOLS/fig7_numbers.py" | tee "$OUT/fig7_numbers.txt"
}

# Fig. 8 at 4.8 GiB: 8M records against 4914 MB.  The runs are found by their
# .meta (newest stamp per cell wins, printed).
FIG8_DIR=${FIG8_DIR:-$DATA/redis/fig8_4914}
FIG8_RECORDS=${FIG8_RECORDS:-8000000}
fig8() {   # Eviction policy (top row) and prefetch (bottom row).
  echo "### Fig. 8  policy_prefetch_combined"
  if ! ls "$FIG8_DIR"/policy/*.meta >/dev/null 2>&1 ||
     ! ls "$FIG8_DIR"/prefetch/*.meta >/dev/null 2>&1; then
    skip "Fig. 8" "sudo ./scripts/run_experiment.sh fig8a-policy       (~1.6 h)" \
                  "sudo ./scripts/run_experiment.sh fig8bcd-prefetch   (~5.5 h)"
    return 0
  fi
  "$PY" "$TOOLS/combined_policy_prefetch.py" \
      --policy-dir "$FIG8_DIR/policy" --dir "$FIG8_DIR/prefetch" \
      --records "$FIG8_RECORDS" \
      --out "$OUT/policy_prefetch_combined.png"
}

case "$WHICH" in
  all)  fig1; fig5; fig6; fig7; fig8 ;;
  fig1) fig1 ;;
  fig5) fig5 ;;
  fig6) fig6 ;;
  fig7) fig7 ;;
  fig8) fig8 ;;
  *) echo "usage: $0 [--reference] [all|fig1|fig5|fig6|fig7|fig8]" >&2; exit 1 ;;
esac
echo
if [ -n "$FAILED" ]; then
  echo "figures written to $OUT (SKIPPED:$FAILED)"
  exit 1
fi
echo "figures written to $OUT"
