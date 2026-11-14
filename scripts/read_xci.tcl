# Reads the configuration of xfft IP instances (.xci files) with Vivado: the IP definition (VLNV), the part and
# every CONFIG.* property, as Vivado itself resolves them. Used by characterize/xci.py.
#
#   vivado -mode batch -nojournal -nolog -source scripts/read_xci.tcl -tclargs <out file> <xci> [<xci> ...]
#
# Each .xci is opened in an in-memory project of its own (read_ip) and only read: nothing is generated, upgraded
# or written next to it. The .xci may come from another Vivado version or another part; an IP that is locked for
# that reason can still be read. <out file> gets tab-separated lines:
#   vivado        <version -short>
#   ip            <xci path>              starts the lines of one IP
#   newest_xfft   <VLNV>                  the newest xfft this Vivado has (what create_xfft_core generates)
#   IPDEF / PART / NAME <value>
#   ARCHITECTURE / FAMILY <value>         of the part, when this Vivado knows it (characterize/parts.py)
#   CONFIG.<name> <value>                 every CONFIG property of the IP

set out [open [lindex $argv 0] w]
puts $out "vivado\t[version -short]"
foreach xci [lrange $argv 1 end] {
    create_project -in_memory
    read_ip $xci
    set ip [lindex [get_ips -all] 0]
    if {$ip eq {}} {
        error "read_xci: $xci: no IP in it"
    }
    puts $out "ip\t$xci"
    set defs [lsort -dictionary [get_ipdefs -all -filter {NAME == xfft}]]
    puts $out "newest_xfft\t[lindex $defs end]"
    foreach p {IPDEF PART NAME} {
        puts $out "$p\t[get_property $p $ip]"
    }
    set part [get_parts -quiet [get_property PART $ip]]
    if {$part ne {}} {
        foreach p {ARCHITECTURE FAMILY} {
            puts $out "$p\t[get_property $p $part]"
        }
    }
    foreach p [lsort [list_property $ip CONFIG.*]] {
        puts $out "$p\t[get_property $p $ip]"
    }
    close_project
}
close $out
