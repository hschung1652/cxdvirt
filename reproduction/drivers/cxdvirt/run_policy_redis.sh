#!/usr/bin/env bash
# run_policy_redis.sh — Redis/YCSB-C point for one DRAM-cache eviction policy.
#
#   ./run_policy_redis.sh <records> [threads]
#
# Derived from ../run_cal_wss.sh.  Differences:
#   - tags output with the LIVE cache_policy module parameter, so runs cannot
#     be mislabelled after a module reload;
#   - defaults to 8 client threads, matching Cylon Figure 11;
#   - snapshots the FIFO/LIFO/CLOCK victim-selection counters around each
#     phase, not just the eviction split.
set -u
RECORDS=${1:?usage: run_policy_redis.sh <records> [threads]}
THREADS=${2:-8}

POLICY=$(cat /sys/module/nvmev/parameters/cache_policy 2>/dev/null || echo unknown)
PGMODE=$(cat /sys/module/nvmev/parameters/page_granularity 2>/dev/null || echo '?')
CACHEMB=$(cat /sys/module/nvmev/parameters/dram_cache_mb 2>/dev/null || echo '?')
# Read the LIVE prefetch knobs for the same reason the policy is read live: they
# are insmod-only, so the only trustworthy source is the module itself.  Every
# run before this was tagged with policy alone, which left the prefetch degree
# -- the independent variable of the sweep -- recorded nowhere at all.
PFMODE=$(cat /sys/module/nvmev/parameters/prefetch_mode 2>/dev/null || echo '?')
PFDEG=$(cat /sys/module/nvmev/parameters/prefetch_degree 2>/dev/null || echo '?')
PFRAND=$(cat /sys/module/nvmev/parameters/prefetch_random 2>/dev/null || echo 0)
# Degree is meaningless unless mode==1 (MISS_NEXTN); normalise so d0 means
# "no prefetch" however it was reached.
[ "$PFMODE" = "1" ] || { PFDEG=0; PFRAND=0; }
# The random control arm has the SAME degree as its next-N twin, so without a
# distinct tag the two are indistinguishable on disk -- the same class of
# mislabelling that made the original degree sweep unattributable.
ARM=$([ "$PFRAND" = "1" ] && echo "rand" || echo "")
# CLOCK second-chance source, read live (insmod-only).  0 = software
# reference bit ONLY, which is set on a fault and never by a hit, so CLOCK
# degenerates to FIFO-plus-one-grace -- the same semantics as Cylon, where
# DER means a buffer hit never reaches the device and its ref bit is set
# once at insert.  1 = tiered (software bit + hardware PTE Accessed bit
# while the hand is inside its first pass over the resident set).
#
# TAGGED IN THE FILENAME, not just the .meta.  The two configurations are
# otherwise identical on disk down to the cache size, and this file already
# records what that costs: "three degree-0 runs spanning the A-bit fix were
# averaged into one curve".
HWY=$(cat /sys/module/nvmev/parameters/cxl_clock_hw_young 2>/dev/null || echo '?')
SWCLK=$([ "$HWY" = "0" ] && echo "swclk" || echo "")
STAMP=$(date +%m%d_%H%M%S)
OUT=${OUT:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/results/redis/policy}
LOG=$OUT/${POLICY}${SWCLK}_d${PFDEG}${ARM}_c${CACHEMB}_${RECORDS}_${THREADS}t_$STAMP
R=/mnt/nvme/cxdvirt/cxdvirt/reproduction/bench/redis/src
Y=/mnt/nvme/cxdvirt/cxdvirt/reproduction/bench/ycsb
# daxmalloc places the heap on the device by interposing malloc(); a Redis built
# with its default jemalloc allocates through its own arenas and bypasses it, so
# the dataset would silently stay in host DRAM.  Build with MALLOC=libc.
case "$($R/redis-server --version)" in
  *malloc=libc*) ;;
  *) echo "FATAL: $R/redis-server is not built with MALLOC=libc ($($R/redis-server --version | grep -o 'malloc=[^ ]*'))" >&2
     exit 1 ;;
esac
DAXSO=/mnt/nvme/cxdvirt/cxdvirt/emulator/tools/daxmalloc/daxmalloc.so
CONF=$OUT/${POLICY}_${RECORDS}.conf
mkdir -p "$OUT"

