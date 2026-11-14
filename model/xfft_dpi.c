// Shim around AMD's bit-accurate C model of the xfft 9.x core, for the C engine (model/xfft_engine.c, compiled
// into the same library by scripts/build_engine.py). Plain C types only: int, long long, double.
// The C model's header and API carry the IP version in their names (xfft_v9_1_bitacc_cmodel.h,
// xilinx_ip_xfft_v9_1_*). scripts/build_engine.py writes xfft_cmodel_api.h, which includes the header of the
// installation's archive and defines XFFT_API_PREFIX; XFFT(name) is the versioned symbol or struct tag.
// One handle per core instance. It holds the input frame being loaded and `slots` result frames; the engine
// decides which slot a frame goes to and when a slot is free again.
#include <math.h>
#include <stdio.h>
#include <stdlib.h>

#include "xfft_cmodel_api.h"

#define XFFT_CAT2(a, b) a##b
#define XFFT_CAT(a, b) XFFT_CAT2(a, b)
#define XFFT(name) XFFT_CAT(XFFT_API_PREFIX, name)

typedef struct {
    struct XFFT(state)* state;
    int nfft;
    int points;
    int width;
    int slots;
    double* in_re;
    double* in_im;
    double* out_re;     // slots * points
    double* out_im;
} xfft_dpi_t;

// Pipelined streaming core with fixed scaling (C_ARCH = 3, C_HAS_SCALING = 1, C_HAS_BFP = 0), as
// ip/xfft_core.tcl configures it. rounding: 1 = convergent, 0 = truncation. Output width = input width.
void* xfft_dpi_new(int nfft, int input_width, int twiddle_width, int rounding, int slots)
{
    struct XFFT(generics) g = XFFT(get_default_generics)();
    g.C_NFFT_MAX = nfft;
    g.C_ARCH = 3;
    g.C_HAS_NFFT = 0;
    g.C_USE_FLT_PT = 0;
    g.C_INPUT_WIDTH = input_width;
    g.C_TWIDDLE_WIDTH = twiddle_width;
    g.C_HAS_SCALING = 1;
    g.C_HAS_BFP = 0;
    g.C_HAS_ROUNDING = rounding;
    g.C_NSSR = 1;
    g.C_SYSTOLICFFT_INV = 0;

    xfft_dpi_t* h = calloc(1, sizeof(*h));
    if (!h)
        return NULL;
    h->state = XFFT(create_state)(g);
    if (!h->state) {
        fprintf(stderr, "xfft_dpi: the C model's create_state failed\n");
        free(h);
        return NULL;
    }
    h->nfft = nfft;
    h->points = 1 << nfft;
    h->width = input_width;
    h->slots = slots;
    h->in_re = calloc(h->points, sizeof(double));
    h->in_im = calloc(h->points, sizeof(double));
    h->out_re = calloc((size_t)slots * h->points, sizeof(double));
    h->out_im = calloc((size_t)slots * h->points, sizeof(double));
    if (!h->in_re || !h->in_im || !h->out_re || !h->out_im) {
        fprintf(stderr, "xfft_dpi: out of memory\n");
        return NULL;
    }
    return h;
}

void xfft_dpi_free(void* handle)
{
    xfft_dpi_t* h = handle;
    if (!h)
        return;
    XFFT(destroy_state)(h->state);
    free(h->in_re);
    free(h->in_im);
    free(h->out_re);
    free(h->out_im);
    free(h);
}

// One input sample (two's complement integers of input_width bits), scaled to -1.0 <= x < 1.0 as the model
// wants. The division by a power of two is exact, so the data is already quantised to the core's input.
void xfft_dpi_put(void* handle, int index, long long re, long long im)
{
    xfft_dpi_t* h = handle;
    const double lsb = ldexp(1.0, -(h->width - 1));
    h->in_re[index] = (double)re * lsb;
    h->in_im[index] = (double)im * lsb;
}

// Transform the loaded frame into result slot `slot`. fwd: 1 = forward, 0 = inverse (FWD_INV of the core).
// scale_sch: the core's SCALE_SCH field, two bits per stage, stage 0 in bits 1:0.
// Returns the model's overflow flag (0 or 1), or -1 if the model fails or gives an output off the grid.
int xfft_dpi_run(void* handle, int slot, int fwd, int scale_sch)
{
    xfft_dpi_t* h = handle;
    const int stages = (h->nfft + 1) / 2;
    int sch[8];
    for (int s = 0; s < stages; s++)
        sch[s] = (scale_sch >> (2 * s)) & 3;

    struct XFFT(inputs) in;
    in.nfft = h->nfft;
    in.xn_re = h->in_re;
    in.xn_re_size = h->points;
    in.xn_im = h->in_im;
    in.xn_im_size = h->points;
    in.scaling_sch = sch;
    in.scaling_sch_size = stages;
    in.direction = fwd ? 1 : 0;

    struct XFFT(outputs) out;
    out.xk_re = h->out_re + (size_t)slot * h->points;
    out.xk_re_size = h->points;
    out.xk_im = h->out_im + (size_t)slot * h->points;
    out.xk_im_size = h->points;
    out.blk_exp = 0;
    out.overflow = 0;

    if (XFFT(bitacc_simulate)(h->state, in, &out) != 0) {
        fprintf(stderr, "xfft_dpi: the C model's bitacc_simulate failed\n");
        return -1;
    }
    if (out.xk_re_size != h->points || out.xk_im_size != h->points) {
        fprintf(stderr, "xfft_dpi: model returned %d / %d points, expected %d\n", out.xk_re_size, out.xk_im_size,
                h->points);
        return -1;
    }
    // Outputs must be whole LSBs of the output width
    const double scale = ldexp(1.0, h->width - 1);
    for (int k = 0; k < h->points; k++) {
        double re = out.xk_re[k] * scale, im = out.xk_im[k] * scale;
        if (re != floor(re) || im != floor(im)) {
            fprintf(stderr, "xfft_dpi: output %d is not on the %d-bit grid (%.6f, %.6f)\n", k, h->width, re, im);
            return -1;
        }
    }
    return out.overflow ? 1 : 0;
}

// Result sample `index` of slot `slot`, as two's complement integers of the output width
long long xfft_dpi_get_re(void* handle, int slot, int index)
{
    xfft_dpi_t* h = handle;
    return llround(h->out_re[(size_t)slot * h->points + index] * ldexp(1.0, h->width - 1));
}

long long xfft_dpi_get_im(void* handle, int slot, int index)
{
    xfft_dpi_t* h = handle;
    return llround(h->out_im[(size_t)slot * h->points + index] * ldexp(1.0, h->width - 1));
}
