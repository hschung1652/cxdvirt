#!/usr/bin/env bash
# sweep_native_redis.sh -- all three WSS points of the Redis native baseline.
#   ./sweep_native_redis.sh [remote|local|both] [threads]
# Default: both arms, 1 thread (the configuration panel (a) plots).
set -u
ARMS=${1:-both}; THREADS=${2:-1}
[ "$ARMS" = both ] && ARMS="remote local"
cd /mnt/nvme/cxdvirt/cxdvirt/reproduction || exit 1
for arm in $ARMS; do
  for r in 1000000 3000000 6000000; do
    echo; echo "############ arm=$arm records=$r threads=$THREADS ############"
    ./run_native_redis.sh $r $arm $THREADS || echo "!!!! FAILED arm=$arm records=$r"
  done
done
echo; echo "############ SWEEP DONE ############"
