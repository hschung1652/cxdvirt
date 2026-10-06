#!/usr/bin/env bash
# sweep_prefetch.sh -- replicated, order-randomised prefetch-degree sweep for
# Fig. 8(b)-(d): CLOCK, next-N prefetch at N in {0,1,2,4,8}, under both
# eviction regimes, one Redis/YCSB-C point per module load.
#
#   sudo env OUT=... ./sweep_prefetch.sh            # the Fig. 8 grid, REPS=1
#   sudo env REPS=3 DEGREES="0 8" ./sweep_prefetch.sh
#
# The artifact's version of the sweep: it drives scripts/init_device.sh and
# scripts/teardown_device.sh instead of the working tree's setup.sh, and it
# sweeps the eviction regime inside the cell list rather than as separate runs.
#
# THE TWO REGIMES differ only in the background drain's throughput ceiling:
#   slow  bg_drain_batch=64   bg_drain_ms=10   ~6.4K evictions/s, below the
#         ~12-13K/s this workload demands, so most evictions land in the fault
#         handler;
#   fast  bg_drain_batch=1024 bg_drain_ms=1    never binds.
# The figure's point is that prefetch looks helpful only in the slow regime,
# where its clean, unreferenced pages displace dirty victims.  Both regimes run
# CLOCK with the hardware Accessed bit (cxl_clock_hw_young=1).
#
# WHY RANDOMISED AND WHY RELOADED.  prefetch_degree is 0444, so every cell is a
# full teardown -> insmod -> region -> Redis load -> run cycle, and a fixed order
# would alias drift onto the degree axis.  The cell list is shuffled inside each
# rep.  run_policy_redis.sh reads the live module parameters and stamps degree,
# cache and drain into the file name and a .meta, so a run can never be
# attributed to the wrong cell.  The figure draws one run per cell; REPS>1 is
# for the between-run variance the text quotes (CV 2.4% on the mean, 3.9% on
# p99 at 1M records).
#
# WSS is set by the (cache, records) pair.  The default is the paper's
# 4.8 GiB point: 8M records (12.7 GiB measured heap) against 4914 MB = 2.65x
# (WSS_TAG keeps the 2.67 label of the earlier 1M-record / 614 MB point).
# The 1M-record / 614 MB grid of the earlier figure is
#   CACHE_MB=614 RECORDS=1000000
# Each cell takes ~32 min at 8M records (load ~20, run ~11) and ~4 min at 1M.
#
# `sudo env VAR=... ./sweep_prefetch.sh`, not `sudo VAR=...`: sudo with
# env_reset and no SETENV rejects command-line assignments.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${CXDVIRT_ROOT:-$(cd "$HERE/../.." && pwd)}
POLICIES=${POLICIES:-clock}
DEGREES=${DEGREES:-"0 1 2 4 8"}
REGIMES=${REGIMES:-"slow fast"}
REPS=${REPS:-1}
# Reps are the outer loop and each is a complete shuffled pass, so a sweep can
# be EXTENDED rather than restarted: REP_START=2 REPS=2 adds reps 2 and 3.
REP_START=${REP_START:-1}
RECORDS=${RECORDS:-8000000}
CACHE_MB=${CACHE_MB:-4914}
WSS_TAG=${WSS_TAG:-2.67}
THREADS=${THREADS:-1}
CLOCK_HW_YOUNG=${CLOCK_HW_YOUNG:-1}
LIFO_STAGE=${LIFO_STAGE:-1024}
OUT=${OUT:-$ROOT/results/redis/fig8_4914/prefetch}
SETTLE=${SETTLE:-30}          # idle seconds between points
# Control arm: 1 = prefetch random pages.  Same mechanism, no prediction.
PREFETCH_RANDOM=${PREFETCH_RANDOM:-0}
DRY=${DRY:-0}

[ "$DRY" = 1 ] || [ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root (sudo env ...)." >&2; exit 1; }
for f in "$ROOT/bench/ycsb/cxdvirt-configs/redis-load-$RECORDS.properties" \
         "$ROOT/bench/ycsb/cxdvirt-configs/redis-run-$RECORDS-${THREADS}t-zipfian.properties"; do
  [ -f "$f" ] || { echo "ERROR: $f missing (scripts/fetch.sh installs ycsb-configs/)" >&2; exit 1; }
done
drain() { case "$1" in slow) echo "64 10" ;; fast) echo "1024 1" ;;
          *) echo "ERROR: unknown regime $1 (slow|fast)" >&2; exit 1 ;; esac; }
for r in $REGIMES; do drain "$r" >/dev/null; done

