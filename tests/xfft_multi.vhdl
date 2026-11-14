library ieee;
use ieee.std_logic_1164.all;

-- Toplevel of test_xfft_multi.py: three stand-ins of the xfft core in one simulation, sharing only the clock and
-- the reset. a0 and a1 are two instances of one configuration (entity xfft_core, package xfft_model_pkg), b one of
-- another (entity xfft_b, package xfft_b_pkg): each wrapper calls its own configuration's library (their C
-- functions have different names, scripts/build_engine.py). The test drives each instance through its own ports
-- (a0_*, a1_*, b_*: the core's vendor port names with the instance's prefix, hence no _in/_out suffixes). Widths
-- from the generated packages, by selected names.
entity xfft_multi is
    port(
        aclk: in std_logic;
        aresetn: in std_logic;

        a0_s_axis_config_tdata: in std_logic_vector(work.xfft_model_pkg.XFFT_CONFIG_WIDTH-1 downto 0);
        a0_s_axis_config_tvalid: in std_logic;
        a0_s_axis_config_tready: out std_logic;
        a0_s_axis_data_tdata: in std_logic_vector(work.xfft_model_pkg.XFFT_DATA_WIDTH-1 downto 0);
        a0_s_axis_data_tvalid: in std_logic;
        a0_s_axis_data_tready: out std_logic;
        a0_s_axis_data_tlast: in std_logic;
        a0_m_axis_data_tdata: out std_logic_vector(work.xfft_model_pkg.XFFT_DATA_WIDTH-1 downto 0);
        a0_m_axis_data_tuser: out std_logic_vector(work.xfft_model_pkg.XFFT_USER_WIDTH-1 downto 0);
        a0_m_axis_data_tvalid: out std_logic;
        a0_m_axis_data_tready: in std_logic;
        a0_m_axis_data_tlast: out std_logic;
        a0_m_axis_status_tdata: out std_logic_vector(7 downto 0);
        a0_m_axis_status_tvalid: out std_logic;
        a0_m_axis_status_tready: in std_logic;
        a0_event_frame_started: out std_logic;
        a0_event_tlast_unexpected: out std_logic;
        a0_event_tlast_missing: out std_logic;
        a0_event_fft_overflow: out std_logic;
        a0_event_status_channel_halt: out std_logic;
        a0_event_data_in_channel_halt: out std_logic;
        a0_event_data_out_channel_halt: out std_logic;

        a1_s_axis_config_tdata: in std_logic_vector(work.xfft_model_pkg.XFFT_CONFIG_WIDTH-1 downto 0);
        a1_s_axis_config_tvalid: in std_logic;
        a1_s_axis_config_tready: out std_logic;
        a1_s_axis_data_tdata: in std_logic_vector(work.xfft_model_pkg.XFFT_DATA_WIDTH-1 downto 0);
        a1_s_axis_data_tvalid: in std_logic;
        a1_s_axis_data_tready: out std_logic;
        a1_s_axis_data_tlast: in std_logic;
        a1_m_axis_data_tdata: out std_logic_vector(work.xfft_model_pkg.XFFT_DATA_WIDTH-1 downto 0);
        a1_m_axis_data_tuser: out std_logic_vector(work.xfft_model_pkg.XFFT_USER_WIDTH-1 downto 0);
        a1_m_axis_data_tvalid: out std_logic;
        a1_m_axis_data_tready: in std_logic;
        a1_m_axis_data_tlast: out std_logic;
        a1_m_axis_status_tdata: out std_logic_vector(7 downto 0);
        a1_m_axis_status_tvalid: out std_logic;
        a1_m_axis_status_tready: in std_logic;
        a1_event_frame_started: out std_logic;
        a1_event_tlast_unexpected: out std_logic;
        a1_event_tlast_missing: out std_logic;
        a1_event_fft_overflow: out std_logic;
        a1_event_status_channel_halt: out std_logic;
        a1_event_data_in_channel_halt: out std_logic;
        a1_event_data_out_channel_halt: out std_logic;

        b_s_axis_config_tdata: in std_logic_vector(work.xfft_b_pkg.XFFT_CONFIG_WIDTH-1 downto 0);
        b_s_axis_config_tvalid: in std_logic;
        b_s_axis_config_tready: out std_logic;
        b_s_axis_data_tdata: in std_logic_vector(work.xfft_b_pkg.XFFT_DATA_WIDTH-1 downto 0);
        b_s_axis_data_tvalid: in std_logic;
        b_s_axis_data_tready: out std_logic;
        b_s_axis_data_tlast: in std_logic;
        b_m_axis_data_tdata: out std_logic_vector(work.xfft_b_pkg.XFFT_DATA_WIDTH-1 downto 0);
        b_m_axis_data_tuser: out std_logic_vector(work.xfft_b_pkg.XFFT_USER_WIDTH-1 downto 0);
        b_m_axis_data_tvalid: out std_logic;
        b_m_axis_data_tready: in std_logic;
        b_m_axis_data_tlast: out std_logic;
        b_m_axis_status_tdata: out std_logic_vector(7 downto 0);
        b_m_axis_status_tvalid: out std_logic;
        b_m_axis_status_tready: in std_logic;
        b_event_frame_started: out std_logic;
        b_event_tlast_unexpected: out std_logic;
        b_event_tlast_missing: out std_logic;
        b_event_fft_overflow: out std_logic;
        b_event_status_channel_halt: out std_logic;
        b_event_data_in_channel_halt: out std_logic;
        b_event_data_out_channel_halt: out std_logic
    );
