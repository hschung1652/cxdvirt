#!/usr/bin/env python3
"""Redis + YCSB-C, CXDVirt vs Cylon: performance comparison.

Metric is Redis-internal HGETALL latency (INFO commandstats / latencystats),
NOT YCSB's reported latency.  YCSB's number is ~90% harness (27.5 us of
JVM/jedis/loopback around 2.55 us of work on CXDVirt) and that harness differs
5x between bare metal and an 8-vCPU guest, so it would plot the test rig
rather than the emulators.  The internal metric is measured identically on
both sides.

All six points come from one procedure (2026-08-20): counters and latency are
reset between load and run, so every number is run-phase only.  Cylon's inline
writeback fraction is MEASURED on CXDVirt at each WSS and transferred, not
fitted -- 14% at WSS 1.1, 61% at WSS 2.2 (0.35 evicts nothing).  Cross-checks
that were not fitted: eviction rates agree to 4.5%/6.3%, and the dirty-page
fraction to 0.7 points (58.4% vs 57.7%), via different mechanisms.

Cylon runs the workload in a VM and carries a ~3x higher baseline throughout;
the device costs above each platform's own baseline converge to 1.28x at
WSS 1.1 and 1.04x at WSS 2.2.
"""
import argparse, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

COL  = {"CXDVirt": "#EE7733", "Cylon": "#0077BB"}
BASE = {"CXDVirt": "#F7C9A6", "Cylon": "#A8CBE8"}   # lighter = baseline portion

# Server-side HGETALL (INFO commandstats usec_per_call / latencystats), run
# phase only.  2026-08-24 re-measurement: BOTH sides previously carried an
# eviction-path artifact that dominated the tail.
#   CXDVirt  its background drain was capped at 64 victims per 10 ms (~5.8K
#            evictions/s) against ~12.7K/s of demand, so 61% of evictions ran
#            inline in the fault path.  bg_drain_batch=1024 bg_drain_ms=1
#            removes the cap; sync_evicts is now 0.
#   Cylon    ran CYLON_EVICT_SYNC=1 with CYLON_WB_INLINE_PCT transferred from
#            CXDVirt's *broken* inline fraction (14 / 61).  Now runs leg 3
#            (CYLON_EVICT_SYNC=3), the structural analogue of CXDVirt's
#            EVICTING state: writeback recorded with a deadline, evictor never
#            pays, a later toucher pays the remainder.  No transferred
#            parameter remains.
# WSS 0.35 was re-run on both sides too, for provenance rather than necessity:
# it evicts nothing (Cylon Entry cnt 457,971/1,258,240 = 36% occupancy, 0
# watermark drains, 0 write-backs; CXDVirt 1 miss in its whole run phase), so
# the eviction configuration provably cannot matter there.  The Cylon numbers
# moved 7.88 -> 7.76 mean and one histogram bucket on p99, which is run-to-run
# noise and confirms that reasoning.
#
# 2026-08-25 re-measurement of the CXDVirt side at cxl_clock_hw_young=0.
# CXDVirt's CLOCK took its second chance from the software reference bit AND the
# hardware PTE Accessed bit; Cylon's takes it from a ref bit set once at insert
# and never re-armed (under DER a buffer hit never reaches the device, so its
# only re-arm path is unreachable).  Running CXDVirt on the software bit alone
# -- which is set on a fault and never by a hit -- gives the same FIFO-plus-one-
# grace behaviour, so both sides now age their reference state identically.
#
# THE LATENCIES BARELY MOVED.  Means shift -3.9 / -6.0 / +1.9 % -- both
# directions, i.e. run-to-run noise -- and two of three p99s are bit-identical.
# Device work moved a lot (nand_reads +29.4% at 2.2x, victims +30.7%): CLOCK's
# recency really was helping on Zipfian lookups.  Redis absorbs it because a
# miss is one ~6 us NAND read against a 10.7 us mean, so the extra fetches land
# in the tail, not the mean.  OCEAN, being miss-bound in its solve phase, moved
# materially under the same change.
#
# 0.35x is policy-independent by construction: victim_probes_4k = 0 in BOTH the
# old and new runs, so cxl_find_victim_4k -- which contains the second-chance
# test the knob gates -- never executed.
#
# Provenance (CXDVirt): redis/clockswclk_d0_c4914_{1,3,6}000000_1t_0825_17*.
# The pre-match 0.35x row traced to redis/cxdvirt_cdf_1000000_0820_0634, filed
# under the older naming scheme that predates run_policy_redis.sh -- worth
# knowing, because a search for the clock_d0_c4914_1000000 pattern finds nothing
# and the row looks unbacked.  Cylon side unchanged (2026-08-24).
#
#  WSS,  CXD mean, Cylon mean,  CXD p99, Cyl p99,  CXD tput, Cyl tput
# PUBLISHED VALUES, kept as a tripwire rather than as the source: extract_fig7
# re-derives all six columns from the run artifacts at plot time and the check
# below fires if a parse and a literal disagree.  Columns are
# (wss, CXDVirt mean, Cylon mean, CXDVirt p99, Cylon p99, CXDVirt ops/s, Cylon ops/s).
PUBLISHED = [("0.35",  2.45,  7.76,   4.015, 10.047, 34189, 6701),
             ("1.1",   4.23, 10.29,  24.063, 36.095, 30053, 6569),
             ("2.2",  10.72, 17.89,  46.079, 62.207, 23212, 6251)]