# World-writable on purpose: this runs as root but analysis runs as the login user.
[ "$DRY" = 1 ] || { mkdir -p "$OUT" && chmod 0777 "$OUT"; }
MANIFEST=$OUT/manifest.csv
[ "$DRY" = 1 ] || [ -s "$MANIFEST" ] ||
  echo "rep,policy,degree,regime,cache_mb,records,threads,started,status" > "$MANIFEST"

CELLS=()
for p in $POLICIES; do for rg in $REGIMES; do for d in $DEGREES; do
  CELLS+=("$p:$d:$rg")
done; done; done

echo "=== prefetch sweep: ${#CELLS[@]} cells x $REPS reps = $(( ${#CELLS[@]} * REPS )) runs ==="
echo "    policies=[$POLICIES] degrees=[$DEGREES] regimes=[$REGIMES]"
echo "    cache=${CACHE_MB}MB records=$RECORDS threads=$THREADS  out=$OUT"
echo "    each run reloads nvmev.ko; unattended from here -- do not touch /dev/dax0.0."

run_cell() {
  local rep=$1 policy=$2 deg=$3 rg=$4 mode=1 batch ms
  [ "$deg" = 0 ] && mode=0        # degree 0 == prefetch genuinely off
  read -r batch ms < <(drain "$rg")
  echo
  echo "########## rep=$rep policy=$policy degree=$deg regime=$rg (drain $batch/${ms}ms) $(date +%T)"
  if [ "$DRY" = 1 ]; then
    echo "  init_device.sh CACHE_POLICY=$policy PREFETCH_MODE=$mode PREFETCH_DEGREE=$deg" \
         "DRAM_CACHE_MB=$CACHE_MB BG_DRAIN_4K=1 BG_DRAIN_BATCH=$batch BG_DRAIN_MS=$ms" \
         "CLOCK_HW_YOUNG=$CLOCK_HW_YOUNG LIFO_STAGE=$LIFO_STAGE PREFETCH_RANDOM=$PREFETCH_RANDOM"
    echo "  run_policy_redis.sh $RECORDS $THREADS  -> $OUT"
    return 0
  fi

  "$ROOT/scripts/teardown_device.sh" 2>&1 | sed 's/^/  /'
  env CACHE_POLICY="$policy" PREFETCH_MODE="$mode" PREFETCH_DEGREE="$deg" \
      DRAM_CACHE_MB="$CACHE_MB" BG_DRAIN_4K=1 BG_DRAIN_BATCH="$batch" BG_DRAIN_MS="$ms" \
      CLOCK_HW_YOUNG="$CLOCK_HW_YOUNG" LIFO_STAGE="$LIFO_STAGE" PREFETCH_RANDOM="$PREFETCH_RANDOM" \
      "$ROOT/scripts/init_device.sh" 2>&1 | sed 's/^/  /'
  [ "${PIPESTATUS[0]}" = 0 ] || { echo "  FATAL: init_device.sh failed" >&2; return 1; }

  # A silently fallen-back parameter is exactly how unlabelled data happened
  # before; check the module came up as asked.
  local P=/sys/module/nvmev/parameters k v
  for kv in "prefetch_mode=$mode" "prefetch_degree=$deg" "dram_cache_mb=$CACHE_MB" \
            "bg_drain_batch=$batch" "bg_drain_ms=$ms" "cache_policy=$policy"; do
    k=${kv%%=*}; v=${kv#*=}
    [ "$(cat "$P/$k" 2>/dev/null)" = "$v" ] ||
      { echo "  FATAL: $k=$(cat "$P/$k" 2>/dev/null), wanted $v" >&2; return 1; }
  done

  OUT="$OUT" REP="$rep" WSS_TAG="$WSS_TAG" \
    "$HERE/run_policy_redis.sh" "$RECORDS" "$THREADS" 2>&1 | sed 's/^/  /'
  local rc=${PIPESTATUS[0]}
  echo "$rep,$policy,$deg,$rg,$CACHE_MB,$RECORDS,$THREADS,$(date -Is),$rc" >> "$MANIFEST"
  sleep "$SETTLE"
  return "$rc"
}

REP_END=$(( REP_START + REPS - 1 ))
for rep in $(seq "$REP_START" "$REP_END"); do
  echo
  echo "===================== REP $rep (of $REP_START..$REP_END) ====================="
  mapfile -t SHUF < <(printf '%s\n' "${CELLS[@]}" | shuf)
  for cell in "${SHUF[@]}"; do
    IFS=: read -r policy deg rg <<< "$cell"
    run_cell "$rep" "$policy" "$deg" "$rg" || echo "  !! cell failed, continuing"
  done
done
[ "$DRY" = 1 ] || "$ROOT/scripts/teardown_device.sh" 2>&1 | sed 's/^/  /'

echo
echo "=== sweep done.  manifest: $MANIFEST ==="
