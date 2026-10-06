#!/usr/bin/env bash
# run_native_redis.sh -- Redis/YCSB-C on plain DRAM, no device.  The panel-(b)
# analogue of the Ocean baseline that draws the dashed line in (a).
#
#   ./run_native_redis.sh <records> <remote|local|heap> [threads]
#
# Identical to run_policy_redis.sh in EVERY respect that touches the
# measurement -- same redis-server binary, same redis.conf, same dense
# percentile list, same YCSB load/run properties, same CONFIG RESETSTAT +
# LATENCY RESET between phases, same load-completeness and READ-error guards --
# with exactly two things removed:
#
#   1. /dev/dax0.0.  The remote and local arms run plain glibc malloc; the heap
#      arm preloads daxmalloc over a node-1 file.  No allocation reaches an
#      emulated device.
#   2. every /proc/nvmev counter snapshot.  The module is not loaded.
#
# ARM SELECTION mirrors the Ocean baseline exactly:
#   remote  numactl --cpunodebind 0 --membind 1   <- the comparable baseline.
#           Node-0 CPUs, node-1 memory: the same NUMA distance the emulators
#           pay, because the dax region the CXDVirt heap lives in is carved
#           from node 1.  A local-DRAM number would charge the emulators 61%
#           of a distance they did not cause.
#   local   numactl --cpunodebind 0 --membind 0   <- context only, matching
#           NATIVE_P8_LOCAL.
#   heap    numactl --cpunodebind 0 --membind 0 + daxmalloc over a node-1 file
#           <- placement-matched to CXDVirt.  Launched exactly as
#           run_policy_redis.sh launches the CXDVirt server, with /dev/dax0.0
#           swapped for a /dev/shm file whose pages are pre-faulted on node 1:
#           the heap sits on node 1 in the same dlmalloc arena, everything else
#           on node 0, and only the device is missing.  Run it on the kernel
#           the CXDVirt runs used (6.18.5).
#
# KNOWN ASYMMETRY of the remote arm, inherited from the Ocean baseline and
# stated there too: --membind 1 binds EVERY allocation, while daxmalloc
# redirects only the heap (binary, stack and libc stay local under CXDVirt), and
# the heap is glibc malloc rather than daxmalloc's dlmalloc.  The heap arm
# removes both.
#
# KERNEL.  Run on 6.18.5, the kernel of the CXDVirt runs, as the fig7-native
# target does; each output name records the kernel (native_<arm>_k<version>_...).
set -u
RECORDS=${1:?usage: run_native_redis.sh <records> <remote|local|heap> [threads]}
ARM=${2:?usage: run_native_redis.sh <records> <remote|local|heap> [threads]}
THREADS=${3:-1}

case "$ARM" in
  remote) MEMNODE=1 ;;
  local)  MEMNODE=0 ;;
  heap)   MEMNODE=0 ;;   # the heap alone goes to node 1, through daxmalloc
  *) echo "FATAL: arm must be 'remote', 'local' or 'heap'" >&2; exit 1 ;;
esac

# Refuse to run against a live emulator: if nvmev is loaded this is not a
# native baseline, and if a QEMU guest is up the machine is not quiet.  Four
# Ocean native points were lost to exactly the second case.
lsmod | grep -q '^nvmev' && { echo "FATAL: nvmev.ko is loaded; this is not a native run" >&2; exit 1; }
pgrep -f 'qemu-system' >/dev/null && { echo "FATAL: a QEMU guest is running; machine is not quiet" >&2; exit 1; }

STAMP=$(date +%m%d_%H%M%S)
OUT=${OUT:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/results/redis_native}
LOG=$OUT/native_${ARM}_k$(uname -r | cut -d- -f1)_${RECORDS}_${THREADS}t_$STAMP
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
CONF=$OUT/native_${RECORDS}.conf
mkdir -p "$OUT"
# heap arm only.  The same daxmalloc.so the CXDVirt runs preload; the arena is
# 2 GiB per million records plus 2 GiB (the 6M heap is 10.4 GB).
DAXSO=/mnt/nvme/cxdvirt/cxdvirt/emulator/tools/daxmalloc/daxmalloc.so
HEAPFILE=${HEAPFILE:-/dev/shm/cxdvirt_native_heap}
HEAP_GB=${HEAP_GB:-$(( RECORDS / 1000000 * 2 + 2 ))}