import extract_fig7 as _X                                       # noqa: E402

_r = _X.redis()
DATA = [(w, _r[w]["cxd"][0], _r[w]["cyl"][0], _r[w]["cxd"][1], _r[w]["cyl"][1],
         _r[w]["cxd"][2], _r[w]["cyl"][2]) for w, *_ in PUBLISHED]

_drift = [(a, b) for a, b in zip(DATA, PUBLISHED)
          if any(abs(x - y) > 5e-4 for x, y in zip(a[1:], b[1:]))]
# The tripwire guards the PAPER dataset only.  In custom mode the numbers are
# supposed to differ -- that is the point -- so it reports and carries on, and
# the figure is drawn from whatever was measured.
if _drift and _X.CUSTOM:
    print(f"redis_perf: plotting CUSTOM data from {_X.DATA} "
          f"({len(_drift)}/{len(DATA)} points differ from the published figure)")
elif _drift:
    raise SystemExit("redis_perf: extracted data differs from the published\n"
                     "figure.  Either plots/data/ changed or a literal is stale:\n"
                     + "\n".join(f"  extracted {a}\n  published {b}" for a, b in _drift))
# All six values now trace to run artifacts.  The previous Cylon 0.35
# throughput (6614) appeared only in data/redis_matched_runs.md and matched no
# surviving run; it is superseded by the re-run.
BASELINE = {"CXDVirt": 2.55, "Cylon": 7.88}

# NATIVE DRAM BASELINE (2026-09-30, placement-matched).  The panel-(b)
# analogue of Ocean's NATIVE_P8.  Launched exactly as run_policy_redis.sh
# launches the CXDVirt server -- node-0 CPUs, --membind 0, the same
# daxmalloc.so preloaded, same redis-server, redis.conf, YCSB-C properties,
# percentile list and RESETSTAT/LATENCY-RESET split -- with /dev/dax0.0 swapped
# for a /dev/shm file pre-faulted on node 1.  The heap sits on node 1 in the
# same dlmalloc arena, everything else on node 0; only the device is missing.
# Kernel 6.18.5, the one the CXDVirt runs used.  Every heap page is checked to
# be on node 1 after the run phase.  Driver: run_native_redis.sh <rec> heap.
#
# WHY 0.35x IS THE ONE THAT MATTERS.  It is the only point where NEITHER
# emulator does device work -- CXDVirt logs 1 miss per 1,000,000 run-phase
# operations, Cylon 0 write-backs at 36% buffer occupancy -- so the distance
# from this line is emulation machinery with no modelled NAND to confound it.
# CXDVirt 2.45 us is -1.3% (inside the 2.44-2.54 rep spread, and p50/p99/p99.9
# identical); Cylon 7.76 us is 3.1x, +5.3 us.
#
# ONE LINE, NOT THREE.  Native is flat across the sweep (the membind arm read
# 2.84 / 2.69 / 2.85 us at 1M/3M/6M records) because plain DRAM has no cache to
# overflow, so the WSS axis does nothing without a device.
#
# WHY NOT THE EARLIER --membind 1 ARM (2.84 us, 6.4.6).  --membind 1 binds EVERY
# allocation, so the binary, stack and libc sat on node 1 too, the heap was
# glibc malloc, and the kernel was Cylon's host.  It read 14% above the matched
# arm, which put CXDVirt below "remote DRAM" -- impossible for an emulator whose
# data lives there.  Kept below as NATIVE_REMOTE_MEMBIND / NATIVE_LOCAL.
#
# DO NOT compare throughput against this line.  The YCSB/JVM harness is not
# comparable across boots; server-side latency is, and that is what this panel
# plots.
#
# Provenance: data/redis_native/native_heap_k6.18.5_1000000_1t_0930_*  (n=3).
_rn = _X.redis_native()
_mean = lambda v: sum(v) / len(v)
NATIVE_REMOTE = _mean(_rn["heap"]["0.35"])  # 0.35x; the line the figure draws
if not _X.CUSTOM:
    assert abs(NATIVE_REMOTE - (2.44 + 2.54 + 2.47) / 3) < 5e-4, NATIVE_REMOTE
# Context only: the superseded --membind arms, kernel 6.4.6.
if _rn["remote"]["0.35"]:
    NATIVE_REMOTE_MEMBIND = _mean(_rn["remote"]["0.35"])
    NATIVE_LOCAL = _mean(_rn["local"]["0.35"])
    NATIVE_REMOTE_ALL = [round(_mean(_rn["remote"][w]), 2)
                         for w in ("0.35", "1.1", "2.2")]  # flat by 3%
    if not _X.CUSTOM:
        assert abs(NATIVE_REMOTE_MEMBIND - (2.82 + 2.86) / 2) < 5e-4, NATIVE_REMOTE_MEMBIND
        assert abs(NATIVE_LOCAL - 2.49) < 5e-4, NATIVE_LOCAL
        assert NATIVE_REMOTE_ALL == [2.84, 2.69, 2.85], NATIVE_REMOTE_ALL


