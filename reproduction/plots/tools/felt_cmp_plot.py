#!/usr/bin/env python3
"""felt_cmp_plot.py — felt per-miss latency, Cylon vs CXDVirt, decomposed.

Apples-to-apples at 3 us tR.  CXDVirt's in-context victim write-back (`evict`)
is EXCLUDED: Cylon's eviction flush is a no-op (ftl.c flush_pg returns 0 on its
first line, every policy calls it), so it charges no NAND program latency at all.
Subtracting evict puts both emulators on the same footing of not modelling
write-back; it is not a claim that CXDVirt's write-back is free.

Methodology mirrors refault_breakdown.py so the two figures are comparable:
each bar reaches the POINT percentile of felt (a true quantile); the servicing
body is the p50-band split held constant; the tail components are band-means
rescaled so their sum equals the point-percentile tail residual.  So the bar
TOTAL is exact and the SPLIT is measured.

Run with the plots venv:
  /mnt/nvme/cxdvirt/cxdvirt/reproduction/plots/.venv/bin/python felt_cmp_plot.py [--out PATH]
"""
import argparse
import os
import sys
import numpy as np

# CYLON_DATA / NVMEV_DATA point at another capture directory -- make_figures.sh
# passes the artifact's results/, so a re-collection is drawn instead of the
# reference data.
D = os.environ.get("CYLON_DATA") or "/mnt/nvme/cxdvirt/cxdvirt/reproduction/results/cylon"
N = os.environ.get("NVMEV_DATA") or "/mnt/nvme/cxdvirt/cxdvirt/reproduction/results/nvmev"
TSC = 2.1                       # cycles/ns, same box for both captures
PCTS = [50, 90, 99, 99.9, 99.99]

# semantic slots.  KVM/QEMU are Cylon's emulation overhead; CXDVirt services the
# miss in-context so its counterpart is a single segment.
STAGES = ["KVM", "QEMU", "emulation overhead", "FTL+NAND", "channel transfer",
          "primary-fill stall", "multi-exit refault", "re-entry gap"]
# tR for the active profile (ssd_config.h, SAMSUNG_ZNAND: NAND_4KB_READ_LATENCY_LSB).
# CXDVirt's `modeled` is tR + the 4 KiB channel transfer, so subtracting the
# constant splits it exactly.  Cylon charges NO transfer: the block is #if 0'd in
# ssd_advance_status AND ch_xfer_lat is hard-zeroed in cxlssd.c, so its band is
# its whole device deadline and `channel transfer` is structurally 0.
TR_US = 3.0

# Legend grouping.  Which stages an emulator CAN have is itself the argument:
# CXDVirt services the miss in-context, so it structurally cannot have a
# primary-fill stall (no FTL handoff) or a multi-exit refault (no instruction
# re-execution); Cylon charges no channel transfer (#if 0'd + ch_xfer_lat=0).
CYLON_ONLY = ["KVM", "QEMU", "primary-fill stall", "multi-exit refault"]
CXD_ONLY = ["emulation overhead", "channel transfer"]
BOTH = ["FTL+NAND", "re-entry gap"]
# Paul Tol bright: distinguishable under deuteranopia and in greyscale order
# Must match refault_breakdown.py's draw_cfgs palette exactly, so a stage is the
# same colour in both figures.  `emulation overhead` has no counterpart there
# (it is CXDVirt's analogue of KVM+QEMU), so it takes wine, the one Tol hue not
# already spoken for.
COLOR = {
    "KVM":                "#4477AA",   # blue
    "QEMU":               "#EE6677",   # red
    "emulation overhead": "#332288",   # indigo (felt_cmp only; scored against
                                       # the 7 shared hues -- min dE 49 normal,
                                       # 33 deuteranope, 127 vs its green neighbour)
    "FTL+NAND":           "#228833",   # green
    "channel transfer":   "#CCBB44",   # yellow
    "primary-fill stall": "#AA3377",   # purple
    "multi-exit refault": "#66CCEE",   # cyan
    "re-entry gap":       "#BBBBBB",   # grey
}
# wrapped legend text; the dict keys stay unwrapped as the data keys
DISPLAY = {"emulation overhead": "emulation\noverhead"}


