#!/usr/bin/env bash
# pin_threads.sh — fine-pin qemu threads for the pin+isolate experiment.
#   vCPU threads      -> node-0 cores 4-11            (1:1)   [guest lives on node 0]
#   FEMU-FTL-Thread   -> node-1 isolated core(s)              [THE CXL MISS PATH]
#   NVMe pollers      -> node-1 isolated cores               [block path only]
#   idle aux threads  -> left on housekeeping (default; not in the miss path)
#
# WHY THE FTL THREAD MUST BE SEPARATED FROM THE vCPUs.  Launching qemu under
# `taskset -c 4-11` confines the WHOLE PROCESS -- every thread inherits the mask,
# including the single FEMU-FTL-Thread that is the sole consumer of the cxl_req
# ring (created MP_SC: many vCPUs produce, exactly one thread consumes).  The
# vCPUs do not block while they wait for it: cxlssd.c busy-polls the response
# ring and then spins again on qemu_clock_get_ns for the modelled latency.  So
# 8 spinning vCPUs end up competing for CPU with the one thread they are all
# waiting on -- priority inversion, and adding threads makes it worse.  Measured
# at OCEAN -n8194 -p8 2.2x: 435.2 s, SLOWER than the same run at -p1 (398.7 s),
# while CXDVirt sped up 2.44x on the same workload.  Re-pinning the FTL thread
# off the vCPU cores is what makes that comparison mean anything.
#
# Re-pinning works even though qemu was launched under taskset: that only sets
# the initial mask, and root may widen any thread's affinity afterwards.
#
# Pollers are auto-detected as the busy-spinning (>BUSY_TICKS/1s) non-vCPU threads,
# so this works with multipoller_enabled=1 (multiple pollers).  vCPU tids come from
# QMP query-cpus-fast.  Requires isolcpus=4-11,20-25,44-51,60-65 + patched kvm.  Root.
set -u
VCPU_CPUS=(${VCPU_CPUS:-4 5 6 7 8 9 10 11})
# The CXL miss path.  Single-threaded and on the critical path of every buffer
# miss, so any sharing shows up directly as miss latency.
#
# NODE-1 TOPOLOGY (this host): isolated cores are 20-27, and 60-67 are their
# HYPERTHREAD SIBLINGS (20/60, 21/61, ...) -- 8 physical cores, not 16.  With
# 8 NVMe pollers that pool is already 1:1, so the FTL thread cannot get a
# physical core without taking something.  It takes 20-21, and 60-61 are left
# DELIBERATELY EMPTY so the miss path owns those cores outright rather than
# sharing execution units with a sibling.
#
# The pollers absorb that: they serve the emulated NVMe BLOCK path, which an
# OCEAN run over CXL.mem never touches (the guest root disk is virtio-scsi, a
# separate device), so they idle.  Pollers 7 and 8 land on 62-63, siblings of
# 22-23 -- harmless between idle threads, and it keeps the count 1:1 so the
# "pollers > cores" warning below stays meaningful.
FTL_CPUS=(${FTL_CPUS:-20 21})
POLLER_CPUS=(${POLLER_CPUS:-22 23 24 25 26 27 62 63})   # FEMU NVMe pollers (block path)
BUSY_TICKS=${BUSY_TICKS:-10}          # fallback: >10 ticks/1s (~10% cpu) => busy poller
SOCK=${SOCK:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/cylon-tree/CylonFEMU/build-femu/qmp-sock}
LOG=${LOG:-/mnt/nvme/cxdvirt/cxdvirt/reproduction/cylon-tree/CylonFEMU/build-femu/log}  # FEMU log: "FEMU-NVMe-Poller-TID: N"

[ "$(id -u)" -eq 0 ] || { echo "run as root (taskset)"; exit 1; }
QPID=""; for p in $(pgrep -f 'qemu-system-x86_64.*femu-cxlssd' 2>/dev/null); do
  [ "$(cat /proc/$p/comm 2>/dev/null)" = qemu-system-x86 ] && QPID=$p; done
[ -n "$QPID" ] || { echo "no qemu running"; exit 1; }
[ -S "$SOCK" ] || { echo "qmp socket $SOCK missing"; exit 1; }

# 1) vCPU thread-ids via QMP -> node-0 cores
mapfile -t VTIDS < <(python3 - "$SOCK" <<'PY'
import socket,json,sys
s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); f=s.makefile('rw'); f.readline()
def cmd(c): f.write(json.dumps(c)+"\n"); f.flush(); return json.loads(f.readline())
cmd({"execute":"qmp_capabilities"})
for c in cmd({"execute":"query-cpus-fast"})["return"]: print(c["thread-id"])
PY
)
[ "${#VTIDS[@]}" -gt 0 ] || { echo "no vcpu tids from QMP"; exit 1; }
declare -A ISV
for i in "${!VTIDS[@]}"; do
  taskset -pc "${VCPU_CPUS[$((i % ${#VCPU_CPUS[@]}))]}" "${VTIDS[$i]}" >/dev/null; ISV[${VTIDS[$i]}]=1
done
echo "pinned ${#VTIDS[@]} vCPUs -> ${VCPU_CPUS[*]} (node 0)"

# 2) FEMU-FTL-Thread by comm name -> its own node-1 core(s).
# Matched on comm rather than the log: the log line the pollers use
# ("FEMU-NVMe-Poller-TID") is the BLOCK path and never names this thread.
# TASK_COMM_LEN is 16, so "FEMU-FTL-Thread" (15 chars) survives intact.
declare -A ISF
FTID=()
# (a) by comm, for builds where qemu_thread_set_name actually took effect.
for t in $(ls "/proc/$QPID/task"); do
  [ -n "${ISV[$t]:-}" ] && continue
  [ "$(cat /proc/$QPID/task/$t/comm 2>/dev/null)" = "FEMU-FTL-Thread" ] && { FTID+=("$t"); ISF[$t]=1; }
