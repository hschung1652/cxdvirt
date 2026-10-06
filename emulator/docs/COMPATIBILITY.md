# Mapping contract and failure modes

File:line citations refer to the module source at `nvmevirt/`.

Violations below generally produce results that look reasonable and are
incorrect, rather than errors.

## Mapping

**Windows at any offset are supported, in 4 KiB mode.** The fault handler
derives the device page from the VMA offset plus `vm_pgoff`
(`cxl_page_fault.c:463`), so a program may map the whole device or several
windows of it, from one process or several. 2 MiB mode supports only a
whole-device mapping from offset 0 and warns otherwise.

**The mapping length must be a multiple of the device alignment**, 2 MiB by
default, even though the module tracks 4 KiB pages. devdax validates the whole
VMA:

    device_dax dax0.0: dax_mmap: fail, unaligned vma (0x78fbcce00000 - 0x78fdbff00000, 0x1fffff)
    NVMeVirt: Original DAX mmap failed: -22

Read the alignment from `/sys/bus/dax/devices/dax0.0/align` and round up. This
arises whenever an odd number of MB is used to derive a working-set size; the
4914 MB cache default is such a value.

**At most 16 mappings are tracked.** `CXL_MAX_MMAP_ENTRIES` is 16
(`nvmev.h:453`). Eviction consults that registry to find PTEs to shoot down. A
seventeenth mmap still succeeds but is not tracked (the module logs
`Too many mmap callers`), so pages evicted behind it stay mapped and continue to
be read at DRAM speed.

**Do not fork while mapped.** `cxl_dax_vm_ops` has no `.open`
(`cxl_dax_hook.c:93`), so a forked child's copy of the VMA is unknown to the
module and its pages are never shot down. `daxmalloc` aborts the child;
`DAXMALLOC_FORK=warn` logs instead.

## daxmalloc

`daxmalloc` is an `LD_PRELOAD` interposer serving the malloc family from one
`MAP_SHARED` mapping of the device. Only the heap is relocated; binary, stack,
libc and page cache remain in host DRAM.

[`tools/daxmalloc/README.md`](../tools/daxmalloc/README.md) is the full
reference: the `DAXMALLOC_*` environment variables, the build flags, and the
validation battery. Two points from it bear on correctness here. Scope the
preload with `env` rather than `export LD_PRELOAD=`, which otherwise leaks the
shim into every later child process. And size a run with `DAXMALLOC_STATS=1`
against a plain file before committing it to the device, since exceeding
capacity surfaces as an allocation failure rather than a diagnosable error.

**The usable arena is the device minus the cache.** The DRAM-cache region is
withheld from the front of the device:

    daxmalloc: withholding 4914 MiB of DRAM-cache region; usable NAND window 93134 MiB

Changing `DRAM_CACHE_MB` therefore shifts the arena base, and a deterministic
allocator lands on different device pages at different cache sizes.

| | Behaviour | Indication |
|---|---|---|
| `fork()` while mapped | child aborts | abort banner |
| Two preloaded processes on one device | second refused | `flock` → "device busy" |
| Working set larger than the device | `NULL`/`ENOMEM` | `bad_alloc` or OOM abort |
| Static or setuid binaries | preload ignored | banner absent |
| `malloc_trim` / `mallinfo` / `mallopt` | act on glibc's empty arena | not interposed by design |
| `page_granularity=2m` | init refuses under `REQUIRE` | offset 0 holds module metadata |

`DAXMALLOC_REQUIRE=1`, which `cxdvirt run` sets, aborts if the device cannot be
used. Without it an incorrect device path yields an ordinary DRAM run.

RSS reads as almost nothing: devdax pages are `VM_PFNMAP` and never appear in
`/proc/self/stat`. Redis's `used_memory` remains exact;
`used_memory_rss` and `mem_fragmentation_ratio` do not apply.

## Device state

The backing store is a physical carve-out the module maps rather than allocates,
so it survives `rmmod`/`insmod`.

* A repeated run finds its pages already programmed and incurs none of the
  first-touch write misses the first run did. Two runs sharing a module load are
  not comparable.
* `malloc` does not return zeroed memory; the arena holds the previous run's
  bytes. SPLASH assumes zeroed allocations. `ZERO_ON_INIT=true` avoids this at
  15–27 s per load over 96 GiB.

## Cores

The device's I/O workers are pinned to the cores named by `CPUS` (default
20–23). Those cores should be covered by `isolcpus` and left free of workload
threads. The boot line is in [`kernel/README.md`](../kernel/README.md).

## Out of scope

2 MiB page granularity (`page_granularity=2m`, `layer_size`, `nr_ranks`) is
unexercised in this release; `daxmalloc` refuses it and the selftest assumes
4 KiB.
