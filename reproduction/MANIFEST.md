# Manifest

Each figure is listed with the command that draws it, the targets that produce
its data, and the reference files under `plots/data/` from which the published
figure is drawn (`make_figures.sh --reference`).  A target writes the same files,
with its own timestamps, under the same relative path in `results/`.  Paths are
relative to `reproduction/`.

Figs. 1 and 6 are drawn from per-access traces, which are not included (about
310 MB compressed for the paper's runs); their targets regenerate them, and the
files are listed under the paths the targets write in `results/`.

## Fig. 1 — Cylon miss latency by percentile

    ./scripts/make_figures.sh fig1
    PROFILE=3us  ./scripts/run_experiment.sh fig1-cylon-breakdown
    PROFILE=40us ./scripts/run_experiment.sh fig1-cylon-breakdown

| File | Contents |
|---|---|
| `results/cylon/guestfelt_znand3us_parity_4914_chase_gtsc.txt` | per-access latency measured in the guest (`rdtscp`) |
| `results/cylon/kvmwin_znand3us_parity_4914_chase_gtsc.csv` | host VM-exit windows from `/proc/kvm_optb` |
| `results/cylon/full_znand3us_parity_4914_chase_gtsc.csv` | FEMU per-request stage times joined to the exit windows |
| the same three files for `stock40us_parity_4914_chase` | 40 µs NAND read profile |

Workload: MIO pointer chase (`bench_4096_tsc_chase -C`, one node per 4 KiB page),
7400 MB, one thread, two timed passes; Cylon buffer 4915 MB (1.5× WSS); launched
with `CYLON_WB_OFF=1`.  `parity` denotes the launch script that charges the
3232 ns channel transfer per 4 KiB page, as CXDVirt does.  Host windows are
joined to guest accesses per access, using only the benchmark's vCPU; each bar
segment is the mean over the accesses within ±0.1% of that percentile.

## Fig. 5 — access-latency CDF, MIO pointer chase

    ./scripts/make_figures.sh fig5
    sudo ./scripts/run_experiment.sh fig5-mio
    ./scripts/run_experiment.sh fig5-mio-cylon

| File | Contents |
|---|---|
| `plots/data/fig5_4914/cxdvirt/c4914_d64/{1,8}thr_{seq,rnd}.lat.gz` | CXDVirt, per-access latency (ns, sorted) |
| `plots/data/fig5_4914/cylon/{1thr_seq,1thr_rnd,8thr_seq}.lat.gz` | Cylon, the same |
| `plots/data/fig5_4914/cylon_8thr_rnd_run3/8thr_rnd.lat.gz` | Cylon, 8 threads, random: the capture plotted in the paper |
| `…/meta.*` | module or VM configuration |

The targets also write MIO's own output, `{1,8}thr_{seq,rnd}.txt` (one latency
per line), and for Cylon `8thr_rnd.optb.csv`, the per-request records from which
`plots/tools/turns_cylon.py` computes the lock hand-off statistics quoted in the
text.  Neither is included for the paper's runs.

Workload: `bench512_W -tN -r1 -i1 -I1 -T0 [-R] -C -c 0 -m M -P1` (512 B nodes,
eight per page), `-m 8390` at one thread (1.7× WSS) and `-m 1534` per thread at
eight threads (2.5× WSS).  CXDVirt: 4914 MB cache, CLOCK without the hardware
Accessed bit, slow drain (64 per 10 ms).  Cylon: launch defaults (4915 MB buffer,
write-back charged).  `scripts/build_mio.sh` rebuilds `bench512_W` to the md5
recorded in each `meta.*`.

## Fig. 6 — CXDVirt and Cylon miss latency by percentile

    ./scripts/make_figures.sh fig6
    sudo ./scripts/run_experiment.sh fig6-felt-cxdvirt
    PROFILE=3us ./scripts/run_experiment.sh fig1-cylon-breakdown

| File | Contents |
|---|---|
| `results/nvmev/felt_joined_iso1_4914_chase_seq.csv` | CXDVirt per-access latency joined to the fault handler's stage records |
| `results/nvmev/optb4k_felt_iso1_4914_chase_seq.csv` | fault-handler stage records |
| `results/cylon/*_znand3us_parity_4914_chase_*` | Cylon: the 3 µs capture of Fig. 1 |

Workload: `felt_probe` pointer chase over 7400 MB on a 4914 MB cache (1.5× WSS),
one thread, pinned to core 24; one unrecorded write pass followed by two recorded
read passes.  CXDVirt is drawn from the second recorded pass, which evicts only
clean pages, matching Cylon's `CYLON_WB_OFF=1` capture.  The module is loaded
with `OPTB4K_MAX=8388608` to hold the 5.7M records.

## Fig. 7 — OCEAN and Redis

    ./scripts/make_figures.sh fig7

`plots/tools/extract_fig7.py` parses every plotted value from the run files:
OCEAN initialization and solve times from the patched Splash-4 output; Redis mean
and P99 latency of `HGETALL` from the server's `INFO commandstats` and
`INFO latencystats`, run phase only.  Against `results/`, runs are selected by
file pattern (most recent first); against `plots/data/`, by the names below.
`plots/tools/fig7_numbers.py` prints each number quoted in the text beside the
published value.

