// Toplevel of test_xfft_compare.py: the real core and the stand-in side by side, sharing only the clock and
// the reset. The test drives each through its own ports (core_*, model_*) with its own handshakes. The
// stand-in (the C engine's SV wrapper, model/xfft_engine.sv) is compiled under another name (`XFFT_ENGINE_NAME),
// the core keeps its own (`XFFT_CORE_NAME).
`ifndef XFFT_CORE_NAME
`define XFFT_CORE_NAME xfft_core
`endif
`ifndef XFFT_ENGINE_NAME
`define XFFT_ENGINE_NAME xfft_engine_model
`endif
// The configuration's package (xfft_model_pkg for module xfft_core, else <module>_pkg)
`ifndef XFFT_MODEL_PKG
`define XFFT_MODEL_PKG xfft_model_pkg
`endif

module xfft_pair
    import `XFFT_MODEL_PKG::*;
(
    input wire aclk,
    input wire aresetn,
    input wire [XFFT_CONFIG_WIDTH-1:0] core_s_axis_config_tdata,
    input wire core_s_axis_config_tvalid,
    output wire core_s_axis_config_tready,
    input wire [XFFT_DATA_WIDTH-1:0] core_s_axis_data_tdata,
    input wire core_s_axis_data_tvalid,
    output wire core_s_axis_data_tready,
    input wire core_s_axis_data_tlast,
    output wire [XFFT_DATA_WIDTH-1:0] core_m_axis_data_tdata,
    output wire [XFFT_USER_WIDTH-1:0] core_m_axis_data_tuser,
    output wire core_m_axis_data_tvalid,
    input wire core_m_axis_data_tready,
    output wire core_m_axis_data_tlast,
    output wire [7:0] core_m_axis_status_tdata,
    output wire core_m_axis_status_tvalid,
    input wire core_m_axis_status_tready,
    output wire core_event_frame_started,
    output wire core_event_tlast_unexpected,
    output wire core_event_tlast_missing,
    output wire core_event_fft_overflow,
    output wire core_event_status_channel_halt,
    output wire core_event_data_in_channel_halt,
    output wire core_event_data_out_channel_halt,
    input wire [XFFT_CONFIG_WIDTH-1:0] model_s_axis_config_tdata,
    input wire model_s_axis_config_tvalid,
    output wire model_s_axis_config_tready,
    input wire [XFFT_DATA_WIDTH-1:0] model_s_axis_data_tdata,
    input wire model_s_axis_data_tvalid,
    output wire model_s_axis_data_tready,
    input wire model_s_axis_data_tlast,
    output wire [XFFT_DATA_WIDTH-1:0] model_m_axis_data_tdata,
    output wire [XFFT_USER_WIDTH-1:0] model_m_axis_data_tuser,
    output wire model_m_axis_data_tvalid,
    input wire model_m_axis_data_tready,
    output wire model_m_axis_data_tlast,
    output wire [7:0] model_m_axis_status_tdata,
    output wire model_m_axis_status_tvalid,
    input wire model_m_axis_status_tready,
    output wire model_event_frame_started,
    output wire model_event_tlast_unexpected,
    output wire model_event_tlast_missing,
    output wire model_event_fft_overflow,
    output wire model_event_status_channel_halt,
    output wire model_event_data_in_channel_halt,
    output wire model_event_data_out_channel_halt
);
    `XFFT_CORE_NAME core(
        .aclk(aclk),
        .aresetn(aresetn),
        .s_axis_config_tdata(core_s_axis_config_tdata),
        .s_axis_config_tvalid(core_s_axis_config_tvalid),
        .s_axis_config_tready(core_s_axis_config_tready),
        .s_axis_data_tdata(core_s_axis_data_tdata),
        .s_axis_data_tvalid(core_s_axis_data_tvalid),
        .s_axis_data_tready(core_s_axis_data_tready),
        .s_axis_data_tlast(core_s_axis_data_tlast),
        .m_axis_data_tdata(core_m_axis_data_tdata),
        .m_axis_data_tuser(core_m_axis_data_tuser),
        .m_axis_data_tvalid(core_m_axis_data_tvalid),
        .m_axis_data_tready(core_m_axis_data_tready),
        .m_axis_data_tlast(core_m_axis_data_tlast),
        .m_axis_status_tdata(core_m_axis_status_tdata),
        .m_axis_status_tvalid(core_m_axis_status_tvalid),
        .m_axis_status_tready(core_m_axis_status_tready),
        .event_frame_started(core_event_frame_started),
        .event_tlast_unexpected(core_event_tlast_unexpected),
        .event_tlast_missing(core_event_tlast_missing),
        .event_fft_overflow(core_event_fft_overflow),
        .event_status_channel_halt(core_event_status_channel_halt),
        .event_data_in_channel_halt(core_event_data_in_channel_halt),
        .event_data_out_channel_halt(core_event_data_out_channel_halt)
    );

    `XFFT_ENGINE_NAME model(
        .aclk(aclk),
        .aresetn(aresetn),
        .s_axis_config_tdata(model_s_axis_config_tdata),
        .s_axis_config_tvalid(model_s_axis_config_tvalid),
        .s_axis_config_tready(model_s_axis_config_tready),
        .s_axis_data_tdata(model_s_axis_data_tdata),
        .s_axis_data_tvalid(model_s_axis_data_tvalid),
        .s_axis_data_tready(model_s_axis_data_tready),
        .s_axis_data_tlast(model_s_axis_data_tlast),
        .m_axis_data_tdata(model_m_axis_data_tdata),
        .m_axis_data_tuser(model_m_axis_data_tuser),
        .m_axis_data_tvalid(model_m_axis_data_tvalid),
        .m_axis_data_tready(model_m_axis_data_tready),
        .m_axis_data_tlast(model_m_axis_data_tlast),
        .m_axis_status_tdata(model_m_axis_status_tdata),
        .m_axis_status_tvalid(model_m_axis_status_tvalid),
        .m_axis_status_tready(model_m_axis_status_tready),
        .event_frame_started(model_event_frame_started),
        .event_tlast_unexpected(model_event_tlast_unexpected),
        .event_tlast_missing(model_event_tlast_missing),
        .event_fft_overflow(model_event_fft_overflow),
        .event_status_channel_halt(model_event_status_channel_halt),
        .event_data_in_channel_halt(model_event_data_in_channel_halt),
        .event_data_out_channel_halt(model_event_data_out_channel_halt)
    );
endmodule