echo "=== Redis native DRAM baseline ==="
echo "  arm=$ARM (membind $MEMNODE)  records=$RECORDS  run-threads=$THREADS"
echo "  kernel=$(uname -r)   out=$LOG.*"

{
  echo "# native Redis baseline, no device, at $(date -Is)"
  echo "kernel $(uname -r)"
  echo "arm $ARM"
  echo "cpunodebind 0"
  echo "membind $MEMNODE"
  echo "records $RECORDS"
  echo "threads $THREADS"
  if [ "$ARM" = heap ]; then
    echo "daxmalloc yes"
    echo "heap_file $HEAPFILE"
    echo "heap_node 1"
    echo "heap_gb $HEAP_GB"
  else
    echo "daxmalloc no"
  fi
  echo "nvmev_loaded no"
  echo "redis $($R/redis-server --version)"
  echo "loadavg $(cut -d' ' -f1-3 /proc/loadavg)"
  echo "node0_free_mb $(numactl -H | awk '/^node 0 free/{print $4}')"
  echo "node1_free_mb $(numactl -H | awk '/^node 1 free/{print $4}')"
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

if [ "$ARM" = heap ]; then
  # Pre-fault the arena on node 1.  A tmpfs page stays on the node it was
  # allocated on, so the server's own --membind 0 cannot pull it local -- the
  # split CXDVirt gets from a node-1 carve-out behind a node-0 process.
  rm -f "$HEAPFILE"
  trap 'rm -f "$HEAPFILE"' EXIT
  numactl --membind 1 -- fallocate -l ${HEAP_GB}G "$HEAPFILE" \
    || { echo "FATAL: could not pre-fault $HEAPFILE on node 1" >&2; exit 1; }
  echo "--- starting redis-server, heap on node-1 DRAM via daxmalloc ($HEAPFILE, $HEAP_GB GiB)"
  numactl --cpunodebind 0 --membind 0 -- \
    env LD_PRELOAD=$DAXSO DAXMALLOC_PATH=$HEAPFILE DAXMALLOC_REQUIRE=1 \
        DAXMALLOC_VERBOSE=1 DAXMALLOC_STATS=1 \
    $R/redis-server "$CONF" > $LOG.server.log 2>&1 &
else
  echo "--- starting redis-server on node-$MEMNODE DRAM"
  numactl --cpunodebind 0 --membind $MEMNODE -- \
    $R/redis-server "$CONF" > $LOG.server.log 2>&1 &
fi
for i in $(seq 1 60); do $R/redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 1; done
$R/redis-cli ping | grep -q PONG || { echo "FATAL: redis did not come up"; cat $LOG.server.log; exit 1; }
# Prove the binding took: every resident page of the server must be on MEMNODE.
SRVPID=$($R/redis-cli info server | awk -F: '/^process_id/{print $2}' | tr -d '\r')
echo "  server pid $SRVPID; numa_maps bind check:"
grep -c "bind:$MEMNODE" /proc/$SRVPID/numa_maps 2>/dev/null | xargs -I{} echo "    {} mappings bound to node $MEMNODE"

$R/redis-cli config set latency-tracking-info-percentiles \
  "0.1 1 2 5 10 20 30 40 50 60 70 80 90 95 99 99.5 99.9 99.99" >/dev/null
echo "  percentiles: $($R/redis-cli config get latency-tracking-info-percentiles | tail -1)"

echo "--- load phase ($RECORDS records, 64 threads)  $(date +%T)"
python2 $Y/bin/ycsb load redis -s -P $Y/workloads/workloadc \
   -P $Y/cxdvirt-configs/redis-load-$RECORDS.properties -p redis.timeout=1800000000 > $LOG.load.out 2>&1
echo "    load done $(date +%T)"

INS_OK=$(grep -m1 '^\[INSERT\], Return=OK,' $LOG.load.out | awk -F', *' '{print $3}')
if [ "${INS_OK:-0}" != "$RECORDS" ]; then
  echo "FATAL: load inserted ${INS_OK:-0} of $RECORDS records" >&2
  grep -c 'SocketTimeoutException' $LOG.load.out \
    | xargs -I{} echo "       ({} socket timeouts in $LOG.load.out)" >&2
  $R/redis-cli shutdown nosave 2>/dev/null; sleep 2
  mv "$LOG.load.out" "$LOG.load.out.FAILED" 2>/dev/null
  exit 1
fi
echo "    load verified: $INS_OK/$RECORDS records"
# Resident-set size is the native analogue of daxmalloc's peak_live, and the
# only way to confirm this baseline holds the SAME working set as the
# emulated runs (1.70 / 5.15 / 10.43 GB heap at 1M / 3M / 6M records).
echo "  used_memory: $($R/redis-cli info memory | awk -F: '/^used_memory:/{print $2}' | tr -d '\r') B"
echo "  server RSS : $(awk '/^VmRSS/{print $2*1024}' /proc/$SRVPID/status) B"
{ echo "used_memory $($R/redis-cli info memory | awk -F: '/^used_memory:/{print $2}' | tr -d '\r')"
  echo "vmrss_bytes $(awk '/^VmRSS/{print $2*1024}' /proc/$SRVPID/status)"
  echo "numa_maps_n$MEMNODE $(grep -c "bind:$MEMNODE" /proc/$SRVPID/numa_maps 2>/dev/null)"
} >> "$LOG.meta"

echo "--- run phase ($THREADS threads, zipfian)  $(date +%T)"
$R/redis-cli config resetstat >/dev/null
$R/redis-cli latency reset  >/dev/null
python2 $Y/bin/ycsb run redis -s -P $Y/workloads/workloadc \
   -P $Y/cxdvirt-configs/redis-run-$RECORDS-${THREADS}t-zipfian.properties \
   -p redis.timeout=1800000000 -p measurementtype=raw -p measurement.raw.output_file=$LOG.raw > $LOG.run.out 2>&1
echo "    run done $(date +%T)"

RD_ERR=$(grep -m1 '^\[READ\], Return=ERROR,' $LOG.run.out | awk -F', *' '{print $3}')
if [ -n "$RD_ERR" ] && [ "$RD_ERR" -gt 0 ]; then
  RD_OK=$(grep -m1 '^\[READ\], Return=OK,' $LOG.run.out | awk -F', *' '{print $3}')
  echo "FATAL: run phase had $RD_ERR READ errors (${RD_OK:-0} OK)" >&2
  $R/redis-cli shutdown nosave 2>/dev/null; sleep 2
  for e in raw run.out; do mv "$LOG.$e" "$LOG.$e.FAILED" 2>/dev/null; done
  exit 1
fi

if [ "$ARM" = heap ]; then
  # Checked after the run phase, i.e. over the pages the measurement touched:
  # every heap page must still be on node 1, or this is not the baseline it
  # claims to be.
  HL=$(grep -F "$HEAPFILE" /proc/$SRVPID/numa_maps)
  H0=$(echo "$HL" | grep -oP '\bN0=\K[0-9]+' || echo 0)
  H1=$(echo "$HL" | grep -oP '\bN1=\K[0-9]+' || echo 0)
  echo "  heap pages after run: node0 $H0  node1 $H1"
  { echo "heap_pages_n0 $H0"; echo "heap_pages_n1 $H1"; } >> "$LOG.meta"
  if [ "$H0" != 0 ] || [ "$H1" = 0 ]; then
    echo "FATAL: heap not on node 1 (N0=$H0 N1=$H1)" >&2
    $R/redis-cli shutdown nosave 2>/dev/null; sleep 2
    for e in raw run.out; do mv "$LOG.$e" "$LOG.$e.FAILED" 2>/dev/null; done
    exit 1
  fi
fi

$R/redis-cli info commandstats > $LOG.commandstats
$R/redis-cli info latencystats > $LOG.latencystats
$R/redis-cli shutdown nosave 2>/dev/null; sleep 2

echo
echo "=== RESULT  arm=$ARM  records=$RECORDS  threads=$THREADS ==="
grep hgetall $LOG.commandstats
grep hgetall $LOG.latencystats
grep -E '^\[OVERALL\]' $LOG.run.out
echo "  artifacts: $LOG.*"
