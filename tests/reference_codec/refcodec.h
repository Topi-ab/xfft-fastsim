// Binary format of the timing references (tests/reference/timing_reference.bin.xz, decompressed). Shared by the
// unpacker (refcodec_unpack.cpp, here) and the packer that makes the file.
//
//   "XFR1"
//   strings:  uvarint count, then each: uvarint length, bytes        (every name and text once)
//   configs:  uvarint count, then each:
//     uvarint tag, uvarint xfft_config, uvarint source               (string indices)
//     uvarint timing count, then each: uvarint name, svarint value   (measured constants; none: count 0)
//     uvarint case count, then each:
//       uvarint name; svarint tready (data), svarint tready (config)
//       for each of the RUN_FIELDS, in order: uvarint run count, then each run:
//         svarint (first clock - end of the run before, 0 at the start), uvarint count
//
// uvarint: unsigned LEB128; svarint: zigzag, then uvarint. A run is a stretch of consecutive clocks.
#ifndef REFCODEC_H
#define REFCODEC_H

#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <vector>

namespace refcodec {

constexpr char MAGIC[4] = {'X', 'F', 'R', '1'};
constexpr int N_RUN_FIELDS = 4;
// The text form's names of the RUN_FIELDS (tests/test_timing_reference.py: RUN_FIELDS)
inline const char* const RUN_FIELDS[N_RUN_FIELDS] = {"input handshakes", "output handshakes", "config handshakes",
                                                       "status clocks"};

class Reader {
public:
    explicit Reader(std::vector<uint8_t> bytes) : b_(std::move(bytes)) {}
    uint64_t uvarint()
    {
        uint64_t v = 0;
        for (int shift = 0; shift < 64; shift += 7) {
            if (pos_ >= b_.size())
                throw std::runtime_error("refcodec: unexpected end of data");
            const uint8_t byte = b_[pos_++];
            v |= uint64_t(byte & 0x7f) << shift;
            if (!(byte & 0x80))
                return v;
        }
        throw std::runtime_error("refcodec: varint too long");
    }
    int64_t svarint()
    {
        const uint64_t z = uvarint();
        return int64_t(z >> 1) ^ -int64_t(z & 1);
    }
    std::string bytes(size_t n)
    {
        if (pos_ + n > b_.size())
            throw std::runtime_error("refcodec: unexpected end of data");
        std::string s(reinterpret_cast<const char*>(b_.data() + pos_), n);
        pos_ += n;
        return s;
    }
    bool at_end() const { return pos_ == b_.size(); }

private:
    std::vector<uint8_t> b_;
    size_t pos_ = 0;
};

class Writer {
public:
    void uvarint(uint64_t v)
    {
        do {
            uint8_t byte = v & 0x7f;
            v >>= 7;
            out_.push_back(byte | (v ? 0x80 : 0));
        } while (v);
    }
    void svarint(int64_t v) { uvarint((uint64_t(v) << 1) ^ uint64_t(v >> 63)); }
    void bytes(const std::string& s) { out_.insert(out_.end(), s.begin(), s.end()); }
    const std::vector<uint8_t>& data() const { return out_; }

private:
    std::vector<uint8_t> out_;
};

inline std::vector<uint8_t> read_all(FILE* f)
{
    std::vector<uint8_t> b;
    uint8_t buf[1 << 16];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0)
        b.insert(b.end(), buf, buf + n);
    return b;
}

}  // namespace refcodec

#endif
