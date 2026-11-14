// Cycle engine of the xfft stand-in: the core's timing, one step per rising clock edge. xfft_engine.h has the
// interface; this file is the single source of the timing (the HDL wrappers make no timing decisions).
//
// Semantics of a step:
// - Blocking state (counters, queues, the slot ring) is held in fields of the engine and changes at once.
// - Registered state (the registered outputs and `running`): read with its value from before the edge (`q`)
//   during the step and written into `nx`, which becomes the state at the end of the step.
// - The two tready are functions of the state: inside the step they have their value from before the edge
//   (taken at the start of the step), after the edge the new state's.
// - Unknown input bits (XE_UNKNOWN): `a && b` is true only for a known 1 and a known 1, `!a` only for a known 0;
//   data bits that are unknown are 0. m_axis_data_tvalid and m_axis_status_tvalid are unknown before the first
//   edge (they start as XE_UNKNOWN).
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "xfft_engine.h"
#include "xfft_model.h"

// model/xfft_dpi.c, compiled into the same library (hidden by -fvisibility=hidden)
void* xfft_dpi_new(int nfft, int input_width, int twiddle_width, int rounding, int slots);
void xfft_dpi_free(void* handle);
void xfft_dpi_put(void* handle, int index, long long re, long long im);
int xfft_dpi_run(void* handle, int slot, int fwd, int scale_sch);
long long xfft_dpi_get_re(void* handle, int slot, int index);
long long xfft_dpi_get_im(void* handle, int slot, int index);

#define N XFFT_N
#define W XFFT_INPUT_WIDTH
#define P (XFFT_DATA_WIDTH / 2)         // W padded to bytes: im starts here
#define S XFFT_INPUT_SLACK
#define A XFFT_OUTPUT_HALT_EVENT_AFTER
#define B XFFT_OUTPUT_BUFFER
#define K XFFT_PIPELINE_LOOKAHEAD
#define Q XFFT_OUTPUT_QUEUE_SIZE
// Derived (model/xfft_timing.py): the pipeline input delay
#define D (XFFT_FRAME_STARTED_DELAY - S)
// Pipeline clocks from a frame's first sample entering the pipeline to its first output in the register
#define PIPE (XFFT_LATENCY_FIRST_OUT - S - 2 - D)
// Input FIFO size, the samples on their way to it included
#define FIFO (XFFT_INPUT_LIMIT - PIPE - A - 1)
// Result frames of the C model: complete frames not yet fully output
#define SLOTS (XFFT_INPUT_LIMIT / N + 3)

#if XFFT_DATA_WIDTH > XE_CHUNKS * XE_CHUNK_BITS
#error "xfft_model.h: DATA_WIDTH wider than the engine's data chunks"
#endif

typedef unsigned __int128 word_t;

// The gap machine's modes (input channel)
enum { GAP_RUN, GAP_R1, GAP_R3 };

// Queues: a growable ring of 64-bit values, indexed from the front
typedef struct {
    long long* v;
    size_t head, len, cap;
} queue_t;

static int queue_push(queue_t* q, long long value)
{
    if (q->len == q->cap) {
        size_t cap = q->cap ? 2 * q->cap : 16;
        long long* v = malloc(cap * sizeof(*v));
        if (!v)
            return -1;
        for (size_t i = 0; i < q->len; i++)
            v[i] = q->v[(q->head + i) % q->cap];
        free(q->v);
        q->v = v;
        q->head = 0;
        q->cap = cap;
    }
    q->v[(q->head + q->len) % q->cap] = value;
    q->len++;
    return 0;
}

// Popping an empty queue does nothing
static void queue_pop(queue_t* q)
{
    if (q->len == 0)
        return;
    q->head = (q->head + 1) % q->cap;
    q->len--;
}

