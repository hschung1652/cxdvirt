#!/usr/bin/env bash
# sweep_ocean_wss.sh — Splash-4 OCEAN across normalized WSS, on CXDVirt.
#
# WSS IS SELECTED BY THE CACHE, NOT THE FOOTPRINT.  Ocean's -n must be a power
# of two plus 2, so its footprint quantises 4x per step and cannot be tuned to
# an arbitrary ratio the way Redis's record count could.  Fixing N=2050
# (887.63 MiB, measured via DAXMALLOC_STATS) and varying dram_cache_mb hits the
# Redis sweep's ratios exactly:
#     2536 MB -> 0.350x    806 MB -> 1.101x    404 MB -> 2.197x
# This mirrors the policy/prefetch studies, which likewise fix the Redis
# footprint at 1640 MiB and vary the cache to select 1.0x / 2.0x / 2.67x.
#
# dram_cache_mb is 0444, so every point needs a module reload.  That also
# resets nand_valid, which matters: it is module state that survives process
# exit, so a second run on a live device finds every page already programmed
# and takes none of the ~227K FREE first-touch write misses the first one did.
#
# MODES defaults to "2 0": mode 2 (read-allocate) is the policy Cylon
# implements and therefore the fidelity comparison; mode 0 is the historical
# default and shows what the allocate patch is worth at each WSS.
#
# Usage:  sudo ./sweep_ocean_wss.sh
#         sudo env MODES=2 CACHES="404" ./sweep_ocean_wss.sh
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
# The artifact's copy of the sweep: each point is a device rebuild through the
# repository's wrappers around the emulator's bin/setup.sh and teardown.sh
# (scripts/init_device.sh, scripts/teardown_device.sh), not a working tree's
# own setup script.
ROOT=$(cd "$HERE/../.." && pwd)
OCEAN=${OCEAN:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/bench/Splash-4/Splash-4/ocean-contiguous_partitions/OCEAN-CONT}
DAXMALLOC=${DAXMALLOC:-/mnt/nvme/cxdvirt/cxdvirt/emulator/tools/daxmalloc/daxmalloc.so}
OUT=${OUT:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/results/ocean_wss}

OCEAN_N=${OCEAN_N:-2050}
# Threads.  OCEAN is a genuinely parallel SPLASH benchmark -- CREATE(slave,
# nprocs), a 2-D xprocs*yprocs domain decomposition, and 10 barriers per
# timestep -- so -p1 is a CHOICE, not a property.  P must be a power of two.
# Node 0 is cpus 0-19,40-59 (20 physical cores) and the module's I/O workers
# are pinned to 20-23, which are on node 1 AND in isolcpus, so Ocean threads
# and device threads never contend for a core.
OCEAN_P=${OCEAN_P:-1}
# -s adds per-process Min/Max/Avg rows, which is how you see load imbalance
# across threads.  The "Proc 0" row stays first, so the parsers below are
# unaffected.  Defaults on whenever P>1, where the spread is the point.
OCEAN_STATS=${OCEAN_STATS:-$([ "${OCEAN_P:-1}" -gt 1 ] && echo 1 || echo 0)}
# Ocean's N must be (power of 2)+2, so the footprint quantises 4x per step and
# a FIXED cache cannot span a range of WSS -- at 4914 MB both N=2050 (0.18x)
# and N=4098 (0.72x) fit entirely and take zero device traffic.  So fix N and
# vary the cache, as at N=2050.
# N     peak_live_B        MiB      source
# 2050    930750968        887.63     measured (DAXMALLOC_STATS)
# 4098   3723003872       3550.52     4x  (RSS 3542.5 MiB)
# 8194  14863016952      14174.48     16x (RSS 14155.0 MiB)
# THE FOOTPRINT DEPENDS ON P AS WELL AS N.  Each of the P tiles carries a ghost
# row/column (the "+2" in ((imx-2)/yprocs+2) x ((jmx-2)/xprocs+2)) and those
# boundaries are DUPLICATED across tiles, so the 8-way split at N=8194 adds
# 17,161,128 B = 16.37 MiB (+0.115%).  Keyed on N alone this check fired a
# warning on every -p8 run and was therefore training us to ignore it.
case "${OCEAN_N}_${OCEAN_P}" in
2050_1) FOOTPRINT_MIB=887.63   ; EXPECT_PEAK=930750968   ;;
4098_1) FOOTPRINT_MIB=3550.52  ; EXPECT_PEAK=3723003872  ;;
8194_1) FOOTPRINT_MIB=14174.48 ; EXPECT_PEAK=14863016952 ;;
8194_8) FOOTPRINT_MIB=14190.85 ; EXPECT_PEAK=14880176288 ;;
*)      : "${FOOTPRINT_MIB:?unknown OCEAN_N/OCEAN_P -- set FOOTPRINT_MIB explicitly}"
        EXPECT_PEAK=${EXPECT_PEAK:-} ;;