The per-miss overhead in the text is computed at 2.2× WSS as
(T − T_remote − fetches · c − inline write-back) / misses, for three
values of c, the wall-clock cost of one NAND read: measured, (t_R +
transfer) / 8, and 0.  The measured value is derived from the mode-0 control
(`fig7a-ocean-mode0`: `cxl_wr_alloc=0` at 2.2× WSS) against the mode-2 runs.

### Remote-DRAM lines

    ./scripts/run_experiment.sh fig7-native

Each workload runs as on CXDVirt (node-0 CPUs, `--membind 0`, `daxmalloc`
preloaded) with a `/dev/shm` file pre-faulted on node 1 in place of
`/dev/dax0.0`, on Linux 6.18.5 with the module unloaded; each run verifies that
every heap page is on node 1.  Three runs of the 0.35× point of each workload.

### Fig. 7(a) — Splash-4 OCEAN

    sudo ./scripts/run_experiment.sh fig7a-ocean
    sudo ./scripts/run_experiment.sh fig7a-ocean-mode0
    ./scripts/run_experiment.sh fig7a-ocean-cylon

| File | Contents |
|---|---|
| `plots/data/ocean_wss_s4atomic/0826_143705_m2_c{40578,12912,6456}_p8_r1.*` | CXDVirt, run 1 |
| `plots/data/ocean_wss_s4/0826_150859_m2_c{40578,12912,6456}_p8_r1.*` | CXDVirt, run 2 |
| `plots/data/ocean_wss_cylon_s4/cylon_n8194_p8_c{40578,12912,6456}.*` | Cylon |
| `plots/data/ocean_native/k6185_s4_heap_r{1,2,3}.out` | remote-DRAM line |
| `plots/data/ocean_wss/0825_203317_m2_c6456_p8_r1.*`, `…/0826_115431_m0_c6456_p8_r1.*` | mode-2 / mode-0 pair (Splash-3) from which the published fetch cost (1.726 µs) is derived |

Workload: OCEAN `-n8194 -p8` (14190.85 MiB).  The DRAM cache sets the WSS:
40578 MB (0.35×), 12912 MB (1.1×), 6456 MB (2.2×).  CXDVirt: read-allocate
(`cxl_wr_alloc=2`), CLOCK without the hardware Accessed bit, drain 4096 per 1 ms,
carve-out zeroed at load.  Each point's `.setup` and `.meta` record the module
parameters, `.run` holds OCEAN's output and `.nvmev` the device counters.

### Fig. 7(b) — Redis YCSB-C

    sudo ./scripts/run_experiment.sh fig7b-redis
    ./scripts/run_experiment.sh fig7b-redis-cylon

| File | Contents |
|---|---|
| `plots/data/redis/clockswclk_d0_c4914_{1,3,6}000000_1t_0825_*.{commandstats,latencystats,run.out}` | CXDVirt, server-side latency and YCSB output |
| `plots/data/redis/clockswclk_d0_c4914_*_1t_0825_*.meta` | CXDVirt, module parameters |
| `plots/data/redis/cylon_leg3/cylon_leg3_{commandstats,latencystats,run}_{1,3,6}000000.*` | Cylon |
| `plots/data/redis_native/native_heap_k6.18.5_1000000_1t_0930_*` | remote-DRAM line |

Workload: 1M, 3M and 6M records (0.35×, 1.1×, 2.2× WSS) against a 4914 MB cache,
one client thread, `io-threads 1`.  A re-run of the Cylon side is written to
`results/redis_cylon/cylon_*`, together with FEMU's write-back counters
(`cylon_buffer_*`).

## Fig. 8(a) — eviction policy

    ./scripts/make_figures.sh fig8
    sudo ./scripts/run_experiment.sh fig8a-policy

| File | Contents |
|---|---|
| `plots/data/redis/fig8_4914/policy/{fifo,clock,lifo}_d0_c4914_8000000_1t_*.lat.gz` | per-operation read latency, run phase |
| `…/*.ctr1`, `…/*.ctr2` | device counters after load and after run, including clean and dirty evictions |
| `…/*.meta` | module parameters, record count, threads |

Workload: Redis YCSB-C, 8M records (13,027 MiB heap, 2.65× WSS), 8M reads from one
thread.  All policies use the fast drain (1024 per 1 ms), CLOCK with the hardware
Accessed bit, and a 1024-page LIFO staging window.  Runs are selected by their
`.meta`.

## Fig. 8(b)–(d) — prefetch degree

    ./scripts/make_figures.sh fig8
    sudo ./scripts/run_experiment.sh fig8bcd-prefetch

| File | Contents |
|---|---|
| `plots/data/redis/fig8_4914/prefetch/clock_d{0,1,2,4,8}_c4914_8000000_1t_*` | one run per degree and drain setting |

Workload as in Fig. 8(a) with CLOCK and next-N prefetch, N ∈ {0, 1, 2, 4, 8},
under the slow (64 per 10 ms) and fast (1024 per 1 ms) drain.  Cells run in a
randomized order, one module load each; the prefetch degree is read back from the
module and recorded in each file name and `.meta`.  `REPS` sets the number of
repetitions (default 1).

## Other reference data

| File | Contents |
|---|---|
| `plots/data/ocean_native/k646_s4_{remote,local}_r1.out`, `plots/data/redis_native/native_{remote,local}_*` | earlier remote-DRAM baselines (`numactl --membind`, Linux 6.4.6); not plotted, printed for context by `extract_fig7.py` |