static long long* queue_at(const queue_t* q, size_t i) { return &q->v[(q->head + i) % q->cap]; }
static long long queue_front(const queue_t* q) { return *queue_at(q, 0); }
static void queue_clear(queue_t* q) { q->head = q->len = 0; }
static long long queue_size(const queue_t* q) { return (long long)q->len; }

// Registered state: the registered outputs and `running`
typedef struct {
    int running;
    int m_tvalid;               // 0, 1 or XE_UNKNOWN (before the first edge)
    long long m_re, m_im;       // m_axis_data_tdata: the two halves, sign-extended
    int m_tuser;
    int m_tlast;
    int status_tvalid;          // 0, 1 or XE_UNKNOWN (before the first edge)
    int status_tdata;
    int frame_started;
    int tlast_unexpected;
    int tlast_missing;
    int fft_overflow;
    int in_halt;
    int out_halt;
} regs_t;

typedef struct {
    void* h;                    // xfft_dpi handle
    long long cycle;            // index of the current clock edge

    int reset_cnt;              // clocks since aresetn went high, saturating
    unsigned cfg_cur;           // configuration of the frame being loaded / of the next frame
    int cfg_pending;
    unsigned cfg_buf;

    // Input side (handshakes)
    int in_cnt;                 // samples of the frame being loaded
    int boundary_left;          // clocks of s_axis_data_tready low after a frame
    queue_t arrive_q;           // edge at which each accepted sample reaches the FIFO
    queue_t fifo_at;            // FIFO: the edge from which each sample in it can enter the pipeline
    queue_t left_at;            // edges of the last D - K at which a sample left the FIFO for the pipeline
    int in_full;                // s_axis_data_tready low: the FIFO was full, INPUT_RESUME not yet free
    int hs_d;                   // an input handshake at the edge before
    // Input gaps near a full FIFO (see the input channel): the gap machine and the fill points lost
    int gap_mode;               // GAP_RUN, GAP_R1 or GAP_R3
    int gap_credit;             // idle clocks GAP_RUN absorbs
    long long gap_idle;         // clocks without an input handshake since the last one
    int fill_lost;              // fill points lost to gap events at the checkpoints
    long long in_taken;         // input handshakes since reset
    long long out_taken;        // output handshakes since reset
    int entered_d;              // the pipeline took a sample in the clock before
    int starve_d;               // the pipeline starved in the clock before
    // Pipeline
    long long pipe_clk;         // pipeline clock
    long long entered;          // samples that entered the pipeline
    queue_t frame_at;           // pipeline clock at which each frame not yet fully output entered
    int out_j;                  // next output sample of the oldest of them
    int paused;                 // paused for the queue (A samples waited, not yet down to B)
    int out_halt_d;             // event_data_out_channel_halt of the next clock
    // Output queue: the sample in the output register first, then those behind it
    queue_t q_re, q_im, q_user, q_last;
    // Status words on their way: the word, its step (1 and 3: waits for a clock without starving, 2: for a
    // running pipeline clock; 0: done) and the edge from which that step may happen
    queue_t tok_word, tok_step, tok_at;

    // C-model results, ring of SLOTS: wr_slot = next frame to complete, rd_slot = frame in output
    int slot_ovflo[SLOTS];
    int wr_slot, rd_slot;

    queue_t status_q;
    queue_t status_at_q;        // edge from which each status word is offered
    queue_t started_q;          // edges of the pending event_frame_started pulses

    regs_t r;
    int error;
} engine_t;

static engine_t** engines;
static int engine_count;

// `a && b` and `!a` on 0, 1, XE_UNKNOWN; a condition takes XE_UNKNOWN as 0
static int and3(int a, int b)
{
    if (a == 0 || b == 0)
        return 0;
    return a == 1 && b == 1 ? 1 : XE_UNKNOWN;
}

static int not3(int a) { return a == 0 ? 1 : a == 1 ? 0 : XE_UNKNOWN; }
static int is1(int a) { return a == 1; }