# ---- counter snapshot: emit "key value" lines -----------------------------
snap() {
  awk '
    /^cache_hits:/               {print "cache_hits",     $2}
    /^cache_misses:/             {print "cache_misses",   $2}
    /^sync_evicts_4k:/           {print "sync_evicts",    $2}
    /^async_evictions:/          {print "async_evicts",   $2}
    /^write_protect_faults:/     {print "wp_faults",      $2}
    /^evict_sync_fallback_4k:/   {print "sync_fallback",  $2}
    /^nand_write_lat_applied:/   {print "nand_wr",        $2}
    /^sync_wb_applied:/          {print "sync_wb",        $2}
    /^sync_wb_applied_ns:/       {print "sync_wb_ns",     $2}
    /^victim_probes_4k:/         {print "victim_probes",  $2}
    /^clock_victims_4k:/         {print "clock_victims",  $2}
    /^fifo_victims_4k:/          {print "fifo_victims",   $2}
    /^lifo_victims_4k:/          {print "lifo_victims",   $2}
    /^zap_failures_4k:/          {print "zap_failures",   $2}
    /^async_loads:/              {print "async_loads",    $2}
    /^async_prefetches:/         {print "async_prefetch", $2}
    /^nand_lat_applied:/         {print "nand_rd",        $2}
    /^pf4k_pushed:/              {print "pf_pushed",      $2}
    /^pf4k_dropped_full:/        {print "pf_dropped",     $2}
    /^pf4k_paired_evicts:/       {print "pf_paired_ev",   $2}
    /^pf4k_no_victim:/           {print "pf_no_victim",   $2}
    /^pf4k_admit_fail:/          {print "pf_admit_fail",  $2}
    /^pf4k_useful:/              {print "pf_useful",      $2}
    /^pf4k_wasted:/              {print "pf_wasted",      $2}
    /^pf4k_late:/                {print "pf_late",        $2}
  ' /proc/nvmev/debug
}

echo "=== CXDVirt Redis policy run ==="
echo "  policy=$POLICY  granularity=$PGMODE  dram_cache_mb=$CACHEMB"
echo "  prefetch_mode=$PFMODE  prefetch_degree=$PFDEG  random_arm=$PFRAND"
echo "  clock_hw_young=$HWY $([ "$HWY" = 0 ] && echo '(software bit only -- Cylon-matched)' || echo '(tiered: sw + hw A-bit)')"
echo "  records=$RECORDS  run-threads=$THREADS"
echo "  out=$LOG.*"
[ "$PGMODE" = 4k ] || { echo "FATAL: need page_granularity=4k"; exit 1; }

