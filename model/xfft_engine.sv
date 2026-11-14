// Simulation-only stand-in for the xfft 9.x core of ip/xfft_core.tcl, SystemVerilog side of the C engine (DPI-C:
// XSim and Verilator): a thin wrapper around model/xfft_engine.c, which holds all timing and state. At each rising
// edge of aclk it passes the sampled inputs to xfft_engine_step and drives every output from the result; it makes
// no timing decisions. The same engine serves NVC and GHDL through model/xfft_engine.vhdl.
//
// The core's ports and port kinds, widths from the generated package xfft_model_pkg. The module has the core's
// name (`XFFT_ENGINE_NAME, default `XFFT_CORE_NAME, default xfft_core): next to the real core it needs another
// name, xvlog -d XFFT_ENGINE_NAME=<name>. The macros are only read, never defined here, so they do not reach the
// files compiled after this one. The library is libxfft_engine.so of scripts/build_engine.py, built for the same
// configuration (xfft_engine_new checks it). This file is for module xfft_core (the C functions xfft_engine_*);
// for any other module scripts/build_engine.py writes a copy of it with that module's names (its package
// <module>_pkg, the C functions <module>_engine_*), and several configurations can share one simulation.
//
// The engine steps on `posedge aclk`; both tready and event_status_channel_halt are 0 before the first edge, the
// other outputs unknown; from the first edge on every output is known. Input bits that are not 0 or 1 are passed
// as unknown (XE_UNKNOWN), which the engine evaluates as SV's `&&` and `!` treat X; unknown bits of the data and
// config words are 0. A C-model failure ends the simulation ($fatal), at time 0 already if xfft_engine_new fails.
// On Verilator (2-state) the outputs are 0 instead of unknown before the first edge and every input bit is 0 or 1;
// single-threaded only (the engine's table has no lock: no --threads).
`ifdef XFFT_ENGINE_NAME
module `XFFT_ENGINE_NAME
`elsif XFFT_CORE_NAME
module `XFFT_CORE_NAME
`else
module xfft_core
`endif
    import xfft_model_pkg::*;