esac
# 2536 -> 0.35x, 806 -> 1.1x, 404 -> 2.2x against the 887.63 MiB footprint.
# 1024 -> 0.87x is the no-eviction baseline used for device-cost subtraction.
CACHES=${CACHES:-"2536 806 404"}
MODES=${MODES:-"2 0"}
REPS=${REPS:-1}
# Zero the carve-out per load.  Without it the arena carries the previous run's
# bytes, SPLASH's implicit "malloc returns zeros" assumption breaks, and Ocean's
# convergence becomes a function of sweep history -- which is what killed the
# 404 MB points.  Adds ~15-27 s to each insmod.
ZERO_ON_INIT=${ZERO_ON_INIT:-true}
# Replacement policy.  clock is the default; fifo exists as a CONTROL.
# Under Cylon's EPT remapping a buffer hit never reaches the device, so its
# CLOCK ref bit is set once at insert and never re-armed -- it degenerates to
# FIFO.  On Ocean's cyclic whole-grid sweeps that accidentally beats true
# CLOCK, and CXDVirt issues ~15% more NAND reads at 2.2x as a result (25.4M
# vs 22.0M), costing ~25 s of modelled latency that has nothing to do with
# emulation.  Running BOTH sides on fifo removes that asymmetry.
CACHE_POLICY=${CACHE_POLICY:-clock}
# 0 = software reference bit only, matching Cylon's degenerate CLOCK.
CLOCK_HW_YOUNG=${CLOCK_HW_YOUNG:-1}
# Drain throughput.  These were HARDCODED into the setup call below, which
# silently swallowed any value passed in the environment -- a BG_DRAIN_BATCH
# override appeared to run and changed nothing.  The ceiling is roughly
# BATCH*1000/MS evictions/s; 1024/1ms suffices at -p1 but the drain still
# falls behind at -p8 (51% of scan probes decline a victim), so this must be
# tunable to tell "under-provisioned" apart from "architecturally saturated".
BG_DRAIN_BATCH=${BG_DRAIN_BATCH:-1024}
BG_DRAIN_MS=${BG_DRAIN_MS:-1}
RUNAS=${SUDO_USER:-$(id -un)}

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root (insmod)." >&2; exit 1; }
[ -x "$OCEAN" ] || { echo "ERROR: $OCEAN missing" >&2; exit 1; }
mkdir -p "$OUT"; [ -n "${SUDO_USER:-}" ] && chown -R "$SUDO_USER" "$OUT" 2>/dev/null || true

STAMP=$(date +%m%d_%H%M%S)
SUMMARY="$OUT/${STAMP}_ocean_wss_summary.txt"
{ echo "=== OCEAN WSS sweep (CXDVirt) ==="
  echo "    -n$OCEAN_N -p$OCEAN_P   footprint ${FOOTPRINT_MIB} MiB"
  echo "    caches [$CACHES] MB   modes [$MODES]   reps $REPS"
  echo "    zero_on_init=$ZERO_ON_INIT   cache_policy=$CACHE_POLICY hw_young=$CLOCK_HW_YOUNG"
  echo "    bg_drain_batch=$BG_DRAIN_BATCH bg_drain_ms=$BG_DRAIN_MS"; } | tee "$SUMMARY"