def _trace(path):
    """The trace itself, or its compressed copy: numpy reads .xz and .gz as it
    reads plain text."""
    for p in (path, path + ".xz", path + ".gz"):
        if os.path.exists(p):
            return p
    return path


def band(order, n, p, frac=0.001):
    # Half-width is 0.1% of n, but never more than half the distance to the
    # top, so the band stays centred on its percentile.  Unclamped, p99.9 and
    # p99.99 averaged everything up to the slowest access (p99.8-p100,
    # p99.89-p100), and a few extreme accesses set the bar.
    half = max(1, int(min(frac, (1 - p / 100) / 2) * n))
    r = int(round(p / 100 * (n - 1)))
    return order[max(0, r - half):min(n, r + half + 1)]


def own_windows(k, all_vcpus=False):
    """Exit windows of the benchmark's own vCPU only (kvm_optb logs every
    vCPU's MMIO exits).  Joining all of them by containment counted other
    vCPUs' concurrent exits as multi-exit refaults of the measured access.
    Single-threaded captures: the benchmark's vCPU has the most windows."""
    gx = k["gtsc_exit"].astype(np.int64)
    gn = k["gtsc_entry"].astype(np.int64)
    if all_vcpus:
        return gx, gn
    v = k["vcpu"].astype(int)
    keep = v == np.bincount(v).argmax()
    return gx[keep], gn[keep]


