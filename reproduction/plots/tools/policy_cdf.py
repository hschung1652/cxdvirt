#!/usr/bin/env python3
"""Redis + YCSB-C read-latency CDF by DRAM-cache eviction policy (CXDVirt).

Cylon Figure 11 counterpart.  Same workload shape as theirs -- YCSB-C, 100%
read, zipfian, 1 KB records (fieldcount=10 x fieldlength=100), 8 client
threads -- and the same measurement, YCSB's own per-operation raw latencies
(`-p measurementtype=raw`), so the axes mean the same thing.

Operating point.  Cylon labels its 6M-record panel "2.67 Norm. WSS", but that
label is not self-consistent: their 1M panel is labelled 0.33, and 6M is only
6x 1M.  Our 1M point measures 1640 MiB (daxmalloc peak_live) = 0.334x against a
4914 MiB cache, matching their 0.33 exactly -- which puts 6M at ~2.0x, not 2.67.
Rather than inherit a suspect label we hit 2.67 directly and by measurement:
1M records against dram_cache_mb=614, giving 1637/614 = 2.666x.  Normalized WSS
is the controlled variable, so absolute capacity is free (Cylon 5.1.2).

Eviction timing is uniform across the three policies within a figure, selected
by --drain.  Default (bg_drain_4k=0) is synchronous, one victim per admission,
matching Cylon's one-victim-per-fill.  --drain 1 is the background watermark
drain (95% -> 85% in batches of 64), which the module originally applied to
CLOCK alone.

The ablation, measured (N=1 per cell, 2.67x WSS, miss/req and ops/s):

    policy   drain 0        drain 1        throughput
    CLOCK    1.009          1.080  +7.0%   12,488 -> 14,986  +20.0%
    FIFO     1.139          1.138  -0.1%   11,950 -> 14,607  +22.2%
    LIFO     2.246          2.075  -7.6%    8,432 ->  8,994   +6.7%

Two things follow.  (a) Uniformity matters: the drain is worth ~20% throughput
to FIFO and CLOCK (28% of evictions move off the fault path), so leaving it on
for one policy only would bias any latency comparison by 4x the ~5% run-to-run
noise floor.  (b) The default is the CONSERVATIVE choice for LIFO -- enabling
the drain narrows LIFO's deficit against CLOCK from 2.23x to 1.92x, because it
converts part of LIFO's frozen set into an adaptive band.  The ranking
CLOCK < FIFO < LIFO holds in both, so no claim here rests on the setting.

CLOCK's +7.0% is not the A-bit degenerating under batch eviction: vforced_4k
went 0 -> 1 and vskip_second_chance_4k 7.40M -> 7.53M, i.e. the tiered sweep
behaved the same.  The likeliest cause is the 5% usable-capacity loss (steady
occupancy 149,312 vs 157,184, the dispatcher holding at high_wm), but FIFO
should then have lost too and did not -- unresolved at N=1, and the drain-off
CLOCK/FIFO points are cross-build (11:20) against drain-on (18:20), a ~1%
confound.  Do not read the per-policy sensitivities as settled.

Degenerate CLOCK is excluded.  Without the hardware A-bit the software
`accessed` flag is only set on a fault, and in 4K mode a read hit leaves the
PTE present and never faults -- so each page got exactly one grace round and
CLOCK collapsed onto FIFO (1.127 vs 1.139 miss/req).  The curve here is A-bit
CLOCK (cxl_clock_hw_young=1).

Colour and line style both carry policy: identity must never rest on colour
alone.  Note the hues are Tol bright, the same family redis_cdf.py uses for
platform identity -- there orange is CXDVirt and blue is Cylon.  Different
figure, different mapping; the legend disambiguates.

--source raw     YCSB client-side, end-to-end (default; what Fig 11 plots).
--source server  Redis INFO latencystats, server-side service time.  NOTE the
                 runs of 2026-08-22 predate the dense percentile list now set by
                 run_policy_redis.sh, so they carry only Redis's default
                 p50/p99/p99.9 -- three points, not a readable CDF.  Re-run
                 before using this mode.
Client latency in a closed loop is concurrency/throughput by Little's Law
(8 threads / 11950 ops/s = 670us vs 666us measured), so the raw CDF is largely
a throughput restatement; the server curve isolates service time.  Both are
reported -- misses/request in the legend is the device-side ground truth.
"""
import argparse, glob, os, re, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import latload  # noqa: E402  (prefers <base>.lat.gz, falls back to <base>.raw)

