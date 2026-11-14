// Cycle engine of the xfft stand-in in C: the core's timing, one step per rising clock edge (the single source
// of the timing), the numbers from AMD's bit-accurate C model through model/xfft_dpi.c (compiled into the same
// library).
//
// Called from thin HDL wrappers that make no timing decisions of their own: model/xfft_engine.sv
// (SystemVerilog, DPI-C: XSim, Verilator) and model/xfft_engine.vhdl (VHDL-2008, VHPIDIRECT: NVC, GHDL). Plain C
// types only: every argument is a 32-bit int, so the same functions serve SV `int` (DPI-C) and VHDL `integer`
// (VHPIDIRECT). The timing constants and widths are compiled in from the generated xfft_model.h
// (model/xfft_timing.py), so there is one library per configuration (scripts/build_engine.py). The library
// exports only the xfft_engine_* symbols below (-fvisibility=hidden, -Wl,-Bsymbolic): it shares the process
// with the simulator, cocotb and Python. One simulation thread only:
// the table of engines has no lock (nor, as far as is known, AMD's C model).
#ifndef XFFT_ENGINE_H
#define XFFT_ENGINE_H

#include <stdint.h>

#define XFFT_ENGINE_API __attribute__((visibility("default")))

// Single-bit inputs: 0, 1, or XE_UNKNOWN for any other value (X, Z, U, W, -). The engine evaluates them as
// SystemVerilog's `&&` and `!` do: `a && b` is true only for a known 1 and a known 1, `!a` only for a known 0. So an
// unknown aresetn resets nothing, an unknown tvalid or tready makes no handshake (and no stall), an unknown
// s_axis_data_tlast raises neither tlast event.
#define XE_UNKNOWN 2

// Data words (s_axis_data_tdata, m_axis_data_tdata, up to 80 bits) are passed as XE_CHUNKS chunks of 16 bits,
// least significant first, zero above the port's width. Unknown bits of data and config words are 0.
#define XE_CHUNKS 5
#define XE_CHUNK_BITS 16

// Index of each value in the output array of xfft_engine_step: every output port in port order (tdata in
// chunks), then the error code. The wrappers repeat these indices; a change here changes them too.
enum {
    XE_CONFIG_TREADY = 0,       // s_axis_config_tready
    XE_DATA_TREADY,             // s_axis_data_tready
    XE_M_TDATA_0,               // m_axis_data_tdata(15 downto 0), then the next chunks
    XE_M_TUSER = XE_M_TDATA_0 + XE_CHUNKS,   // m_axis_data_tuser
    XE_M_TVALID,                // m_axis_data_tvalid
    XE_M_TLAST,                 // m_axis_data_tlast
    XE_STATUS_TDATA,            // m_axis_status_tdata
    XE_STATUS_TVALID,           // m_axis_status_tvalid
    XE_FRAME_STARTED,           // event_frame_started
    XE_TLAST_UNEXPECTED,        // event_tlast_unexpected
    XE_TLAST_MISSING,           // event_tlast_missing
    XE_FFT_OVERFLOW,            // event_fft_overflow
    XE_STATUS_CHANNEL_HALT,     // event_status_channel_halt
    XE_DATA_IN_CHANNEL_HALT,    // event_data_in_channel_halt
    XE_DATA_OUT_CHANNEL_HALT,   // event_data_out_channel_halt
    XE_ERROR,                   // XE_OK, or why the simulation must stop (the wrapper ends it)
    XE_OUT_COUNT
};

// Error codes (out[XE_ERROR]); the message is on stderr too
enum {
    XE_OK = 0,
    XE_ERR_CMODEL,              // the C model failed
    XE_ERR_SLOTS,               // out of result slots (internal error)
    XE_ERR_ENGINE               // no such engine, or out of memory
};

// A new engine for one core instance; returns its id (>= 0), or -1 (message on stderr). The arguments are the
// configuration as the HDL package xfft_model_pkg has it; they must equal the xfft_model.h the library was built
// with. Also checks that the timing constants fit the model.
XFFT_ENGINE_API int32_t xfft_engine_new(int32_t nfft, int32_t input_width, int32_t twiddle_width,
                                        int32_t convergent, int32_t config_width, int32_t data_width,
                                        int32_t user_width);

// One rising edge of aclk. The inputs are the port values sampled at the edge (single bits 0, 1 or
// XE_UNKNOWN; s_axis_config_tdata as an unsigned value; s_axis_data_tdata in chunks d0..d4). out[XE_OUT_COUNT]
// receives every output for the clock after the edge. The registered outputs depend on
// the inputs of this edge only through the state, and the two tready are functions of the state: inside the
// step they have their value from before the edge, in out the value after it.
XFFT_ENGINE_API void xfft_engine_step(int32_t id, int32_t aresetn, int32_t config_tvalid, int32_t config_tdata,
                                      int32_t data_tvalid, int32_t d0, int32_t d1, int32_t d2, int32_t d3,
                                      int32_t d4, int32_t data_tlast, int32_t m_data_tready,
                                      int32_t m_status_tready, int32_t* out);

// Frees an engine and its C model (an id is not reused). The SV wrapper calls it in a final block; the VHDL
// wrapper has no such hook, so there an engine lives until the simulator process exits.
XFFT_ENGINE_API void xfft_engine_free(int32_t id);

// Wall-clock seconds (monotonic), for speed measurements in HDL testbenches.
XFFT_ENGINE_API double xfft_engine_wallclock(void);

// Before the first edge (no step yet) the wrappers drive both tready and event_status_channel_halt 0 and every
// other output unknown. From the first edge on every output is known.

#endif
