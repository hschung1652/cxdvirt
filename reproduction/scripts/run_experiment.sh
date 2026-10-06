#!/usr/bin/env bash
# run_experiment.sh -- one entry point per figure in the paper.
#
#   ./run_experiment.sh list
#   ./run_experiment.sh <target> [--dry-run]
#
# Each target below records the EXACT configuration that produced the published
# figure, so the mapping from a number in the paper to the command that made it
# is in one place instead of in shell history.
#
# EVERY TARGET WRITES UNDER results/ (DATA_ROOT overrides), and make_figures.sh
# draws from results/ by default -- the figures you get are from your runs.  The
# paper's own measurements are the read-only reference under plots/data/
# (make_figures.sh --reference draws those).
#
# THE EMULATOR is ../emulator.  Nothing here edits it: the device is brought up
# by scripts/init_device.sh, a wrapper around ../emulator/bin/setup.sh.  --dry-run prints the commands
# without running them; read that output before running anything, because most
# targets reload the kernel module per point and several take hours.
#
# TARGETS THAT NEED ROOT reload nvmev.ko themselves and call teardown between
# points; they must be started with the device DOWN.  Targets marked (guest)
# run against Cylon and need the FEMU VM already up -- the two emulators cannot
# share a boot, since nvmev.ko's vermagic is 6.18.5 and FEMU's host kernel is
# 6.4.6.  See scripts/README.md, step 5.
#
# THE 4.8 GiB CONFIGURATION.  Figs. 1, 5, 6, 7(b) and 8 all run a 4914 MB DRAM
# cache on the 96 GiB device -- Cylon's default buffer is ssd_size/20 = 4915 MB
# there -- and choose each workload's footprint to hit the normalized WSS the
# figure needs.  Fig. 7(a) varies the cache instead (OCEAN's footprint cannot
# be tuned; see its target).
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${CXDVIRT_ROOT:-$(cd "$HERE/.." && pwd)}
BENCH=$ROOT/bench
DRIVERS=$ROOT/drivers
DATA=${DATA_ROOT:-$ROOT/results}
# A tree moved since it was staged still names the old path in its drivers.
# Relocating rewrites files, which a root process must not do to the owner's
# tree, so this refuses instead; scripts/fetch.sh relocates automatically.
STAGED=$(grep -m1 '^ARTIFACT_ROOT=' "$HERE/.artifact_root" 2>/dev/null | cut -d= -f2-)
if [ -n "$STAGED" ] && [ "$STAGED" != "$(cd "$ROOT/.." && pwd)" ]; then
  echo "ERROR: this tree was staged at $STAGED; run scripts/relocate.sh (not as root) first." >&2
  exit 1
fi

DRY=0
for a in "$@"; do [ "$a" = "--dry-run" ] && DRY=1; done
TARGET=${1:-list}

run() {
  # shell-quoted, so a --dry-run line can be pasted back into a shell as is
  printf '\n$'; printf ' %q' "$@"; printf '\n'
  [ "$DRY" = 1 ] || "$@"
}
# Both checks stand aside under --dry-run, so a target can be previewed without
# root and with a device up -- which is the point of previewing it.
need_root() { [ "$DRY" = 1 ] && return 0; [ "$(id -u)" -eq 0 ] || { echo "ERROR: $TARGET needs root." >&2; exit 1; }; }
need_down() { [ "$DRY" = 1 ] && return 0; lsmod | grep -q '^nvmev ' && { echo "ERROR: nvmev is loaded; this target reloads it itself. Run teardown_device.sh." >&2; exit 1; }; return 0; }
# Cylon targets: the VM and its guest are brought up by hand (see each target);
# these check what a script can check before the capture starts.
need_vm() {
  [ "$DRY" = 1 ] && return 0
  pgrep -f 'qemu-system-x86_64.*femu-cxlssd' >/dev/null || { echo "ERROR: no Cylon VM running (see the steps above)." >&2; exit 1; }
  [ -e /proc/kvm_optb ] || { echo "ERROR: /proc/kvm_optb missing -- boot the patched CylonLinux host kernel." >&2; exit 1; }
}

case "$TARGET" in

list)
  cat <<'EOT'
