// Plain XSim example: one forward FFT of a complex tone through the stand-in, no cocotb.
//
// The core is `XFFT_CORE_NAME (default xfft_core): the stand-in (the C engine's SV wrapper, model/xfft_engine.sv)
// compiled under the core's name. All widths and the timing constants come from the generated package
// xfft_model_pkg (scripts/build_engine.py), so the bench follows the configuration (XFFT_CONFIG).
//
// It sends one config word (forward, SCALE_SCH 2 per stage, 1 in a radix-2 last stage: about 1/N in all), then
// N samples of exp(2 pi i TONE n / N) at a quarter of full scale, and reads the N outputs. It checks that the
// outputs come in natural order (XK_INDEX), that the largest magnitude is at bin TONE, and that the first output
// comes XFFT_LATENCY_FIRST_OUT clocks after the first input. Prints PASS or FAIL and finishes.
`ifndef XFFT_CORE_NAME
`define XFFT_CORE_NAME xfft_core
`endif

module tb_tone;
    import xfft_model_pkg::*;

    localparam int N = XFFT_N;
    localparam int W = XFFT_INPUT_WIDTH;
    localparam int P = XFFT_DATA_WIDTH / 2;     // each component padded to whole bytes
    localparam int TONE = N / 8 + 3;

    logic aclk = 0;
    logic aresetn = 0;
    logic [XFFT_CONFIG_WIDTH-1:0] s_axis_config_tdata = '0;
    logic s_axis_config_tvalid = 0;
    wire s_axis_config_tready;
    logic [XFFT_DATA_WIDTH-1:0] s_axis_data_tdata = '0;
    logic s_axis_data_tvalid = 0;
    wire s_axis_data_tready;
    logic s_axis_data_tlast = 0;
    wire [XFFT_DATA_WIDTH-1:0] m_axis_data_tdata;
    wire [XFFT_USER_WIDTH-1:0] m_axis_data_tuser;
    wire m_axis_data_tvalid;
    logic m_axis_data_tready = 1;
    wire m_axis_data_tlast;
    wire [7:0] m_axis_status_tdata;
    wire m_axis_status_tvalid;
    logic m_axis_status_tready = 1;
    wire event_frame_started, event_tlast_unexpected, event_tlast_missing, event_fft_overflow;
    wire event_status_channel_halt, event_data_in_channel_halt, event_data_out_channel_halt;

    `XFFT_CORE_NAME fft (.*);

    always #5 aclk = ~aclk;

    longint cycle = 0;
    always @(posedge aclk) cycle <= cycle + 1;

    function automatic bit [XFFT_CONFIG_WIDTH-1:0] config_word(bit forward);
        bit [XFFT_CONFIG_WIDTH-1:0] word = forward;
        for (int s = 0; s < XFFT_STAGES; s++)
            word |= (XFFT_NFFT % 2 && s == XFFT_STAGES - 1 ? 1 : 2) << (1 + 2 * s);
        return word;
    endfunction

    function automatic bit [XFFT_DATA_WIDTH-1:0] sample(int n);
        real phase = 2.0 * 3.14159265358979 * TONE * n / N;
        real amplitude = 2.0 ** (W - 3);
        bit [XFFT_DATA_WIDTH-1:0] word = '0;
        word[P-1:0] = P'($rtoi(amplitude * $cos(phase)));
        word[2*P-1:P] = P'($rtoi(amplitude * $sin(phase)));
        return word;
    endfunction

    longint first_in = -1, first_out = -1;
    int errors = 0;

    // Handshakes are sampled at the falling edge, when the core's outputs of the last rising edge have settled:
    // tvalid and tready high there means a handshake at the next rising edge, at clock `cycle`.

    // Input: the config word, then the frame
    initial begin
        automatic bit taken;
        repeat (20) @(posedge aclk);
        aresetn <= 1;
        s_axis_config_tdata <= config_word(1);
        s_axis_config_tvalid <= 1;
        do begin
            @(negedge aclk) taken = s_axis_config_tready;
            @(posedge aclk);
        end while (!taken);
        s_axis_config_tvalid <= 0;
        for (int n = 0; n < N; n++) begin
            s_axis_data_tdata <= sample(n);
            s_axis_data_tlast <= n == N - 1;
            s_axis_data_tvalid <= 1;
            do begin
                @(negedge aclk) taken = s_axis_data_tready;
                if (taken && n == 0)
                    first_in = cycle;
                @(posedge aclk);
            end while (!taken);
        end
        s_axis_data_tvalid <= 0;
        s_axis_data_tlast <= 0;
    end

    // Output: N samples, the peak and the latency
    initial begin
        automatic int j = 0, peak = -1;
        automatic real best = -1.0;
        while (j < N) begin
            @(negedge aclk);
            if (m_axis_data_tvalid && m_axis_data_tready) begin
                automatic real re = real'(signed'(m_axis_data_tdata[W-1:0]));
                automatic real im = real'(signed'(m_axis_data_tdata[P+W-1:P]));
                if (j == 0)
                    first_out = cycle;
                if (int'(m_axis_data_tuser[XFFT_OVFLO_BIT-1:0]) != j) begin
                    $display("FAIL: output %0d has XK_INDEX %0d", j, m_axis_data_tuser[XFFT_OVFLO_BIT-1:0]);
                    errors++;
                end
                if (m_axis_data_tlast != (j == N - 1)) begin
                    $display("FAIL: tlast on output %0d", j);
                    errors++;
                end
                if (re * re + im * im > best) begin
                    best = re * re + im * im;
                    peak = j;
                end
                j++;
            end
        end
        $display("tb_tone: %0d points, %0d-bit data: peak at bin %0d (expected %0d), magnitude %0.1f", N, W, peak,
                 TONE, $sqrt(best));
        $display("tb_tone: first input -> first output %0d clocks (XFFT_LATENCY_FIRST_OUT = %0d)",
                 first_out - first_in, XFFT_LATENCY_FIRST_OUT);
        if (peak != TONE)
            errors++;
        if (first_out - first_in != XFFT_LATENCY_FIRST_OUT)
            errors++;
        if (errors == 0)
            $display("PASS");
        else
            $display("FAIL: %0d errors", errors);
        $finish;
    end

    initial begin
        #(10 * (N * 8 + 1000));
        $display("FAIL: timeout");
        $finish;
    end
endmodule
