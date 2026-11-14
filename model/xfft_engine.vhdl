library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

use work.xfft_model_pkg.all;

-- Simulation-only stand-in for the xfft 9.x core of ip/xfft_core.tcl, VHDL side of the C engine (NVC, GHDL:
-- VHPIDIRECT): a thin wrapper around model/xfft_engine.c, which holds all timing and state. At each rising edge
-- of aclk it passes the sampled inputs to xfft_engine_step and drives every output from the result; it makes no
-- timing decisions. The same engine serves XSim and Verilator through model/xfft_engine.sv.
-- Same entity name and ports as the generated core (its vendor port names, hence no _in/_out suffixes), widths
-- from the generated package xfft_model_pkg. This file is for module xfft_core (the C functions xfft_engine_*); for
-- any other module scripts/build_engine.py writes a copy of it with that module's names (entity <module>, package
-- <module>_pkg, the C functions <module>_engine_*), and several configurations can share one simulation.
-- Before the first edge both tready and event_status_channel_halt are '0', the other outputs 'X'; from the first
-- edge on every output is known. Input bits: '1'/'H' are 1, '0'/'L' are 0, any other value is passed as unknown
-- (XE_UNKNOWN), which the engine evaluates as SV's `&&` and `!` treat X (xfft_engine.h); unknown bits of the
-- data and config words are 0. An edge is rising_edge(aclk). A C-model failure ends the simulation (severity
-- failure), at time 0 already if xfft_engine_new fails.
-- VHDL-2008 only (NVC --std=2008, GHDL --std=08): the C functions take int32_t, and NVC's VHDL-2019 integer is 64
-- bits wide; the process stops at time 0 with a failure then. The C library libxfft_engine.so is linked into
-- GHDL's executable (-Wl,) or preloaded into NVC (LD_PRELOAD). VHDL has no hook at the end of a simulation, so
-- the engine and its C model live until the simulator process exits.
entity xfft_core is
    port(
        aclk: in std_logic;
        aresetn: in std_logic;

        s_axis_config_tdata: in std_logic_vector(XFFT_CONFIG_WIDTH-1 downto 0);
        s_axis_config_tvalid: in std_logic;
        s_axis_config_tready: out std_logic := '0';

        s_axis_data_tdata: in std_logic_vector(XFFT_DATA_WIDTH-1 downto 0);
        s_axis_data_tvalid: in std_logic;
        s_axis_data_tready: out std_logic := '0';
        s_axis_data_tlast: in std_logic;

        m_axis_data_tdata: out std_logic_vector(XFFT_DATA_WIDTH-1 downto 0) := (others => 'X');
        m_axis_data_tuser: out std_logic_vector(XFFT_USER_WIDTH-1 downto 0) := (others => 'X');
        m_axis_data_tvalid: out std_logic := 'X';
        m_axis_data_tready: in std_logic;
        m_axis_data_tlast: out std_logic := 'X';

        m_axis_status_tdata: out std_logic_vector(7 downto 0) := (others => 'X');
        m_axis_status_tvalid: out std_logic := 'X';
        m_axis_status_tready: in std_logic;

        event_frame_started: out std_logic := 'X';
        event_tlast_unexpected: out std_logic := 'X';
        event_tlast_missing: out std_logic := 'X';
        event_fft_overflow: out std_logic := 'X';
        event_status_channel_halt: out std_logic := '0';
        event_data_in_channel_halt: out std_logic := 'X';
        event_data_out_channel_halt: out std_logic := 'X'
    );
end;

architecture c_engine of xfft_core is
    -- Output array layout of xfft_engine.h
    constant XE_CHUNKS: natural := 5;
    constant XE_CONFIG_TREADY: natural := 0;
    constant XE_DATA_TREADY: natural := 1;
    constant XE_M_TDATA_0: natural := 2;
    constant XE_M_TUSER: natural := XE_M_TDATA_0+XE_CHUNKS;
    constant XE_M_TVALID: natural := XE_M_TUSER+1;
    constant XE_M_TLAST: natural := XE_M_TUSER+2;
    constant XE_STATUS_TDATA: natural := XE_M_TUSER+3;
    constant XE_STATUS_TVALID: natural := XE_M_TUSER+4;
    constant XE_FRAME_STARTED: natural := XE_M_TUSER+5;
    constant XE_TLAST_UNEXPECTED: natural := XE_M_TUSER+6;
    constant XE_TLAST_MISSING: natural := XE_M_TUSER+7;
    constant XE_FFT_OVERFLOW: natural := XE_M_TUSER+8;
    constant XE_STATUS_CHANNEL_HALT: natural := XE_M_TUSER+9;
    constant XE_DATA_IN_CHANNEL_HALT: natural := XE_M_TUSER+10;
    constant XE_DATA_OUT_CHANNEL_HALT: natural := XE_M_TUSER+11;
    constant XE_ERROR: natural := XE_M_TUSER+12;
    constant XE_OUT_COUNT: natural := XE_M_TUSER+13;
    constant XE_UNKNOWN: natural := 2;
    constant XE_ERR_CMODEL: natural := 1;
    constant XE_ERR_SLOTS: natural := 2;

    type xe_out_a is array(0 to XE_OUT_COUNT-1) of integer;

    -- C functions of xfft_engine.h; the bodies only run if the library is missing
    impure function xfft_engine_new(nfft, input_width, twiddle_width, convergent, config_width, data_width,
                                    user_width: integer) return integer;
    attribute foreign of xfft_engine_new: function is "VHPIDIRECT xfft_engine_new";
    procedure xfft_engine_step(id, aresetn_v, config_tvalid, config_tdata, data_tvalid, d0, d1, d2, d3, d4,
                               data_tlast, m_data_tready, m_status_tready: in integer; result: out xe_out_a);
    attribute foreign of xfft_engine_step: procedure is "VHPIDIRECT xfft_engine_step";

    impure function xfft_engine_new(nfft, input_width, twiddle_width, convergent, config_width, data_width,
                                    user_width: integer) return integer is
    begin
        report "xfft_engine_new: C library libxfft_engine.so not loaded" severity failure;
        return -1;
    end;

    procedure xfft_engine_step(id, aresetn_v, config_tvalid, config_tdata, data_tvalid, d0, d1, d2, d3, d4,
                               data_tlast, m_data_tready, m_status_tready: in integer; result: out xe_out_a) is
    begin
        report "xfft_engine_step: C library libxfft_engine.so not loaded" severity failure;
    end;

    type bit_value_a is array(std_ulogic) of natural;
    -- A single bit as the engine takes it: 0, 1 or unknown
    constant BIT3: bit_value_a := ('1' | 'H' => 1, '0' | 'L' => 0, others => XE_UNKNOWN);
    -- A bit of a data or config word: unknown is 0
    constant BIT2: bit_value_a := ('1' | 'H' => 1, others => 0);

    -- Bits lo to lo+15 of a word (fewer at its top, none above it) as an unsigned number
    function chunk(v: std_logic_vector; lo: natural) return integer is
        variable v_r: integer := 0;
    begin
        for i in lo+15 downto lo loop
            v_r := 2*v_r;
            if i <= v'high then
                v_r := v_r + BIT2(v(i));
            end if;
        end loop;
        return v_r;
    end;

    function to_int(v: std_logic_vector) return integer is
        variable v_r: integer := 0;
    begin
        for i in v'range loop -- 'left (the MSB of a downto vector) first
            v_r := 2*v_r + BIT2(v(i));
        end loop;
        return v_r;
    end;

    function to_sl(i: integer) return std_logic is
    begin
        if i mod 2 = 1 then
            return '1';
        end if;
        return '0';
    end;

    function to_slv(i: integer; bits: positive) return std_logic_vector is
    begin
        return std_logic_vector(to_unsigned(i, bits));
    end;