def build(style, a):
    wss  = [d[0] for d in DATA]
    mean = {"CXDVirt": np.array([d[1] for d in DATA]),
            "Cylon":   np.array([d[2] for d in DATA])}
    p99  = {"CXDVirt": np.array([d[3] for d in DATA]),
            "Cylon":   np.array([d[4] for d in DATA])}
    LO_MAX, HI_MIN, HI_MAX = 50.0, 95.0, 400.0

    with plt.style.context(style):
        # scienceplots/ieee fixes tick+label sizes in its rcParams, so --fs
        # alone would only scale the hand-placed text.  Drive both from --fs.
        plt.rcParams.update({
            "font.size": a.fs, "axes.labelsize": a.fs, "axes.titlesize": a.fs,
            "xtick.labelsize": a.fs, "ytick.labelsize": a.fs,
            "legend.fontsize": a.fs - 1,
        })
        fig, (axhi, axlo) = plt.subplots(
            2, 1, sharex=True,
            figsize=tuple(float(t) for t in a.figsize.split(",")),
            gridspec_kw={"height_ratios": [1.0, 1.6], "hspace": 0.06})
        x, w = np.arange(len(DATA)), 0.20

        series = [(-1.5*w, "CXDVirt", mean, None,  "CXDVirt mean"),
                  (-0.5*w, "Cylon",   mean, None,  "Cylon mean"),
                  ( 0.5*w, "CXDVirt", p99,  "///", "CXDVirt p99"),
                  ( 1.5*w, "Cylon",   p99,  "///", "Cylon p99")]

        for ax in (axhi, axlo):
            for off, sysname, src, hat, lab in series:
                ax.bar(x + off, src[sysname], w, color=COL[sysname],
                       edgecolor="black", linewidth=0.4, hatch=hat, zorder=3,
                       label=lab if ax is axlo else None)

        # value labels on whichever segment the bar top lands in
        for off, sysname, src, hat, lab in series:
            for xi, v in zip(x + off, src[sysname]):
                if v <= LO_MAX:
                    axlo.text(xi, v + LO_MAX*0.025, f"{v:.1f}" if v < 100 else f"{v:.0f}",
                              ha="center", va="bottom", fontsize=a.fs - 2)
                else:
                    axhi.text(xi, v + (HI_MAX-HI_MIN)*0.025, f"{v:.0f}",
                              ha="center", va="bottom", fontsize=a.fs - 2)

        axlo.set_ylim(0, LO_MAX)
        axhi.set_ylim(HI_MIN, HI_MAX)
        axhi.spines["bottom"].set_visible(False)
        axlo.spines["top"].set_visible(False)
        for _ax, _side in ((axhi, 'bottom'), (axlo, 'top')):
            for _which in ('major', 'minor'):
                _ax.tick_params(which=_which, **{_side: False})
        axhi.tick_params(labelbottom=False)

        # diagonal break marks
        dk = dict(marker=[(-1, -0.6), (1, 0.6)], markersize=5, linestyle="none",
                  color="k", mec="k", mew=0.8, clip_on=False)
        axhi.plot([0, 1], [0, 0], transform=axhi.transAxes, **dk)
        axlo.plot([0, 1], [1, 1], transform=axlo.transAxes, **dk)

        axlo.set_xticks(x); axlo.set_xticklabels(wss)
        axlo.set_xlabel("normalized WSS to DRAM cache")
        fig.supylabel(r"Redis read latency [$\mu$s]", x=0.005, fontsize=a.fs)
        h, l = axlo.get_legend_handles_labels()
        axhi.legend(h, l, loc="upper left", frameon=False, fontsize=a.fs - 2,
                    handlelength=1.3, borderaxespad=0.3, labelspacing=0.25,
                    ncol=2, columnspacing=1.0)

        fig.savefig(a.out, bbox_inches="tight", dpi=400)
        fig.savefig(a.out.rsplit(".", 1)[0] + ".pdf", bbox_inches="tight")
        plt.close(fig)
    print("wrote", a.out)
    for wl, ct, yt, cp, yp, ck, yk in DATA:
        print(f"  WSS {wl:<5} mean {ct:6.2f} / {yt:6.2f}   p99 {cp:6.1f} / {yp:6.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures/redis_cxdvirt_vs_cylon.png")
    ap.add_argument("--figsize", default="4.0,3.0")
    ap.add_argument("--fs", type=float, default=10)
    a = ap.parse_args()
    import scienceplots  # noqa: F401
    try:
        build(["science", "ieee"], a)
    except Exception as e:
        print(f"[latex failed ({e})]", file=sys.stderr)
        build(["science", "ieee", "no-latex"], a)


if __name__ == "__main__":
    main()
