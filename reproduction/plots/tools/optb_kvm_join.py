#!/usr/bin/env python3
"""
Join the kvm_exit/kvm_entry ftrace stream with the optb CSV into a per-miss
FULL VM-overhead vector [KVM, QEMU, inbound, service, outbound, total], where
`total` = the full host-side miss window.  Feed the result to optb_plot.py for
the stacked breakdown at every percentile.

Method (no kernel patch):
  * The vCPU thread serializes a miss: kvm_exit(EPT) -> [userspace MMIO] -> kvm_entry.
    So per vCPU, each EPT kvm_exit at x pairs with the next kvm_entry at e:
      host_window = e - x   (full host-side miss: KVM handle + QEMU + FTL + INVEPT/entry)
  * optb records the userspace portion with an ABSOLUTE MMIO-entry time t_mmio_in_ns.
    With trace_clock=mono, trace timestamps and t_mmio_in_ns share CLOCK_MONOTONIC,
    so an optb row joins the interval (x,e) on the same vCPU that CONTAINS t_mmio_in_ns.
  * Then:  QEMU = optb.qemu ;  FTL = optb.total (inbound+service+outbound)
           KVM  = host_window - (QEMU + FTL)        (lumped exit+entry+INVEPT, as in Fig.2)

Caveat: ftrace text timestamps are microsecond-resolution, so host_window (hence KVM)
carries ~us noise; QEMU/FTL come from ns-precise qemu_clock.  For ns-precise KVM use
the kernel-patch route.

Usage:
  optb_kvm_join.py --trace kvm_run1_seq.trace --optb optb_stages_run1_seq.csv \
                   --out full_run1_seq.csv
"""
import argparse, re, bisect, sys
import numpy as np

TS_EV = re.compile(r'\s(\d+\.\d+):\s+(kvm_exit|kvm_entry):\s*(.*)$')
VCPU_TASK = re.compile(r'CPU (\d+)/KVM')
VCPU_FIELD = re.compile(r'vcpu(?:_id)?[ =](\d+)')


def parse_trace(path):
    """vcpu -> sorted list of (ts_ns, ev). vcpu from the 'CPU N/KVM' task name (=cpu_index)."""
    per = {}
    nlines = nmatch = 0
    with open(path, errors="replace") as f:
        for line in f:
            nlines += 1
            m = TS_EV.search(line)
            if not m:
                continue
            vm = VCPU_TASK.search(line) or VCPU_FIELD.search(m.group(3))
            if not vm:
                continue
            ts = int(round(float(m.group(1)) * 1e9))
            per.setdefault(int(vm.group(1)), []).append((ts, m.group(2)))
            nmatch += 1
    for v in per.values():
        v.sort()
    print(f"[trace] {nlines} lines, {nmatch} kvm events, {len(per)} vCPUs", file=sys.stderr)
    return per


def build_intervals_from_csv(path, max_window_ns):
    """Kernel /proc/kvm_optb dump: each line 'vcpu,t_exit_ns,t_entry_ns' is ONE
    pre-paired miss interval (CLOCK_MONOTONIC ns).  No ftrace, no pairing, no
    overflow -> all misses, ns resolution.  vcpu -> (xs[], es[]) sorted by exit."""
    raw, n = {}, 0
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line[0] in "#v":          # skip blank / 'vcpu,...' header
                continue
            p = line.split(",")
            if len(p) < 3:
                continue
            try:
                vcpu, x, e = int(p[0]), int(p[1]), int(p[2])
                # gtsc_exit: the guest TSC at this window's VM-exit.  Carried
                # through so each joined stage row can later be attached to the
                # guest ACCESS that contains it (refault_breakdown.py's felt
                # join), which is what lets the breakdown be measured per
                # percentile instead of frozen at a p50 body.  0 on old dumps.
                g = int(p[3]) if len(p) > 3 else 0
            except ValueError:
                continue
            if e <= x or (e - x) > max_window_ns:     # drop bad / stale-pairing windows
                continue
            raw.setdefault(vcpu, []).append((x, e, g))
            n += 1
    iv = {}
    for vcpu, lst in raw.items():
        lst.sort()
        iv[vcpu] = (np.asarray([a for a, _, _ in lst], dtype=np.int64),
                    np.asarray([b for _, b, _ in lst], dtype=np.int64),
                    np.asarray([g for _, _, g in lst], dtype=np.int64))
    print(f"[kvmcsv] {n} miss intervals, {len(iv)} vCPUs", file=sys.stderr)
    return iv


