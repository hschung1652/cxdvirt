#!/bin/bash
# guest_redis_cdf.sh -- ONE Fig. 7(b) point on Cylon, run INSIDE the guest.
#
#   usage: guest_redis_cdf.sh <recordcount>
#
# This is the script the published Cylon points ran (leg 3, recovered verbatim
# from the guest image; only the header and the path overrides are new).
# drivers/cylon/run_redis_cylon.sh copies it to the guest as
# /root/cylon_redis_cdf.sh, runs it once per record count and copies the results
# back; drivers/cylon/guest_stage_redis.sh puts its inputs in place.
#
# Mirror of the CXDVirt side (drivers/cxdvirt/run_policy_redis.sh): the same
# RESETSTAT / LATENCY RESET between load and run, the same dense percentile list,
# the same YCSB property files, 1 run thread.
#
# PLACEMENT.  The device is onlined as guest node 1 (system-ram, ZONE_MOVABLE) and
# the server runs under --membind 1, so its heap and stack are on the emulated
# CXL-SSD.  Its binary is copied to tmpfs and its libraries warmed into node-0
# page cache BEFORE node 1 exists, and pin_binary.so binds and mlocks the text on
# node 0 -- so, as on CXDVirt, the device holds the process's data, not its code.
# The system-ram conversion cannot be undone without a VM restart: the MIO and
# OCEAN targets, which need /dev/dax0.0 in devdax mode, cannot follow in the
# same boot.
#
# Host-side write-back counters are bracketed by `cxl read-labels mem0 -s 1`,
# which dumps FEMU's counters to CylonLogs/cxlssd_buffer.txt and RESETS them --
# so the marker before the run phase is what isolates the run-phase numbers.
N=${1:?usage: cylon_redis_cdf.sh <recordcount>}
CLI=/dev/shm/redis-cli
SRV=/dev/shm/redis-server
STAGE=${STAGE:-/root/stage}
YCSB=$STAGE/ycsb
R=${RESULTS:-/root/results}
PIN=${PIN:-/root/pin_binary.so}
CONF=${CONF:-/root/redis.conf}
PCTL="0.1 1 2 5 10 20 30 40 50 60 70 80 90 95 99 99.5 99.9 99.99"
mkdir -p $R

echo "=== binaries -> tmpfs, libraries -> node-0 page cache (node 1 still ABSENT) ==="
sync; echo 3 > /proc/sys/vm/drop_caches
cp $STAGE/redis/redis-server $STAGE/redis/redis-cli /dev/shm/
md5sum /dev/shm/redis-server | awk '{print "  srv "$1}'
for f in $(ldd $STAGE/redis/redis-server | awk '$3~/^\//{print $3}') \
         /lib64/ld-linux-x86-64.so.2 $PIN /lib/x86_64-linux-gnu/libnuma.so.1 \
         $(which numactl); do
  cat "$f" > /dev/null 2>&1
done
echo "  warmed $(ldd $STAGE/redis/redis-server | grep -c /) libs + loader"

if ! numactl -H | grep -q "^node 1"; then
  echo "=== CXL -> node 1 (system-ram), ZONE_MOVABLE ==="
  daxctl reconfigure-device --mode=system-ram --force dax0.0 >/dev/null 2>&1
  ok=0; for m in $(ls -d /sys/devices/system/memory/memory* | sort -t y -k3 -n); do
    [ "$(cat $m/state 2>/dev/null)" = offline ] || continue
    echo online_movable > $m/state 2>/dev/null && ok=$((ok+1))
  done
  echo "  movable: $ok"
else
  echo "=== node 1 already online ==="
fi
awk '/^Node 1, zone/{z=$4} /present/&&z{if($2>1000)printf "  node1 zone %-8s %.1f GiB\n",z,$2*4096/2**30; z=""}' /proc/zoneinfo

echo "########## N=$N (load threadcount=8, run 1 thread) ##########"
pkill -x redis-server 2>/dev/null; sleep 3
cxl read-labels mem0 -s 2 >/dev/null 2>&1        # flush device buffer, nothing mapped
nohup setsid env LD_PRELOAD=$PIN numactl --cpunodebind 0 --membind 1 -- \
  $SRV $CONF > $R/redis_$N.log 2>&1 < /dev/null &
sleep 6
[ "$($CLI ping 2>/dev/null)" = PONG ] || { echo "  ERROR redis down"; dmesg | tail -5; exit 1; }

$CLI config set latency-tracking yes >/dev/null
$CLI config set latency-tracking-info-percentiles "$PCTL" >/dev/null
echo "  percentiles: $($CLI config get latency-tracking-info-percentiles | tail -1 | tr -d '\r')"

cxl read-labels mem0 -s 1 >/dev/null 2>&1        # MARKER: reset host counters

echo "--- load phase"
numactl --cpunodebind 0 --membind 0 -- $YCSB/bin/ycsb.sh load redis -s \
  -P $YCSB/workloads/workloadc -P $YCSB/cxdvirt-configs/redis-load-$N.properties \
  -p threadcount=8 > $R/load_$N.out 2>&1
DB=$($CLI dbsize)
if [ "$DB" != "$((N+1))" ]; then
  echo "  LOAD INCOMPLETE dbsize=$DB expected=$((N+1))"
  grep -m1 -oE "SocketTimeoutException|JedisConnectionException" $R/load_$N.out | sed 's/^/    cause: /'
else
  echo "  load OK tput=$(awk -F', ' '/^\[OVERALL\], Throughput/{printf "%.0f",$3}' $R/load_$N.out) ops/s dbsize=$DB"
fi
echo "  used_mem=$($CLI info memory | grep -oP '(?<=^used_memory:)\d+' | tr -d '\r')"
echo "  MARKER_AFTER_LOAD_$N"; cxl read-labels mem0 -s 1 >/dev/null 2>&1

echo "--- resetting redis stats, run phase (1 thread)"
$CLI config resetstat >/dev/null
$CLI latency reset >/dev/null

numactl --cpunodebind 0 --membind 0 -- $YCSB/bin/ycsb.sh run redis -s \
  -P $YCSB/workloads/workloadc -P $YCSB/cxdvirt-configs/redis-run-$N-1t-zipfian.properties \
  -p measurementtype=raw -p measurement.raw.output_file=$R/run_${N}_1t.raw \
  > $R/run_$N.out 2>&1
echo "  run tput=$(awk -F', ' '/^\[OVERALL\], Throughput/{printf "%.0f",$3}' $R/run_$N.out) ops/s"

$CLI info commandstats > $R/commandstats_$N.txt 2>&1
$CLI info latencystats > $R/latencystats_$N.txt 2>&1
$CLI latency histogram hgetall > $R/histogram_$N.txt 2>&1
echo "  internal: $(grep cmdstat_hgetall $R/commandstats_$N.txt | tr -d '\r')"
echo "  pctl    : $(grep hgetall $R/latencystats_$N.txt | tr -d '\r')"
echo "  MARKER_AFTER_RUN_$N"; cxl read-labels mem0 -s 1 >/dev/null 2>&1
echo "########## DONE N=$N ##########"
