#!/usr/bin/env bash
# guest_stage_redis.sh -- put what Fig. 7(b)'s Cylon side needs into the guest.
# Once per guest image, with the VM up; run on the HOST, no root needed.
#
#   /root/stage/redis/      redis-server, redis-cli    (this repo's bench/redis build)
#   /root/stage/ycsb/       YCSB 0.17.0's Redis binding: bin/ lib/ workloads/ and
#                           cxdvirt-configs/ (the property files both sides use)
#   /root/redis.conf        drivers/cylon/guest-redis.conf
#   /root/pin_binary.so     built in the guest from Cylon's tools/pin_binary
#   /root/cylon_redis_cdf.sh  drivers/cylon/guest_redis_cdf.sh
#
# The guest also needs a JRE, numactl, and ndctl's cxl/daxctl -- the guest image
# Cylon's own setup produces has all three.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
SSH_PORT=${SSH_PORT:-8080}
SSH=(ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@localhost)
SCP=(scp -q -P "$SSH_PORT" -o StrictHostKeyChecking=no)
R=$ROOT/bench/redis/src
Y=$ROOT/bench/ycsb
PIN=$ROOT/cylon-tree/tools/pin_binary

for f in "$R/redis-server" "$R/redis-cli" "$Y/bin/ycsb.sh" "$PIN/pin_binary.c"; do
  [ -e "$f" ] || { echo "ERROR: $f missing -- scripts/fetch.sh --cylon && scripts/build.sh all" >&2; exit 1; }
done
"${SSH[@]}" true || { echo "ERROR: guest not reachable on :$SSH_PORT" >&2; exit 1; }

echo "--- redis + ycsb -> /root/stage"
"${SSH[@]}" 'mkdir -p /root/stage/redis /root/stage/ycsb /root/pin_binary /root/results'
"${SCP[@]}" "$R/redis-server" "$R/redis-cli" root@localhost:/root/stage/redis/
"${SCP[@]}" -r "$Y/bin" "$Y/lib" "$Y/workloads" "$Y/cxdvirt-configs" root@localhost:/root/stage/ycsb/
"${SCP[@]}" "$HERE/guest-redis.conf" root@localhost:/root/redis.conf
"${SCP[@]}" "$HERE/guest_redis_cdf.sh" root@localhost:/root/cylon_redis_cdf.sh

echo "--- pin_binary.so (built in the guest, against its libnuma)"
"${SCP[@]}" "$PIN/pin_binary.c" "$PIN/build.sh" root@localhost:/root/pin_binary/
"${SSH[@]}" 'cd /root/pin_binary && sh build.sh >/dev/null && cp pin_binary.so /root/ && ls -la /root/pin_binary.so'

"${SSH[@]}" 'chmod +x /root/cylon_redis_cdf.sh /root/stage/redis/* /root/stage/ycsb/bin/ycsb.sh
             /root/stage/redis/redis-server --version; command -v java numactl cxl daxctl >/dev/null ||
             echo "WARNING: the guest is missing one of java numactl cxl daxctl"'
echo "guest staged"