// The configuration after reset: forward, SCALE_SCH 2 in every stage (1 in a radix-2 last stage)
static unsigned reset_config(void)
{
    unsigned word = 1;
    for (int s = 0; s < XFFT_STAGES; s++)
        word |= (unsigned)(XFFT_NFFT % 2 && s == XFFT_STAGES - 1 ? 1 : 2) << (1 + 2 * s);
    return word & ((1u << XFFT_CONFIG_WIDTH) - 1);
}

static long long sign_extend(long long v, int bits)
{
    const unsigned long long m = 1ULL << (bits - 1);
    const unsigned long long u = (unsigned long long)v & ((m << 1) - 1);
    return (long long)((u ^ m) - m);
}

static word_t mask(int bits) { return bits >= 128 ? ~(word_t)0 : (((word_t)1) << bits) - 1; }

// A sample of a frame in the pipeline is due at pipeline clock pc
static int due(const engine_t* e, long long pc)
{
    for (size_t k = 0; k < e->frame_at.len; k++) {
        const long long d = pc - *queue_at(&e->frame_at, k) - PIPE;
        if (d >= (k == 0 ? e->out_j : 0) && d < N)
            return 1;
    }
    return 0;
}

static void push(engine_t* e, queue_t* q, long long value)
{
    if (queue_push(q, value)) {
        fprintf(stderr, "xfft_engine: out of memory\n");
        e->error = XE_ERR_ENGINE;
    }
}

XFFT_ENGINE_API int32_t xfft_engine_new(int32_t nfft, int32_t input_width, int32_t twiddle_width,
                                        int32_t convergent, int32_t config_width, int32_t data_width,
                                        int32_t user_width)
{
    if (nfft != XFFT_NFFT || input_width != XFFT_INPUT_WIDTH || twiddle_width != XFFT_TWIDDLE_WIDTH
        || convergent != XFFT_CONVERGENT || config_width != XFFT_CONFIG_WIDTH || data_width != XFFT_DATA_WIDTH
        || user_width != XFFT_USER_WIDTH) {
        fprintf(stderr, "xfft_engine: the HDL's configuration (NFFT %d, INPUT_WIDTH %d, TWIDDLE_WIDTH %d, "
                "CONVERGENT %d, widths %d/%d/%d) is not the one this library was built for (NFFT %d, INPUT_WIDTH %d, "
                "TWIDDLE_WIDTH %d, CONVERGENT %d, widths %d/%d/%d): build it with scripts/build_engine.py for this "
                "configuration\n", nfft, input_width, twiddle_width, convergent, config_width, data_width,
                user_width, XFFT_NFFT, XFFT_INPUT_WIDTH, XFFT_TWIDDLE_WIDTH, XFFT_CONVERGENT, XFFT_CONFIG_WIDTH,
                XFFT_DATA_WIDTH, XFFT_USER_WIDTH);
        return -1;
    }
    // The timing constants must fit this model
    if (!XFFT_HAS_TIMING) {
        fprintf(stderr, "xfft_engine: xfft_model.h has no timing constants for this configuration: measure them "
                "first\n");
        return -1;
    }
    if (XFFT_OUTPUT_STALL_COST != 1 || XFFT_CONFIG_ACCEPT_LOADING != 0 || XFFT_STARVE_HALTS_OUTPUT != 1
        || XFFT_LATENCY_LAST_OUT != XFFT_LATENCY_FIRST_OUT + N - 1 || XFFT_RESET_TO_READY < 1
        || XFFT_STATUS_AFTER_LAST_OUT < 1 || B < 0 || A <= B || K < 1 || Q < K || D < K || PIPE < N
        || XFFT_INPUT_RESUME < 1 || FIFO < XFFT_INPUT_RESUME || FIFO < D + S + 2) {
        fprintf(stderr, "xfft_engine: xfft_model.h does not fit this model (OUTPUT_STALL_COST = 1, "
                "CONFIG_ACCEPT_LOADING = 0, STARVE_HALTS_OUTPUT = 1, LATENCY_LAST_OUT = LATENCY_FIRST_OUT + N - 1, "
                "RESET_TO_READY and STATUS_AFTER_LAST_OUT >= 1, OUTPUT_HALT_EVENT_AFTER > OUTPUT_BUFFER >= 0, "
                "OUTPUT_QUEUE_SIZE >= PIPELINE_LOOKAHEAD >= 1, FRAME_STARTED_DELAY - INPUT_SLACK >= "
                "PIPELINE_LOOKAHEAD (the input side's lag D - K), latency > N, an input FIFO of at least "
                "INPUT_RESUME and the samples in flight)\n");
        return -1;
    }
    engine_t* e = calloc(1, sizeof(*e));
    engine_t** table = realloc(engines, (engine_count + 1) * sizeof(*table));
    if (table)
        engines = table;
    if (!e || !table) {
        fprintf(stderr, "xfft_engine: out of memory\n");
        free(e);
        return -1;
    }
    e->h = xfft_dpi_new(XFFT_NFFT, W, XFFT_TWIDDLE_WIDTH, XFFT_CONVERGENT, SLOTS);
    if (!e->h) {
        fprintf(stderr, "xfft_engine: xfft_dpi_new failed\n");
        free(e);
        return -1;
    }
    // Everything else starts at 0 (cfg_cur too: reset_config() only by reset)
    e->r.m_tvalid = XE_UNKNOWN;
    e->r.status_tvalid = XE_UNKNOWN;
    e->gap_credit = 1;
    engines[engine_count] = e;
    return engine_count++;
}

