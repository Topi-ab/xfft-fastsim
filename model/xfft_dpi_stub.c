// Timing-only replacement for model/xfft_dpi.c (scripts/build_engine.py --timing-only): the same functions without
// AMD's C model. The engine's timing does not depend on the data, so these do nothing: every output sample is 0
// and no frame overflows. Needs no Vivado and no generated core: for timing tests where the data does not matter
// (CI).
static int dummy;

void* xfft_dpi_new(int nfft, int input_width, int twiddle_width, int rounding, int slots)
{
    (void)nfft;
    (void)input_width;
    (void)twiddle_width;
    (void)rounding;
    (void)slots;
    return &dummy;
}

void xfft_dpi_free(void* handle) { (void)handle; }

void xfft_dpi_put(void* handle, int index, long long re, long long im)
{
    (void)handle;
    (void)index;
    (void)re;
    (void)im;
}

int xfft_dpi_run(void* handle, int slot, int fwd, int scale_sch)
{
    (void)handle;
    (void)slot;
    (void)fwd;
    (void)scale_sch;
    return 0;
}

long long xfft_dpi_get_re(void* handle, int slot, int index)
{
    (void)handle;
    (void)slot;
    (void)index;
    return 0;
}

long long xfft_dpi_get_im(void* handle, int slot, int index)
{
    (void)handle;
    (void)slot;
    (void)index;
    return 0;
}
