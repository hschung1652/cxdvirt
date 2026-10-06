#!/usr/bin/env bash
# run_smoke.sh — the full daxmalloc validation battery.
#
#   bash test/run_smoke.sh              tmpfs backing (no kernel module needed)
#   DAX=/dev/dax0.0 bash test/run_smoke.sh   against a live CXDVirt device
#
# Phase A proves the shim is a faithful malloc replacement using an ordinary
# file as the backing store, so failures there are shim bugs, not module bugs.
# Phase B repeats the core of it against the real device.
set -u

HERE=$(cd "$(dirname "$0")/.." && pwd)
SO="$HERE/daxmalloc.so"
PIN=${PIN:-}                       # optional path to pin_binary.so
DAX=${DAX:-}                       # set to /dev/dax0.0 for phase B
BACK=${BACK:-/dev/shm/daxsmoke}
SIZE=${SIZE:-$((768 << 20))}
SMALL=${SMALL:-/dev/shm/daxsmall}

pass=0; fail=0
ok()   { pass=$((pass+1)); printf '  ok   %s\n' "$1"; }
bad()  { fail=$((fail+1)); printf '  FAIL %s\n' "$1"; }
run()  { # run <name> <expected-exit> <cmd...>
	local name=$1 want=$2; shift 2
	"$@" >/dev/null 2>&1; local got=$?
	[ "$got" = "$want" ] && ok "$name" || bad "$name (exit $got, wanted $want)"
}

[ -x "$SO" ] || { echo "build first: make -C $HERE"; exit 1; }

if [ -n "$DAX" ]; then
	TARGET=$DAX; TSIZE=""       # size comes from sysfs on a real device
	echo "=== phase B: device backing $DAX ==="
else
	TARGET=$BACK; TSIZE=$SIZE
	# Pre-fill with 0xAA so the calloc test can prove zeroing over dirty
	# memory rather than over a conveniently blank file.
	dd if=/dev/zero bs=1M count=$((SIZE >> 20)) 2>/dev/null | tr '\0' '\252' > "$BACK"
	dd if=/dev/zero bs=1M count=64 of="$SMALL" 2>/dev/null
	echo "=== phase A: tmpfs backing $BACK ($((SIZE >> 20)) MiB, prefilled 0xAA) ==="
fi

E=(env "DAXMALLOC_PATH=$TARGET" "DAXMALLOC_REQUIRE=1" "LD_PRELOAD=$SO")
[ -n "$TSIZE" ] && E+=("DAXMALLOC_SIZE=$TSIZE")

echo "-- conformance batteries --"
"${E[@]}" "$HERE/test/conformance" | tail -1
"${E[@]}" "$HERE/test/conformance" >/dev/null 2>&1 && ok "C conformance" || bad "C conformance"
"${E[@]}" "$HERE/test/conformance_cpp" >/dev/null 2>&1 && ok "C++ conformance" || bad "C++ conformance"
# Re-run leaves stale dlmalloc metadata at offset 0; init must rebuild over it.
"${E[@]}" "$HERE/test/conformance" >/dev/null 2>&1 && ok "re-run on dirty arena" || bad "re-run on dirty arena"

echo "-- fork policy --"
"${E[@]}" "$HERE/test/fork_probe" >/dev/null 2>&1 && ok "fork aborts child (default)" || bad "fork aborts child (default)"
"${E[@]}" DAXMALLOC_FORK=warn "$HERE/test/fork_probe" >/dev/null 2>&1 && ok "fork survives (warn mode)" || bad "fork survives (warn mode)"

echo "-- exhaustion --"
if [ -z "$DAX" ]; then
	env DAXMALLOC_PATH="$SMALL" DAXMALLOC_SIZE=$((64 << 20)) DAXMALLOC_REQUIRE=1 \
	    LD_PRELOAD="$SO" "$HERE/test/exhaust" >/dev/null 2>&1 \
	    && ok "NULL+ENOMEM at capacity, coalescing after free" \
	    || bad "NULL+ENOMEM at capacity, coalescing after free"
else
	echo "  (skipped: would fill the device)"
fi

echo "-- stock binaries --"
run "ls -laR"          0 "${E[@]}" /bin/ls -laR /usr/share/doc
run "sort"             0 "${E[@]}" /usr/bin/sort /etc/services
"${E[@]}" python3 -c "import json,random; d=[{'k':random.random()} for _ in range(10**6)]; assert len(json.dumps(d))>10**6" >/dev/null 2>&1 \
	&& ok "python3 (obmalloc atop the shim)" || bad "python3"

echo "-- policy guards --"
# A forking program's CHILD must die (the parent is meant to survive, so its
# own exit status proves nothing -- check the child's termination signal).
"${E[@]}" python3 -c "import os,sys
pid=os.fork()
if pid==0: os._exit(0)
_,st=os.waitpid(pid,0)
sys.exit(0 if os.WIFSIGNALED(st) and os.WTERMSIG(st)==6 else 1)" >/dev/null 2>&1 \
	&& ok "forked child is killed" || bad "forked child is killed"

# Missing device: abort under REQUIRE, transparent fallback without it.
( env DAXMALLOC_PATH=/nonexistent DAXMALLOC_REQUIRE=1 LD_PRELOAD="$SO" /bin/true ) >/dev/null 2>&1 \
	&& bad "REQUIRE=1 should abort on a bad path" || ok "REQUIRE=1 aborts on a bad path"
env DAXMALLOC_PATH=/nonexistent LD_PRELOAD="$SO" /bin/true >/dev/null 2>&1 \
	&& ok "fallback to glibc without REQUIRE" || bad "fallback to glibc without REQUIRE"

# Two writers on one region would corrupt each other through MAP_SHARED.
"${E[@]}" sleep 3 >/dev/null 2>&1 &
sleep 0.7
( "${E[@]}" /bin/true ) >/dev/null 2>&1 && bad "second process should be refused" || ok "flock refuses a second process"
wait 2>/dev/null

if [ -n "$PIN" ] && [ -e "$PIN" ]; then
	echo "-- composition with pin_binary.so --"
	env "DAXMALLOC_PATH=$TARGET" ${TSIZE:+DAXMALLOC_SIZE=$TSIZE} DAXMALLOC_REQUIRE=1 \
	    LD_PRELOAD="$SO $PIN" "$HERE/test/conformance" >/dev/null 2>&1 \
	    && ok "daxmalloc first" || bad "daxmalloc first"
	env "DAXMALLOC_PATH=$TARGET" ${TSIZE:+DAXMALLOC_SIZE=$TSIZE} DAXMALLOC_REQUIRE=1 \
	    LD_PRELOAD="$PIN $SO" "$HERE/test/conformance" >/dev/null 2>&1 \
	    && ok "pin_binary first" || bad "pin_binary first"
fi

echo
echo "$pass passed, $fail failed"
exit $((fail > 0))