Targets (paper figure -> experiment).  Times are for one repetition.

  fig1-cylon-breakdown   (guest) Cylon per-miss latency decomposed by percentile,
                         PROFILE=3us or PROFILE=40us: one VM boot each, ~10 and
                         ~20 min.  The 3us capture is also Fig. 6's Cylon side.
  fig5-mio               (root)  CXDVirt MIO pointer chase, 1 and 8 threads,
                         sequential and random, 4914 MB cache.  ~90 min.
  fig5-mio-cylon         (guest) The same four cells on Cylon.  ~35 min.
  fig6-felt-cxdvirt      (root)  CXDVirt felt per-miss latency, same decomposition
                         as Fig. 1, 4914 MB cache.  ~10 min.
  fig7a-ocean            (root)  Splash-4 OCEAN -n8194 -p8 across 0.35/1.1/2.2x WSS,
                         2 reps.  ~50 min.
  fig7a-ocean-mode0      (root)  The same at 2.2x with cxl_wr_alloc=0: the control
                         the text's per-miss overhead takes its fetch cost from.
                         ~3 min.
  fig7a-ocean-cylon      (guest) The same three points on Cylon, one VM boot each.
  fig7b-redis            (root)  Redis/YCSB-C, 1 thread, 1M/3M/6M records at 4914 MB.
  fig7b-redis-cylon      (guest) The same three points on Cylon, one VM boot.
  fig7-native            (bare)  Remote-DRAM baselines for both dashed lines,
                         placement-matched to CXDVirt, 3 reps each.  ~8 min.
                         On 6.18.5 with no module loaded and no VM running.
  fig8a-policy           (root)  FIFO / CLOCK / LIFO, Redis 8M records against
                         4914 MB (2.65x WSS), 1 thread.  ~1.6 h.
  fig8bcd-prefetch       (root)  Prefetch degree 0/1/2/4/8 under slow and fast
                         eviction, same point.  ~5.5 h (REPS=1).

  all-cxdvirt            (root)  fig5, fig6, fig7a, fig7a-mode0, fig7b, fig8a, fig8bcd.

Results land under results/; ./scripts/make_figures.sh then draws from them.
Add --dry-run to print commands without executing.
EOT
  ;;

# ---------------------------------------------------------------------------
# Fig. 1 -- Cylon's per-miss latency, decomposed.  The motivation measurement.
#
# Two NAND profiles, 3 us (Z-NAND, the profile the paper argues about) and 40 us
# (planar), each a separate VM boot: tR is a launch parameter (PG_RD_LAT).  The
# launch script is the parity build's, which charges the same 3232 ns channel
# transfer per 4 KiB CXDVirt does; stock Cylon has that stage #if 0'd and would
# be handed a free 3.2 us per miss.
#
# Each capture pairs, IN ONE RUN, the guest-felt per-access latency (rdtscp in
# MIO) with the host window from /proc/kvm_optb and FEMU's per-stage record, so
# the VM transition cost is a per-miss difference rather than a difference of
# means.  MIO is the chase build (bench_4096_tsc_chase -C): one node per page, a
# dependent load per access, and 7400 MB against the 4915 MB buffer -- 1.5x
# WSS -- so every access misses.  The 3 us capture is also the Cylon side of Fig. 6.
# ---------------------------------------------------------------------------
fig1-cylon-breakdown)
  PROFILE=${PROFILE:-}
  case "$PROFILE" in
    3us)  LAUNCH="CYLON_WB_OFF=1 ./run-cxlssd.sh 98304" ;;
    40us) LAUNCH="CYLON_WB_OFF=1 PG_RD_LAT=40000 ./run-cxlssd.sh 98304" ;;
    *) if [ "$DRY" = 1 ]; then
         for p in 3us 40us; do PROFILE=$p "$0" fig1-cylon-breakdown --dry-run; done; exit 0
       fi
       echo "ERROR: set PROFILE=3us or PROFILE=40us (one VM boot per profile)." >&2; exit 1 ;;
  esac
  cat <<EOT
