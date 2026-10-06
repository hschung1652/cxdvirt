#!/usr/bin/env bash
# run_ocean_cylon.sh — one Splash-4 OCEAN point on Cylon, via daxmalloc.
#
# Run on the HOST, as a normal user, AFTER the VM is up.  Needs no root.
#
# WHY daxmalloc AND NOT `numactl --membind 1`:
# --membind applies the mempolicy to EVERY allocation the process makes --
# heap, stack, and the page-cache pages backing the binary and its libraries.
# That put OCEAN's own text on the emulated CXL-SSD, where eviction could reach
# it; a run at BUFSZ_MB=806 came back with its text segment reading as zeros
# (SIGSEGV with `error 6` and a RIP pointing at a run of 00 bytes, then SIGILL
# on the next run from the still-corrupt page cache).
#
# It is also not the comparison we want even when it survives.  On CXDVirt,
# daxmalloc redirects ONLY malloc, so the binary/stack/libc stay in host DRAM
# and just the heap lives on the device.  Reproducing that on Cylon means using
# the same library, not a whole-process NUMA bind -- otherwise Cylon is charged
# for device work CXDVirt never does.
#
# daxmalloc is guest-safe: it computes the withheld DRAM-cache region by
# reading /sys/module/nvmev/parameters/*, and returns 0 ("not an nvmev device")
# when absent.  In the guest there is no nvmev, so it withholds nothing and
# maps the whole device -- which is right, because the SIGBUS tail that
# withholding exists to avoid is a CXDVirt-only artifact.
#
# LEAVE THE DEVICE IN devdax.  Do not convert to system-ram: that is only
# needed for the --membind path, the conversion cannot be reversed without a VM
# restart, and onlining as ZONE_MOVABLE lets the guest migrate pages underneath
# Cylon's EPT remapping.
#
# Usage:  ./run_ocean_cylon.sh            # cache inferred from the qemu cmdline
#         OCEAN_N=2050 ./run_ocean_cylon.sh
set -u

SSH_PORT=${SSH_PORT:-8080}
SSH="ssh -p $SSH_PORT -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@localhost"
OCEAN_N=${OCEAN_N:-2050}
# Threads.  OCEAN is a genuinely parallel SPLASH benchmark (CREATE(slave,nprocs),
# a 2-D xprocs*yprocs decomposition, 10 barriers per timestep), so -p1 is a
# CHOICE.  P must be a power of two and must MATCH the CXDVirt run being
# compared against -- the tiling changes locality, so -p1 and -p8 are different
# workloads at the device level and cannot be compared across thread counts.
# The guest has 8 vCPUs (n_threads=8 in run-cxlssd.sh), so -p8 is the ceiling.
OCEAN_P=${OCEAN_P:-1}
# -s adds per-process Min/Max/Avg rows; the "Proc 0" row stays first so the
# parsers below are unaffected.  On by default whenever P>1.
OCEAN_STATS=${OCEAN_STATS:-$([ "${OCEAN_P:-1}" -gt 1 ] && echo 1 || echo 0)}
OUT=${OUT:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/results/ocean_wss_cylon}
BUFLOG=${BUFLOG:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/cylon-tree/CylonLogs/cxlssd_buffer.txt}
GUEST_OCEAN=${GUEST_OCEAN:-/root/OCEAN}
GUEST_DAXMALLOC=${GUEST_DAXMALLOC:-/root/daxmalloc.so}
# Footprint per grid size.  2050 is MEASURED on CXDVirt via DAXMALLOC_STATS;
# both platforms must report the same peak_live.  8194 is 16x that -- Ocean's N
# must be (power of 2)+2 so the footprint quantises 4x, and the measured RSS
# ratio 14155.0/3542.5 = 3.996 confirms the step is clean.  8194 is now
# MEASURED too: 14863016952 B, 0.195% under the 16x extrapolation because
# the multigrid hierarchy gains a level whose coarse grids do not scale 16x.
# N must be fixed and the CACHE varied: at a fixed 4914 MB buffer both N=2050
# (0.18x) and N=4098 (0.72x) fit entirely and generate no device traffic at all.
# THE FOOTPRINT DEPENDS ON P AS WELL AS N.  Each of the P tiles carries a ghost
# row/column (the "+2" in ((imx-2)/yprocs+2) x ((jmx-2)/xprocs+2)), and those
# boundaries are DUPLICATED across tiles.  At N=8194 the 8-way split adds
# 17,161,128 B = 16.37 MiB (+0.115%) -- measured on CXDVirt, must match here.
case "${OCEAN_N}_${OCEAN_P}" in
2050_1) EXPECT_PEAK=930750968   ; FOOTPRINT_MIB=887.63   ;;
4098_1) EXPECT_PEAK=3723003872  ; FOOTPRINT_MIB=3550.52  ;;
8194_1) EXPECT_PEAK=14863016952 ; FOOTPRINT_MIB=14174.48 ;;
8194_8) EXPECT_PEAK=14880176288 ; FOOTPRINT_MIB=14190.85 ;;
*)      : "${FOOTPRINT_MIB:?unknown OCEAN_N/OCEAN_P -- set FOOTPRINT_MIB explicitly}"
        EXPECT_PEAK=${EXPECT_PEAK:-0} ;;
