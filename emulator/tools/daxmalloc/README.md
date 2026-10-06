# daxmalloc — heap-on-devdax preload library

CXDVirt exposes its emulated CXL-SSD only as `/dev/dax0.0`, with no
system-ram/NUMA mode. `daxmalloc.so`, preloaded into a process, maps the device
once and serves the entire malloc family from it, placing an unmodified
program's heap on the device with no source changes.

Only the heap is relocated. The binary, stack, libc and page cache remain in
host DRAM.

## Build

    make            # daxmalloc.so + test binaries
    make verify     # assert the exported and undefined symbol sets
    make check      # tmpfs validation battery; no kernel module required

Requires gcc and glibc. `malloc.c` is Doug Lea's dlmalloc 2.8.6, vendored
unmodified, compiled `ONLY_MSPACES=1 HAVE_MMAP=0` so the arena is fixed: when
the device fills, allocation returns `NULL`/`ENOMEM` rather than spilling onto
host DRAM.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `DAXMALLOC_PATH` | `/dev/dax0.0` | Device, or a regular file for testing. |
| `DAXMALLOC_SIZE` | device size from sysfs | Override the arena size, in bytes. |
| `DAXMALLOC_REQUIRE` | unset | `1` aborts if the device cannot be used. Without it, an unusable path yields an ordinary DRAM run. |
| `DAXMALLOC_VERBOSE` | unset | `1` prints a `daxmalloc: mapped …` banner. |
| `DAXMALLOC_STATS` | unset | `1` prints peak live bytes at exit. dlmalloc's `mspace_footprint` is not usable here; `create_mspace_with_base` presets it to the region size. |
| `DAXMALLOC_FORK` | abort | `warn` logs instead of aborting the forked child. |

## Use

The preload is scoped with `env` so that it applies only to the target process;
an exported `LD_PRELOAD` also applies to every later child process, including
`numactl`, `redis-cli` and a JVM.

    env LD_PRELOAD=./daxmalloc.so DAXMALLOC_REQUIRE=1 ./my_workload

`cxdvirt run -- CMD` does this, with `DAXMALLOC_REQUIRE=1` set.

## Usable capacity is the device minus the DRAM cache

The dax device node spans both the DRAM-cache and NAND regions, so
`/sys/bus/dax/devices/dax0.0/size` reports more than the heap can address. The
fault handler treats a VMA offset as a NAND page index bounded by
`nand_size >> PAGE_SHIFT`, so mapping the full device leaves a tail of
`dram_cache_mb` worth of pages past the valid range:

    page 1926655  last valid       OK
    page 1926656  first invalid    *** SIGBUS ***
    NVMeVirt: 4K page fault beyond range: idx=1926656 max=1926656

dlmalloc allocates upward, so that tail is reached only when the arena is nearly
full, and the resulting SIGBUS is attributed to whichever access reached it.
`daxmalloc` therefore withholds
`dram_cache_mb`, read from `/sys/module/nvmev/parameters/dram_cache_mb`, so the
limit surfaces as `NULL`/`ENOMEM` at the true capacity. The adjustment is
skipped when that parameter is absent, leaving a plain devdax device
unaffected.

Withholding it does not affect hit/miss behaviour: the cache is a residency
budget over the same NAND PFNs, orthogonal to the arena.

`page_granularity=2m` is refused, because offset 0 there holds the module's
shared chunk table. The mode is read from the module parameter, not inferred
from the device's `align` attribute, which is 2 MiB on devdax regardless of
mode.

## Limitations

| | Behaviour | Indication |
|---|---|---|
| `fork()` while mapped | child aborts | abort banner; `DAXMALLOC_FORK=warn` to log instead |
| Two preloaded processes on one device | second refused | `flock` → "device busy" |
| Working set larger than the device | `NULL`/`ENOMEM` | `bad_alloc` or an OOM abort |
| Static or setuid binaries | preload ignored | banner absent |
| `malloc_trim` / `mallinfo` / `mallopt` | act on glibc's empty arena | not interposed by design |
| `page_granularity=2m` | init refuses under `REQUIRE` | offset 0 holds module metadata |

Fork is fatal rather than unsupported: the child's copy of the VMA is unknown to
the module, so eviction shootdowns miss it and the child reads pages at DRAM
speed with no NAND latency charged.

## Redis `INFO memory`

`used_memory` is exact, being fed by `malloc_usable_size`, which the shim
implements against the same chunk headers. `used_memory_rss` comes from
`/proc/self/stat`, and devdax pages are `VM_PFNMAP`, so they never appear in
RSS. Expect a small RSS and `mem_fragmentation_ratio` well below 1. Nothing
depends on those values at these settings.

## Validation

`make check` runs 147 C conformance assertions, 9 C++ assertions, and 15
policy and smoke checks. The load-bearing ones are:

- glibc's own internal allocations (`strdup`, `getline`, `realpath`, `scandir`,
  `open_memstream`, `getaddrinfo`, `glob`) land inside the region, confirming
  that glibc routes its internals through the interposed symbols;
- the program break grows by less than 256 KiB and no new anonymous mapping of
  1 MiB or more appears while 64 MiB is allocated, confirming that nothing fell
  back to the real heap;
- `calloc` returns zeroed memory over a backing store pre-filled with `0xAA`;
- glibc corner cases match: `realloc(p,0)` returns `NULL`, `realloc(NULL,n)`
  allocates in-region, `posix_memalign` follows the `EINVAL` rules without
  touching `errno`, and `ENOMEM` is set on every failure path.

The last requires explicit handling: dlmalloc's `mspace_*` entry points return
`NULL` without running `MALLOC_FAILURE_ACTION`, so `errno` is set in the
wrappers (`oom()` in `daxmalloc.c`).
