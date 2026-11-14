// Timing bench of the xfft core, plain XSim: measures the cycle timing of whatever module `XFFT_CORE_NAME is,
// the real core or the stand-in (the C engine's SV wrapper). One case per run (xsim -testplusarg CASE=<name>), so
// that the cases can run as separate processes at the same time (xfft_timing_bench.py, which also derives and
// checks the constants from the numbers printed here). Every case starts from reset and drives forward frames of
// random data; it stops as soon as it has what it measures.
//
//   first_out     one ideal frame, stop at the first output: latency, reset to ready, frame_started
//   last_out      one ideal frame, stop after tlast: latency to the last output, status word
//   gap           input gap of +LEN=<n> clocks before sample N/2, stop at the first output
//   stall         output stall of +LEN=<n> clocks before output sample N/2, stop after tlast
//   stall_end     output stall of +LEN=<n> clocks before output sample N - +FROM_END=<k> of frame 0, +FRAMES=<n>
//                 frames back to back (default 1; 2: frame 0 in a stream), stop after frame 0's tlast
//   back_to_back  two frames back to back, frame 1's config word offered while frame 0 loads
//   starve        frame 0 complete, frame 1 stops after 16 samples until 3N + 1000 clocks after frame 0's
//                 first input (later than its first output would be due), stop at frame 0's first output
//   halt_1        three frames back to back, frame 2 with a 1-clock input gap before every sample k = 32 mod 64,
//                 stop at frame 0's last output
//   blocked       m_axis_data_tready low throughout, frames offered back to back: samples accepted
//   resume        like blocked, until the input has been refused for 100 clocks; then m_axis_data_tready high:
//                 clocks from the first output handshake to the input's resume
//   release       like resume, but m_axis_data_tready high for +LEN=<k> output handshakes only, then low
//                 again; stop 200 clocks later: input handshakes after the release began
//   speed         +FRAMES=<n> ideal frames back to back: simulated clocks per wall-clock second
//   probe         +FRAMES=<n> frames (default 1, at most 3 reported) back to back with up to 4 input gaps
//                 (+GAPPOS<i>=<sample> +GAPLEN<i>=<clocks>, i = 0..3) and up to 4 output stalls (+STALLPOS<i>,
//                 +STALLLEN<i>); positions count over all frames (frame f, sample k: f * N + k). Stops after
//                 the last tlast: for exploring a core
// Output: one line "XFFT_TIMING_RESULT <case> key=value ..." (the keys are explained in xfft_timing_bench.py).
// The configuration's names (scripts/build_engine.py): its package `XFFT_MODEL_PKG and the C name of the engine
// library's clock `XFFT_ENGINE_WALLCLOCK (xfft_model_pkg and xfft_engine_wallclock for module xfft_core).
`ifndef XFFT_CORE_NAME
`define XFFT_CORE_NAME xfft_core
`endif
`ifndef XFFT_MODEL_PKG
`define XFFT_MODEL_PKG xfft_model_pkg
`endif
`ifndef XFFT_ENGINE_WALLCLOCK
`define XFFT_ENGINE_WALLCLOCK xfft_engine_wallclock
`endif
`timescale 1ps / 1ps
module tb_xfft_timing;
    import `XFFT_MODEL_PKG::*;
    // The engine's library, linked for every model
    import "DPI-C" `XFFT_ENGINE_WALLCLOCK = function real xfft_engine_wallclock();

    localparam int N = XFFT_N;
    localparam int W = XFFT_INPUT_WIDTH;
    localparam int P = XFFT_DATA_WIDTH / 2;
    localparam int STARVE_AT = 16;
    localparam longint STARVE_UNTIL = 3 * N + 1000;

    // Forward, SCALE_SCH 2 in every stage (1 in the radix-2 last stage of an odd NFFT): no overflow
    function automatic bit [XFFT_CONFIG_WIDTH-1:0] config_word();
        bit [XFFT_CONFIG_WIDTH-1:0] word = 1;
        for (int s = 0; s < XFFT_STAGES; s++)
            word |= (XFFT_NFFT % 2 && s == XFFT_STAGES - 1 ? 1 : 2) << (1 + 2 * s);
        return word;
    endfunction

    logic clk = 1'b0;
    logic aresetn = 1'b0;
    logic [XFFT_CONFIG_WIDTH-1:0] cfg_tdata = config_word();
    logic cfg_tvalid = 1'b0;
    wire cfg_tready;
    logic [XFFT_DATA_WIDTH-1:0] s_tdata = '0;
    logic s_tvalid = 1'b0, s_tlast = 1'b0;
    wire s_tready;
    wire [XFFT_DATA_WIDTH-1:0] m_tdata;
    wire [XFFT_USER_WIDTH-1:0] m_tuser;
    wire m_tvalid, m_tlast;
    logic m_tready = 1'b0;
    wire ev_in_halt, ev_out_halt, ev_started, st_tvalid;

    always #2000 clk = ~clk;   // simulation clock; only clock counts matter

    `XFFT_CORE_NAME dut(
        .aclk(clk),
        .aresetn(aresetn),
        .s_axis_config_tdata(cfg_tdata),
        .s_axis_config_tvalid(cfg_tvalid),
        .s_axis_config_tready(cfg_tready),
        .s_axis_data_tdata(s_tdata),
        .s_axis_data_tvalid(s_tvalid),
        .s_axis_data_tready(s_tready),
        .s_axis_data_tlast(s_tlast),
        .m_axis_data_tdata(m_tdata),
        .m_axis_data_tuser(m_tuser),
        .m_axis_data_tvalid(m_tvalid),
        .m_axis_data_tready(m_tready),
        .m_axis_data_tlast(m_tlast),
        .m_axis_status_tdata(),
        .m_axis_status_tvalid(st_tvalid),
        .m_axis_status_tready(1'b1),
        .event_frame_started(ev_started),
        .event_tlast_unexpected(),
        .event_tlast_missing(),
        .event_fft_overflow(),
        .event_status_channel_halt(),
        .event_data_in_channel_halt(ev_in_halt),
        .event_data_out_channel_halt(ev_out_halt)
    );

    function automatic bit [31:0] mix(bit [31:0] x);
        x ^= x >> 16;
        x *= 32'h7feb352d;
        x ^= x >> 15;
        x *= 32'h846ca68b;
        x ^= x >> 16;
        return x;
    endfunction

    // Random sample, |re|, |im| < 2^(W-3)
    function automatic bit [XFFT_DATA_WIDTH-1:0] sample(int f, int k);
        int re = signed'(mix(32'h5a5a_0000 ^ (f << 17 | k) << 1)) >>> (35 - W);
        int im = signed'(mix(32'h5a5a_0001 ^ (f << 17 | k) << 1)) >>> (35 - W);
        return {P'(longint'(im)), P'(longint'(re))};
    endfunction

    typedef enum {STOP_FIRST_OUT, STOP_LAST_OUT, STOP_INPUT_DONE, STOP_BLOCKED, STOP_RESUMED, STOP_RELEASED} stop_t;

    string name;
    int in_frames = 1, gap_len = 0, stall_len = 0;
    int gap_pos[4] = '{-1, -1, -1, -1}, gap_lens[4] = '{0, 0, 0, 0};
    int stall_pos[4] = '{-1, -1, -1, -1}, stall_lens[4] = '{0, 0, 0, 0};
    bit starve = 0, halt_1 = 0, blocked = 0, resume = 0;
    int from_end = 0;
    int release_len = -1;                           // release: output handshakes of the release
    stop_t stop = STOP_FIRST_OUT;

    // Measurements (edge indices)
    longint cycle = 0, reset_edge = -1, data_ready_edge = -1, cfg_ready_edge = -1;
    longint cfg_rise[2] = '{-1, -1}, cfg_acc[2] = '{-1, -1};
    longint first_in[3] = '{-1, -1, -1}, last_in[3] = '{-1, -1, -1};
    longint first_out = -1, last_out = -1, resume_at = -1, last_hs = -1, max_gap = 0;
    longint frame_first_out[3] = '{-1, -1, -1}, frame_last_out[3] = '{-1, -1, -1};
    int in_stalls = 0, in_halts = 0, out_halts = 0, halt_gaps = 0, accepted = 0, cfg_refused = 0;
    longint started_edge = -1, status_edge = -1, idle = 0, last_tlast = -1;
    longint release_edge = -1, resume_edge = -1;   // first output handshake after a stall, input resume after it
    int released = 0, release_in = 0;               // output and input handshakes from release_edge on
    bit out_stalled = 0;                            // the output was stalled at the edge before
    bit finished = 0;
    real wall0, wall1;

    int cfg_sent = 0, in_f = 0, in_k = 0, gap_left = 0, rx_j = 0, stall_left = 0;
    bit gap_done = 0, stall_done = 0;

    initial begin
        if (!$value$plusargs("CASE=%s", name))
            $fatal(1, "XFFT_TIMING: no +CASE=<name> (xsim -testplusarg CASE=<name>)");
        case (name)
            "first_out": ;
            "last_out": stop = STOP_LAST_OUT;
            "gap": begin
                if (!$value$plusargs("LEN=%d", gap_len))
                    $fatal(1, "XFFT_TIMING: gap needs +LEN=<n>");
                gap_pos[0] = N / 2;
                gap_lens[0] = gap_len;
            end
            "stall": begin
                if (!$value$plusargs("LEN=%d", stall_len))
                    $fatal(1, "XFFT_TIMING: stall needs +LEN=<n>");
                stall_pos[0] = N / 2;
                stall_lens[0] = stall_len;
                stop = STOP_LAST_OUT;
            end
            "stall_end": begin
                if (!$value$plusargs("LEN=%d", stall_len) || !$value$plusargs("FROM_END=%d", from_end))
                    $fatal(1, "XFFT_TIMING: stall_end needs +LEN=<n> +FROM_END=<k>");
                void'($value$plusargs("FRAMES=%d", in_frames));
                stall_pos[0] = N - from_end;
                stall_lens[0] = stall_len;
                stop = STOP_LAST_OUT;
            end
            "probe": begin
                void'($value$plusargs("FRAMES=%d", in_frames));
                for (int i = 0; i < 4; i++) begin
                    void'($value$plusargs($sformatf("GAPPOS%0d=%%d", i), gap_pos[i]));
                    void'($value$plusargs($sformatf("GAPLEN%0d=%%d", i), gap_lens[i]));
                    void'($value$plusargs($sformatf("STALLPOS%0d=%%d", i), stall_pos[i]));
                    void'($value$plusargs($sformatf("STALLLEN%0d=%%d", i), stall_lens[i]));
                    gap_len += gap_lens[i];
                    stall_len += stall_lens[i];
                end
                stop = STOP_LAST_OUT;
            end
            "back_to_back": begin in_frames = 2; stop = STOP_INPUT_DONE; end
            "starve": begin in_frames = 2; starve = 1; end
            "halt_1": begin in_frames = 3; halt_1 = 1; stop = STOP_LAST_OUT; end
            "blocked": begin in_frames = 1000; blocked = 1; stop = STOP_BLOCKED; end
            "resume": begin in_frames = 1000; blocked = 1; resume = 1; stop = STOP_RESUMED; end
            "release": begin
                if (!$value$plusargs("LEN=%d", release_len))
                    $fatal(1, "XFFT_TIMING: release needs +LEN=<k>");
                in_frames = 1000;
                blocked = 1;
                resume = 1;
                stop = STOP_RELEASED;
            end
            "speed": begin
                if (!$value$plusargs("FRAMES=%d", in_frames))
                    $fatal(1, "XFFT_TIMING: speed needs +FRAMES=<n>");
                stop = STOP_LAST_OUT;
            end
            default: $fatal(1, "XFFT_TIMING: unknown case %s", name);
        endcase
        repeat (100) @(posedge clk);
        aresetn <= 1'b1;
        wall0 = xfft_engine_wallclock();
        wait (finished);
        wall1 = xfft_engine_wallclock();
        report();
        $finish;
    end

    always @(posedge clk) begin
        if (aresetn) begin
            if (reset_edge < 0)
                reset_edge = cycle;
            if (s_tready && data_ready_edge < 0)
                data_ready_edge = cycle;
            if (cfg_tready && cfg_ready_edge < 0)
                cfg_ready_edge = cycle;
            // Handshakes of this edge
            if (cfg_tvalid && cfg_sent < 2 && cfg_rise[cfg_sent] < 0)
                cfg_rise[cfg_sent] = cycle;
            if (cfg_tvalid && !cfg_tready)
                cfg_refused++;
            if (cfg_tvalid && cfg_tready) begin
                if (cfg_sent < 2)
                    cfg_acc[cfg_sent] = cycle;
                cfg_sent++;
            end
            if (s_tvalid && !s_tready)
                in_stalls++;
            if (s_tvalid && s_tready) begin
                if (release_edge >= 0 && resume_edge < 0 && in_stalls > 0)
                    resume_edge = cycle;
                if (release_edge >= 0)
                    release_in++;
                accepted++;
                idle = 0;
                if (in_k == 0 && in_f < 3)
                    first_in[in_f] = cycle;
                else if (in_k > 0 && cycle - last_hs - 1 > max_gap)
                    max_gap = cycle - last_hs - 1;  // longest input gap inside a frame
                last_hs = cycle;
                if (in_k == N - 1) begin
                    if (in_f < 3)
                        last_in[in_f] = cycle;
                    in_f++;
                    in_k = 0;
                end else
                    in_k++;
                gap_done = 0;
            end else
                idle++;
            if (m_tvalid && m_tready && out_stalled && release_edge < 0)
                release_edge = cycle;
            if (m_tvalid && m_tready && release_edge >= 0)
                released++;
            out_stalled = m_tvalid && !m_tready;
            if (m_tvalid && m_tready) begin
                if (rx_j == 0)
                    first_out = cycle;
                if (rx_j % N == 0 && rx_j / N < 3)
                    frame_first_out[rx_j / N] = cycle;
                if (m_tlast && rx_j / N < 3)
                    frame_last_out[rx_j / N] = cycle;
                if (m_tlast && last_out < 0)
                    last_out = cycle;       // frame 0
                if (m_tlast)
                    last_tlast = cycle;
                rx_j++;
                stall_done = 0;
            end
            in_halts += ev_in_halt;
            if (ev_started && started_edge < 0)
                started_edge = cycle;
            if (st_tvalid && status_edge < 0)
                status_edge = cycle;
            // Gap clocks inside frames 1 and 2 before frame 0's last output
            if (in_f >= 1 && in_f < in_frames && in_k > 0 && !s_tvalid && last_out < 0)
                halt_gaps++;
            out_halts += ev_out_halt;
            if (starve && in_f == 1 && in_k == STARVE_AT && resume_at < 0)
                resume_at = first_in[0] + STARVE_UNTIL;

            // Drive this clock's inputs
            cfg_tvalid <= cfg_sent < in_frames && (cfg_sent == 0 || in_f > cfg_sent - 1
                                                   || (in_f == cfg_sent - 1 && in_k > 0));
            if (gap_left > 0) begin
                s_tvalid <= 1'b0;
                gap_left--;
            end else if (in_f < in_frames && cfg_sent > in_f) begin
                if (starve && in_f == 1 && in_k == STARVE_AT && cycle + 1 < resume_at)
                    s_tvalid <= 1'b0;
                else if (!gap_done && halt_1 && in_f == 2 && in_k % 64 == 32) begin
                    s_tvalid <= 1'b0;
                    gap_done = 1;
                end else if (!gap_done && gap_at(in_f * N + in_k) > 0) begin
                    s_tvalid <= 1'b0;
                    gap_left = gap_at(in_f * N + in_k) - 1;
                    gap_done = 1;
                end else begin
                    s_tvalid <= 1'b1;
                    s_tdata <= sample(in_f, in_k);
                    s_tlast <= in_k == N - 1;
                    gap_done = 1;
                end
            end else
                s_tvalid <= 1'b0;
            if (blocked && resume && in_stalls >= 100 && (release_len < 0 || released < release_len))
                m_tready <= 1'b1;
            else if (blocked)
                m_tready <= 1'b0;
            else if (stall_left > 0) begin
                m_tready <= 1'b0;
                stall_left--;
            end else if (!stall_done && stall_at(rx_j) > 0) begin
                m_tready <= 1'b0;
                stall_left = stall_at(rx_j) - 1;
                stall_done = 1;
            end else begin
                m_tready <= 1'b1;
                stall_done = 1;
            end

            case (stop)
                STOP_FIRST_OUT: finished <= first_out >= 0;
                // frame 0's (speed: the last frame's) tlast, and time for the status word after it
                STOP_LAST_OUT: finished <= (name == "speed" || name == "probe" ? rx_j == in_frames * N : last_out >= 0)
                                           && cycle > last_tlast + 16;
                STOP_INPUT_DONE: finished <= in_f == in_frames;
                STOP_BLOCKED: finished <= idle > 4 * N;
                STOP_RESUMED: finished <= resume_edge >= 0;
                STOP_RELEASED: finished <= release_edge >= 0 && cycle > release_edge + 200;
            endcase
            if (cycle - reset_edge > (in_frames + 8) * 4 * N)
                $fatal(1, "XFFT_TIMING %s: timeout", name);
        end
        cycle++;
    end

    // Clocks of input gap before input sample k, of output stall before output sample j (over all frames)
    function automatic int gap_at(int k);
        foreach (gap_pos[i])
            if (gap_pos[i] == k)
                return gap_lens[i];
        return 0;
    endfunction

    function automatic int stall_at(int j);
        foreach (stall_pos[i])
            if (stall_pos[i] == j)
                return stall_lens[i];
        return 0;
    endfunction

    task automatic report();
        longint clocks = cycle - reset_edge;
        $display("XFFT_TIMING_RESULT %s reset_to_data_ready=%0d reset_to_cfg_ready=%0d reset_to_cfg0_accept=%0d cfg1_accept=%0d cfg1_offered=%0d first_in0=%0d last_in0=%0d first_in1=%0d last_in1=%0d first_out=%0d last_out=%0d resume=%0d max_gap=%0d halt_gaps=%0d started=%0d status=%0d in_stalls=%0d in_halt_events=%0d out_halt_events=%0d accepted=%0d cfg_refused=%0d gap_len=%0d stall_len=%0d frames=%0d out_samples=%0d last_tlast=%0d lat1_first=%0d lat1_last=%0d lat2_first=%0d lat2_last=%0d status_after_release=%0d resume_in=%0d release_in=%0d clocks=%0d wall_s=%0.3f",
                 name, data_ready_edge - reset_edge, cfg_ready_edge - reset_edge, cfg_acc[0] - reset_edge,
                 cfg_acc[1] - cfg_rise[1], cfg_rise[1] - first_in[0], first_in[0] - reset_edge,
                 last_in[0] - first_in[0], first_in[1] - first_in[0], last_in[1] - first_in[0],
                 first_out - first_in[0], last_out - first_in[0], resume_at - first_in[0], max_gap, halt_gaps,
                 started_edge - first_in[0], status_edge - last_out, in_stalls, in_halts, out_halts, accepted,
                 cfg_refused, gap_len, stall_len, in_frames, rx_j, last_tlast - first_in[0], frame_first_out[1] - first_in[1], frame_last_out[1] - first_in[1],
                 frame_first_out[2] - first_in[2], frame_last_out[2] - first_in[2], status_edge - release_edge,
                 resume_edge - release_edge - 1, release_in, clocks, wall1 - wall0);
    endtask
endmodule
