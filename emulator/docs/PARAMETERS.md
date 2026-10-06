# Module parameters

Reference for the parameters `bin/setup.sh` and `cxdvirt up` accept as
`KEY=VAL`. `cxdvirt params` prints the same set live from the built module
(`modinfo -p`); `cxdvirt status` prints the values the loaded module is running
with.

**perm** is the sysfs mode. `0444` is settable only at `insmod`, so changing it
requires `cxdvirt down && cxdvirt up KEY=VAL`. `0644` can be written at runtime
under `/sys/module/nvmev/parameters/`.

Defaults below are the artifact's, which differ from the module's own defaults
where `setup.sh` overrides them.

## Geometry

| | perm | default | |
|---|---|---|---|
| `MEMMAP_START` | 0444 | `0x4600000000` | Physical base of the carve-out. Must match `memmap=` on the kernel command line. |
| `MEMMAP_SIZE` | 0444 | `96G` | Size of the carve-out. Also sets the NAND block count, and so the FTL line geometry. |
| `CPUS` | 0444 | `20,21,22,23` | Cores for the device I/O workers. |
| `DRAM_CACHE_MB` | 0444 | `4914` | Emulated in-device DRAM cache, in MB. Must be 2 MiB-aligned. The remainder of the device is emulated NAND. |

## NAND model

| | perm | default | |
|---|---|---|---|
| `NAND_TR_NS` | 0444 | `3000` | 4 KiB read latency, ns. Compiled from the `SAMSUNG_ZNAND` profile and only reported here, so each run records it; `setup.sh` refuses a value, since passing one would change the record and not the model. |
| `NAND_TPROG_NS` | 0444 | `100000` | Program latency, ns. Reported, like `NAND_TR_NS`. |

A serial 4 KiB read costs tR plus the 4 KiB channel transfer: 3000 + 3232 =
6.232 µs on the compiled profile.

## Cache replacement

| | perm | default | |
|---|---|---|---|
| `CACHE_POLICY` | 0444 | `clock` | Eviction policy: `clock`, `fifo` or `lifo`. |
| `CLOCK_HW_YOUNG` | 0644 | `1` | CLOCK second-chance source. `1` = software reference bit plus the hardware PTE Accessed bit; `0` = software bit only. |
| `LIFO_STAGE` | 0444 | `1024` | LIFO only. Number of newest residents held out of the victim pool. `0` disables. |

## Background eviction

| | perm | default | |
|---|---|---|---|
| `BG_DRAIN_4K` | 0444 | `1` | `1` = the dispatcher drains the cache from a 95% high watermark to 85% in batches. `0` = each admission evicts one victim at the hard budget, on the fault path. |
| `BG_DRAIN_BATCH` | 0644 | `1024` | Maximum victims per drain pass. |
| `BG_DRAIN_MS` | 0644 | `1` | Minimum period between passes, ms. `0` drains on every dispatcher iteration. |

Drain throughput ceiling is approximately `BG_DRAIN_BATCH × 1000 / BG_DRAIN_MS`
evictions per second. Demand above it falls to the fault path and appears as
`sync_evicts_4k`.

## Write-miss allocation

| | perm | default | |
|---|---|---|---|
| `CXL_WR_ALLOC` | 0644 | `0` | `0` = never fetch. `1` = defer, using sector valid bits with a read-modify-write merge charged at write-back. `2` = read-allocate, fetching on the miss. Applies to pages faulted after the change. |

## Prefetch

| | perm | default | |
|---|---|---|---|
| `PREFETCH_MODE` | 0444 | `0` | `0` = off, `1` = miss-triggered next-N. |
| `PREFETCH_DEGREE` | 0444 | `0` | N, the number of pages prefetched per miss. |
| `PREFETCH_RANDOM` | 0444 | `0` | `1` prefetches random pages instead of the next N, as a control arm. |

`PREFETCH_DEGREE` is not reported in `/proc/nvmev/debug`; read it from
`/sys/module/nvmev/parameters/prefetch_degree` or `cxdvirt status`.

## Initialization and instrumentation

| | perm | default | |
|---|---|---|---|
| `ZERO_ON_INIT` | 0444 | `false` | Zero the whole carve-out at load. Costs 15–27 s over 96 GiB. |
| `OPTB_TSC` | 0644 | `1` | Stamp `rdtsc` at fault entry and exit for `/proc/nvmev/optb4k`. Costs two serialising reads per fault. |
| `OPTB4K_MAX` | 0444 | `2097152` | Capacity of `/proc/nvmev/optb4k` in records of 48 B, allocated at the first miss. Raise it for a capture of more than 2M misses; `0` disables recording. |

## Settings that affect comparability

Three of the above change what a measurement means, not merely its value.
`BG_DRAIN_4K` moves NAND write-back on or off the request path;
`CLOCK_HW_YOUNG=0` reduces CLOCK to FIFO with a one-sweep grace, since the
software bit is set only on a fault and read hits never fault; `ZERO_ON_INIT`
determines whether a run inherits the previous run's device contents. Any of the
three set differently across arms of a comparison invalidates it. `cxdvirt
status` reports all three.