esac

mkdir -p "$OUT"

# --- what is the VM actually configured as? -------------------------------
qline=$(pgrep -af "qemu-system-x86_64.*femu-cxlssd" | head -1)
[ -n "$qline" ] || { echo "ERROR: no FEMU VM running -- launch it first" >&2; exit 1; }
BUFSZ=$(echo "$qline" | tr ',' '\n' | grep -oP 'bufsz_mb=\K[0-9]+')
PGRD=$(echo "$qline"  | tr ',' '\n' | grep -oP 'pg_rd_lat=\K[0-9]+')
LUNS=$(echo "$qline"  | tr ',' '\n' | grep -oP 'luns_per_ch=\K[0-9]+')
[ -n "$BUFSZ" ] || { echo "ERROR: could not read bufsz_mb from the qemu cmdline" >&2; exit 1; }
wss=$(echo "scale=3; $FOOTPRINT_MIB / $BUFSZ" | bc)
echo "=== Cylon OCEAN: bufsz=${BUFSZ}MB  wss=${wss}x  -p${OCEAN_P}  pg_rd_lat=${PGRD}  luns_per_ch=${LUNS} ==="

$SSH true 2>/dev/null || { echo "ERROR: guest not reachable on :$SSH_PORT" >&2; exit 1; }
$SSH 'test -f /run/cxdvirt_cxl_ready' 2>/dev/null ||
	{ echo "REFUSING: guest device not prepared in this boot (run drivers/cylon/guest_setup_cxl.sh)" >&2; exit 1; }
$SSH "test -x $GUEST_OCEAN && test -f $GUEST_DAXMALLOC" || {
	echo "ERROR: need $GUEST_OCEAN and $GUEST_DAXMALLOC in the guest -- see scripts/README.md, step 5" >&2; exit 1; }

# The device must still be a chardev.  If a previous session converted it to
# system-ram, /dev/dax0.0 is gone and only a VM restart brings it back.
mode=$($SSH 'daxctl list 2>/dev/null | grep -oP "\"mode\":\"\K[a-z-]+"' | head -1)
[ "$mode" = devdax ] || {
	echo "ERROR: dax0.0 is in '$mode' mode, need devdax." >&2
	echo "       The system-ram conversion is not reversible in place --" >&2
	echo "       restart the VM and do NOT run daxctl reconfigure-device." >&2
	exit 1; }

# --- run ------------------------------------------------------------------
NAME="cylon_n${OCEAN_N}_p${OCEAN_P}_c${BUFSZ}"
echo "--- running OCEAN -n$OCEAN_N -p$OCEAN_P under daxmalloc (heap on device, rest in guest DRAM)"
res=$($SSH "t0=\$(date +%s.%N)
            LD_PRELOAD=$GUEST_DAXMALLOC DAXMALLOC_REQUIRE=1 DAXMALLOC_VERBOSE=1 \
            DAXMALLOC_STATS=1 numactl --cpunodebind 0 -- \
              $GUEST_OCEAN -n$OCEAN_N -p$OCEAN_P $([ "$OCEAN_STATS" = 1 ] && echo -s) > /tmp/$NAME.txt 2>&1
            rc=\$?
            t1=\$(date +%s.%N)
            echo \"rc=\$rc wall=\$(echo \"\$t1-\$t0\" | bc)\"")
echo "    $res"
rc=$(echo "$res" | grep -oP 'rc=\K[0-9]+')
wall=$(echo "$res" | grep -oP 'wall=\K[0-9.]+')

scp -q -P "$SSH_PORT" -o StrictHostKeyChecking=no \
	root@localhost:/tmp/$NAME.txt "$OUT/$NAME.run" 2>/dev/null