(
    input wire aclk,
    input wire aresetn,
    input wire [XFFT_CONFIG_WIDTH-1:0] s_axis_config_tdata,
    input wire s_axis_config_tvalid,
    output wire s_axis_config_tready,
    input wire [XFFT_DATA_WIDTH-1:0] s_axis_data_tdata,
    input wire s_axis_data_tvalid,
    output wire s_axis_data_tready,
    input wire s_axis_data_tlast,
    output logic [XFFT_DATA_WIDTH-1:0] m_axis_data_tdata,
    output logic [XFFT_USER_WIDTH-1:0] m_axis_data_tuser,
    output logic m_axis_data_tvalid,
    input wire m_axis_data_tready,
    output logic m_axis_data_tlast,
    output logic [7:0] m_axis_status_tdata,
    output logic m_axis_status_tvalid,
    input wire m_axis_status_tready,
    output logic event_frame_started,
    output logic event_tlast_unexpected,
    output logic event_tlast_missing,
    output logic event_fft_overflow,
    output wire event_status_channel_halt,
    output logic event_data_in_channel_halt,
    output logic event_data_out_channel_halt
);
    // Output array layout of xfft_engine.h
    localparam int XE_CHUNKS = 5;
    localparam int XE_CONFIG_TREADY = 0;
    localparam int XE_DATA_TREADY = 1;
    localparam int XE_M_TDATA_0 = 2;
    localparam int XE_M_TUSER = XE_M_TDATA_0 + XE_CHUNKS;
    localparam int XE_M_TVALID = XE_M_TUSER + 1;
    localparam int XE_M_TLAST = XE_M_TUSER + 2;
    localparam int XE_STATUS_TDATA = XE_M_TUSER + 3;
    localparam int XE_STATUS_TVALID = XE_M_TUSER + 4;
    localparam int XE_FRAME_STARTED = XE_M_TUSER + 5;
    localparam int XE_TLAST_UNEXPECTED = XE_M_TUSER + 6;
    localparam int XE_TLAST_MISSING = XE_M_TUSER + 7;
    localparam int XE_FFT_OVERFLOW = XE_M_TUSER + 8;
    localparam int XE_STATUS_CHANNEL_HALT = XE_M_TUSER + 9;
    localparam int XE_DATA_IN_CHANNEL_HALT = XE_M_TUSER + 10;
    localparam int XE_DATA_OUT_CHANNEL_HALT = XE_M_TUSER + 11;
    localparam int XE_ERROR = XE_M_TUSER + 12;
    localparam int XE_OUT_COUNT = XE_M_TUSER + 13;
    localparam int XE_UNKNOWN = 2;
    localparam int XE_ERR_CMODEL = 1;
    localparam int XE_ERR_SLOTS = 2;

    // The C functions of xfft_engine.h, by their C names (the configuration's prefix)
    import "DPI-C" xfft_engine_new = function int xfft_engine_new(int nfft, int input_width, int twiddle_width,
        int convergent, int config_width, int data_width, int user_width);
    import "DPI-C" xfft_engine_step = function void xfft_engine_step(int id, int aresetn, int config_tvalid,
        int config_tdata, int data_tvalid, int d0, int d1, int d2, int d3, int d4, int data_tlast,
        int m_data_tready, int m_status_tready, output int out[XE_OUT_COUNT]);
    import "DPI-C" xfft_engine_free = function void xfft_engine_free(int id);

    if (XFFT_DATA_WIDTH > 16 * XE_CHUNKS)
        $error("%m: DATA_WIDTH %0d is wider than the engine's data chunks", XFFT_DATA_WIDTH);

    int id = -1;
    int o[XE_OUT_COUNT];
    // The two tready, as continuous assignments of 2-state variables: 0 before the first edge
    bit config_tready;
    bit data_tready;

    assign s_axis_config_tready = config_tready;
    assign s_axis_data_tready = data_tready;

    assign event_status_channel_halt = 1'b0;

    // A single bit as the engine takes it: 0, 1 or unknown
    function automatic int bit3(logic b);
        return b === 1'b1 ? 1 : b === 1'b0 ? 0 : XE_UNKNOWN;
    endfunction

    // The engine of this instance, made at time 0 (or at a clock edge at time 0 that comes first)
    task automatic create();
        if (id >= 0)
            return;
        id = xfft_engine_new(XFFT_NFFT, XFFT_INPUT_WIDTH, XFFT_TWIDDLE_WIDTH, XFFT_CONVERGENT, XFFT_CONFIG_WIDTH,
                             XFFT_DATA_WIDTH, XFFT_USER_WIDTH);
        if (id < 0)
            $fatal(1, "%m: xfft_engine_new failed");
    endtask

    initial
        create();

    // The engine and its C model released at the end of the simulation
    final
        if (id >= 0) begin
            xfft_engine_free(id);
            id = -1;
        end

    always @(posedge aclk) begin
        automatic bit [16*XE_CHUNKS-1:0] v_d, v_m;

        create();
        v_d = (16 * XE_CHUNKS)'(s_axis_data_tdata);     // unknown bits 0, zero above the width
        xfft_engine_step(id, bit3(aresetn), bit3(s_axis_config_tvalid), int'(s_axis_config_tdata),
                         bit3(s_axis_data_tvalid), int'(v_d[15:0]), int'(v_d[31:16]), int'(v_d[47:32]),
                         int'(v_d[63:48]), int'(v_d[79:64]), bit3(s_axis_data_tlast), bit3(m_axis_data_tready),
                         bit3(m_axis_status_tready), o);
        if (o[XE_ERROR] == XE_ERR_CMODEL)
            $fatal(1, "%m: C model failed");
        if (o[XE_ERROR] == XE_ERR_SLOTS)
            $fatal(1, "%m: out of result slots (internal error)");
        if (o[XE_ERROR] != 0)
            $fatal(1, "%m: C engine failed (%0d)", o[XE_ERROR]);
        for (int c = 0; c < XE_CHUNKS; c++)
            v_m[16*c +: 16] = o[XE_M_TDATA_0 + c][15:0];
        config_tready = o[XE_CONFIG_TREADY][0];
        data_tready = o[XE_DATA_TREADY][0];
        m_axis_data_tdata <= v_m[XFFT_DATA_WIDTH-1:0];
        m_axis_data_tuser <= o[XE_M_TUSER][XFFT_USER_WIDTH-1:0];
        m_axis_data_tvalid <= o[XE_M_TVALID][0];
        m_axis_data_tlast <= o[XE_M_TLAST][0];
        m_axis_status_tdata <= o[XE_STATUS_TDATA][7:0];
        m_axis_status_tvalid <= o[XE_STATUS_TVALID][0];
        event_frame_started <= o[XE_FRAME_STARTED][0];
        event_tlast_unexpected <= o[XE_TLAST_UNEXPECTED][0];
        event_tlast_missing <= o[XE_TLAST_MISSING][0];
        event_fft_overflow <= o[XE_FFT_OVERFLOW][0];
        event_data_in_channel_halt <= o[XE_DATA_IN_CHANNEL_HALT][0];
        event_data_out_channel_halt <= o[XE_DATA_OUT_CHANNEL_HALT][0];
    end
endmodule
