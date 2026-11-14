// Input gaps with the output blocked, on the C engine alone (no simulator). Offers input samples every clock from
// the first clock with s_axis_data_tready high, m_axis_data_tready low throughout. stdin: one pattern per line,
// "d:g d:g ...": g clocks with s_axis_data_tvalid low before input sample F - d, where F is the number of samples
// the engine accepts with no gaps (measured first). stdout: "F <value>", then per pattern the shortfall, F minus
// the samples accepted before the first refused clock.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "xfft_engine.h"
#include "xfft_model.h"

#define RESET_CLOCKS 10
#define MAX_CLOCKS 100000000L

// Samples accepted before the first refused clock; gap[i] idle clocks before sample i (gap may be NULL)
static int accepted(const int* gap, int n_gap)
{
    const int id = xfft_engine_new(XFFT_NFFT, XFFT_INPUT_WIDTH, XFFT_TWIDDLE_WIDTH, XFFT_CONVERGENT,
                                   XFFT_CONFIG_WIDTH, XFFT_DATA_WIDTH, XFFT_USER_WIDTH);
    if (id < 0)
        exit(2);
    int32_t out[XE_OUT_COUNT];
    for (int c = 0; c < RESET_CLOCKS; c++)
        xfft_engine_step(id, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, out);
    int i = 0;
    int idle_left = gap ? gap[0] : 0;
    int started = 0;
    for (long c = 0; c < MAX_CLOCKS; c++) {
        const int tready = out[XE_DATA_TREADY];
        started = started || tready;
        int tvalid = 0;
        if (started) {
            if (idle_left > 0)
                idle_left--;
            else
                tvalid = 1;
        }
        if (tvalid && !tready)
            break;
        xfft_engine_step(id, 1, 0, 0, tvalid, 0, 0, 0, 0, 0, 0, 0, 1, out);
        if (tvalid && tready) {
            i++;
            idle_left = gap && i < n_gap ? gap[i] : 0;
        }
    }
    xfft_engine_free(id);
    return i;
}

int main(void)
{
    const int fill = accepted(NULL, 0);
    printf("F %d\n", fill);
    const int n_gap = fill + 64;
    int* gap = calloc(n_gap, sizeof(*gap));
    if (!gap)
        return 2;
    static char line[1 << 16];
    while (fgets(line, sizeof line, stdin)) {
        memset(gap, 0, n_gap * sizeof(*gap));
        for (char* tok = strtok(line, " \n"); tok; tok = strtok(NULL, " \n")) {
            int d, g;
            if (sscanf(tok, "%d:%d", &d, &g) == 2 && fill - d >= 0 && fill - d < n_gap)
                gap[fill - d] = g;
        }
        printf("%d\n", fill - accepted(gap, n_gap));
        fflush(stdout);
    }
    free(gap);
    return 0;
}