### Fig. 1, $PROFILE profile.  On the CylonLinux (6.4.6) host kernel, which exports
### /proc/kvm_optb, with the FEMU VM brought up by hand:
###
###   cd $ROOT/cylon-tree/CylonFEMU/build-femu
###   $LAUNCH
###   sudo $DRIVERS/cylon/pin_threads.sh        # once the guest is up
###   drivers/cylon/guest_setup_cxl.sh          # region, devdax, warm-up
###
### CYLON_WB_OFF=1 is stock Cylon's no-op eviction flush: this figure isolates the
### per-miss cost of the VM path.  Everything else is the launch script's default
### (96 GiB device, 4915 MB buffer, CLOCK, 3232 ns channel transfer).
EOT
  need_vm
  run "$DRIVERS/cylon/guest_build_mio.sh"
  run env DATA_ROOT="$DATA" "$DRIVERS/cylon/capture_fig1_cylon.sh" "$PROFILE"
  ;;

# ---------------------------------------------------------------------------
# Fig. 5 -- access-latency CDF, MIO pointer chase, 1 and 8 threads.
#
# 4914 MB cache, and the SLOW background drain (64 victims per 10 ms): one MIO
# thread misses ~100K times a second, so most evictions happen in the fault
# handler, as in the published capture.  CLOCK_HW_YOUNG=0 puts CXDVirt's CLOCK
# on Cylon's footing (a buffer hit under EPT remapping never re-arms the
# reference bit).  Footprints: 1 thread -m 8390 (1.7x), 8 threads -m 1534 each
# (2.5x).  See drivers/cxdvirt/run_mio_cxdvirt.sh.
# ---------------------------------------------------------------------------
fig5-mio)
  need_root; need_down
  run env DRAM_CACHE_MB=4914 CACHE_POLICY=clock CLOCK_HW_YOUNG=0 \
          BG_DRAIN_4K=1 BG_DRAIN_BATCH=64 BG_DRAIN_MS=10 \
      "$HERE/init_device.sh"
  run sudo -u "${SUDO_USER:-root}" env OUT="$DATA/fig5_4914/cxdvirt/c4914_d64" \
      "$DRIVERS/cxdvirt/run_mio_cxdvirt.sh" all
  run "$HERE/teardown_device.sh"
  ;;

fig5-mio-cylon)
  cat <<EOT
### Fig. 5, Cylon side.  CylonLinux host kernel, VM at the launch script's
### defaults -- 4915 MB buffer, CLOCK, 3 us tR, eviction write-back ON:
###
###   cd $ROOT/cylon-tree/CylonFEMU/build-femu
###   ./run-cxlssd.sh 98304
###   sudo $DRIVERS/cylon/pin_threads.sh        # once the guest is up
###   drivers/cylon/guest_setup_cxl.sh          # region, devdax, warm-up
###
### Do not reuse a VM launched for Fig. 1 (CYLON_WB_OFF=1); the capture refuses
### one once it has evicted.
EOT
  need_vm
  run "$DRIVERS/cylon/guest_build_mio.sh"
  run env OUT="$DATA/fig5_4914/cylon" "$DRIVERS/cylon/capture_mio_cylon.sh" all
  ;;