begin
    engine_pr: process
        variable v_id: integer;
        variable v_out: xe_out_a;
        variable v_m: std_logic_vector(16*XE_CHUNKS-1 downto 0);
    begin
        -- The C side takes int32_t; NVC --std=2019 has a 64-bit integer (the result array would come back garbled)
        assert integer'high = 2147483647
            report "xfft_core: VHPIDIRECT needs a 32-bit integer (VHDL-2008)" severity failure;
        assert XFFT_DATA_WIDTH <= 16*XE_CHUNKS
            report "xfft_core: DATA_WIDTH is wider than the engine's data chunks" severity failure;
        v_id := xfft_engine_new(XFFT_NFFT, XFFT_INPUT_WIDTH, XFFT_TWIDDLE_WIDTH, XFFT_CONVERGENT, XFFT_CONFIG_WIDTH,
                                XFFT_DATA_WIDTH, XFFT_USER_WIDTH);
        assert v_id >= 0 report "xfft_core: xfft_engine_new failed" severity failure;
        loop
            wait until rising_edge(aclk);
            xfft_engine_step(v_id, BIT3(aresetn), BIT3(s_axis_config_tvalid), to_int(s_axis_config_tdata),
                             BIT3(s_axis_data_tvalid), chunk(s_axis_data_tdata, 0), chunk(s_axis_data_tdata, 16),
                             chunk(s_axis_data_tdata, 32), chunk(s_axis_data_tdata, 48),
                             chunk(s_axis_data_tdata, 64), BIT3(s_axis_data_tlast), BIT3(m_axis_data_tready),
                             BIT3(m_axis_status_tready), v_out);
            assert v_out(XE_ERROR) /= XE_ERR_CMODEL report "xfft_core: C model failed" severity failure;
            assert v_out(XE_ERROR) /= XE_ERR_SLOTS
                report "xfft_core: out of result slots (internal error)" severity failure;
            assert v_out(XE_ERROR) = 0 report "xfft_core: C engine failed" severity failure;
            for c in 0 to XE_CHUNKS-1 loop
                v_m(16*c+15 downto 16*c) := to_slv(v_out(XE_M_TDATA_0+c), 16);
            end loop;
            s_axis_config_tready <= to_sl(v_out(XE_CONFIG_TREADY));
            s_axis_data_tready <= to_sl(v_out(XE_DATA_TREADY));
            m_axis_data_tdata <= v_m(XFFT_DATA_WIDTH-1 downto 0);
            m_axis_data_tuser <= to_slv(v_out(XE_M_TUSER), XFFT_USER_WIDTH);
            m_axis_data_tvalid <= to_sl(v_out(XE_M_TVALID));
            m_axis_data_tlast <= to_sl(v_out(XE_M_TLAST));
            m_axis_status_tdata <= to_slv(v_out(XE_STATUS_TDATA), 8);
            m_axis_status_tvalid <= to_sl(v_out(XE_STATUS_TVALID));
            event_frame_started <= to_sl(v_out(XE_FRAME_STARTED));
            event_tlast_unexpected <= to_sl(v_out(XE_TLAST_UNEXPECTED));
            event_tlast_missing <= to_sl(v_out(XE_TLAST_MISSING));
            event_fft_overflow <= to_sl(v_out(XE_FFT_OVERFLOW));
            event_status_channel_halt <= to_sl(v_out(XE_STATUS_CHANNEL_HALT));
            event_data_in_channel_halt <= to_sl(v_out(XE_DATA_IN_CHANNEL_HALT));
            event_data_out_channel_halt <= to_sl(v_out(XE_DATA_OUT_CHANNEL_HALT));
        end loop;
    end process;
end;
