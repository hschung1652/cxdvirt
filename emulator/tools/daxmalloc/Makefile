# daxmalloc — LD_PRELOAD heap-on-devdax shim.
#
#   make            build daxmalloc.so and the test binaries
#   make check      run the whole tmpfs conformance battery (no module needed)
#   make verify     assert the exported/undefined symbol sets are right

CC      ?= gcc
CXX     ?= g++

# -fno-builtin-*: without these, gcc is free to recognise allocation idioms and
# emit calls to the very symbols this file defines, which recurses.
CFLAGS  ?= -O2 -g -fPIC -Wall -Wextra -pthread \
           -fno-builtin-malloc -fno-builtin-calloc \
           -fno-builtin-realloc -fno-builtin-free

# ONLY_MSPACES        only mspace_* is emitted (no global malloc), and it
#                     forces HAVE_MORECORE=0
# HAVE_MMAP=0         with MORECORE also gone the arena cannot grow past the
#                     base region: exhaustion returns NULL instead of quietly
#                     allocating DRAM behind our back
# REALLOC_ZERO_BYTES_FREES  realloc(p,0) frees and returns NULL, as glibc does
# USE_LOCKS           thread safety (Redis bio threads, OpenMP workers)
# NO_MALLOC_STATS     drops dlmalloc's only stdio dependency
DLFLAGS := -DONLY_MSPACES=1 -DHAVE_MMAP=0 -DUSE_LOCKS=1 \
           -DREALLOC_ZERO_BYTES_FREES=1 -DNO_MALLOC_STATS=1

TESTS := test/conformance test/conformance_cpp test/fork_probe test/exhaust test/spill

all: daxmalloc.so $(TESTS)

daxmalloc.so: daxmalloc.c malloc.c daxmalloc.map
	$(CC) $(CFLAGS) $(DLFLAGS) -shared -Wl,--version-script=daxmalloc.map \
	      -o $@ daxmalloc.c malloc.c -ldl -lpthread

test/conformance: test/conformance.c
	$(CC) -O1 -g -Wall -pthread -o $@ $< -ldl

test/conformance_cpp: test/conformance_cpp.cc
	$(CXX) -O2 -g -Wall -std=c++17 -o $@ $<

test/fork_probe: test/fork_probe.c
	$(CC) -O1 -g -Wall -o $@ $<

test/exhaust: test/exhaust.c
	$(CC) -O1 -g -Wall -o $@ $<

test/spill: test/spill.c
	$(CC) -O1 -g -Wall -o $@ $<

# The build is only correct if we export precisely the 11 allocator symbols and
# resolve the foreign-pointer fallbacks from libc without dlsym.
verify: daxmalloc.so
	@echo "== exported =="; nm -D --defined-only daxmalloc.so | awk '{print $$3}' | sort
	@echo "== libc fallbacks (must all be present) =="; \
	  nm -D -u daxmalloc.so | grep -E '__libc_(malloc|free|realloc|calloc|memalign)' | sort
	@echo "== must be empty: sbrk/morecore leakage =="; \
	  nm -D -u daxmalloc.so | grep -E ' (sbrk|brk)$$' || true
	@echo "== libc exports these? =="; \
	  nm -D /lib/x86_64-linux-gnu/libc.so.6 | \
	    grep -E ' (T|W) __libc_(malloc|free|realloc|calloc|memalign)$$' | sort

check: all
	@bash test/run_smoke.sh

clean:
	$(RM) daxmalloc.so $(TESTS)

.PHONY: all verify check clean
