#!/usr/bin/env python3
"""Splash-4 OCEAN across normalized WSS: CXDVirt vs Cylon.  Data module.

The write-intensive counterpart to redis_perf.py.  OCEAN -n8194 dirties roughly
its entire 14,190 MiB working set every timestep, so it drives the allocate and
write-back paths a read-only KV workload never touches.

SUITE: SPLASH-4 (IISWC'22), stock build -- ATOMIC_BARRIERS on, which is its
default and the only thing "we ran Splash-4" can mean.  Splash-4 is Splash-3
with three lock->atomic reductions in Ocean (executed 8, 8 and 48 times) and a
sense-reversing spin barrier replacing the pthread mutex+condvar one; every
compute kernel and decs.h are BYTE-IDENTICAL, so the grids, the first-touch
order and the dirty set do not change.  Measured against Splash-3:
  - footprint 14,880,176,288 B vs 14,880,178,080 -- exactly 1,792 B smaller,
    the 20 BARDEC structs compiled out of the G_MALLOC'd `bars`;
  - Cylon's per-miss service time 15.85/15.69/15.88 us vs 15.86/15.89/15.78 --
    the ceiling below is suite-independent;
  - CXDVirt's per-fault cost 2.22 us vs 2.26;
  - the ratio moves 3.41/3.54/3.21x -> 3.44/3.51/3.06x.
ONE ASYMMETRY, REPORTED BECAUSE IT POINTS OUR WAY.  At 1.1x the spin barrier
cuts CXDVIRT's fetch count 4.91% (4,132,480 vs the Splash-3 control's
4,346,009; two Splash-4 reps agree to 0.007%, so it is not noise) while CYLON's
moves by six fetches (+0.0001%).  Different interleaving moves CXDVirt's CLOCK
hand and drain timing; under DER a Cylon buffer hit never reaches the device,
so its reference bit is set once at insert and its eviction order is
insertion-order and interleaving-insensitive.  At 2.2x the signs reverse
(CXDVirt +0.31%, inside its own 0.835% rep spread; Cylon -1.84%).

RUNS ARE 8-THREAD (-p8).  OCEAN is a genuinely parallel SPLASH benchmark:
CREATE(slave,nprocs), a 2-D xprocs*yprocs domain decomposition (2x4 at P=8, so
2050x4098 points per thread), and 10 barriers per timestep.  -p1 was a choice,
and it turned out to be the choice that hid the largest effect in the dataset --
see CONCURRENCY below.  The single-threaded series is kept in DATA_P1 and is
SPLASH-3; do not mix it with DATA without saying so.

TIMES ARE OCEAN'S OWN, NOT WALL CLOCK.  The m4 CLOCK macro was patched from
Splash-4's gettimeofday (1 us) to clock_gettime(CLOCK_MONOTONIC); total = init
+ solve exactly, and external wall runs 4-8 ms higher (linking + daxmalloc ctor
+ exit).  Splash-4's own 4.0.1 fix for Splash-3's time(0) bug only reached us
resolution, so this patch supersedes it rather than duplicating it.

  init   allocation and initialization of 52 grids, plus the first timestep.
         Every page is first-touched here, so this segment IS the fault path:
         3,626,625 faults, identical on both platforms and both suites.
  solve  the 5 timed timesteps.  Ocean excludes the first, hence the split.

CONCURRENCY IS THE HEADLINE.  Cylon's cxl_req ring is created MP_SC -- many
vCPUs produce, exactly ONE FEMU-FTL-Thread consumes, and that thread performs
victim selection, the drain and flush_pg for every miss.  Its throughput is
therefore a hard bound, measured flat at 51.7-55.2K misses/s across a 6.6x swing
in miss count, two benchmark suites and every replacement/allocate configuration
tested, versus CXDVirt's 158.6-178.0K/s.  The ratio is 3.06-3.51x at -p8 against
1.17-1.83x at -p1 (the -p1 pair is Splash-3).

Three independent confirmations, so this is not a tuning artifact:
  (a) the ceiling is flat across WSS, suite and configuration (above);
  (b) at 0.35x, where solve has NO device traffic at all, Cylon's solve scales
      4.54x -- statistically identical to CXDVirt's 4.56x -- while its init
      (3.6M faults through the ring) scales only 1.15x.  DER hits parallelise
      perfectly; only misses serialise.  (Splash-3 -p1 vs -p8 measurement.)
  (c) scheduling was eliminated by experiment: pinning the FTL thread to two
      dedicated isolated cores with idle HT siblings, pollers separated, aux
      threads on housekeeping, changed the 2.2x result by 1.2% (435.2 ->
      440.5 s, Splash-3).  Per-miss cost barely moved under 8x concurrency
      (16.7 -> 18.1 us), which is saturation, not contention.

CXDVIRT'S OWN CONCURRENCY LIMIT -- reported, not hidden.  It scales 2.4x, not
8x.  Its drain becomes SCAN-bound: 8 threads taking write-protect faults re-arm
reference bits faster than the single CLOCK hand retires them (51.6% of probes
decline a victim at 2.2x -- unchanged from Splash-3's 51.7%, and invariant
across a 16x drain-budget change), leaving ~15% of write-backs to complete
in-fault and each thread stalled ~37% of its wall time -- while the modelled
NAND sits at 12% utilisation.  So that stall is the cost of tracking dirty state
in software, not device backpressure.  All runs here use bg_drain_batch=4096:
1024 demonstrably under-provisions (-10.6% wall at 2.2x) and 16384 buys only
another 2.8%.

EVICTION SEMANTICS ARE MATCHED.  CXDVirt runs cache_policy=clock with
cxl_clock_hw_young=0, so its second chance comes from the software reference bit
alone -- set on a fault, never by a hit -- which degenerates CLOCK to
FIFO-plus-one-grace, exactly Cylon's behaviour under DER.  Under the previous
tiered setting CXDVirt ran true CLOCK against Cylon's degenerate one and issued
15.3% MORE fetches at 2.2x, paying ~25 s of modelled NAND for a policy
difference rather than an emulation one.

COMPARE ON TOTALS, NEVER ON CYLON'S READ/WRITE SPLIT.  Its split is an
instruction-set artifact (the KVM fallback logs FP-store misses as reads) and
its "read miss" column excludes write-miss fetches that CXDVirt's nand_reads
includes.  The consistent bases are fetches = total misses minus virgin skips,
and programs = dirty write-backs.

BOTH SIDES ARE MATCHED AND COLD:
  - peak_live = 14,880,176,288 B on all six runs.  Byte-identical working set.
  - CXDVirt reloads the module per point (dram_cache_mb is 0444); Cylon gets a
    VM restart per point, its buffer persisting for the VM's lifetime.
  - Cylon runs CYLON_FT_PROG=0, removing its first-touch allocate+program on
    virgin LPNs -- stock Cylon charges a full pg_wr_lat there, on read misses
    too, and flush_pg discards that page at eviction anyway.  Confirmed in every
    buffer log: "First-touch programs: DEFERRED to eviction".
  - Cylon: evict_sync=3, watermarks 95/85% of the buffer, wm_batch=1,
    wb_slots=4, replacement=CLOCK, 32-way, pg_rd_lat=3000 -- verified per run.
  - Device models are matched: 8 ch x 16 LUN = 128 dies, tR 3000 ns,
    tPROG 100000 ns, 1200 MB/s channel, on both sides.

n: CXDVirt is the MEAN OF 2 REPS per point (spread 0.34/0.52/0.55%); Cylon is
n=1 per point.  Splash-3's spread at 2.2x was 2.06%, so the 3.06x ratio -- whose
numerator and denominator are each carried by a single run on one side -- is the
one number here that still wants a second rep.

Ocean's N must be (power of 2)+2, so the footprint quantises 4x and the CACHE is
varied to select WSS -- 40578 / 12912 / 6456 MB against 14,190.85 MiB.
"""
import numpy as np