for MODE in $MODES; do
	for CACHE in $CACHES; do
		wss=$(echo "scale=3; $FOOTPRINT_MIB / $CACHE" | bc)
		for rep in $(seq 1 "$REPS"); do
			NAME="${STAMP}_m${MODE}_c${CACHE}_p${OCEAN_P}_r${rep}"
			echo | tee -a "$SUMMARY"
			echo "#### mode=$MODE cache=${CACHE}MB wss=${wss}x rep=$rep ####" | tee -a "$SUMMARY"

			"$ROOT/scripts/teardown_device.sh" >"$OUT/$NAME.teardown" 2>&1
			if ! env DRAM_CACHE_MB="$CACHE" BG_DRAIN_4K=1 BG_DRAIN_BATCH="$BG_DRAIN_BATCH" \
			         BG_DRAIN_MS="$BG_DRAIN_MS" CXL_WR_ALLOC="$MODE" \
			         ZERO_ON_INIT="$ZERO_ON_INIT" CACHE_POLICY="$CACHE_POLICY" CLOCK_HW_YOUNG="$CLOCK_HW_YOUNG" \
			         "$ROOT/scripts/init_device.sh" >"$OUT/$NAME.setup" 2>&1; then
				echo "  SETUP FAILED — see $OUT/$NAME.setup" | tee -a "$SUMMARY" >&2
				continue
			fi
			live=$(cat /sys/module/nvmev/parameters/cxl_wr_alloc)
			lcache=$(cat /sys/module/nvmev/parameters/dram_cache_mb)
			lpol=$(cat /sys/module/nvmev/parameters/cache_policy)
			lhwy=$(cat /sys/module/nvmev/parameters/cxl_clock_hw_young)
			lbat=$(cat /sys/module/nvmev/parameters/bg_drain_batch)
			[ "$live" = "$MODE" ] && [ "$lcache" = "$CACHE" ] &&
			[ "$lpol" = "$CACHE_POLICY" ] && [ "$lhwy" = "$CLOCK_HW_YOUNG" ] && [ "$lbat" = "$BG_DRAIN_BATCH" ] || {
				echo "  MISMATCH: wanted mode=$MODE cache=$CACHE pol=$CACHE_POLICY hw_young=$CLOCK_HW_YOUNG batch=$BG_DRAIN_BATCH, got $live/$lcache/$lpol/$lhwy/$lbat" \
					| tee -a "$SUMMARY" >&2; continue; }

			t0=$(date +%s.%N)
			sudo -u "$RUNAS" -- numactl --cpunodebind 0 --membind 0 -- env \
				LD_PRELOAD="$DAXMALLOC" DAXMALLOC_REQUIRE=1 DAXMALLOC_STATS=1 \
				"$OCEAN" -n"$OCEAN_N" -p"$OCEAN_P" $([ "$OCEAN_STATS" = 1 ] && echo -s) \
				>"$OUT/$NAME.run" 2>&1
			rc=$?
			t1=$(date +%s.%N)
			wall=$(echo "$t1 - $t0" | bc)
			[ $rc -eq 0 ] || echo "  WARNING: OCEAN exited $rc" | tee -a "$SUMMARY" >&2

			peak=$(grep -aoP 'peak_live=\K[0-9]+' "$OUT/$NAME.run" 2>/dev/null | head -1)
			if [ -n "$EXPECT_PEAK" ] && [ -n "$peak" ] && [ "$peak" != "$EXPECT_PEAK" ]; then
				echo "  WARNING: peak_live=$peak, expected $EXPECT_PEAK for N=$OCEAN_N" \
					| tee -a "$SUMMARY" >&2
			fi
			cat /proc/nvmev/debug >"$OUT/$NAME.nvmev" 2>&1
			# Ocean's own ns timers (m4 CLOCK patched to CLOCK_MONOTONIC).
			# solve_s excludes init and the first timestep; init_s
			# includes the first timestep; 5 timed timesteps.
			solve=$(grep -oP 'Total time without initialization\s+:\s+\K[0-9.]+' "$OUT/$NAME.run")
			init=$(grep -oP  'Initialization time\s+:\s+\K[0-9.]+'                "$OUT/$NAME.run")
			multi=$(awk '/^ +0 +[0-9]/{print $3; exit}'                             "$OUT/$NAME.run")
			steps=$(grep -oP 'Timed timesteps\s+:\s+\K[0-9]+'                     "$OUT/$NAME.run")
			{ echo "mode $MODE"; echo "cache_mb $CACHE"; echo "wss $wss";
			  echo "rep $rep"; echo "ocean_n $OCEAN_N"; echo "exit $rc";
			  echo "wall_s $wall"; echo "solve_s ${solve:-NA}";
			  echo "init_s ${init:-NA}"; echo "multi_s ${multi:-NA}";
			  echo "timesteps ${steps:-NA}"; echo "ocean_n $OCEAN_N";
					  echo "cache_policy $CACHE_POLICY"; echo "ocean_p $OCEAN_P"; echo "bg_drain_batch $BG_DRAIN_BATCH";
					  echo "bg_drain_ms $BG_DRAIN_MS";
					  echo "clock_hw_young $CLOCK_HW_YOUNG";
			  echo "peak_live ${peak:-NA}"; } >"$OUT/$NAME.meta"

			printf "  wall %.3f s\n" "$wall" | tee -a "$SUMMARY"
			[ -n "$solve" ] && [ -n "$steps" ] && awk -v s="$solve" -v i="$init" \
			  -v m="$multi" -v n="$steps" 'BEGIN{printf "  solve %.3f s (%.4f s/timestep)   init %.3f s   multigrid %.3f s (%.1f%%)\n",
			         s, s/n, i, m, 100*m/s}' | tee -a "$SUMMARY"
			grep -E "^(nand_reads|nand_writes|rmw_reads|nand_lat_skip_invalid|sync_evicts_4k|dram_occupancy)" \
				"$OUT/$NAME.nvmev" | sed 's/^/  /' | tee -a "$SUMMARY"
		done
	done
done

{ echo; echo "=== wall (s) by mode x WSS ==="
  for MODE in $MODES; do
	for CACHE in $CACHES; do
		wss=$(echo "scale=3; $FOOTPRINT_MIB / $CACHE" | bc)
		vals=$(grep -h "^wall_s" "$OUT/${STAMP}_m${MODE}_c${CACHE}_r"*.meta 2>/dev/null | awk '{print $2}')
		[ -n "$vals" ] || continue
		mean=$(echo "$vals" | awk '{s+=$1;n++} END {if(n) printf "%.3f", s/n}')
		printf "  mode %s  cache %5s MB  wss %6sx : %8s s\n" "$MODE" "$CACHE" "$wss" "$mean"
	done
  done; } | tee -a "$SUMMARY"
echo "=== artifacts in $OUT ===" | tee -a "$SUMMARY"
[ -n "${SUDO_USER:-}" ] && chown -R "$SUDO_USER" "$OUT" 2>/dev/null || true