# Freeze the full live module configuration next to the data.  Without this a
# .raw file cannot be attributed to a configuration after the fact.
mkdir -p "$OUT"
{
  echo "# live /sys/module/nvmev/parameters at $(date -Is)"
  for p in /sys/module/nvmev/parameters/*; do
    printf '%s %s\n' "$(basename "$p")" "$(cat "$p" 2>/dev/null)"
  done
  # Module source fingerprint.  Runs that differ only in build are otherwise
  # indistinguishable on disk, and the analysis silently POOLED them --
  # three degree-0 runs spanning the A-bit fix were averaged into one curve.
  echo "srcversion $(cat /sys/module/nvmev/srcversion 2>/dev/null)"
  echo "records $RECORDS"
  echo "threads $THREADS"
  echo "rep ${REP:-na}"
  echo "wss_tag ${WSS_TAG:-na}"
  echo "kbuild_base_ssd $(grep -m1 '^ccflags.*CXL_SUPPORT' \
       /mnt/nvme/cxdvirt/cxdvirt/emulator/nvmevirt/Kbuild \
       2>/dev/null | grep -oE 'BASE_SSD=[A-Z_0-9]+')"
} > "$LOG.meta"

cat > "$CONF" <<CONFEOF
port 6379
bind 127.0.0.1
protected-mode no
daemonize no
save ""
appendonly no
maxmemory 0
io-threads 1
latency-tracking yes
logfile ""
CONFEOF

pkill -f 'redis-server .*6379' 2>/dev/null; sleep 2

echo "--- starting redis-server on /dev/dax0.0"
numactl --cpunodebind 0 --membind 0 -- \
  env LD_PRELOAD=$DAXSO DAXMALLOC_PATH=/dev/dax0.0 DAXMALLOC_REQUIRE=1 \
      DAXMALLOC_VERBOSE=1 DAXMALLOC_STATS=1 \
  $R/redis-server "$CONF" > $LOG.server.log 2>&1 &
for i in $(seq 1 60); do $R/redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 1; done
$R/redis-cli ping | grep -q PONG || { echo "FATAL: redis did not come up"; cat $LOG.server.log; exit 1; }
grep -q "daxmalloc: mapped" $LOG.server.log && echo "  daxmalloc banner OK" || { echo "FATAL: no daxmalloc banner"; exit 1; }
grep -o "withholding .* MiB of DRAM-cache region" $LOG.server.log | head -1

# Dense percentile list for INFO latencystats -- Redis defaults to only
# p50/p99/p99.9, which is far too coarse to read as a server-side CDF.
$R/redis-cli config set latency-tracking-info-percentiles \
  "0.1 1 2 5 10 20 30 40 50 60 70 80 90 95 99 99.5 99.9 99.99" >/dev/null
echo "  percentiles: $($R/redis-cli config get latency-tracking-info-percentiles | tail -1)"

# Setup phase runs prefetch-free at EVERY degree.  The load is a
# write-allocate storm; leaving prefetch armed made each degree enter the
# measured phase from a different cache state (0 speculative fills at degree
# 0 vs 10.3M at degree 4 for the identical load) -- a confound in the
# independent variable -- and its extra tail latency killed Jedis loader
# threads on their un-raisable 2000 ms default timeout.
echo prefetch_off > /proc/nvmev/debug 2>/dev/null && echo "  prefetch DISARMED for load"

snap > $LOG.ctr0
echo "--- load phase ($RECORDS records, 64 threads)  $(date +%T)"
python2 $Y/bin/ycsb load redis -s -P $Y/workloads/workloadc \
   -P $Y/cxdvirt-configs/redis-load-$RECORDS.properties -p redis.timeout=1800000000 > $LOG.load.out 2>&1
snap > $LOG.ctr1
echo "    load done $(date +%T)"
grep -E "peak_live" $LOG.server.log | tail -1

# HARD GUARD: the load must have inserted every record.
#
# site.ycsb.db.RedisClient in this build knows only redis.{host,port,cluster,
# password} -- there is NO redis.timeout property, so the -p flag above is
# silently ignored and Jedis uses its 2000 ms default socket timeout.  A load
# thread that stalls past that dies with SocketTimeoutException, YCSB exits
# without an [INSERT] summary, and the run phase then reads a half-populated
# keyspace: a smaller effective WSS that looks like a spectacular result.
# That is exactly how a degree-2 cell once reported "half the misses and a
# 34% better median" -- it had loaded 893 MiB instead of 1634 MiB.
# Fail loudly here rather than emit data that silently means something else.
INS_OK=$(grep -m1 '^\[INSERT\], Return=OK,' $LOG.load.out | awk -F', *' '{print $3}')
if [ "${INS_OK:-0}" != "$RECORDS" ]; then
  echo "FATAL: load inserted ${INS_OK:-0} of $RECORDS records" >&2
  grep -c 'SocketTimeoutException' $LOG.load.out \
    | xargs -I{} echo "       ({} socket timeouts in $LOG.load.out)" >&2
  echo "       run is UNUSABLE; not proceeding to the run phase." >&2
  $R/redis-cli shutdown nosave 2>/dev/null; sleep 2
  mv "$LOG.load.out" "$LOG.load.out.FAILED" 2>/dev/null
  exit 1
fi
echo "    load verified: $INS_OK/$RECORDS records"

# Arm prefetch only now, so ctr1->ctr2 measures prefetch against a cache
# state identical to every other degree's.
echo prefetch_on > /proc/nvmev/debug 2>/dev/null && echo "  prefetch ARMED for run phase"
snap > $LOG.ctr1b   # post-arm baseline; ctr1 stays the load-phase boundary

echo "--- run phase ($THREADS threads, zipfian)  $(date +%T)"
$R/redis-cli config resetstat >/dev/null
$R/redis-cli latency reset  >/dev/null
python2 $Y/bin/ycsb run redis -s -P $Y/workloads/workloadc \
   -P $Y/cxdvirt-configs/redis-run-$RECORDS-${THREADS}t-zipfian.properties \
   -p redis.timeout=1800000000 -p measurementtype=raw -p measurement.raw.output_file=$LOG.raw > $LOG.run.out 2>&1
snap > $LOG.ctr2
echo "    run done $(date +%T)"

# Same guard on the read side: any ERROR return means keys were missing or
# the client broke, so the latency sample is not of the intended workload.
RD_ERR=$(grep -m1 '^\[READ\], Return=ERROR,' $LOG.run.out | awk -F', *' '{print $3}')
if [ -n "$RD_ERR" ] && [ "$RD_ERR" -gt 0 ]; then
  RD_OK=$(grep -m1 '^\[READ\], Return=OK,' $LOG.run.out | awk -F', *' '{print $3}')
  echo "FATAL: run phase had $RD_ERR READ errors (${RD_OK:-0} OK)" >&2
  echo "       latency sample is contaminated; marking run FAILED." >&2
  $R/redis-cli shutdown nosave 2>/dev/null; sleep 2
  for e in raw run.out; do mv "$LOG.$e" "$LOG.$e.FAILED" 2>/dev/null; done
  exit 1
fi

cp /proc/nvmev/debug $LOG.nvmev_final
# Per-miss stage timing (dispatch/wait/evict/modeled/install).  This is the
# only artifact that can decompose WHERE a latency delta between degrees comes
# from (inline write-back vs modeled NAND vs waits); it was lost at teardown
# for every run before this line existed.  ~100-200MB, gzip to ~a tenth.
gzip -c /proc/nvmev/optb4k > $LOG.optb4k.gz 2>/dev/null &
OPTB_PID=$!
$R/redis-cli info commandstats > $LOG.commandstats
$R/redis-cli info latencystats > $LOG.latencystats
$R/redis-cli shutdown nosave 2>/dev/null; sleep 2
# The optb4k dump must finish before the sweep tears the module down
# (removing /proc/nvmev truncates the read mid-stream).
wait $OPTB_PID 2>/dev/null && echo "  optb4k dumped: $(du -h $LOG.optb4k.gz | cut -f1)"

python3 - "$LOG" "$POLICY" <<'PYEOF'
import sys
L, pol = sys.argv[1], sys.argv[2]
rd = lambda p: {k: int(v) for k, v in (l.split() for l in open(p))}
c1, c2 = rd(L + ".ctr1"), rd(L + ".ctr2")
d = {k: c2.get(k, 0) - c1.get(k, 0) for k in c2}
h, m = d["cache_hits"], d["cache_misses"]
tot = h + m
print("\n=== RUN-PHASE deltas  policy=%s ===" % pol)
print(f"  cache_hits            : {h:,}")
print(f"  cache_misses          : {m:,}")
if tot:
    print(f"  HIT RATE              : {100.0*h/tot:.2f}%")
ev = d["sync_evicts"] + d["async_evicts"]
print(f"  evictions (sync/async): {ev:,}  ({d['sync_evicts']:,} / {d['async_evicts']:,})")
vic = d["fifo_victims"] + d["lifo_victims"] + d["clock_victims"]
print(f"  policy victims        : {vic:,}   [fifo {d['fifo_victims']:,} | lifo {d['lifo_victims']:,} | clock {d['clock_victims']:,}]")
if m:
    print(f"  victims / miss        : {vic/m:.3f}      <- ~1 = frozen cache; >>1 = thrash")
    print(f"  victim_probes / miss  : {d['victim_probes']/m:.1f}")
print(f"  zap_failures_4k       : {d['zap_failures']:,}")
print(f"  evict_sync_fallback   : {d['sync_fallback']:,}")
print(f"  nand_write_lat_applied: {d['nand_wr']:,}   <- dirty write-backs charged")
print(f"  sync_wb_applied:        {d.get('sync_wb',0):,}   <- of those, IN FAULT CONTEXT (blocked the app)")
print(f"  sync_wb stall:          {d.get('sync_wb_ns',0)/1e9:,.1f} s   <- real serial stall; compare to run wall-clock")
print(f"  write_protect_faults  : {d['wp_faults']:,}")
ap, al = d.get("async_prefetch", 0), d.get("async_loads", 0)
# Post paired-replacement fix: async_prefetches counts only REAL submits
# (cxl_async_load_4k_page returned 1), async_loads counts completions, so the
# two should track within the in-flight window.  A large gap, or attempts
# stuck at 0 with pf_pushed > 0, is a regression back to the budget-gate bug.
print(f"  prefetch submits      : {ap:,}")
print(f"  prefetch completions  : {al:,}   <- async_loads")
if ap:
    print(f"  submit->complete gap  : {100.0*(ap-al)/ap:.1f}%   <- ~0%; large = regression")
pp = d.get("pf_pushed", 0)
if pp or ap:
    print(f"  pf triggers pushed    : {pp:,}  (dropped {d.get('pf_dropped',0):,})")
    print(f"  pf paired evictions   : {d.get('pf_paired_ev',0):,}"
          f"   no-victim {d.get('pf_no_victim',0):,}"
          f"   admit-fail {d.get('pf_admit_fail',0):,}")
# Measured prefetch quality (pf4k_useful/wasted/late).  These are the only
# numbers that isolate prediction from this prefetcher's side effects; note
# useful/wasted come from the A-bit the CLOCK finder samples, so they are
# CLOCK-only, while `late` is policy-independent.
pu, pw, pl = d.get("pf_useful", 0), d.get("pf_wasted", 0), d.get("pf_late", 0)
if pu + pw:
    print(f"  PREFETCH ACCURACY     : {100.0*pu/(pu+pw):.2f}%  "
          f"({pu:,} used / {pu+pw:,} retired)")
    print(f"  of those used, LATE   : {100.0*pl/pu if pu else 0:.1f}%  "
          f"({pl:,} demand faults waited on an in-flight prefetch)")
if m:
    print(f"  completions / miss    : {al/m:.2f}      <- TRUE effective degree")
    print(f"  nand_wr / miss        : {d['nand_wr']/m:.3f}  <- write-back debt per miss"
          f" (now includes prefetch's paired write-backs)")
PYEOF
echo "files: $LOG.*"