done
# (b) BY ELIMINATION -- the usual path.  This qemu is built without
# CONFIG_PTHREAD_SETNAME_NP, so every one of its ~25 threads reports comm
# "qemu-system-x86" and (a) finds nothing.  But the FTL thread busy-spins on
# the cxl_req ring, the vCPUs are known from QMP, and the NVMe pollers log
# their own TIDs -- so the busy thread that is neither is the FTL thread.
# Verified live: 17 busy threads = 8 vCPUs + 8 logged pollers + exactly 1 left.
if [ "${#FTID[@]}" -eq 0 ]; then
  declare -A ISP
  for t in $(grep -oE "FEMU-NVMe-Poller-TID: [0-9]+" "$LOG" 2>/dev/null | awk '{print $NF}' | sort -un); do
    ISP[$t]=1
  done
  [ "${#ISP[@]}" -gt 0 ] || echo "  (no poller TIDs in $LOG -- pollers cannot be excluded; result may include them)" >&2
  declare -A F0
  for t in $(ls "/proc/$QPID/task"); do
    [ -n "${ISV[$t]:-}" ] && continue; [ -n "${ISP[$t]:-}" ] && continue
    F0[$t]=$(awk '{print $14+$15}' /proc/$QPID/task/$t/stat 2>/dev/null)
  done
  sleep 1
  while read -r d t; do
    [ "$d" -gt "$BUSY_TICKS" ] && { FTID+=("$t"); ISF[$t]=1; }
  done < <(for t in "${!F0[@]}"; do
             u=$(awk '{print $14+$15}' /proc/$QPID/task/$t/stat 2>/dev/null)
             echo "$((u-${F0[$t]:-0})) $t"
           done | sort -rn)
  [ "${#FTID[@]}" -gt 0 ] && echo "  (comm lookup empty -- identified ${#FTID[@]} FTL thread(s) by elimination)"
fi

if [ "${#FTID[@]}" -eq 0 ]; then
  echo "WARN: no FTL thread found -- the CXL miss path is NOT pinned." >&2
  echo "      Every vCPU miss is serviced by that thread; if it shares a core" >&2
  echo "      with a spinning vCPU the run measures scheduling, not emulation." >&2
else
  if [ "${#FTID[@]}" -gt "${#FTL_CPUS[@]}" ]; then
    echo "  WARN: ${#FTID[@]} candidate FTL threads for ${#FTL_CPUS[@]} cpus -- elimination may have" >&2
    echo "        caught an aux thread too.  Widen FTL_CPUS or check the tids by hand." >&2
  fi
  n=0; for t in "${FTID[@]}"; do
    cpu=${FTL_CPUS[$((n % ${#FTL_CPUS[@]}))]}
    taskset -pc "$cpu" "$t" >/dev/null; echo "  FTL tid=$t -> cpu $cpu (node 1, CXL miss path)"; n=$((n+1))
  done
  echo "pinned ${#FTID[@]} FTL thread(s) -> ${FTL_CPUS[*]}"
fi
for c in "${FTL_CPUS[@]}"; do for v in "${VCPU_CPUS[@]}"; do [ "$c" = "$v" ] &&
  echo "  WARN: FTL cpu $c is also a vCPU cpu -- that is the inversion this script exists to avoid" >&2; done; done

# 3) FEMU poller TIDs from the log (exact) -> node-1 cores (1:1); busy-spin fallback
mapfile -t PTIDS < <(grep -oE "FEMU-NVMe-Poller-TID: [0-9]+" "$LOG" 2>/dev/null | awk '{print $NF}' | sort -un)
PT=(); for t in "${PTIDS[@]}"; do [ -d "/proc/$QPID/task/$t" ] && [ -z "${ISV[$t]:-}" ] &&
  [ -z "${ISF[$t]:-}" ] && PT+=("$t"); done
if [ "${#PT[@]}" -eq 0 ]; then
  echo "  (log gave no live poller TIDs — falling back to busy-spin detection)"
  declare -A U0
  for t in $(ls /proc/$QPID/task); do [ -n "${ISV[$t]:-}" ] && continue
    [ -n "${ISF[$t]:-}" ] && continue   # already placed as the CXL miss path
    U0[$t]=$(awk '{print $14+$15}' /proc/$QPID/task/$t/stat 2>/dev/null); done
  sleep 1
  while read -r d t; do [ "$d" -gt "$BUSY_TICKS" ] && PT+=("$t"); done < <(
    for t in "${!U0[@]}"; do u1=$(awk '{print $14+$15}' /proc/$QPID/task/$t/stat 2>/dev/null); echo "$((u1-${U0[$t]:-0})) $t"; done | sort -rn)
fi
n=0
for t in "${PT[@]}"; do
  cpu=${POLLER_CPUS[$((n % ${#POLLER_CPUS[@]}))]}
  taskset -pc "$cpu" "$t" >/dev/null; echo "  poller tid=$t -> cpu $cpu (node 1)"; n=$((n+1))
done
echo "pinned $n pollers -> ${POLLER_CPUS[*]}   (idle aux left on housekeeping)"
[ "$n" -gt "${#POLLER_CPUS[@]}" ] && echo "  WARN: $n pollers > ${#POLLER_CPUS[@]} node-1 cores — some share; widen POLLER_CPUS + isolcpus"
echo "verify: for t in \$(ls /proc/$QPID/task); do grep -H Cpus_allowed_list /proc/$QPID/task/\$t/status; done | awk '{print \$NF}' | sort | uniq -c"