# The shim must have engaged, and on the same footprint CXDVirt measured --
# a silent fallback to guest DRAM would produce a fast, plausible, wrong number.
peak=$(grep -oP 'peak_live=\K[0-9]+' "$OUT/$NAME.run" 2>/dev/null | head -1)
if [ -z "$peak" ]; then
	echo "    ERROR: no daxmalloc banner -- shim did not engage" >&2
elif [ "$EXPECT_PEAK" != 0 ] && [ "$peak" != "$EXPECT_PEAK" ]; then
	echo "    WARNING: peak_live=$peak, expected $EXPECT_PEAK (CXDVirt)" >&2
else
	echo "    peak_live=$peak B (${FOOTPRINT_MIB} MiB) — matches CXDVirt exactly"
fi
[ "$rc" = 0 ] || echo "    WARNING: OCEAN exited $rc" >&2

# --- device counters ------------------------------------------------------
# The dump lands host-side in CylonLogs and that file APPENDS, so snapshot the
# tail now or the next point buries this one.
$SSH 'cxl read-labels mem0 -s 1 >/dev/null 2>&1' ; sleep 2
tail -12 "$BUFLOG" > "$OUT/$NAME.buffer" 2>/dev/null

# Ocean's own timers, now nanosecond CLOCK_MONOTONIC (m4 CLOCK patched).
# solve_s excludes init AND the first timestep; init_s includes the first
# timestep.  timesteps=5 is the timed count, verified by breakpointing slave2.
solve=$(grep -oP 'Total time without initialization\s+:\s+\K[0-9.]+' "$OUT/$NAME.run")
init=$(grep -oP  'Initialization time\s+:\s+\K[0-9.]+'                "$OUT/$NAME.run")
multi=$(awk '/^ +0 +[0-9]/{print $3; exit}'                             "$OUT/$NAME.run")
steps=$(grep -oP 'Timed timesteps\s+:\s+\K[0-9]+'                     "$OUT/$NAME.run")
{ echo "platform cylon"; echo "placement daxmalloc"; echo "bufsz_mb $BUFSZ";
  echo "wss $wss"; echo "ocean_n $OCEAN_N"; echo "ocean_p $OCEAN_P"; echo "pg_rd_lat $PGRD";
  echo "peak_live $peak"; echo "exit $rc"; echo "wall_s $wall";
  echo "solve_s ${solve:-NA}"; echo "init_s ${init:-NA}";
  echo "multi_s ${multi:-NA}"; echo "timesteps ${steps:-NA}"; } > "$OUT/$NAME.meta"
[ -n "$solve" ] && [ -n "$steps" ] && awk -v s="$solve" -v i="$init" -v m="$multi" -v n="$steps" \
  'BEGIN{printf "    solve %.3f s (%.4f s/timestep)   init %.3f s   multigrid %.3f s (%.1f%%)\n",
         s, s/n, i, m, 100*m/s}'

# THE BUFFER PERSISTS FOR THE VM'S LIFETIME, so a second run on the same VM is
# NOT a repeat measurement.  Entry cnt stays at the previous run's residency and
# the new run finds its whole working set already there: 0 misses, 0 first-touch
# fills, no device work at all.  Observed directly -- rep 1 at 2536 MB took
# 6.46 s with 226,894 misses; reps 2 and 3 took 4.49 s with *zero*.  This is the
# same hazard CXDVirt's nand_valid poses, which is why sweep_ocean_wss.sh
# reloads the module for every single point.  Cylon has no equivalent reset, so
# error bars need one VM restart per rep.
miss_r=$(grep -aoP 'Buffer read: *[0-9]+ hit/ *\K[0-9]+' "$OUT/$NAME.buffer" 2>/dev/null | tail -1)
miss_w=$(grep -aoP 'Buffer write: *[0-9]+ hit/ *\K[0-9]+' "$OUT/$NAME.buffer" 2>/dev/null | tail -1)
if [ "${miss_r:-0}" -eq 0 ] && [ "${miss_w:-0}" -eq 0 ]; then
	echo "    ERROR: 0 buffer misses -- the working set was ALREADY RESIDENT from a" >&2
	echo "           previous run on this VM.  This measurement is meaningless." >&2
	echo "           Restart the VM for a cold buffer before re-running." >&2
fi

echo "--- device counters"
grep -aE "Buffer read|Buffer write|Dirty evictions|Entry cnt|Buffer size" \
	"$OUT/$NAME.buffer" 2>/dev/null | sed 's/^/    /'
echo "=== done: $OUT/$NAME.{run,buffer,meta} ==="