COL  = {"CXDVirt": "#EE7733", "Cylon": "#0077BB"}
RCOL = "#009988"

# ---- 8 THREADS, SPLASH-4 (plotted) ---------------------------------------
# CXDVirt: ocean_wss_s4atomic/0826_143705_* and ocean_wss_s4/0826_150859_*
#          (mean of the two; batch=4096, clock, hw_young=0, mode 2)
# Cylon:   ocean_wss_cylon_s4/cylon_n8194_p8_c{40578,12912,6456}
# WSS,  CXDVirt (init, solve),      Cylon (init, solve)
# PUBLISHED VALUES, kept so a redraw that silently moves is loud rather than
# quiet.  They are no longer what gets plotted: extract_fig7 re-derives every
# one of them from the run artifacts under plots/data/ at plot time, and the
# assertion below fails if a parse and a literal ever disagree.  If you are
# adding runs and the assertion fires, update these -- do not delete the check.
PUBLISHED = [("0.35",  8.056660,  12.315675,  57.476612,  12.629829),   # 3.44x
             ("1.1",  13.186201,  35.747045,  69.368692, 102.468106),   # 3.51x
             ("2.2",  31.495840, 109.762505,  97.998664, 334.727519)]   # 3.06x

import extract_fig7 as _X                                       # noqa: E402

