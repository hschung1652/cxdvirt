#!/usr/bin/env bash
# setup.sh -- bring the device up, in a straight line.
#
# This is the whole bring-up with nothing wrapped around it: read it top to
# bottom, copy it, change the insmod line.  `cxdvirt up` does the same thing with
# argument handling and checks; this is the version to start from when you want
# to understand or modify the sequence.
#
#     sudo ./bin/setup.sh
#     sudo env DRAM_CACHE_MB=8192 CACHE_POLICY=fifo ./bin/setup.sh
#
# Defaults: a 96 GiB device with a 4.8 GiB DRAM cache, 4 KiB tracking, CLOCK
# replacement, no prefetch.  Reverse it with bin/teardown.sh.
#
# PREREQUISITE.  The module MAPS a physical region the kernel was told to leave
# alone; it does not allocate one.  Boot with:
#
#     memmap=96G$0x4600000000 isolcpus=20-24,60-64 nohz_full=20-24,60-64 \
#     rcu_nocbs=20-24,60-64 irqaffinity=0-19,40-59 \
#     intel_idle.max_cstate=1 processor.max_cstate=1 intremap=off nokaslr
#
# The `$` is not a typo and is not interchangeable with `!`: it marks the range
# reserved (E820 type 2), which is what the module expects to find and map.
# `!` marks it as persistent memory and hands it to a different driver.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${CXDVIRT_ROOT:-$(cd "$HERE/.." && pwd)}

# cxl(1) and daxctl(1) from ndctl v78 or newer.  Anything older has no `cxl`
# subcommand at all, which is the one used below.
CXL=${CXL:-$(command -v cxl)}
DAXCTL=${DAXCTL:-$(command -v daxctl)}
KO=${KO:-$ROOT/nvmevirt/nvmev.ko}

# ---- the device ------------------------------------------------------------
MEMMAP_START=${MEMMAP_START:-0x4600000000}   # must match memmap= above
MEMMAP_SIZE=${MEMMAP_SIZE:-96G}              # 96 GiB total device
DRAM_CACHE_MB=${DRAM_CACHE_MB:-4914}         # 4.8 GiB cache; the rest is NAND
CACHE_POLICY=${CACHE_POLICY:-clock}          # clock | fifo | lifo
CPUS=${CPUS:-20,21,22,23}                    # I/O workers; keep in isolcpus
ZERO_ON_INIT=${ZERO_ON_INIT:-false}          # true if malloc must return zeros

# Background eviction.  ON by default, and the module's own default of 0 is the
# wrong one for general use: with the drain off, every admission into a full
# cache evicts a dirty victim INLINE, so a read miss pays tPROG (100 us) before
# it pays tR (3 us).  Measured here: a cold read sweep against a full dirty
# cache came back at a p50 of 108 us, all but 6 of it write-back.  That is a
# faithful model of a controller with no background flush, which is not what
# real ones do.  Set BG_DRAIN_4K=0 only to study that case deliberately -- and
# for any LIFO comparison, since the drain batches victims off the policy's
# victim end, which for LIFO is the recent working set.
BG_DRAIN_4K=${BG_DRAIN_4K:-1}
# Throughput ceiling is roughly BATCH * 1000 / MS evictions per second.  The
# module's 64-per-10ms default caps it near 5.8K/s, well under what a sweep over
# a few GB demands, and the surplus falls back onto the fault path.
BG_DRAIN_BATCH=${BG_DRAIN_BATCH:-1024}
BG_DRAIN_MS=${BG_DRAIN_MS:-1}

# ---- replacement, prefetch and write-allocate ------------------------------
# These are the knobs a policy or prefetch study varies.  All are insmod-time
# except CLOCK_HW_YOUNG, CXL_WR_ALLOC and OPTB_TSC, which are 0644 and can also
# be written under /sys/module/nvmev/parameters afterwards.
CLOCK_HW_YOUNG=${CLOCK_HW_YOUNG:-1}     # CLOCK second chance: 1 = +hardware A bit
LIFO_STAGE=${LIFO_STAGE:-1024}          # LIFO: newest N held out of the victim pool
CXL_WR_ALLOC=${CXL_WR_ALLOC:-0}         # 0 none, 1 deferred merge, 2 read-allocate
PREFETCH_MODE=${PREFETCH_MODE:-0}       # 0 off, 1 miss-triggered next-N
PREFETCH_DEGREE=${PREFETCH_DEGREE:-0}   # N
PREFETCH_RANDOM=${PREFETCH_RANDOM:-0}   # 1 = random-page control arm
OPTB_TSC=${OPTB_TSC:-1}                 # rdtsc stamps at fault entry/exit
OPTB4K_MAX=${OPTB4K_MAX:-}              # /proc/nvmev/optb4k capacity, records (empty = 2097152)