# ---------------------------------------------------------------------------
# Fig. 6 -- CXDVirt's felt per-miss latency, decomposed like Fig. 1.
#
# The in-handler dump alone only sees inside the fault handler.  What the
# application waits for also includes fault entry/exit, TLB work and resume, so
# felt_probe brackets each page touch with rdtscp and join_felt.py matches those
# windows against the handler's own records, splitting the difference out as the
# re-entry gap.  This is the quantity Fig. 1 measures on Cylon, measured the
# same way, against the same 7400 MB pointer chase over a 4914 MB cache.
#
# PASSES=3 WARMUP=1 MODE=wr: an unrecorded write pass fills the device, then two
# recorded passes read.  The figure takes the SECOND recorded pass
# (felt_cmp_plot.py --cxd-read-pass): the first evicts the dirty pages the
# warm-up wrote, and Cylon, launched with CYLON_WB_OFF=1 for Fig. 1, charges no
# write-back.  7400 MB x 3 passes = 5.7M misses recorded in the handler, past
# the module's default 2M-record buffer, hence OPTB4K_MAX.
#
# CPU=24 pins the probe to a core on the carve-out's node that is isolated and
# not one of the module's I/O workers (20-23): unpinned, the published p99s do
# not reproduce (re-entry gap 3.2 vs 1.4 us).  Set FELT_CPU for your machine,
# or FELT_CPU= to leave it to the scheduler.
# ---------------------------------------------------------------------------
fig6-felt-cxdvirt)
  need_root; need_down
  run env DRAM_CACHE_MB=4914 CACHE_POLICY=clock CLOCK_HW_YOUNG=0 \
          BG_DRAIN_4K=1 BG_DRAIN_BATCH=64 BG_DRAIN_MS=10 OPTB4K_MAX=8388608 \
      "$HERE/init_device.sh"
  # TAG is what make_figures.sh looks for (felt_joined_<TAG>_seq.csv); pass
  # the same FELT_TAG to both to keep your own name.
  run sudo -u "${SUDO_USER:-root}" env \
      PROBE="$ROOT/tools/felt/felt_probe" JOIN="$ROOT/tools/felt/join_felt.py" \
      OUTDIR="$DATA/nvmev" TAG="${FELT_TAG:-iso1_4914_chase}" CPU="${FELT_CPU-24}" \
      CHASE=1 ORDER=seq KEEP=0 MB=7400 PASSES=3 WARMUP=1 MODE=wr THREADS=1 \
      "$DRIVERS/cxdvirt/run_felt.sh"
  run "$HERE/teardown_device.sh"
  ;;

# ---------------------------------------------------------------------------
# Fig. 7(a) -- Splash-4 OCEAN, 8 threads.
#
# WSS IS SELECTED BY THE CACHE, NOT THE FOOTPRINT.  OCEAN's -n must be a power
# of two plus 2, so its footprint quantises 4x per step and cannot be tuned to
# an arbitrary ratio.  Fix N=8194 -p8 (14190.85 MiB, measured via
# DAXMALLOC_STATS) and vary dram_cache_mb: 40578 -> 0.35x, 12912 -> 1.1x,
# 6456 -> 2.2x, the same three ratios the Redis panel sweeps.
#
# ZERO_ON_INIT is mandatory here.  daxmalloc serves malloc from a fixed arena
# over the device, not from fresh zero-filled mmap pages, and SPLASH assumes
# malloc returns zeros -- without zeroing, OCEAN's multigrid starts from the
# previous run's residual and its convergence path depends on sweep history.
#
# MODE 2 (read-allocate) is the arm plotted: it is the write-miss policy Cylon
# implements, so it is the fidelity comparison.  Set MODES="2 0" to also get the
# never-fetch arm.
#
# CLOCK_HW_YOUNG=0 puts CXDVirt's CLOCK on the same footing as Cylon's, which
# degenerates to FIFO because a buffer hit under EPT remapping never reaches the
# device to re-arm the reference bit.
# ---------------------------------------------------------------------------
fig7a-ocean)
  need_root; need_down
  run env OCEAN="$BENCH/Splash-4/Splash-4/ocean-contiguous_partitions/OCEAN-CONT" \
          OCEAN_N=8194 OCEAN_P=8 CACHES="40578 12912 6456" MODES=2 REPS=2 \
          ZERO_ON_INIT=true CACHE_POLICY=clock CLOCK_HW_YOUNG=0 \
          BG_DRAIN_BATCH=4096 BG_DRAIN_MS=1 \
          OUT="$DATA/ocean_wss_s4" \
      "$DRIVERS/cxdvirt/sweep_ocean_wss.sh"
  ;;