_o = _X.ocean()
DATA = [(w, _o[w]["cxd"][0], _o[w]["cxd"][1], _o[w]["cyl"][0], _o[w]["cyl"][1])
        for w, *_ in PUBLISHED]
REPS = {w: _o[w]["reps"] for w, *_ in PUBLISHED}

_drift = [(a, b) for a, b in zip(DATA, PUBLISHED)
          if any(abs(x - y) > 5e-6 for x, y in zip(a[1:], b[1:]))]
# The tripwire guards the PAPER dataset only.  In custom mode the numbers are
# supposed to differ -- that is the point -- so it reports and carries on, and
# the figure is drawn from whatever was measured.
if _drift and _X.CUSTOM:
    print(f"ocean_wss_bars: plotting CUSTOM data from {_X.DATA} "
          f"({len(_drift)}/{len(DATA)} points differ from the published figure)")
elif _drift:
    raise SystemExit("ocean_wss_bars: extracted data differs from the published\n"
                     "figure.  Either plots/data/ changed or a literal is stale:\n"
                     + "\n".join(f"  extracted {a}\n  published {b}" for a, b in _drift))

# ---- 1 THREAD, SPLASH-3 (superseded; kept for the -p1 vs -p8 scaling claim)
DATA_P1 = [("0.35", 35.894, 56.337, 66.144801, 57.389696),
           ("1.1",  39.732, 122.105, 68.700071, 227.070137),
           ("2.2",  74.191, 267.361, 95.791349, 302.925600)]

# NATIVE BASELINE (2026-09-30, placement-matched) -- same binary, no device.
#
# Launched exactly as sweep_ocean_wss.sh launches OCEAN on CXDVirt -- node-0
# CPUs, --membind 0, the same daxmalloc.so preloaded -- with /dev/dax0.0 swapped
# for a /dev/shm file pre-faulted on node 1.  The heap (all 13.86 GiB of it)
# sits on node 1 in the same dlmalloc arena, everything else on node 0; only the
# device is missing.  Kernel 6.18.5, the one the CXDVirt runs used.  n=3
# (19.500 / 19.455 / 19.348 s), every heap page verified on node 1.
#
# Against it CXDVirt at 0.35x is +4.8%, all of it in init (+14%, the 3.63M
# compulsory faults: ~2.2 us of thread time each); solve, where every access
# hits, is -0.6%.  Cylon is 3.61x.
#
# WHY NOT THE EARLIER --membind 1 ARM (20.357 s, 6.4.6).  --membind 1 binds
# EVERY allocation (binary, stack and libc on node 1 too), the heap was glibc
# malloc with first-touch zeroing, and the kernel was Cylon's host.  It read
# 4.7% slow, which made CXDVirt look like +0.08% and hid its fault-path cost.
# Kept as NATIVE_P8_MEMBIND / NATIVE_P8_LOCAL for context.
_on = _X.ocean_native()
NATIVE_P8 = sum(_on["heap"])             # Splash-4, 6.18.5, heap-only on node 1
if not _X.CUSTOM:
    assert abs(NATIVE_P8 - (7.044346 + 12.390025)) < 5e-6, NATIVE_P8
