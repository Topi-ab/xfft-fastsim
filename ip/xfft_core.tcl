# AMD LogiCORE FFT (xfft 9.x) as this repository uses it. The .xci is generated from here, never edited.
#
# Fixed by this file (the stand-in model assumes them):
# - pipelined streaming I/O, fixed point, natural-order output;
# - non-real-time throttling, so the core honours m_axis_data_tready;
# - XK_INDEX and OVFLO in m_axis_data_tuser, aresetn;
# - one core for both directions: FWD_INV and SCALE_SCH come per frame on the config channel.
# Parameters (dict, see model/xfft_config.py, which writes the call):
#   points          transform length, 8 .. 65536
#   input_width     data width, 8 .. 34 (the output has the same width when scaled)
#   twiddle_width   phase factor width, 8 .. 34
#   scaling         scaled | unscaled | block_floating_point (the stand-in supports scaled only)
#   rounding        convergent_rounding | truncation
#   clock_mhz       target clock frequency: changes the pipeline, so the latency
#   version         xfft version, e.g. 9.1; default: the newest this Vivado has. Only major version 9 is
#                   accepted: the stand-in's C-model shim (model/xfft_dpi.c) is written for its API.
#
#   source xfft_core.tcl
#   create_xfft_core <ip dir> <module name> {points 1024 input_width 16 ...}

proc create_xfft_core {ip_dir module_name params} {
    set p [dict merge {points 1024 input_width 16 twiddle_width 16 scaling scaled rounding convergent_rounding
                       clock_mhz 100 version {}} $params]
    # IP definitions are xilinx.com:ip:xfft:<major>.<minor>
    set defs [lsort -dictionary [get_ipdefs -all -filter {NAME == xfft}]]
    if {[llength $defs] == 0} {
        error "create_xfft_core: this Vivado has no xfft IP"
    }
    if {[dict get $p version] eq {}} {
        set vlnv [lindex $defs end]
    } else {
        set vlnv [lsearch -inline -exact $defs "xilinx.com:ip:xfft:[dict get $p version]"]
        if {$vlnv eq {}} {
            error "create_xfft_core: xfft [dict get $p version] not in this Vivado (it has: $defs)"
        }
    }
    set major [lindex [split [lindex [split $vlnv :] end] .] 0]
    if {$major != 9} {
        error "create_xfft_core: $vlnv: the stand-in is written for xfft 9.x"
    }
    set config [list \
        CONFIG.transform_length [dict get $p points] \
        CONFIG.implementation_options pipelined_streaming_io \
        CONFIG.target_clock_frequency [dict get $p clock_mhz] \
        CONFIG.data_format fixed_point \
        CONFIG.scaling_options [dict get $p scaling] \
        CONFIG.rounding_modes [dict get $p rounding] \
        CONFIG.input_width [dict get $p input_width] \
        CONFIG.phase_factor_width [dict get $p twiddle_width] \
        CONFIG.output_ordering natural_order \
        CONFIG.throttle_scheme nonrealtime \
        CONFIG.xk_index true \
        CONFIG.aresetn true \
    ]
    if {[dict get $p scaling] eq "scaled"} {
        lappend config CONFIG.ovflo true
    }
    file mkdir $ip_dir
    create_ip -vlnv $vlnv -module_name $module_name -dir $ip_dir
    set ip [get_ips $module_name]
    set_property -dict $config $ip
    return $ip
}