def build_intervals(per, max_window_ns):
    """vcpu -> (xs[], es[]) clean exit->immediate-entry windows.

    A miss is exit -> next entry, with the entry coming BEFORE the next exit (clean
    alternation).  If the immediate re-entry is missing (e.g. a burst trace-drop on
    ring overflow), pairing the exit with a far-future entry would create a giant
    interval that swallows every miss in the dropped region.  So we require the entry
    to precede the next exit AND the window to be <= max_window_ns; otherwise we skip
    that exit (its misses end up unmatched — correct — rather than mis-assigned)."""
    iv = {}
    for vcpu, evs in per.items():
        exits = [t for t, e in evs if e == "kvm_exit"]
        entries = [t for t, e in evs if e == "kvm_entry"]
        if not exits or not entries:
            continue
        xs, es = [], []
        n = len(exits)
        for k, x in enumerate(exits):
            nx = exits[k + 1] if k + 1 < n else None
            j = bisect.bisect_right(entries, x)          # first entry strictly after x
            if j >= len(entries):
                break
            e = entries[j]
            if nx is not None and e >= nx:
                continue                                  # immediate re-entry missing -> skip
            if e - x > max_window_ns:
                continue                                  # implausible window (gap) -> skip
            xs.append(x); es.append(e)
        if xs:
            # ftrace carries no guest TSC; emit zeros so the tuple shape matches
            # the --kvmcsv path (a 0 gtsc simply won't join to any access).
            iv[vcpu] = (np.asarray(xs, dtype=np.int64), np.asarray(es, dtype=np.int64),
                        np.zeros(len(xs), dtype=np.int64))
    return iv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", help="ftrace text dump (kvm_exit/kvm_entry); µs-resolution, windowed")
    ap.add_argument("--kvmcsv", help="kernel /proc/kvm_optb dump (vcpu,t_exit,t_entry); ns, full coverage")
    ap.add_argument("--optb", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--maxwin", type=float, default=1e6,
                    help="max host_window ns to accept (default 1ms); drops giant "
                         "intervals from ring-overflow trace drops")
    args = ap.parse_args()

    if args.kvmcsv:
        iv = build_intervals_from_csv(args.kvmcsv, int(args.maxwin))   # ns, full coverage (kernel patch)
    elif args.trace:
        iv = build_intervals(parse_trace(args.trace), int(args.maxwin))  # µs ftrace fallback
    else:
        sys.exit("give --kvmcsv (kernel /proc/kvm_optb) or --trace (ftrace)")

    cols = [c.strip() for c in open(args.optb).readline().strip().split(",")]
    ci = {c: i for i, c in enumerate(cols)}
    need = {"vcpu", "t_mmio_in_ns", "qemu", "inbound", "service", "outbound", "total"}
    if need - set(cols):
        sys.exit(f"optb CSV missing columns {need - set(cols)}; rebuild FEMU with the KVM-join schema")
    data = np.loadtxt(args.optb, delimiter=",", skiprows=1, dtype=np.int64)
    if data.ndim == 1:
        data = data.reshape(1, -1)
    vcpu_c = data[:, ci["vcpu"]]
    t_c    = data[:, ci["t_mmio_in_ns"]]
    qemu_c = data[:, ci["qemu"]]
    inb_c, svc_c, out_c = data[:, ci["inbound"]], data[:, ci["service"]], data[:, ci["outbound"]]
    tot_c  = data[:, ci["total"]]
    # t_run_exit_ns (KVM->QEMU boundary) is optional: if present, the QEMU bucket
    # absorbs the accelerator MMIO dispatch (t_mmio_in - t_run_exit) and KVM becomes
    # kernel-only; if absent (old captures), fall back to QEMU = marshalling only.
    has_tre = "t_run_exit_ns" in ci
    tre_c = data[:, ci["t_run_exit_ns"]] if has_tre else np.zeros(len(data), dtype=np.int64)
    print(f"[join] KVM/QEMU split: {'ON (kernel-only KVM)' if has_tre else 'OFF (lumped KVM)'}",
          file=sys.stderr)
    # modeled device (NAND) latency = expire-stime, the post-t_recv vCPU spin (FEMU optb
    # `modeled` column).  Without it the spin is absorbed into the KVM residual; with it we
    # subtract it so KVM is pure VM machinery and NAND is its own stage.  0/absent at NOLAT.
    has_mdl = "modeled" in ci
    mdl_c = data[:, ci["modeled"]] if has_mdl else np.zeros(len(data), dtype=np.int64)
    emit_mdl = has_mdl and bool(np.any(mdl_c > 0))     # add the NAND stage only when latency is on
    if emit_mdl:
        print("[join] modeled NAND latency present -> KVM = VM machinery; NAND emitted as its own stage",
              file=sys.stderr)
    # qemu_out: the QEMU MMIO RETURN path (new FEMU stamp).  Added to QEMU so the symmetric
    # outbound dispatch is no longer charged to the KVM residual.  Absent in old captures.
    has_qo = "qemu_out" in ci
    qo_c = data[:, ci["qemu_out"]] if has_qo else np.zeros(len(data), dtype=np.int64)

    n_total = len(data)
    n_unmatched = n_negkvm = n_nots = 0
    pieces = []
    for vcpu in np.unique(vcpu_c):
        mask = vcpu_c == vcpu
        ivv = iv.get(int(vcpu))
        if ivv is None:
            n_unmatched += int(mask.sum()); continue
        xs, es, gs = ivv
        t = t_c[mask]
        ts_bad = t <= 0
        n_nots += int(ts_bad.sum())
        k = np.searchsorted(xs, t, side="right") - 1            # rightmost x <= t
        kk = np.clip(k, 0, len(xs) - 1)
        contained = (~ts_bad) & (k >= 0) & (xs[kk] < t) & (t < es[kk])
        n_unmatched += int(((~ts_bad) & ~contained).sum())
        if not contained.any():
            continue
        xsel = xs[kk][contained]
        gsel = gs[kk][contained]                                # guest TSC at VM-exit
        host = (es[kk] - xs[kk])[contained]
        tin  = t[contained]                                     # t_mmio_in (absolute)
        tre  = tre_c[mask][contained]                           # t_run_exit (absolute)
        qm   = qemu_c[mask][contained]                          # device marshalling (inbound)
        qo   = qo_c[mask][contained]                            # QEMU MMIO return path (outbound)
        ib, sv, ob, tt = (inb_c[mask][contained], svc_c[mask][contained],
                          out_c[mask][contained], tot_c[mask][contained])
        md = mdl_c[mask][contained]                             # FEMU modeled = spin = expire - t_recv
        # QEMU = inbound MMIO dispatch (t_mmio_in - t_run_exit, when t_run_exit is valid:
        # kvm_exit <= t_run_exit <= t_mmio_in) + device marshalling + outbound return path.
        valid = (tre > 0) & (tre >= xsel) & (tre <= tin)
        dispatch = np.where(valid, tin - tre, 0)
        qemu = dispatch + qm + qo
        # NAND = the delivered spin: FEMU now stamps `modeled = expire - t_recv` directly
        # (exact; excludes the FTL ring and the pre-enqueue prologue), so no ring subtraction.
        nand = np.maximum(0, md)
        kvm = host - qemu - tt - nand
        pos = kvm >= 0
        n_negkvm += int((~pos).sum())                           # join mismatch (coarse ts) — drop
        if not pos.any():
            continue
        kvm, qemu, ib, sv, ob, nand, gsel = (kvm[pos], qemu[pos], ib[pos], sv[pos],
                                             ob[pos], nand[pos], gsel[pos])
        if emit_mdl:                                            # KVM,QEMU,FTL,NAND,total(=host)
            pieces.append(np.column_stack([gsel, kvm, qemu, ib, sv, ob, nand,
                                           kvm + qemu + ib + sv + ob + nand]))
        else:
            pieces.append(np.column_stack([gsel, kvm, qemu, ib, sv, ob,
                                           kvm + qemu + ib + sv + ob]))

    if not pieces:
        sys.exit("no misses joined — check vCPU mapping / trace_clock=mono / timebase")

    arr = np.concatenate(pieces).astype(np.int64)
    # gtsc_exit leads so old readers that index by name still work and the new
    # column is additive rather than positional-breaking.
    hdr = ("gtsc_exit,KVM,QEMU,inbound,service,outbound,modeled,total" if emit_mdl
           else "gtsc_exit,KVM,QEMU,inbound,service,outbound,total")
    np.savetxt(args.out, arr, fmt="%d", delimiter=",", header=hdr, comments="")
    matched = len(arr)
    print(f"[join] {matched}/{n_total} optb rows matched "
          f"({100*matched/n_total:.1f}%); unmatched={n_unmatched}, "
          f"neg-KVM dropped={n_negkvm}, no-ts={n_nots}", file=sys.stderr)
    # quick sanity: median bucket split
    med = np.median(arr, axis=0)
    if emit_mdl:
        print(f"[join] medians ns  KVM={med[0]:.0f} QEMU={med[1]:.0f} "
              f"FTL={med[2]+med[3]+med[4]:.0f} NAND={med[5]:.0f} total={med[6]:.0f}", file=sys.stderr)
    else:
        print(f"[join] medians ns  KVM={med[0]:.0f} QEMU={med[1]:.0f} "
              f"FTL={med[2]+med[3]+med[4]:.0f} total={med[5]:.0f}", file=sys.stderr)
    print(f"wrote {args.out}  ({matched} misses) -> feed to optb_plot.py")


if __name__ == "__main__":
    main()