# ---------------------------------------------------------------------------
# Fig. 7(a) text -- the fetch-cost control behind the per-miss overhead.
#
# The text's "1.1-2.6 us per miss on CXDVirt against 15.8-17.3 us on Cylon"
# subtracts from each emulator's excess over remote DRAM the NAND read time that
# actually reached wall-clock, and that needs the wall-clock cost of one fetch.
# This point measures it: the 2.2x configuration of fig7a-ocean with
# cxl_wr_alloc=0 (a write miss installs the page without fetching it), which
# removes about half of the fetches and leaves the evictions unchanged, so
#     c = (dT + d_inline_writeback) / d_fetches
# against the mode-2 reps.  plots/tools/fig7_numbers.py does the arithmetic.
# Mode 0 is an instrumentation probe, not a valid OCEAN execution (partial-page
# writes see stale data); its timing is what is used, not its output.
# ---------------------------------------------------------------------------
fig7a-ocean-mode0)
  need_root; need_down
  run env OCEAN="$BENCH/Splash-4/Splash-4/ocean-contiguous_partitions/OCEAN-CONT" \
          OCEAN_N=8194 OCEAN_P=8 CACHES="6456" MODES=0 REPS=1 \
          ZERO_ON_INIT=true CACHE_POLICY=clock CLOCK_HW_YOUNG=0 \
          BG_DRAIN_BATCH=4096 BG_DRAIN_MS=1 \
          OUT="$DATA/ocean_wss_s4" \
      "$DRIVERS/cxdvirt/sweep_ocean_wss.sh"
  ;;

fig7a-ocean-cylon)
  cat <<'EOT'
### ONE VM BOOT PER POINT.  Cylon's buffer size is a launch parameter (BUFSZ_MB,
### which lands as bufsz_mb= on the qemu command line), not something a script
### can set at runtime -- run_ocean_cylon.sh READS it back off
### /proc/<qemu>/cmdline and derives the WSS from it.  So for each of
### 40578 / 12912 / 6456 MB, on the CylonLinux host kernel:
###
###     1. cd cylon-tree/CylonFEMU/build-femu
###        BUFSZ_MB=<MB> CYLON_FT_PROG=0 CYLON_EVICT_SYNC=3 CYLON_WM_HIGH=95 \
###            CYLON_WM_LOW=85 CYLON_WM_BATCH=1 CYLON_WB_SLOTS=4 ./run-cxlssd.sh 98304
###     2. sudo drivers/cylon/pin_threads.sh once the guest is up (it moves FEMU's
###        FTL thread off the vCPU cores); drivers/cylon/guest_setup_cxl.sh
###     3. run the command below
###     4. shut the VM down -- a second run in the same VM starts with a warm buffer
###
### The CYLON_* settings are the ones the published points ran with, as each
### point's .buffer records (its MODE, Watermark and First-touch lines): the
### 95%/85% watermark CXDVirt drains to, one victim per batch, four background
### write-back slots, evict_sync mode 3, and the first-touch NAND program
### deferred to eviction.  run-cxlssd.sh documents each knob.
###
### The guest runs its own OCEAN and daxmalloc, /root/OCEAN and /root/daxmalloc.so
### (GUEST_OCEAN, GUEST_DAXMALLOC), put there once with the VM up:
###     scp -P 8080 bench/Splash-4/Splash-4/ocean-contiguous_partitions/OCEAN-CONT \
###         root@localhost:/root/OCEAN
###     scp -r -P 8080 ../emulator/tools/daxmalloc root@localhost:/root/daxmalloc-src
###     guest: make -C /root/daxmalloc-src daxmalloc.so && cp /root/daxmalloc-src/daxmalloc.so /root/
###
### daxmalloc, NOT numactl --membind 1.  --membind puts OCEAN's own text on the
### emulated device where eviction can reach it: a run at 806 MB came back with
### its text segment reading as zeros, SIGSEGV with error 6 and a RIP pointing
### into a run of 00 bytes.  It is also not the comparison we want even when it
### survives -- on CXDVirt daxmalloc redirects only malloc, so the binary, stack
### and libc stay in host DRAM and only the heap is on the device.
EOT
  run env OCEAN_N=8194 OCEAN_P=8 OUT="$DATA/ocean_wss_cylon_s4" \
      "$DRIVERS/cylon/run_ocean_cylon.sh"
  ;;