R = "/mnt/nvme/cxdvirt/cxdvirt/reproduction/plots/data/redis"

# Paul Tol *high-contrast* -- TOL_HC in spread_two_panel.py:39, already part of
# this repo's palette set.  Tol designed this scheme specifically for three
# qualitative classes, and unlike Tol bright its members differ in LIGHTNESS as
# well as hue, so it is greyscale-safe.
#
# That property is the reason for it here rather than Tol bright: FIFO and CLOCK
# sit almost on top of each other (p50 474 vs 456 us), and two mid-lightness
# hues -- bright's #4477AA blue against #228833 green -- are hard to separate
# where thin lines overlap.  A dark/light/mid triple stays legible there.
#
# CLOCK takes Okabe-Ito's amber-orange (#E69F00, OKABE slot 2 in
# spread_two_panel.py:35) rather than TOL_HC's gold #DDAA33 -- reads as orange
# while holding the same lightness band, L* ~70.  Tol vibrant's #EE7733 is a
# stronger orange but sits at L* ~63, close enough to the #BB5566 red to give
# back some of the greyscale separation this palette was chosen for.  Okabe-Ito
# and Tol are both CVD-safe sets and spread_two_panel.py already treats them as
# interchangeable, so borrowing one slot is consistent with house practice.
#
# Resulting lightness ladder: blue L* ~30, red ~48, orange ~70 -- the spread is
# what keeps FIFO and CLOCK legible where their curves coincide.
#
# Slots are assigned once and fixed: dark blue is the FIFO baseline, orange the
# best performer, red the worst.  Deliberate but STATIC -- it does not re-map if
# the numbers move.  Line style (a) and hatch + x-tick labels (b) repeat the
# identity, so nothing rests on colour alone.
COL = {"FIFO": "#004488", "CLOCK": "#E69F00", "LIFO": "#BB5566"}
LS  = {"FIFO": "-",       "CLOCK": "--",      "LIFO": ":"}

# bg_drain_4k -> (data dir, {policy: run stem}).  Misses/request are NOT
# hardcoded: they are recomputed from each run's own .ctr1/.ctr2 counter
# snapshots, so the legend can never drift from the data it labels.
# Keyed (bg_drain_4k, run-phase client threads).  The LOAD phase is 64 threads
# in every case -- redis-load-*.properties fixes threadcount=64 -- so --threads
# selects the run phase only.
#
# 1t exists because Cylon's Figure 11 says "8 threads" in both its caption and
# its text, but the artifact script that reproduces it, run-figure11.sh, hard-
# codes THREADS=1 (not even overridable, unlike figure 12's ${THREADS:-1}).
# Three things say the artifact is right and the prose is wrong: Figure 11[b]
# spans 0-600 us while its sibling Figure 12[b] spans 0-3000 us at LOWER cache
# pressure and one thread; our 8t p99 is 1306 us, outside their axis; our 1t
# p99 is 374 us, inside it.  Redis serves commands single-threaded, so 8 clients
# add ~6.5x queueing and no throughput.
DATA = {
    (0, 8): (f"{R}/policy", {
        "FIFO":  "fifo_1000000_8t_0822_1043",
        "CLOCK": "clock_1000000_8t_0822_1122",
        "LIFO":  "lifo_1000000_8t_0822_1052",
    }),
    (1, 8): (f"{R}/drain_ablation/drain1", {
        "FIFO":  "fifo_d0_c614_1000000_8t_0822_205614",
        "CLOCK": "clock_d0_c614_1000000_8t_0822_210140",
        "LIFO":  "lifo_d0_c614_1000000_8t_0822_203634",
    }),
    # Fast-eviction set (bg_drain_batch=1024 bg_drain_ms=1).  The (1,1) entry
    # below is the RATE-LIMITED drain: 64 victims per 10 ms sustains ~5.8K
    # evictions/s against ~12.7K/s of demand, so 67-77% of evictions ran on the
    # fault path.  Keyed 2 because the key is (bg_drain_4k, threads) and both
    # sets have the drain enabled -- they differ in its throughput ceiling.
    (2, 1): (f"{R}/prefetch", {
        "FIFO":  "fifo_d0_c614_1000000_1t_0824_090028",
        "CLOCK": "clock_d0_c614_1000000_1t_0824_090326",
        "LIFO":  "lifo_d0_c614_1000000_1t_0824_093058",
    }),
    (1, 1): (f"{R}/policy_1t_drain1", {
        "FIFO":  "fifo_d0_c614_1000000_1t_0823_145444",
        "CLOCK": "clock_d0_c614_1000000_1t_0823_151218",
        "LIFO":  "lifo_d0_c614_1000000_1t_0823_150033",
    }),
}


