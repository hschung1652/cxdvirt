/*
 * conformance_cpp.cc — the C++ side of the contract.
 *
 * GAPBS allocates its CSR arrays with `new T[n]` (pvector.h), so what matters
 * is that libstdc++'s operator new funnels into the interposed malloc, that
 * the throwing/nothrow/aligned variants all behave, and that a destructor
 * running after main() can still free into the arena.
 */
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <new>
#include <string>
#include <vector>

extern "C" size_t malloc_usable_size(void *);

static uintptr_t g_lo, g_hi;
static int g_pass, g_fail;

static void ok(bool cond, const char *what)
{
	if (cond) {
		g_pass++;
	} else {
		g_fail++;
		std::printf("  FAIL: %s\n", what);
	}
}

static bool in_region(const void *p)
{
	uintptr_t a = (uintptr_t)p;
	return p && g_lo && a >= g_lo && a < g_hi;
}

static void ok_in(const void *p, const char *what)
{
	if (!in_region(p)) {
		g_fail++;
		std::printf("  FAIL: %s -> %p outside [%#lx,%#lx)\n", what, p,
			    (unsigned long)g_lo, (unsigned long)g_hi);
	} else {
		g_pass++;
	}
}

static int find_region(const char *backing)
{
	char line[512];
	FILE *f = std::fopen("/proc/self/maps", "r");

	if (!f)
		return -1;
	while (std::fgets(line, sizeof(line), f)) {
		unsigned long lo, hi;

		if (!std::strstr(line, backing))
			continue;
		if (std::sscanf(line, "%lx-%lx", &lo, &hi) != 2)
			continue;
		if (hi - lo < (1u << 20))
			continue;
		if (!g_lo || lo < g_lo)
			g_lo = lo;
		if (hi > g_hi)
			g_hi = hi;
	}
	std::fclose(f);
	return g_lo ? 0 : -1;
}

struct alignas(64) Over {
	char pad[64];
};

/* Frees after main() returns, when the arena must still be mapped. */
struct LateDtor {
	int *p;
	LateDtor() : p(new int[1024]) {}
	~LateDtor() { delete[] p; }
};
static LateDtor g_late;

int main()
{
	const char *backing = std::getenv("DAXMALLOC_PATH");

	if (!backing || !*backing)
		backing = "/dev/dax0.0";
	if (find_region(backing) != 0) {
		std::fprintf(stderr, "conformance_cpp: shim not loaded for %s\n",
			     backing);
		return 2;
	}
	std::printf("region: [%#lx, %#lx)  backing=%s\n\n", (unsigned long)g_lo,
		    (unsigned long)g_hi, backing);

	ok_in(g_late.p, "static-object new[] (ran before main)");

	int *a = new int[1 << 20];
	ok_in(a, "new int[1<<20]");
	a[0] = 1;
	a[(1 << 20) - 1] = 2;
	delete[] a;

	{
		std::vector<int> v;
		for (int i = 0; i < 10'000'000; i++)
			v.push_back(i);
		ok_in(v.data(), "std::vector grown to 10M ints");
		ok(v[9'999'999] == 9'999'999, "vector contents intact");
	}

	{
		std::string s(1 << 20, 'x');
		ok_in(s.data(), "std::string 1MiB");
	}

	{
		Over *o = new Over;
		ok_in(o, "aligned new (alignas(64))");
		ok(((uintptr_t)o & 63) == 0, "aligned new is 64-aligned");
		delete o;
	}

	{
		/* Impossible size: nothrow must yield nullptr, throwing must
		 * throw -- i.e. our ENOMEM-on-NULL contract reaches libstdc++. */
		void *n = ::operator new(SIZE_MAX / 2, std::nothrow);
		ok(n == nullptr, "nothrow new of impossible size -> nullptr");

		bool threw = false;
		try {
			void *t = ::operator new(SIZE_MAX / 2);
			(void)t;
		} catch (const std::bad_alloc &) {
			threw = true;
		}
		ok(threw, "throwing new of impossible size -> std::bad_alloc");
	}

	std::printf("\n%d passed, %d failed\n", g_pass, g_fail);
	return g_fail ? 1 : 0;
}