# ---------------------------------------------------------------------------
# Fig. 7(b) -- Redis/YCSB-C read latency, 1 thread.
#
# Cache fixed at 4914 MB (Cylon's bufsz = ssd_size/20 = 4915 MB, matched), and
# the record count selects WSS: 1M -> 0.35x, 3M -> 1.1x, 6M -> 2.2x.
#
# ONE THREAD, deliberately: it is the conventional single-client configuration
# and the one Cylon's own evaluation uses.  redis.conf sets io-threads 1, so the
# command loop is single-threaded and device queue depth stays at 1 regardless.
#
# Latency comes from the SERVER (INFO commandstats usec_per_call and INFO
# latencystats percentiles), run phase only.  Do NOT read throughput off these
# runs against the native baseline -- the YCSB/JVM harness is not comparable
# across boots; server-side latency is.
# ---------------------------------------------------------------------------
fig7b-redis)
  need_root; need_down
  # Parameters are the ones recorded in the published runs' .meta files
  # (plots/data/redis/clockswclk_d0_c4914_*_1t_0825_*.meta), not init_device.sh's
  # defaults: CLOCK_HW_YOUNG=0 puts CLOCK on Cylon's footing (a buffer hit under
  # EPT remapping never re-arms the reference bit), and the drain ran at 1024/1ms.
  for r in 1000000 3000000 6000000; do
    run env DRAM_CACHE_MB=4914 CACHE_POLICY=clock CLOCK_HW_YOUNG=0 CXL_WR_ALLOC=0 \
            BG_DRAIN_4K=1 BG_DRAIN_BATCH=1024 BG_DRAIN_MS=1 \
        "$HERE/init_device.sh"
    run sudo -u "${SUDO_USER:-root}" env OUT="$DATA/redis" \
        "$DRIVERS/cxdvirt/run_policy_redis.sh" "$r" 1
    run "$HERE/teardown_device.sh"
  done
  ;;

fig7b-redis-cylon)
  cat <<'EOT'
### ONE VM BOOT for all three record counts, used for nothing else: the guest
### script onlines the device as node-1 system RAM, which only a restart undoes.
### On the CylonLinux host kernel:
###
###     1. cd cylon-tree/CylonFEMU/build-femu
###        CYLON_WB_OFF=0 CYLON_EVICT_SYNC=3 CYLON_WM_HIGH=95 CYLON_WM_LOW=85 \
###            CYLON_WM_BATCH=1 ./run-cxlssd.sh 98304
###     2. sudo drivers/cylon/pin_threads.sh once the guest is up, then
###        drivers/cylon/guest_setup_cxl.sh (region, devdax, warm-up)
###     3. drivers/cylon/guest_stage_redis.sh     (once per guest image)
###     4. the command below; then shut the VM down
###
### These are the settings the published points ran with (leg 3): the default
### 4915 MB buffer that CXDVirt's 4914 MB matches, write-back on, CXDVirt's
### 95%/85% watermarks with one victim per fill, and evict_sync mode 3 -- the
### evictor never pays, as with CXDVirt's background drain.  The driver refuses
### a VM launched otherwise, and copies back FEMU's write-back counters with
### each point.
EOT
  need_vm
  run env OUT="$DATA/redis_cylon" "$DRIVERS/cylon/run_redis_cylon.sh"
  ;;

# ---------------------------------------------------------------------------
# Fig. 7 dashed lines -- remote DRAM, no device.
#
# Placement-matched to the CXDVirt runs: each workload is launched exactly as
# the CXDVirt drivers launch it (node-0 CPUs, --membind 0, daxmalloc preloaded)
# with /dev/dax0.0 swapped for a /dev/shm file pre-faulted on node 1.  The heap
# sits on node 1 -- where the carve-out CXDVirt's heap lives -- in the same
# dlmalloc arena, and everything else on node 0; only the device is missing.
# Each run verifies every heap page is on node 1 afterwards.
#
# Each line is drawn from one point, so only that point is run, 3 reps each:
# Redis at 1M records (0.35x, the only WSS where neither emulator touches NAND)
# and OCEAN -n8194 -p8.  ~8 min.
#
# Run on CXDVirt's kernel (6.18.5) with the module UNLOADED
# (scripts/teardown_device.sh).  The earlier `--membind 1` arms on 6.4.6 are
# still available (sweep_native_redis.sh; ARMS="remote local") but no longer
# drawn: they also put the binary, stack and libc on node 1, and read 4.7%
# (OCEAN) and 14% (Redis) slower than the matched arm.
# ---------------------------------------------------------------------------
fig7-native)
  lsmod | grep -q '^nvmev ' && { echo "ERROR: nvmev loaded; the baseline must have no device (scripts/teardown_device.sh)." >&2; exit 1; }
  pgrep -f 'qemu-system' >/dev/null && { echo "ERROR: QEMU running; stop the VM first." >&2; exit 1; }
  for rep in 1 2 3; do
    run env OUT="$DATA/redis_native" "$DRIVERS/native/run_native_redis.sh" 1000000 heap 1
  done
  run env OUT="$DATA/ocean_native" OCEAN_N=8194 OCEAN_P=8 ARMS=heap REPS=3 \
      "$DRIVERS/native/run_native_ocean.sh"
  ;;