def cylon(tag="znand3us", body_tag=None, ch_xfer_ns=0.0, measured=False, all_vcpus=False):
    """-> rows[pct][stage] in us, exactly as refault_breakdown.py builds them.

    measured=True takes the servicing body from full_<tag>_gtsc.csv joined
    per-access by gtsc_exit (same run as the felt log), so each percentile gets
    its OWN body rather than a p50 constant, and the tail terms keep their raw
    band-means with no rescale.  primary-fill stall is then identically zero:
    optb_kvm_join defines KVM = host_window - QEMU - FTL - NAND, so the stages
    sum to the window and `first - body` vanishes."""
    # exact guest-TSC containment join: felt, first window, sum of windows
    gt = np.loadtxt(_trace(f"{D}/guestfelt_{tag}_gtsc.txt"))
    ts, dc = gt[:, 0].astype(np.int64), gt[:, 1].astype(np.int64)
    k = np.genfromtxt(_trace(f"{D}/kvmwin_{tag}_gtsc.csv"), delimiter=",", names=True)
    gx, gn = own_windows(k, all_vcpus)
    win = gn - gx
    Ng = len(ts); ge = ts + dc; order = np.argsort(ts)
    idx = np.searchsorted(ts[order], gx, side="right") - 1
    ok = (idx >= 0) & (idx < Ng); j = order[np.clip(idx, 0, Ng - 1)]
    c = ok & (gx >= ts[j]) & (gn <= ge[j])
    ci = np.where(c)[0]; acc = j[ci]
    ow = np.lexsort((gx[ci], acc)); ci = ci[ow]; acc = acc[ow]
    fmask = np.r_[True, acc[1:] != acc[:-1]]
    fw = np.zeros(Ng, np.int64); sw = np.zeros(Ng, np.int64)
    cnt = np.zeros(Ng, int)
    fw[acc[fmask]] = win[ci[fmask]]
    np.add.at(sw, acc, win[ci]); np.add.at(cnt, acc, 1)
    have = cnt > 0
    felt = dc[have] / TSC / 1e3
    first = fw[have] / TSC / 1e3
    swin = sw[have] / TSC / 1e3

    if measured:
        ds = np.genfromtxt(_trace(f"{D}/full_{tag}_gtsc.csv"), delimiter=",", names=True)
        if "gtsc_exit" not in ds.dtype.names:
            raise SystemExit(f"full_{tag}_gtsc.csv has no gtsc_exit column")
        sx = ds["gtsc_exit"].astype(np.int64)
        i2 = np.searchsorted(ts[order], sx, side="right") - 1
        ok2 = (i2 >= 0) & (i2 < Ng); j2 = order[np.clip(i2, 0, Ng - 1)]
        c2 = ok2 & (sx >= ts[j2]) & (sx <= ge[j2])
        SK = np.zeros(Ng); SQ = np.zeros(Ng); SD = np.zeros(Ng)
        np.add.at(SK, j2[c2], ds["KVM"][c2] / 1e3)
        np.add.at(SQ, j2[c2], ds["QEMU"][c2] / 1e3)
        np.add.at(SD, j2[c2], ((ds["inbound"] + ds["service"] + ds["outbound"]
                                + ds["modeled"])[c2]) / 1e3)
        SK, SQ, SD = SK[have], SQ[have], SD[have]
        o = np.argsort(felt); n = len(felt); rows = []
        for P in PCTS:
            s2 = band(o, n, P)
            kv, qe, dv = SK[s2].mean(), SQ[s2].mean(), SD[s2].mean()
            ch = max(0.0, dv - TR_US) if ch_xfer_ns > 0 else 0.0
            fn = min(dv, TR_US) if ch_xfer_ns > 0 else dv
            ref = max(0.0, (swin - first)[s2].mean())
            gap = max(0.0, (felt - swin)[s2].mean())
            rows.append([kv, qe, 0.0, fn, ch, 0.0, ref, gap])
        return np.array(rows), felt

    # Modelled mode only: the servicing body is the p50 band of the optb+ftrace
    # capture, a separate run; body_tag lets it differ from the felt capture.
    # (Measured mode takes each percentile's body from the same run's _gtsc
    # stages above and never reads this file.)
    d = np.genfromtxt(_trace(f"{D}/full_{body_tag or tag}_seq.csv"), delimiter=",", names=True)
    tot = d["total"]
    s = band(np.argsort(tot), len(tot), 50)
    KVM = d["KVM"][s].mean() / 1e3
    QEMU = d["QEMU"][s].mean() / 1e3
    FTLNAND = ((d["inbound"] + d["service"] + d["outbound"])[s].mean()
               + d["modeled"][s].mean()) / 1e3
    # If this capture had Cylon's channel stage enabled (ch_xfer_lat != 0), its
    # device budget now contains the transfer too, so split it exactly as
    # CXDVirt's is: tR in the FTL+NAND slot, the remainder as channel transfer.
    # Stock Cylon (ch_xfer_lat=0) keeps the whole band in FTL+NAND.
    CHAN = 0.0
    if ch_xfer_ns > 0:
        CHAN = max(0.0, FTLNAND - TR_US)
        FTLNAND = min(FTLNAND, TR_US)

    body = KVM + QEMU + FTLNAND + CHAN
    o = np.argsort(felt); n = len(felt); rows = []
    for P in PCTS:
        s = band(o, n, P)
        fill = max(0.0, first[s].mean() - body)
        ref = max(0.0, (swin - first)[s].mean())
        gap = max(0.0, (felt - swin)[s].mean())
        tb = fill + ref + gap
        tp = max(0.0, np.percentile(felt, P) - body)
        if tb > 0:
            kk = tp / tb; fill *= kk; ref *= kk; gap *= kk
        rows.append([KVM, QEMU, 0.0, FTLNAND, CHAN, fill, ref, gap])
    return np.array(rows), felt