def _meta(path):
    m = {}
    for line in open(path):
        if not line.startswith("#"):
            k, _, v = line.partition(" ")
            m[k.strip()] = v.strip()
    return m


def discover(d, records, threads=1, regime="fast", hw_young="1"):
    """-> (d, {POLICY: stem}) from the runs present in d, in place of DATA's
    fixed stems -- how a re-collected Fig. 8(a) is plotted.

    A run qualifies when the .meta run_policy_redis.sh froze beside it (the live
    module parameters) says prefetch off, `threads` run threads, `records`
    records, CLOCK's hardware-bit setting `hw_young`, and a drain in `regime`:
    slow = 64 victims per 10 ms (also what a .meta without bg_drain_batch means,
    the value being hardcoded then), fast = anything larger.  Its run phase must
    have finished (.ctr2) and its latency sample survived (.lat.gz or .raw).
    Where a policy has several, the newest stamp wins, and the choice is
    printed."""
    out = {}
    for meta in sorted(glob.glob(os.path.join(d, "*.meta"))):
        b = meta[:-5]
        m = _meta(meta)
        pol = m.get("cache_policy", "").upper()
        if pol not in COL or m.get("cxl_clock_hw_young") != hw_young:
            continue
        if m.get("records") != str(records) or m.get("threads") != str(threads):
            continue
        if m.get("prefetch_mode") == "1" or m.get("prefetch_random") == "1":
            continue
        rg = "slow" if m.get("bg_drain_batch", "64") in ("64", "") else "fast"
        if rg != regime or not os.path.exists(b + ".ctr2"):
            continue
        if not (os.path.exists(b + ".lat.gz") or os.path.exists(b + ".raw")):
            continue
        out[pol] = os.path.basename(b)        # sorted, so the newest stays
    missing = [p for p in ("FIFO", "CLOCK", "LIFO") if p not in out]
    if missing:
        raise SystemExit(f"{d}: no complete {regime}-drain run for "
                         f"{', '.join(missing)} ({records:,} records, "
                         f"{threads} thread(s))")
    for p in ("FIFO", "CLOCK", "LIFO"):
        print(f"  {p:<5} {out[p]}", file=sys.stderr)
    return d, out


def runs(a):
    """(data dir, {POLICY: stem}) for this invocation: discovered from
    --policy-dir when given, else the published stems in DATA."""
    if getattr(a, "policy_dir", None):
        return discover(a.policy_dir, a.records, a.threads,
                        getattr(a, "regime", None) or "fast")
    return DATA[(a.drain, a.threads)]


def describe(a):
    """One line naming the runs, for the console summary."""
    if getattr(a, "policy_dir", None):
        return (f"{a.records:,} records, {getattr(a, 'regime', None) or 'fast'} "
                f"drain, {a.threads} thread(s), from {a.policy_dir}")
    return (f"2.67x Norm. WSS (1637 MiB / 614 MiB), 1M records, "
            f"bg_drain_4k={a.drain}, {a.threads} thread(s)")


def misses_per_request(base, records):
    """Run-phase cache_misses per operation, from the counter snapshots."""
    rd = lambda p: {k: int(v) for k, v in (l.split() for l in open(p))}
    c1, c2 = rd(base + ".ctr1"), rd(base + ".ctr2")
    return (c2["cache_misses"] - c1["cache_misses"]) / float(records)


def load_raw(stem):
    """-> sorted latency samples [us] from a YCSB measurementtype=raw file."""
    # YCSB emits one header per measurement type, so a CLEANUP block with its
    # own header follows the READ rows -- filter on the op name, don't skiprows.
    return latload.read_latencies(stem, dtype=np.float64)


def load_server(stem):
    """-> (latency_us, cumulative_fraction) from Redis INFO latencystats."""
    txt = open(f"{stem}.latencystats").read()
    m = re.search(r"latency_percentiles_usec_hgetall:(\S+)", txt)
    if not m:
        raise ValueError(f"no hgetall percentiles for {stem}")
    pts = sorted((float(v), float(k.lstrip("p")) / 100.0)
                 for k, v in (kv.split("=") for kv in m.group(1).split(",")))
    return np.array([x for x, _ in pts]), np.array([p for _, p in pts])