# ---- NAND timing -----------------------------------------------------------
# Compile-time: tR and tPROG come from the BASE_SSD profile in Kbuild, and the
# nand_tr_ns / nand_tprog_ns parameters only report them (docs/PARAMETERS.md).
# Passing them at insmod would change the record, not the model, so a value
# here is refused rather than passed.
NAND_TR_NS=${NAND_TR_NS:-}
NAND_TPROG_NS=${NAND_TPROG_NS:-}
[ -z "$NAND_TR_NS$NAND_TPROG_NS" ] || {
	echo "NAND_TR_NS/NAND_TPROG_NS cannot change the model: they are compiled in (BASE_SSD in Kbuild)." >&2
	exit 1; }

[ -f "$KO" ] || { echo "$KO missing -- run 'cxdvirt build'" >&2; exit 1; }

# Check the VERSION, not just the presence.  Distributions ship daxctl long
# before they ship a `cxl` subcommand -- Ubuntu 20.04 carries v67, which has
# daxctl and no cxl at all.  Finding daxctl on PATH therefore says nothing about
# whether the bring-up will work, and the failure it produces later ("no such
# subcommand") is far from the cause.
need_v78() {
	local bin=$1 name=$2 v
	[ -x "${bin:-}" ] || { echo "$name not found. Build ndctl v78+ and put it on PATH, or set ${name^^}=/path/to/$name" >&2; return 1; }
	v=$("$bin" version 2>/dev/null | head -1 | tr -cd '0-9.' | cut -d. -f1)
	[ -n "$v" ] && [ "$v" -ge 78 ] 2>/dev/null && return 0
	echo "$name is v${v:-?}; the CXL region commands need v78 or newer." >&2
	echo "  git clone https://github.com/pmem/ndctl && cd ndctl && meson setup build && meson compile -C build" >&2
	echo "  then: sudo env CXL=\$PWD/build/cxl/cxl DAXCTL=\$PWD/build/daxctl/daxctl $0" >&2
	return 1
}
need_v78 "${CXL:-}" cxl       || exit 1
need_v78 "${DAXCTL:-}" daxctl || exit 1

# Checked last, so that a missing or too-old ndctl is reported to whoever runs
# this rather than hidden behind "run as root".
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

set -e

# 1. CXL bus drivers.  nvmev.ko presents a Type 3 endpoint underneath them.
for m in cxl_core cxl_port cxl_mem cxl_pci cxl_acpi; do modprobe "$m"; done

# 2. Load.  memmap_size is not cosmetic: with BLKS_PER_PLN=0 the NAND block
#    count is derived from it, so it sets the FTL line geometry and therefore
#    every modelled latency.  dram_cache_mb must be 2 MiB-aligned (even MB).
ARGS=(
    memmap_start="$MEMMAP_START"
    memmap_size="$MEMMAP_SIZE"
    cpus="$CPUS"
    enable_cxl=true
    page_granularity=4k
    cache_policy="$CACHE_POLICY"
    cxl_clock_hw_young="$CLOCK_HW_YOUNG"
    lifo_stage="$LIFO_STAGE"
    dram_cache_mb="$DRAM_CACHE_MB"
    bg_drain_4k="$BG_DRAIN_4K"
    bg_drain_batch="$BG_DRAIN_BATCH"
    bg_drain_ms="$BG_DRAIN_MS"
    cxl_wr_alloc="$CXL_WR_ALLOC"
    prefetch_mode="$PREFETCH_MODE"
    prefetch_degree="$PREFETCH_DEGREE"
    prefetch_random="$PREFETCH_RANDOM"
    optb_tsc="$OPTB_TSC"
    layer_size=0            # 2 MiB mode only
    nr_ranks=1              # 2 MiB mode only
    zero_on_init="$ZERO_ON_INIT"
)
[ -n "$OPTB4K_MAX" ]    && ARGS+=(optb4k_max="$OPTB4K_MAX")

insmod "$KO" "${ARGS[@]}"

# 3. Let acpi_pci_root_add finish building the bus before touching the memdev.
sleep 3

# 4. cxl_mem's first auto-probe runs BEFORE NVMeV_init() has built the port
#    hierarchy, so it fails with -6.  Re-binding triggers a probe that finds the
#    ports.  The failed first probe is expected, not an error.
echo mem0 > /sys/bus/cxl/drivers/cxl_mem/unbind 2>/dev/null || true
sleep 1
echo mem0 > /sys/bus/cxl/drivers/cxl_mem/bind

# 5. Create the region, which is what makes /dev/dax0.0 appear.
"$CXL" create-region -m -d decoder0.0 mem0
sleep 2
"$DAXCTL" reconfigure-device --mode=devdax dax0.0

# 6. 0666 so workloads, and /proc/nvmev, work without root.
chmod 666 /dev/dax0.0
ls -l /dev/dax0.0

echo
echo "device up: ${MEMMAP_SIZE} total, ${DRAM_CACHE_MB} MB DRAM cache, ${CACHE_POLICY},"
echo "           background drain ${BG_DRAIN_4K} (${BG_DRAIN_BATCH}/${BG_DRAIN_MS}ms)"
echo "check it:  ./bin/cxdvirt selftest"