static void fill_out(const engine_t* e, int32_t* out)
{
    const regs_t* r = &e->r;
    // The combinational outputs, from the state after the edge
    out[XE_CONFIG_TREADY] = r->running && !e->cfg_pending;
    out[XE_DATA_TREADY] = r->running && e->boundary_left == 0 && !e->in_full;
    const word_t word = ((word_t)r->m_im & mask(P)) << P | ((word_t)r->m_re & mask(P));
    for (int c = 0; c < XE_CHUNKS; c++)
        out[XE_M_TDATA_0 + c] = (int32_t)((word >> (XE_CHUNK_BITS * c)) & 0xFFFF);
    out[XE_M_TUSER] = r->m_tuser;
    out[XE_M_TVALID] = r->m_tvalid;
    out[XE_M_TLAST] = r->m_tlast;
    out[XE_STATUS_TDATA] = r->status_tdata;
    out[XE_STATUS_TVALID] = r->status_tvalid;
    out[XE_FRAME_STARTED] = r->frame_started;
    out[XE_TLAST_UNEXPECTED] = r->tlast_unexpected;
    out[XE_TLAST_MISSING] = r->tlast_missing;
    out[XE_FFT_OVERFLOW] = r->fft_overflow;
    out[XE_STATUS_CHANNEL_HALT] = 0;
    out[XE_DATA_IN_CHANNEL_HALT] = r->in_halt;
    out[XE_DATA_OUT_CHANNEL_HALT] = r->out_halt;
    out[XE_ERROR] = e->error;
}