def cxdvirt(path=f"{N}/felt_joined_m2_seq.csv", measured=False, read_pass=False):
    """Same construction, evict excluded.  -> rows[pct][stage] in us.

    measured=True drops BOTH approximations the modelled construction makes:
    the body is taken at each percentile's own band instead of being frozen at
    p50, and the tail terms keep their raw band-means instead of being rescaled
    so the bar sums to the point quantile.  The bar then no longer equals the
    quantile exactly -- and that residual is the point: it measures how much of
    the felt tail the per-access join actually accounts for, which the rescale
    otherwise forces to zero by construction.

    read_pass=True keeps only the second recorded pass.  The capture records
    two passes in access order (PASSES=3 WARMUP=1); the first evicts the dirty
    pages the warm-up wrote, the second evicts only clean ones.  Subtracting
    `evict` removes the first pass's in-fault write-back but not its side
    effects (reads queued behind background write-backs, longer re-entry gaps),
    which make up the whole p99.9/p99.99 band.  The second pass has no
    write-back at all, matching Cylon's no-op flush without subtracting
    anything; its `evict` is victim selection and unmap, emulator work, so it
    joins the emulation overhead."""
    x = np.genfromtxt(_trace(path), delimiter=",", names=True)
    if read_pass:
        if len(x) % 2:
            raise SystemExit(f"{path}: odd row count, not two equal passes")
        x = x[len(x) // 2:]
        # A wrong split would show the first pass's ~42% write-backs.  A clean
        # second pass still has one or two evict stalls > 50 us per capture
        # (1.0 ms in iso1; 0.48 and 21.8 ms in iso1_4914, where the module's
        # sync write-back counter says at most 4 long evicts were not write-backs).
        # They sit far above p99.99's band and do not move any bar.
        long_ev = x["evict"] > 50e3
        if long_ev.mean() > 1e-4:
            raise SystemExit(f"{path}: {long_ev.sum():,} second-pass rows have "
                             "evict > 50 us -- not a write-back-free pass")
        if long_ev.any():
            print(f"note: {long_ev.sum()} second-pass access(es) with evict > 50 us "
                  f"(max {x['evict'].max() / 1e3:.0f} us), kept", file=sys.stderr)
        felt = x["felt_ns"] / 1e3
        over = (x["dispatch"] + x["evict"] + x["install"]) / 1e3
    else:
        felt = (x["felt_ns"] - x["evict"]) / 1e3      # write-back off the path
        over = (x["dispatch"] + x["install"]) / 1e3   # CXDVirt's KVM+QEMU analogue
    modeled = x["modeled"] / 1e3
    wait = x["wait"] / 1e3                            # primary-fill stall analogue
    gap = x["gap_ns"] / 1e3

    o = np.argsort(felt); n = len(felt)
    s50 = band(o, n, 50)
    OVER = over[s50].mean()
    m = modeled[s50].mean()
    FTLNAND = min(m, TR_US)          # the array read itself (the configured tR)
    CHAN = max(0.0, m - TR_US)       # 4 KiB clocked out over the NAND channel
    body = OVER + FTLNAND + CHAN

    rows = []
    for P in PCTS:
        s = band(o, n, P)
        fill = max(0.0, wait[s].mean())
        # multi-exit refault analogue: extra handler entries beyond the first.
        # 0% of accesses fault more than once single-threaded (no instruction
        # re-execution, no cross-thread handoff), so this is structurally 0.
        ref = 0.0
        g = max(0.0, gap[s].mean())
        if measured:
            # every segment at THIS percentile's band; no rescale
            ov = over[s].mean(); mm = modeled[s].mean()
            rows.append([0.0, 0.0, ov, min(mm, TR_US), max(0.0, mm - TR_US),
                         fill, ref, g])
            continue
        tb = fill + ref + g
        tp = max(0.0, np.percentile(felt, P) - body)
        if tb > 0:
            kk = tp / tb; fill *= kk; ref *= kk; g *= kk
        rows.append([0.0, 0.0, OVER, FTLNAND, CHAN, fill, ref, g])
    return np.array(rows), felt


def report(name, rows):
    print(f"\n=== {name} [us] ===")
    print("  pP  " + "".join(f"{s[:10]:>12}" for s in STAGES) + f"{'TOTAL':>9}")
    for i, P in enumerate(PCTS):
        v = rows[i]
        print(f"  p{P:<3}" + "".join(f"{x:>12.2f}" for x in v) + f"{v.sum():>9.2f}")


def draw(sets, out, groups=None, paper_width=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    try:
        import scienceplots  # noqa
        plt.style.use(["science", "no-latex"])
    except Exception:
        pass

    y = np.arange(len(PCTS))
    hgt, offs = 0.34, [+0.19, -0.19]
    # paper mode: design at the final printed width so nothing is downscaled and
    # every point size below is the size that reaches the page.
    narrow = bool(paper_width) and float(paper_width) < 5.0
    if paper_width:
        W = float(paper_width)
        # Designed at the final printed width, so LaTeX applies no scaling and
        # every point size below is the size that reaches the page.  Floor is
        # 8pt: under a 9-10pt body font, figure text smaller than that renders
        # visibly finer than the caption and reviewers call it unreadable.
        if narrow:
            # half-width subfigure panel: 8pt is all the density allows, and the
            # aspect goes taller so the in-axes legend has vertical room
            # sized ~6% over target: bbox_inches="tight" pads the box out past
            # `paper_width`, so LaTeX shrinks it slightly back to the panel
            # width and these land at 8/9/9.5pt on the page.
            F = dict(val=8.5, name=8.5, ytick=10.0, xtick=9.0, xlab=10.0,
                     leg=8.5, legtitle=9.0)
            ypad, elw = 30, 0.35
            fig, ax = plt.subplots(figsize=(W, W * 0.857))
        else:
            F = dict(val=9.0, name=9.0, ytick=10.0, xtick=9.0, xlab=10.0,
                     leg=9.0, legtitle=9.0)
            # y tick pad must clear the per-bar names, ~36% wider at 9pt
            ypad, elw = 34, 0.4
            fig, ax = plt.subplots(figsize=(W, W * 0.50))
    else:
        F = dict(val=14, name=13, ytick=17, xtick=15, xlab=17,
                 leg=14, legtitle=15)
        ypad, elw = 70, 0.4
        fig, ax = plt.subplots(figsize=(9.6, 4.4))

    xmax = max(r.sum(1).max() for _, r in sets)
    for (label, rows), off in zip(sets, offs):
        base = np.zeros(len(PCTS))
        for si, s in enumerate(STAGES):
            v = rows[:, si]
            if not np.any(v > 0):
                continue                     # zero segments carry no bar area
            ax.barh(y + off, v, height=hgt, left=base, color=COLOR[s],
                    edgecolor="black", linewidth=elw)
            base += v
        for i in range(len(PCTS)):
            ax.text(base[i] + xmax * 0.012, y[i] + off, f"{base[i]:.1f}",
                    va="center", ha="left", fontsize=F["val"])
        # name every bar, not just the first pair
        for i in range(len(PCTS)):
            ax.text(-0.008, y[i] + off, label,
                    transform=ax.get_yaxis_transform(), va="center",
                    ha="right", fontsize=F["name"], style="italic", color="0.25")

    ax.set_yticks(y)
    ax.set_yticklabels([f"p{p:g}" for p in PCTS], fontsize=F["ytick"])
    ax.tick_params(axis="y", length=0, pad=ypad)   # clear the per-bar names
    ax.tick_params(axis="x", labelsize=F["xtick"])
    ax.invert_yaxis()
    ax.set_xlabel(r"felt per-miss latency [$\mu$s]", fontsize=F["xlab"])
    ax.set_xlim(0, xmax * 1.10)
    ax.grid(axis="x", alpha=0.3, lw=0.5)
    ax.set_axisbelow(True)

    # three grouped legends: what only Cylon has, what only CXDVirt has, and
    # what both charge.  The grouping carries the architectural argument, so the
    # stages that are structurally zero for one side still appear.
    def pat(names):
        disp = {} if paper_width else DISPLAY      # no wrapping needed above the axes
        return [mpatches.Patch(facecolor=COLOR[s], edgecolor="black",
                               linewidth=0.5, label=disp.get(s, s))
                for s in names]

    # the top three rows leave the right half of the axes empty (their bars are
    # short), so the legend lives there instead of stealing height above the plot
    common = dict(loc="upper right", frameon=False, fontsize=F["leg"],
                  title_fontsize=F["legtitle"], columnspacing=1.0, handlelength=1.3,
                  handletextpad=0.45, labelspacing=0.2 if narrow else 0.3,
                  borderpad=0.05 if narrow else 0.15, alignment="left")
    # one compact block: the wide group on top, the two narrow ones side by side
    # beneath it, all inside the empty right half of the p50/p90/p99 rows
    g_cyl, g_cxd, g_both = groups or (CYLON_ONLY, CXD_ONLY, BOTH)
    if paper_width:
        # At print width the empty right half of the short rows is too small for
        # a three-block legend, so the groups stack above the axes as single
        # rows -- each group's membership is the architectural argument, so the
        # three titles stay.
        # Two bands, not three: at 9pt the title+entries pair costs ~0.34in of
        # height apiece, so stacking all three groups would spend a quarter of
        # the figure on the legend.  The single-entry CXDVirt group rides beside
        # the Cylon one, which keeps every group label -- the membership is the
        # architectural argument -- inside two lines instead of three.
        asp = 0.857 if narrow else 0.50
        if narrow:
            # Anchor to the FIGURE, not the axes: the axes start 27.5% in (the
            # p99.99 tick plus the italic per-bar names), which leaves the
            # 3-entry "both" row too little width and clips it off the page.
            tr, xx0 = fig.transFigure, 0.012
            pitch = (F["leg"] + F["legtitle"] + 5.0) / (72.0 * W * asp)
            base = 0.670   # base + 3*pitch must stay under 1.0 or the top title clips
        else:
            tr, xx0 = ax.transAxes, 0.0
            pitch = (F["leg"] + F["legtitle"] + 6.0) / (72.0 * W * asp * 0.72)
            base = 1.02
        if W >= 5.0:
            anchors = [(g_cyl, "Cylon only", xx0, base + pitch),
                       (g_cxd, "CXDVirt only", 0.50, base + pitch),
                       (g_both, "both", xx0, base)]
        else:
            # a single-column figure has no room to set two groups side by side
            # (the Cylon row alone runs the full width), so stack all three
            anchors = [(g_cyl, "Cylon only", xx0, base + 2 * pitch),
                       (g_cxd, "CXDVirt only", xx0, base + pitch),
                       (g_both, "both", xx0, base)]
        for names, title, xx, yy in anchors:
            lg = fig.legend(handles=pat(names), title=title, ncol=len(names),
                            bbox_to_anchor=(xx, yy), bbox_transform=tr,
                            **dict(common, loc="lower left"))
            lg._legend_box.align = "left"
    else:
        l1 = ax.legend(handles=pat(g_cyl), title="Cylon only", ncol=2,
                       bbox_to_anchor=(0.545, 0.99), **dict(common, loc="upper left"))
        ax.add_artist(l1)
        l2 = ax.legend(handles=pat(g_cxd), title="CXDVirt only", ncol=1,
                       bbox_to_anchor=(0.545, 0.74), **dict(common, loc="upper left"))
        ax.add_artist(l2)
        ax.legend(handles=pat(g_both), title="both", ncol=1,
                  bbox_to_anchor=(1.00, 0.74), **common)

    if narrow:
        # left clears the p99.99 tick plus the italic per-bar names; top leaves
        # the three legend bands their ~0.95in; bottom the x ticks and label.
        fig.subplots_adjust(left=0.275, right=0.995, top=0.660, bottom=0.150)
        # The scienceplots style sets savefig.bbox="tight" in rcParams, and
        # savefig(bbox_inches=None) *falls back* to that rcParam rather than
        # overriding it -- the rcParam itself has to be cleared.
        matplotlib.rcParams["savefig.bbox"] = None
        bb = {}
    else:
        bb = dict(bbox_inches="tight")
    fig.savefig(out, dpi=200, **bb)
    fig.savefig(out[:-4] + ".pdf", **bb)
    print(f"\nwrote {out}\n      {out[:-4]}.pdf")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cxd", default=f"{N}/felt_joined_iso1_seq.csv")
    ap.add_argument("--cylon-tag", default="znand3us")
    ap.add_argument("--cylon-ch-xfer", type=float, default=0.0,
                    help="ns of channel transfer Cylon was configured with "
                         "(ch_xfer_lat); >0 splits its device band like CXDVirt's")
    ap.add_argument("--cylon-body-tag", default=None,
                    help="tag for full_<tag>_seq.csv if it differs from --cylon-tag "
                         "(modelled mode only; --cylon-measured never reads it)")
    ap.add_argument("--cylon-measured", action="store_true",
                    help="build Cylon's bars from the same-run _gtsc capture, "
                         "per-percentile, no rescale (see cylon.__doc__)")
    ap.add_argument("--cylon-all-vcpus", action="store_true",
                    help="join exit windows from every vCPU (the old join), not "
                         "only the benchmark's")
    ap.add_argument("--cxd-read-pass", action="store_true",
                    help="CXDVirt from its second (read-only, no write-back) pass, "
                         "evict kept as emulation overhead (see cxdvirt.__doc__)")
    ap.add_argument("--cxd-measured", action="store_true",
                    help="build CXDVirt's bars from per-percentile band-means "
                         "with no rescale (see cxdvirt.__doc__)")
    ap.add_argument("--paper-width", type=float, default=None,
                    help="design at this printed width in inches (print-size fonts)")
    ap.add_argument("--out",
                    default="/mnt/nvme/cxdvirt/cxdvirt/reproduction/figures/felt_cmp_3us.png")
    a = ap.parse_args()

    crow, cfelt = cylon(a.cylon_tag, a.cylon_body_tag, a.cylon_ch_xfer, a.cylon_measured,
                         a.cylon_all_vcpus)
    xrow, xfelt = cxdvirt(a.cxd, a.cxd_measured, a.cxd_read_pass)
    how = "read-only pass" if a.cxd_read_pass else "evict excluded"
    print(f"Cylon   3us : {len(cfelt):,} misses   felt p50 {np.median(cfelt):.2f} us")
    print(f"CXDVirt 3us : {len(xfelt):,} misses   felt p50 {np.median(xfelt):.2f} us"
          f"   ({how})")
    report("Cylon 3us", crow)
    report(f"CXDVirt 3us ({how})", xrow)
    # with Cylon's channel stage enabled, "channel transfer" is no longer a
    # CXDVirt-only segment -- it belongs under "both"
    groups = (CYLON_ONLY, CXD_ONLY, BOTH)
    if a.cylon_ch_xfer > 0:
        groups = (CYLON_ONLY,
                  [g for g in CXD_ONLY if g != "channel transfer"],
                  ["FTL+NAND", "channel transfer", "re-entry gap"])
    if a.cylon_measured:
        # primary-fill stall is identically zero once each side's body is its
        # own per-access measurement, so drop it from the legend rather than
        # advertise a band that is never drawn.  (It is also unreachable in
        # these single-threaded captures: the FTL service stage is flat to
        # +0.02 us from p50 to p99.9, i.e. no poller contention occurs.)
        groups = tuple([g for g in grp if g != "primary-fill stall"]
                       for grp in groups)
    draw([("Cylon", crow), ("CXDVirt", xrow)], a.out, groups, a.paper_width)


if __name__ == "__main__":
    main()