end;

architecture three of xfft_multi is
begin
    a0: entity work.xfft_core port map(
        aclk => aclk,
        aresetn => aresetn,
        s_axis_config_tdata => a0_s_axis_config_tdata,
        s_axis_config_tvalid => a0_s_axis_config_tvalid,
        s_axis_config_tready => a0_s_axis_config_tready,
        s_axis_data_tdata => a0_s_axis_data_tdata,
        s_axis_data_tvalid => a0_s_axis_data_tvalid,
        s_axis_data_tready => a0_s_axis_data_tready,
        s_axis_data_tlast => a0_s_axis_data_tlast,
        m_axis_data_tdata => a0_m_axis_data_tdata,
        m_axis_data_tuser => a0_m_axis_data_tuser,
        m_axis_data_tvalid => a0_m_axis_data_tvalid,
        m_axis_data_tready => a0_m_axis_data_tready,
        m_axis_data_tlast => a0_m_axis_data_tlast,
        m_axis_status_tdata => a0_m_axis_status_tdata,
        m_axis_status_tvalid => a0_m_axis_status_tvalid,
        m_axis_status_tready => a0_m_axis_status_tready,
        event_frame_started => a0_event_frame_started,
        event_tlast_unexpected => a0_event_tlast_unexpected,
        event_tlast_missing => a0_event_tlast_missing,
        event_fft_overflow => a0_event_fft_overflow,
        event_status_channel_halt => a0_event_status_channel_halt,
        event_data_in_channel_halt => a0_event_data_in_channel_halt,
        event_data_out_channel_halt => a0_event_data_out_channel_halt
    );

    a1: entity work.xfft_core port map(
        aclk => aclk,
        aresetn => aresetn,
        s_axis_config_tdata => a1_s_axis_config_tdata,
        s_axis_config_tvalid => a1_s_axis_config_tvalid,
        s_axis_config_tready => a1_s_axis_config_tready,
        s_axis_data_tdata => a1_s_axis_data_tdata,
        s_axis_data_tvalid => a1_s_axis_data_tvalid,
        s_axis_data_tready => a1_s_axis_data_tready,
        s_axis_data_tlast => a1_s_axis_data_tlast,
        m_axis_data_tdata => a1_m_axis_data_tdata,
        m_axis_data_tuser => a1_m_axis_data_tuser,
        m_axis_data_tvalid => a1_m_axis_data_tvalid,
        m_axis_data_tready => a1_m_axis_data_tready,
        m_axis_data_tlast => a1_m_axis_data_tlast,
        m_axis_status_tdata => a1_m_axis_status_tdata,
        m_axis_status_tvalid => a1_m_axis_status_tvalid,
        m_axis_status_tready => a1_m_axis_status_tready,
        event_frame_started => a1_event_frame_started,
        event_tlast_unexpected => a1_event_tlast_unexpected,
        event_tlast_missing => a1_event_tlast_missing,
        event_fft_overflow => a1_event_fft_overflow,
        event_status_channel_halt => a1_event_status_channel_halt,
        event_data_in_channel_halt => a1_event_data_in_channel_halt,
        event_data_out_channel_halt => a1_event_data_out_channel_halt
    );

    b: entity work.xfft_b port map(
        aclk => aclk,
        aresetn => aresetn,
        s_axis_config_tdata => b_s_axis_config_tdata,
        s_axis_config_tvalid => b_s_axis_config_tvalid,
        s_axis_config_tready => b_s_axis_config_tready,
        s_axis_data_tdata => b_s_axis_data_tdata,
        s_axis_data_tvalid => b_s_axis_data_tvalid,
        s_axis_data_tready => b_s_axis_data_tready,
        s_axis_data_tlast => b_s_axis_data_tlast,
        m_axis_data_tdata => b_m_axis_data_tdata,
        m_axis_data_tuser => b_m_axis_data_tuser,
        m_axis_data_tvalid => b_m_axis_data_tvalid,
        m_axis_data_tready => b_m_axis_data_tready,
        m_axis_data_tlast => b_m_axis_data_tlast,
        m_axis_status_tdata => b_m_axis_status_tdata,
        m_axis_status_tvalid => b_m_axis_status_tvalid,
        m_axis_status_tready => b_m_axis_status_tready,
        event_frame_started => b_event_frame_started,
        event_tlast_unexpected => b_event_tlast_unexpected,
        event_tlast_missing => b_event_tlast_missing,
        event_fft_overflow => b_event_fft_overflow,
        event_status_channel_halt => b_event_status_channel_halt,
        event_data_in_channel_halt => b_event_data_in_channel_halt,
        event_data_out_channel_halt => b_event_data_out_channel_halt
    );
end;