XFFT_ENGINE_API void xfft_engine_step(int32_t id, int32_t aresetn, int32_t config_tvalid, int32_t config_tdata,
                                      int32_t data_tvalid, int32_t d0, int32_t d1, int32_t d2, int32_t d3,
                                      int32_t d4, int32_t data_tlast, int32_t m_data_tready,
                                      int32_t m_status_tready, int32_t* out)
{
    if (id < 0 || id >= engine_count || !engines[id]) {
        fprintf(stderr, "xfft_engine: no engine %d\n", id);
        memset(out, 0, XE_OUT_COUNT * sizeof(*out));
        out[XE_ERROR] = XE_ERR_ENGINE;
        return;
    }
    engine_t* e = engines[id];
    const regs_t q = e->r;      // values from before the edge
    regs_t nx = e->r;           // nonblocking assignments of this edge

    // tready inside the step: from before the edge
    const int config_tready = q.running && !e->cfg_pending;
    const int data_tready = q.running && e->boundary_left == 0 && !e->in_full;

    nx.frame_started = 0;
    nx.tlast_unexpected = 0;
    nx.tlast_missing = 0;
    nx.fft_overflow = 0;
    nx.in_halt = 0;

    // Status channel (values from before this edge)
    const int v_status_stalled = is1(and3(q.status_tvalid, not3(m_status_tready)));
    if (is1(and3(q.status_tvalid, m_status_tready))) {
        queue_pop(&e->status_q);
        queue_pop(&e->status_at_q);
    }

    // Output register; the samples waiting behind it, counted before its handshake
    const long long v_wait = queue_size(&e->q_re) - 1;
    const int v_taken = is1(and3(q.m_tvalid, m_data_tready));
    const int v_stalled = is1(and3(q.m_tvalid, not3(m_data_tready)));
    if (v_taken) {
        e->out_taken++;
        nx.fft_overflow = (q.m_tuser >> XFFT_OVFLO_BIT) & 1;
        queue_pop(&e->q_re);
        queue_pop(&e->q_im);
        queue_pop(&e->q_user);
        queue_pop(&e->q_last);
    }

    // Pipeline: halts when a started frame starves, and while paused for the queue unless one of its next
    // K + 1 clocks has no sample due and the samples due before that one fit into the queue
    if (v_wait >= A)
        e->paused = 1;
    else if (v_wait <= B)
        e->paused = 0;
    const int v_next = e->fifo_at.len > 0 && queue_front(&e->fifo_at) <= e->cycle;
    const int v_starve = q.running && e->entered % N != 0 && !v_next;
    int v_halt = e->paused;
    if (e->paused) {
        int v_due = 0;
        while (v_due <= K && due(e, e->pipe_clk + 1 + v_due))
            v_due++;
        if (v_due <= K && v_wait + v_due <= Q)
            v_halt = 0;
    }
    const int v_run = q.running && !v_halt && !v_starve;
    if (v_starve && e->fifo_at.len == 0)
        nx.in_halt = 1;
    // event_data_out_channel_halt: two clocks after a clock in which the pipeline is paused for a full queue
    // (A samples waiting) with a sample due and the output stalled
    nx.out_halt = e->out_halt_d;
    e->out_halt_d = q.running && v_halt && v_stalled && v_wait >= A && due(e, e->pipe_clk + 1);
    // Status words on their way: a clock without starving, a running pipeline clock, then again a clock
    // without starving
    for (size_t i = 0; i < e->tok_step.len; i++) {
        long long* at = queue_at(&e->tok_at, i);
        long long* step = queue_at(&e->tok_step, i);
        if (*at <= e->cycle && *step == 1 && !v_starve) {
            *step = 2;
            *at = e->cycle + 1;
        } else if (*at <= e->cycle && *step == 2 && v_run) {
            *step = 3;
            *at = e->cycle + 1;
        } else if (*at <= e->cycle && *step == 3 && !v_starve) {
            push(e, &e->status_q, *queue_at(&e->tok_word, i));
            push(e, &e->status_at_q, e->cycle + 1 + XFFT_STATUS_AFTER_LAST_OUT);
            *step = 0;
        }
    }
    while (e->tok_step.len > 0 && queue_front(&e->tok_step) == 0) {
        queue_pop(&e->tok_word);
        queue_pop(&e->tok_step);
        queue_pop(&e->tok_at);
    }
    int v_entered = 0;
    if (v_run) {
        e->pipe_clk++;
        if (v_next) {
            queue_pop(&e->fifo_at);
            if (e->entered % N == 0)
                push(e, &e->frame_at, e->pipe_clk);
            e->entered++;
            v_entered = 1;
        }
        if (e->frame_at.len > 0 && e->pipe_clk == queue_front(&e->frame_at) + PIPE + e->out_j) {
            const long long v_re = xfft_dpi_get_re(e->h, e->rd_slot, e->out_j);
            const long long v_im = xfft_dpi_get_im(e->h, e->rd_slot, e->out_j);
            push(e, &e->q_re, sign_extend(v_re, W));
            push(e, &e->q_im, sign_extend(v_im, W));
            push(e, &e->q_user, (long long)((e->slot_ovflo[e->rd_slot] & 1) << XFFT_OVFLO_BIT
                                            | (e->out_j & ((1 << XFFT_OVFLO_BIT) - 1))));
            push(e, &e->q_last, e->out_j == N - 1);
            // The status logic sees the frame's end K samples ahead
            if (e->out_j == N - 1 - K) {
                push(e, &e->tok_word, e->slot_ovflo[e->rd_slot] & 1);
                push(e, &e->tok_step, 1);
                push(e, &e->tok_at, e->cycle + 1);
            }
            if (e->out_j == N - 1) {
                e->out_j = 0;
                queue_pop(&e->frame_at);
                e->rd_slot = (e->rd_slot + 1) % SLOTS;
            } else
                e->out_j++;
        }
    }

    // Config channel
    if (is1(and3(config_tvalid, config_tready))) {
        e->cfg_buf = (unsigned)config_tdata & ((1u << XFFT_CONFIG_WIDTH) - 1);
        e->cfg_pending = 1;
    }

    // Input channel
    if (e->boundary_left > 0)
        e->boundary_left--;
    const int v_hs = is1(and3(data_tvalid, data_tready));
    if (v_hs) {
        push(e, &e->arrive_q, e->cycle + D);
        if (e->in_cnt == 0) {
            push(e, &e->started_q, e->cycle + XFFT_FRAME_STARTED_DELAY);
            if (e->cfg_pending) {
                e->cfg_cur = e->cfg_buf;
                e->cfg_pending = 0;
            }
        }
        const int32_t chunks[XE_CHUNKS] = {d0, d1, d2, d3, d4};
        word_t word = 0;
        for (int c = XE_CHUNKS - 1; c >= 0; c--)
            word = word << XE_CHUNK_BITS | (word_t)(chunks[c] & 0xFFFF);
        word &= mask(XFFT_DATA_WIDTH);
        xfft_dpi_put(e->h, e->in_cnt, sign_extend((long long)(word & mask(W)), W),
                     sign_extend((long long)((word >> P) & mask(W)), W));
        if (is1(and3(data_tlast, e->in_cnt != N - 1)))
            nx.tlast_unexpected = 1;
        if (is1(and3(not3(data_tlast), e->in_cnt == N - 1)))
            nx.tlast_missing = 1;
        if (e->in_cnt == N - 1) {
            const int v_ov = xfft_dpi_run(e->h, e->wr_slot, (int)(e->cfg_cur & 1),
                                          (int)((e->cfg_cur >> 1) & ((1u << 2 * XFFT_STAGES) - 1)));
            if (v_ov < 0) {
                fprintf(stderr, "xfft_engine: C model failed\n");
                e->error = XE_ERR_CMODEL;
            }
            e->slot_ovflo[e->wr_slot] = v_ov & 1;
            e->wr_slot = (e->wr_slot + 1) % SLOTS;
            if (e->wr_slot == e->rd_slot) {
                fprintf(stderr, "xfft_engine: out of result slots (internal error)\n");
                e->error = XE_ERR_SLOTS;
            }
            e->in_cnt = 0;
            e->boundary_left = XFFT_FRAME_BOUNDARY_STALL;
        } else
            e->in_cnt++;
    }
    // A sample that arrives into an empty FIFO while the pipeline waits for one enters it S clocks later
    // than it could; one that arrives as the restarting pipeline takes the FIFO's last sample, S + 1 (S + 2
    // after starving inside its frame)
    if (e->arrive_q.len > 0 && queue_front(&e->arrive_q) <= e->cycle) {
        queue_pop(&e->arrive_q);
        const int slack = e->fifo_at.len > 0 ? 0
                          : !v_entered ? S
                          : e->entered_d ? 0
                          : e->starve_d && e->entered % N != 0 ? S + 2 : S + 1;
        push(e, &e->fifo_at, e->cycle + 1 + slack);
    }
    e->entered_d = v_entered;
    e->starve_d = v_starve;
    // s_axis_data_tready: the input side sees a sample leave the FIFO D - K clocks late
    if (v_entered)
        push(e, &e->left_at, e->cycle);
    while (e->left_at.len > 0 && queue_front(&e->left_at) <= e->cycle - (D - K))
        queue_pop(&e->left_at);
    const long long in_fifo = queue_size(&e->arrive_q) + queue_size(&e->fifo_at) + queue_size(&e->left_at);
    // Input gaps near a full FIFO. Each accepted sample first passes the gap machine with the idle clocks
    // before it (clocks without a handshake since the one before):
    // - GAP_RUN: a gap of up to gap_credit clocks uses that much credit; a longer one is an event (to GAP_R1);
    // - GAP_R1 (the sample after an event): no gap restarts GAP_RUN with a credit of 1; any gap is an event (to
    //   GAP_R3);
    // - GAP_R3: a gap of up to 2 clocks restarts GAP_RUN with a credit of 3 - gap; a longer one is an event.
    // The sample's distance to the fill point, the sample the core would refuse first if the output stayed
    // blocked from now on (INPUT_LIMIT samples beyond those output). An event 17 or 19 samples before the fill
    // point (as moved by the events before) costs the FIFO one sample, unless the output takes a sample at the
    // same edge. The cost lasts while the FIFO stays near full: once a sample is accepted more than 24 samples
    // before the fill point, it is gone.
    if (v_hs) {
        e->in_taken++;
        const long long v_gap = e->gap_idle;
        int v_event = 0;
        if (e->gap_mode == GAP_RUN) {
            if (v_gap <= e->gap_credit)
                e->gap_credit -= (int)v_gap;
            else {
                v_event = 1;
                e->gap_mode = GAP_R1;
            }
        } else if (e->gap_mode == GAP_R1) {
            if (v_gap == 0) {
                e->gap_mode = GAP_RUN;
                e->gap_credit = 1;
            } else {
                v_event = 1;
                e->gap_mode = GAP_R3;
            }
        } else if (v_gap <= 2) {
            e->gap_mode = GAP_RUN;
            e->gap_credit = 3 - (int)v_gap;
        } else
            v_event = 1;
        const long long v_dist = XFFT_INPUT_LIMIT + e->out_taken - e->in_taken + 1;
        const long long v_from = v_dist - e->fill_lost;
        if (v_from > 24)
            e->fill_lost = 0;
        else if (v_event && !v_taken && (v_from == 17 || v_from == 19))
            e->fill_lost++;
        e->gap_idle = 0;
    } else
        e->gap_idle++;
    // The FIFO's fill level as tready sees it: two clocks late, so the handshakes of this edge and the one
    // before do not count yet (fill_lost samples less room)
    const long long v_seen = in_fifo - v_hs - e->hs_d;
    if (v_seen >= FIFO - 2 - e->fill_lost)
        e->in_full = 1;
    else if (in_fifo <= FIFO - XFFT_INPUT_RESUME)
        e->in_full = 0;
    e->hs_d = v_hs;

    // Registered outputs become visible at the next edge. In a clock in which the pipeline starves, the
    // output register (when nothing is queued behind it) and the status channel offer nothing, unless they
    // already offer a word the sink has not taken: AXI4-Stream keeps it offered until its handshake
    const int v_starve_next = q.running && e->entered % N != 0
                              && !(e->fifo_at.len > 0 && queue_front(&e->fifo_at) <= e->cycle + 1);
    if (e->started_q.len > 0 && queue_front(&e->started_q) == e->cycle + 1) {
        nx.frame_started = 1;
        queue_pop(&e->started_q);
    }
    const long long q_size = queue_size(&e->q_re);
    nx.m_tvalid = v_stalled || (q_size > 0 && !(q_size == 1 && v_starve_next));
    nx.m_re = q_size > 0 ? queue_front(&e->q_re) : 0;
    nx.m_im = q_size > 0 ? queue_front(&e->q_im) : 0;
    nx.m_tuser = q_size > 0 ? (int)queue_front(&e->q_user) : 0;
    nx.m_tlast = q_size > 0 ? (int)queue_front(&e->q_last) : 0;
    nx.status_tvalid = v_status_stalled
                       || (e->status_q.len > 0 && queue_front(&e->status_at_q) <= e->cycle + 1 && !v_starve_next);
    nx.status_tdata = e->status_q.len > 0 ? (int)queue_front(&e->status_q) : 0;
    // running is visible at edge (first edge with aresetn high) + RESET_TO_READY
    if (e->reset_cnt < XFFT_RESET_TO_READY)
        e->reset_cnt++;
    nx.running = e->reset_cnt >= XFFT_RESET_TO_READY;
    e->cycle++;

    if (is1(not3(aresetn))) {
        e->reset_cnt = 0;
        nx.running = 0;
        e->cfg_cur = reset_config();
        e->cfg_pending = 0;
        e->in_cnt = 0;
        e->boundary_left = 0;
        queue_clear(&e->arrive_q);
        queue_clear(&e->fifo_at);
        queue_clear(&e->left_at);
        e->in_full = 0;
        e->hs_d = 0;
        e->gap_mode = GAP_RUN;
        e->gap_credit = 1;
        e->gap_idle = 0;
        e->fill_lost = 0;
        e->in_taken = 0;
        e->out_taken = 0;
        e->entered_d = 0;
        e->starve_d = 0;
        e->pipe_clk = 0;
        e->entered = 0;
        queue_clear(&e->frame_at);
        e->out_j = 0;
        e->paused = 0;
        e->out_halt_d = 0;
        queue_clear(&e->q_re);
        queue_clear(&e->q_im);
        queue_clear(&e->q_user);
        queue_clear(&e->q_last);
        queue_clear(&e->tok_word);
        queue_clear(&e->tok_step);
        queue_clear(&e->tok_at);
        e->wr_slot = 0;
        e->rd_slot = 0;
        queue_clear(&e->status_q);
        queue_clear(&e->status_at_q);
        queue_clear(&e->started_q);
        nx.m_tvalid = 0;
        nx.m_tlast = 0;
        nx.status_tvalid = 0;
        nx.frame_started = 0;
        nx.tlast_unexpected = 0;
        nx.tlast_missing = 0;
        nx.fft_overflow = 0;
        nx.in_halt = 0;
        nx.out_halt = 0;
    }

    e->r = nx;
    fill_out(e, out);
}

XFFT_ENGINE_API void xfft_engine_free(int32_t id)
{
    if (id < 0 || id >= engine_count || !engines[id])
        return;
    engine_t* e = engines[id];
    queue_t* queues[] = {&e->arrive_q, &e->fifo_at, &e->left_at, &e->frame_at, &e->q_re, &e->q_im, &e->q_user,
                         &e->q_last, &e->tok_word, &e->tok_step, &e->tok_at, &e->status_q, &e->status_at_q,
                         &e->started_q};
    for (size_t i = 0; i < sizeof(queues) / sizeof(queues[0]); i++)
        free(queues[i]->v);
    xfft_dpi_free(e->h);
    free(e);
    engines[id] = NULL;
}

XFFT_ENGINE_API double xfft_engine_wallclock(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + 1e-9 * ts.tv_nsec;
}
