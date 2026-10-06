# Device counters

`cxdvirt stats` dumps `/proc/nvmev/debug`. Every value is a counter accumulated
since module load, so a per-phase measurement is the difference between two
snapshots:

    cxdvirt stats > before
    cxdvirt run -- ./my_workload
    cxdvirt stats > after

Fields whose names are also module parameters (`cache_policy`,
`page_granularity`, `clock_hw_young`, `lifo_stage`, `wr_alloc_policy`,
`latency_enabled`) echo the live setting rather than counting anything.

## Cache

| | |
|---|---|
| `cache_hits`, `cache_misses`, `hit_rate` | Hits and misses on the emulated DRAM cache. |
| `dram_occupancy: R/T (wm: L/H)` | Resident pages, budget, and the drain's low and high watermarks. |
| `huge_faults` | 2 MiB faults. Zero in 4 KiB mode. |
| `fault_read_enter`, `fault_write_enter` | Handler entries, each split into CACHED and MISS. |
| `write_protect_faults` | Clean-to-dirty transitions. |

## NAND

| | |
|---|---|
| `nand_reads`, `nand_writes`, `nand_erases` | Operations issued by the FTL. |
| `nand_lat_applied` *(avg N us)* | Misses charged read latency, and the mean charge. |
| `nand_lat_total_ns` | Total read latency charged. |
| `nand_write_lat_applied` | Dirty evictions that paid program latency. |
| `nand_lat_skip_disabled` | Misses not charged: latency modelling off. |
| `nand_lat_skip_noftl` | Misses not charged: no FTL attached to the region. |
| `nand_lat_skip_invalid` | Misses not charged: the page was never programmed. |
| `channel_conflicts`, `die_conflicts` | Resource contention in the channel model. |
| `total_inflight` | Outstanding NAND operations. |

`Reads-miss vs nand_lat_applied` is printed as a line of its own; read misses
equal `nand_lat_applied` plus the three `nand_lat_skip_*` counters.

## Eviction

| | |
|---|---|
| `sync_evicts_4k` | Evictions taken on the fault path. |
| `sync_wb_applied` *(N ms stalled)*, `sync_wb_applied_ns` | Write-backs charged synchronously, and the time spent in them. |
| `victim_probes_4k` *(N/miss)* | Victim-scan probes per miss. |
| `clock_victims_4k`, `fifo_victims_4k`, `lifo_victims_4k` | Victims chosen, by policy. |
| `vskip_second_chance_4k` | Pages granted a CLOCK second chance. |
| `vforced_4k` | Victims taken with no clean candidate available. |
| `zap_failures_4k` | Evictions deferred by `mmap_lock` contention. |
| `evict_sync_fallback_4k` | Async evictions forced synchronous by a full queue. |
| `rmw_reads` *(N ms)* | Read-modify-write fetches under `CXL_WR_ALLOC=1`. |

## Prefetch

| | |
|---|---|
| `pf4k_pushed` | Prefetch triggers enqueued by the fault handler. |
| `pf4k_useful`, `pf4k_wasted`, `pf4k_late` | Prefetches consumed before eviction, evicted unused, and still in flight at the demand touch. |
| `pf4k_accuracy_pct` | `pf4k_useful` as a percentage of prefetches issued. |
| `pf4k_paired_evicts` | Victims evicted to admit a prefetch. |
| `pf4k_dropped_full`, `pf4k_no_victim`, `pf4k_admit_fail` | Prefetches not issued, by reason. |
| `pf4k_random_arm` | Echoes `PREFETCH_RANDOM`. |

## Async I/O

| | |
|---|---|
| `async_loads`, `async_evictions`, `async_prefetches` | Operations handled by the background workers. |

## Mapping

| | |
|---|---|
| `nr_mmap_entries` | Mappings currently tracked. The limit is 16. |
| `zap_no_mmap_entries` | Evictions that ran with no mapping registered. |
| `zap_effective`, `zap_ineffective`, `zap_already_clear` | Outcome of PTE shootdown on eviction. |

A non-zero `zap_no_mmap_entries`, or a rising `zap_ineffective`, indicates a
violation of the mapping contract in [COMPATIBILITY.md](COMPATIBILITY.md).

## Per-access records

`/proc/nvmev/optb4k` emits one record per fault with the handler's internal
stage timestamps. Reading the file dumps it; writing anything to it resets it.
Recording stops at the `optb4k_max` capacity (2,097,152 records by default; see
[PARAMETERS.md](PARAMETERS.md)) rather than wrapping, and the buffer is not
reset between processes sharing a module load.