def build(style, a):
    with plt.style.context(style):
        # scienceplots/ieee pins tick+label sizes in its rcParams, so --fs alone
        # would only scale hand-placed text.  Drive both from --fs.
        plt.rcParams.update({
            "font.size": a.fs, "axes.labelsize": a.fs, "axes.titlesize": a.fs,
            "xtick.labelsize": a.fs, "ytick.labelsize": a.fs,
            "legend.fontsize": a.fs - 1,
        })
        fig, ax = plt.subplots(figsize=tuple(float(t) for t in a.figsize.split(",")))
        d, stems = runs(a)
        stats = {}
        for pol in ("FIFO", "CLOCK", "LIFO"):
            stem = f"{d}/{stems[pol]}"
            mpr = misses_per_request(stem, a.records)
            if a.source == "raw":
                v = load_raw(stem)
                x = np.linspace(0, a.xmax, 600)
                y = np.searchsorted(v, x, side="right") / v.size
                stats[pol] = (np.percentile(v, [50, 90, 99]), v.size, mpr)
            else:
                x, y = load_server(stem)
                stats[pol] = (np.interp([.5, .9, .99], y, x), None, mpr)
            lbl = f"{pol}" + (f"  ({mpr:.2f} miss/req)" if a.annotate else "")
            ax.plot(x, y, color=COL[pol], linestyle=LS[pol], linewidth=1.2,
                    zorder=3, label=lbl,
                    solid_capstyle="round", dash_capstyle="round")

        if a.source == "server":
            ax.set_xscale("log")
        else:
            ax.set_xlim(0, a.xmax)
        ax.set_ylim(0, 1.005)
        ax.set_xlabel(r"Redis read latency [$\mu$s]")
        ax.set_ylabel("CDF")
        ax.grid(True, which="major", alpha=0.25, linewidth=0.4, zorder=0)
        ax.legend(loc="lower right", frameon=False, fontsize=a.fs - 2,
                  handlelength=2.0, labelspacing=0.3, borderaxespad=0.4)

        fig.savefig(a.out, bbox_inches="tight", dpi=400)
        fig.savefig(a.out.rsplit(".", 1)[0] + ".pdf", bbox_inches="tight")
        plt.close(fig)

    print("wrote", a.out)
    print(f"  {describe(a)}, source={a.source}")
    for pol, (pct, n, mpr) in stats.items():
        p50, p90, p99 = pct
        n_s = f"n={n:,}" if n else "HDR percentiles"
        print(f"  {pol:<6} p50 {p50:7.1f}  p90 {p90:7.1f}  p99 {p99:7.1f}   "
              f"{mpr:.3f} miss/req   {n_s}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["raw", "server"], default="raw")
    ap.add_argument("--out", default=None)
    ap.add_argument("--figsize", default="4.0,3.0")
    ap.add_argument("--fs", type=float, default=10)
    ap.add_argument("--xmax", type=float, default=0,
                    help="0 = auto: 600 us at 1 thread, 2000 at 8")
    ap.add_argument("--no-annotate", dest="annotate", action="store_false")
    ap.add_argument("--drain", type=int, choices=[0, 1, 2], default=2,
                    help="bg_drain_4k the runs were taken under")
    ap.add_argument("--threads", type=int, choices=[1, 8], default=1,
                    help="run-phase client threads (load is always 64)")
    ap.add_argument("--records", type=int, default=1000000)
    ap.add_argument("--policy-dir", dest="policy_dir", default=None,
                    help="plot the runs found in this directory (see discover()) "
                         "instead of the published stems; --drain is then unused")
    ap.add_argument("--regime", choices=["slow", "fast"], default="fast",
                    help="with --policy-dir: which drain regime's runs to take")
    a = ap.parse_args()
    if not a.xmax:
        a.xmax = 600.0 if a.threads == 1 else 2000.0
    if a.out is None:
        a.out = ("/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures/"
                 f"redis_policy_cdf_267wss_{a.source}"
                 + f"_drain{a.drain}_{a.threads}t.png")
    import scienceplots  # noqa: F401
    try:
        build(["science", "ieee"], a)
    except Exception as e:
        print(f"[latex failed ({e})]", file=sys.stderr)
        build(["science", "ieee", "no-latex"], a)


if __name__ == "__main__":
    main()