# ---------------------------------------------------------------------------
# Fig. 8(a) -- eviction policy at 2.65x WSS, 4.8 GiB cache.
#
# Redis/YCSB-C with 8M records -- 12.7 GiB (13,027 MiB) of heap measured by daxmalloc --
# against the 4914 MB cache: 2.65x.  (WSS_TAG=2.67, recorded in each .meta, is
# the ratio of the earlier 1M-record / 614 MB point; the published runs carry
# the same label.)  The run phase is 8M zipfian reads, 1 client
# thread.  Every policy is a separate module load, because cache_policy and
# lifo_stage are insmod-only.  Each point is ~32 min: a 20-min load at 64
# threads, then an 11-min run.
#
# ALL THREE POLICIES SHARE ONE DRAIN SETTING, and that is the point of the
# panel: enabling the drain for one arm and not another would measure the drain.
# It is the FAST drain (1024 victims per 1 ms), which never binds, with
# hw_young=1 and lifo_stage=1024 -- the configuration of the fast arm of
# Fig. 8(b)-(d).
#
# Isolating LIFO's own behaviour would call for the drain OFF, because the drain
# evicts from the policy's victim end, which for LIFO is the recent working set.
# That is not this figure's configuration.
# ---------------------------------------------------------------------------
fig8a-policy)
  need_root; need_down
  for pol in fifo clock lifo; do
    run env CACHE_POLICY="$pol" DRAM_CACHE_MB=4914 BG_DRAIN_4K=1 \
            BG_DRAIN_BATCH=1024 BG_DRAIN_MS=1 CLOCK_HW_YOUNG=1 LIFO_STAGE=1024 \
        "$HERE/init_device.sh"
    run sudo -u "${SUDO_USER:-root}" env OUT="$DATA/redis/fig8_4914/policy" WSS_TAG=2.67 \
        "$DRIVERS/cxdvirt/run_policy_redis.sh" 8000000 1
    run "$HERE/teardown_device.sh"
  done
  ;;

# ---------------------------------------------------------------------------
# Fig. 8(b)-(d) -- prefetch degree, both eviction regimes, same point.
#
# CLOCK, N in {0,1,2,4,8}, under slow (64 per 10 ms) and fast (1024 per 1 ms)
# eviction: 10 cells, each a module load, in an order shuffled per rep.  The
# figure draws one run per cell, so REPS=1 (~5.5 h) reproduces it; the text's
# run-to-run variance came from replicates, REPS=3 or 5 for that.
#
#   prefetch_degree is 0444 and is not echoed into /proc/nvmev/debug, so the
#   driver reads it back from the module and stamps it into the file name and
#   a .meta -- a run cannot be attributed to the wrong cell.  Every degree
#   change is a full reload, so a fixed order would confound degree with drift.
# ---------------------------------------------------------------------------
fig8bcd-prefetch)
  need_root; need_down
  run env REPS="${REPS:-1}" POLICIES=clock DEGREES="0 1 2 4 8" REGIMES="slow fast" \
          CACHE_MB=4914 RECORDS=8000000 THREADS=1 WSS_TAG=2.67 \
          OUT="$DATA/redis/fig8_4914/prefetch" \
      "$DRIVERS/cxdvirt/sweep_prefetch.sh"
  ;;

all-cxdvirt)
  for t in fig5-mio fig6-felt-cxdvirt fig7a-ocean fig7a-ocean-mode0 fig7b-redis fig8a-policy fig8bcd-prefetch; do
    echo; echo "######## $t ########"
    if [ "$DRY" = 1 ]; then "$0" "$t" --dry-run; else "$0" "$t"; fi
  done
  ;;

*)
  echo "unknown target: $TARGET" >&2; "$0" list >&2; exit 1 ;;
esac