if "remote" in _on:
    NATIVE_P8_MEMBIND = sum(_on["remote"])   # 6.4.6, cpunodebind 0, membind 1
    NATIVE_P8_LOCAL   = sum(_on["local"])    # same, membind 0
    if not _X.CUSTOM:
        assert abs(NATIVE_P8_MEMBIND - (7.969836 + 12.386888)) < 5e-6, NATIVE_P8_MEMBIND
        assert abs(NATIVE_P8_LOCAL   - (5.322122 +  7.295776)) < 5e-6, NATIVE_P8_LOCAL
NATIVE_P1       = 42.691094 + 59.904779  # Splash-3, -p1
NATIVE_P1_LOCAL = 83.122

# Per first-touch fault at 0.35x -- the only point where init is pure fault
# path (zero evictions on either side).  RAW here (init / faults), because no
# native -p8 baseline exists to subtract:
#   -p8   CXDVirt 8.057 s / 3,626,625 = 2.22 us    Cylon 57.477 s = 15.85 us
#   -p1   CXDVirt 0.20 us net of native            Cylon 8.55 us   (Splash-3)
# The -p8 pair also expresses the ceiling directly: 450,140 vs 63,097 faults/s.
FAULT_US    = {"CXDVirt": 2.22, "Cylon": 15.85}   # -p8, raw
FAULT_US_P1 = {"CXDVirt": 0.20, "Cylon": 8.55}    # -p1, net of native init
N_FAULTS = 3_626_625

# NAND fetches per point (total misses minus the virgin skips, which are free
# and identical on both sides).  Needed for the per-miss service time below.
# CXDVirt: nand_reads (mean of 2 reps).  Cylon: (read_miss + write_miss) - virgin.
FETCHES = {"CXDVirt": [0, 4_132_480, 21_341_366],
           "Cylon":   [0, 5_726_374, 20_277_813]}


def service_us():
    """Per-miss service time, us, at each WSS.

    THREE INDEPENDENT MEASUREMENTS OF THE SAME QUANTITY, one per WSS:
      0.35x  init / faults          -- compulsory faults, no eviction exists
      1.1x   (solve - solve@0.35x) / fetches   -- capacity misses
      2.2x   same                              -- capacity misses, 4.8x more

    The 0.35x entry comes from init because solve has NO misses there (zero
    device traffic); the other two come from solve because init is contaminated
    by eviction once the cache is smaller than the footprint.  Subtracting the
    0.35x solve removes compute and hit cost, which is valid because compute per
    timestep is cache-size-invariant (multigrid absolute time is ~constant).

    Cylon returns 15.85 / 15.69 / 15.88 us -- 1.2% spread across compulsory vs
    capacity misses and a 4.9x swing in miss count, and within 0.7% of the
    Splash-3 figures (15.86 / 15.89 / 15.78).  That is its single FTL consumer's
    service time (~63,000 misses/s), recovered without reading its code.
    CXDVirt returns 2.22 / 5.67 / 4.57 us -- cost that varies with load, which
    is what a parallel fault path looks like.
    """
    out = {}
    for sysname, ii, ss in (("CXDVirt", 1, 2), ("Cylon", 3, 4)):
        base = DATA[0][ss]                      # the 0.35x solve: no misses
        v = [1e6 * DATA[0][ii] / N_FAULTS]      # 0.35x from init
        for i in (1, 2):
            v.append(1e6 * (DATA[i][ss] - base) / FETCHES[sysname][i])
        out[sysname] = v
    return out


# Miss throughput, total misses (incl. virgin) / total time.  Cylon's is the
# single-consumer ceiling; CXDVirt's is what a parallel fault path sustains.
MISS_RATE = {"CXDVirt": {"0.35": 178_017, "1.1": 158_565, "2.2": 176_754},
             "Cylon":   {"0.35":  51_730, "1.1":  54_430, "2.2":  55_242}}


def totals(data=None):
    """(labels, cxd_init, cxd_solve, cyl_init, cyl_solve, ratio) as arrays."""
    d = DATA if data is None else data
    lab = [r[0] for r in d]
    ci  = np.array([r[1] for r in d])
    cs  = np.array([r[2] for r in d])
    yi  = np.array([r[3] for r in d])
    ys  = np.array([r[4] for r in d])
    return lab, ci, cs, yi, ys, (yi + ys) / (ci + cs)
