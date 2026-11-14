// Unpacks the binary timing references (refcodec.h) from stdin into their text form on stdout, one line each:
//   config <tag> <xfft_config>
//   source <text>
//   timing <NAME> <value>                         (one per measured constant)
//   case <name>
//   tready <data> <config>
//   runs <field index> <first> <count> ...        (one per RUN_FIELDS, in order; empty when there is none)
//   end                                           (after the cases of a configuration)
// With tags as arguments, only those configurations. tests/test_timing_reference.py builds and runs it.
#include <cstdio>
#include <set>
#include <string>

#include "refcodec.h"

int main(int argc, char** argv)
{
    try {
        std::set<std::string> want(argv + 1, argv + argc);
        refcodec::Reader r(refcodec::read_all(stdin));
        if (r.bytes(4) != std::string(refcodec::MAGIC, 4))
            throw std::runtime_error("refcodec: not a timing reference file");
        std::vector<std::string> strings(r.uvarint());
        for (auto& s : strings)
            s = r.bytes(r.uvarint());
        auto str = [&](uint64_t i) -> const std::string& {
            if (i >= strings.size())
                throw std::runtime_error("refcodec: bad string index");
            return strings[i];
        };
        const uint64_t configs = r.uvarint();
        for (uint64_t c = 0; c < configs; c++) {
            const std::string& tag = str(r.uvarint());
            const std::string& spec = str(r.uvarint());
            const std::string& source = str(r.uvarint());
            const bool show = want.empty() || want.count(tag);
            if (show)
                printf("config %s %s\nsource %s\n", tag.c_str(), spec.c_str(), source.c_str());
            for (uint64_t n = r.uvarint(); n > 0; n--) {
                const std::string& name = str(r.uvarint());
                const long long value = r.svarint();
                if (show)
                    printf("timing %s %lld\n", name.c_str(), value);
            }
            for (uint64_t n = r.uvarint(); n > 0; n--) {
                const std::string& name = str(r.uvarint());
                const long long d = r.svarint(), k = r.svarint();
                if (show)
                    printf("case %s\ntready %lld %lld\n", name.c_str(), d, k);
                for (int f = 0; f < refcodec::N_RUN_FIELDS; f++) {
                    if (show)
                        printf("runs %d", f);
                    long long end = 0;
                    for (uint64_t runs = r.uvarint(); runs > 0; runs--) {
                        const long long first = end + r.svarint();
                        const long long count = (long long)r.uvarint();
                        if (show)
                            printf(" %lld %lld", first, count);
                        end = first + count;
                    }
                    if (show)
                        printf("\n");
                }
            }
            if (show)
                printf("end\n");
        }
        if (!r.at_end())
            throw std::runtime_error("refcodec: trailing data");
    } catch (const std::exception& e) {
        fprintf(stderr, "%s\n", e.what());
        return 1;
    }
    return 0;
}
