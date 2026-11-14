# The simulation caches

Two caches share one store. The result cache (below) skips a cocotb run that passed with the same inputs. The [elaboration cache](#elaboration-cache) skips the compile and elaboration of a design whose snapshot has been built before, here or on another machine, and restores the snapshot instead.

## The result cache

A cocotb run that passed is not run again with the same inputs. `sim_runner.run(cache=True)` (the flow test, the comparison, the examples and every case of the characterization use it) looks the run up in a content-addressed store. On a hit it prints the stored log, restores the result files and lists the test as `CachedResult` in pytest's warnings summary; a real-core run of minutes becomes a hit of seconds. The implementation is `tests/sim_cache.py`.

### Enabling it

It is on by default, with a directory store `.sim_cache/` in the repository. Two environment variables control it:

| Variable | Values |
|---|---|
| `SIM_CACHE` | `on` (default: use a hit, store a pass), `off` (no lookup, no store), `refresh` (run, and overwrite the entry) |
| `SIM_CACHE_URL` | a directory (absolute, or relative to the repository root; default `.sim_cache`), or `s3://<bucket>/<prefix>` |

`python -m characterize` takes the same as `--cache` and `--cache-url`. `WAVES=1` bypasses the cache (a waveform run always runs).

### What a hit means

- **Key**: the sha256 of a canonical JSON manifest of everything that decides the result:
  - the HDL sources by content, in compile order;
  - the IP builder Tcl (self-contained: `ip/xfft_core.tcl` is inlined), so the core's configuration;
  - the stand-in's own files, its timing constants and the C-model archive generated with the core;
  - declared data inputs;
  - the test's local Python import closure and `conftest.py`, but for the caches' own code (`tests/sim_cache.py`, `tests/elab_cache.py`), which decides no result: a change to the cache does not miss every entry;
  - the run arguments: toplevel, generics, test case, seed, the environment cocotb passes on;
  - the simulator version (`xelab --version`, `nvc --version`, `ghdl --version`, `verilator --version`), on XSim Vivado's `version.dat` and patch areas;
  - the Python and package versions (the cache's own backend packages excluded).

  No timestamps, machine paths, user or host names go in, so a key is the same on every machine. The two models give different keys. Another Vivado version gives other keys, so results of different versions never mix.
- **Entry**: `<key>/files/` holds the result XML, `build.log`, `sim.log` and the test's declared outputs, each named by the sha256 of its content. `<key>/manifest.json` maps the file names to them and is written last, as the commit marker. `index/<test id>.json` points at a test's latest entry, so that a miss says which inputs changed, for example `changed since 8bd378169e1e2687: ~ sources[0].inputs[model/xfft_engine.c].sha256: ...`.
- **Policy**: only passing runs are stored; a miss builds from scratch, or takes its snapshot from the elaboration cache, whose key holds every build input. A cached run uses a fixed seed (`COCOTB_RANDOM_SEED`, else 1). The cache never fails a test: a store it cannot reach is logged and the test runs uncached.

A test that opts in (`cache=True`) follows three rules:

- the pytest function does nothing outside `sim_runner.run()`;
- every file the test or the HDL reads at run time is declared in `cache_inputs`;
- every file it writes for later is declared in `cache_outputs`.

## Command line

```sh
python tests/sim_cache.py list                 # the entries of the store, with their test and date
python tests/sim_cache.py show <key>           # one entry's manifest
python tests/sim_cache.py diff <key> <key>     # what differs between two entries' inputs
python tests/sim_cache.py elab list            # the same for the elaboration cache: build, date, size, host
python tests/sim_cache.py elab show <key>
python tests/sim_cache.py elab diff <key> <key>
```

## Directory store

The default is `.sim_cache/` in the repository (gitignored). A shared directory works the same way, for example on a network mount:

```sh
export SIM_CACHE_URL=/mnt/team/fft-sim-cache
```

The layout is the same as on S3 (`<key>/manifest.json`, `<key>/files/...`, `index/...`, and the same under `elab/` for the elaboration cache), so a directory store can later be copied one to one into a bucket (`aws s3 sync .sim_cache s3://<bucket>/<prefix>`).

## S3 store

Any S3-compatible object store works: AWS S3, or a server such as MinIO, Garage, SeaweedFS, Ceph RGW or Cloudflare R2. The S3 store needs `boto3` (in `requirements.txt`). Everything about the server comes from your own environment; nothing is configured in the repository.

1. Create a bucket (here `<bucket>`, with the prefix `<prefix>`).
2. Give the users of the cache this policy:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
        "Resource": "arn:aws:s3:::<bucket>/<prefix>/*"},
       {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::<bucket>",
        "Condition": {"StringLike": {"s3:prefix": ["<prefix>/*"]}}}
     ]
   }
   ```

   Without `s3:ListBucket`, S3 answers a missing key with 403 instead of 404, which the cache cannot tell from a denied read: every miss is then logged as a failed lookup (the test still runs), and `sim_cache.py list` fails. If misses still show up as 403 with the prefix condition (servers differ in how they evaluate it for a missing key), grant `s3:ListBucket` without the condition. Read-only users drop `s3:PutObject`: they get hits and store nothing.
3. Give boto3 the credentials in the usual way: `~/.aws/credentials` (with `AWS_PROFILE` for a named profile), or the environment:

   ```sh
   export AWS_ACCESS_KEY_ID=<key id> AWS_SECRET_ACCESS_KEY=<secret>
   export AWS_DEFAULT_REGION=<the bucket's region>
   ```

4. Point the cache at the bucket:

   ```sh
   export SIM_CACHE_URL=s3://<bucket>/<prefix>
   ```

5. For a server other than AWS, also set its endpoint, and the region the server is configured with:

   ```sh
   export AWS_ENDPOINT_URL=https://<your S3 server>:<port>
   ```

   boto3 sends integrity checksums with every request by default, and some S3-compatible servers reject them. For such a
   server, also set:

   ```sh
   export AWS_REQUEST_CHECKSUM_CALCULATION=when_required AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
   ```

6. Check it:

   ```sh
   FFT_MODEL=engine pytest tests/test_xfft_flow.py     # first run: "sim_cache: stored <key> in s3://<bucket>/<prefix>/"
   python tests/sim_cache.py list                      # the entry, with its test and date
   FFT_MODEL=engine pytest tests/test_xfft_flow.py     # again, or on another machine: a CachedResult warning
   ```

## Caveats

- A result hit needs no build at all. A result miss builds locally, or restores the snapshot from the elaboration cache. The generated IP (but for its `.xci`) and the compiled libraries never go into the store.
- A complete entry is kept per key: a run that stores a key that is already there keeps the entry, unless it is a refresh. Two runs that miss at the same time both store; each writes its own files (they are named by content, and logs differ between runs), so the manifest that lands last names files that are all there, whichever run it is. The other run's files stay beside them unused.
- An entry whose manifest is there but which is damaged (a missing or changed file, a damaged manifest) is a miss, and the next run that passes replaces it: the log says `replacing the damaged entry <key>`.
- The key is computed before the build. A source edited while a run builds or simulates is stored under the key of the source as it was at the start; the next run with the edited source has another key and misses.
- Nothing expires. Age entries out with the bucket's lifecycle rules (or by deleting directories of a directory store); a missing entry is just a miss.
- Whoever can write to the store can store results that others will take as passes, and snapshots that others will load and run as native code ([security](#security)). Give write access only to trusted runners (for example CI with Vivado), and read access to everyone else.
- The client waits at most 5 s to connect and 30 s per read. A request that times out, cannot connect or gets a server error (5xx, `SlowDown`) is tried 3 times in all, with a growing random pause between (about 2 s, then 4 s); a missing key is a miss at once, and a denied request (403) is not tried again. A lookup that still fails is logged and is a miss, so the test runs; a store that fails is logged and the result is not stored. A broken or overloaded store slows a run down but never fails it.

## Elaboration cache

Compiling and elaborating a design takes longer than many of its runs: for the real core at 256 points, about 30 s of IP generation, 1 s of `xvhdl` and 16 s of `xelab`, against a few seconds of simulation. A snapshot depends on the build inputs only, not on the test that runs on it, so it is cached by them. The implementation is `tests/elab_cache.py`. `sim_runner.run()` uses it for every XSim build, cached runs and uncached ones alike, and so does the characterization for its one snapshot per model ([CHARACTERIZATION.md](CHARACTERIZATION.md)). NVC and GHDL (the C engine's VHDL wrapper) build in about a second and always build afresh, without it; their runs use the result cache as XSim's do.

### How it works

1. Before the build, the key is computed: the sha256 of a manifest of the build inputs (below).
2. **Local**: the build directory has a stamp `elab.json` with the same key, and every file it lists still has its hash. The snapshot is used as it is.
3. **Store**: otherwise the key is looked up in the store. The manifest may name only files a snapshot is made of (the table below); an entry with any other name, such as a `cocotb.py` that would shadow an import in the test directory, is refused. Every file is fetched and checked against its sha256. The files are written into a temporary directory beside the build directory, and the snapshot is load-checked there ([below](#snapshot-portability)). Only then does the directory replace the build directory, with `elab.json` written last. The log says `elab_cache: hit in <store>: ... restored 17 files (2.3 MB) into ... in 1.2 s; not elaborated`.
4. **Miss**: the build directory is emptied (but for `dpi/`, which the stand-in's build script stamps itself), and the design is compiled and elaborated from scratch. The build directory is stamped. The snapshot's link paths are checked ([below](#snapshot-portability)); then the files are stored under the key, named by their sha256, the manifest last. A miss says why: `miss <key>: changed since <old key> (...): ~ sources[...].sha256: ...`, the same way the result cache does.

On a hit, `xvlog`, `xvhdl`, `xelab` and, for the real core, Vivado's IP generation are not run. cocotb-vivado's `test()` runs on the restored snapshot. The stand-in's key holds the C-model archive, which comes with the generated core, so a clean checkout still generates the core once (about 30 s) before it can look the stand-in up.

What is stored, relative to the build directory, gzipped (the stand-in at 256 points: 2.3 MB, 1.3 MB gzipped; the real core: 8.7 MB, 3.9 MB gzipped):

| Files | Why |
|---|---|
| `xsim.dir/<snapshot>/*` but `xsimkernel.log` | the snapshot: the IP's compiled code is in it |
| `dpi/libxfft_engine.so`, `dpi/cmodel/*.so*` | the stand-in's library (the C engine) and the C model's shared libraries it links |
| `dpi/*.h`, `dpi/*.txt` | what tests read from there (the C model's version, the constants' source) |
| `ip/*/*.xci` | the generated core's version, which the characterization records |
| `build.log` | how it was built |

Never stored: the compiled libraries (`xsim.dir/xil_defaultlib/`), `xsim.ini` and the rest of the generated IP. A snapshot does not read them at run time; the XSim kernel libraries come from the Vivado installation.

### The key

In the key:

- the sources by content in compile order, as in the result cache: the HDL, the IP builder Tcl (so the core's configuration) and part, the stand-in's build script and files, its timing constants and the C-model archive;
- the toplevel, the library, the generics, the defines and the DPI library names;
- the simulator: `xelab --version`, Vivado's `version.dat` and patch areas (the compiler is the one bundled with Vivado, so this covers it);
- the resolved Vivado installation path (`XILINX_VIVADO`), see [below](#snapshot-portability);
- the machine architecture, the cocotb and cocotb-vivado versions (they write the compile and `xelab` commands) and the layout version (where the DPI directory is, how the library finds the C model);
- the compiler environment variables `CPATH`, `C_INCLUDE_PATH`, `CPLUS_INCLUDE_PATH`, `LIBRARY_PATH`, `GCC_EXEC_PREFIX` and `COMPILER_PATH`, when set.

Not in the key: the test module, case, seed and run environment; the repository's or the build directory's path; the Python version (no snapshot file links libpython); the OS and glibc. The entry records the elaborating host's OS and glibc for diagnosis, and the load check catches a host the snapshot does not run on.

### Snapshot portability

An XSim snapshot is a shared library (`xsimk.so`) with data files, built for one place. Restored at another path or on another machine, it can fail to load, or worse, load the wrong thing. This is what was measured and how each case is handled:

| In the snapshot | Risk | Handling |
|---|---|---|
| The DPI library's path in `xsimk.so` (`DT_NEEDED`), absolute when `-sv_root` names a directory outside the elaboration directory | Elsewhere, it does not load (`cannot open shared object file`). On the same machine it silently loads the library that is still at the old path. | The DPI library is built into `dpi/` inside the build directory. `xelab` then writes `./dpi/libxfft_engine.so`, which resolves against the test directory, where the snapshot is. `xelab` strips the elaboration directory from `-sv_root` as a string, so this holds only while both are spelled alike. Before storing, the cache therefore reads the dynamic sections of `xsimk.so` and of every stored shared library. If the DPI library is linked by an absolute path, or any `DT_NEEDED`, `DT_RPATH` or `DT_RUNPATH` names the build directory, the snapshot is used here and not stored: `not stored: ... links the DPI library by an absolute path (...)`. |
| The C model's directory in the DPI library's rpath | The C model's libraries are not found | `scripts/build_engine.py` links with the rpath `$ORIGIN/cmodel`: `cmodel/` beside the library, wherever the directory is |
| The Vivado installation's bundled compiler libraries in the rpath of `xsimk.so` | With Vivado at another path, the stand-in loads an older `libstdc++` (`GLIBCXX_3.4.26 not found`) | The resolved installation path is in the key: snapshots are shared between machines with Vivado at the same path (CI images, team installations), and another path is another key |
| System libraries (glibc, `libstdc++`) | An older host may lack a symbol version. The snapshots need only glibc 2.17 symbols, but the bundled libraries can need more. | Before a restored snapshot is used, a subprocess loads `xsim.dir/<snapshot>/xsimk.so` from the restored directory with cocotb-vivado's `LD_LIBRARY_PATH` (about 1 s). If it fails, the log says `does not load on this host: <reason>; elaborating`, the design is elaborated here, and the entry is left for hosts it suits. |
| Source paths in the debug data (`xsim.dbg`, the names in `xsimk.so`) | XSim messages, such as an `$error` location, can name the elaborating machine's source paths | None needed: they are names only, nothing is read from them |
| The waveform file's absolute path (`WAVES=1`: `$dumpfile` in a generated module) | The waves would be written at the elaborating machine's path | `WAVES=1` bypasses the elaboration cache (and the result cache): such a build is always local |

The snapshot runs from the directory that holds it: cocotb-vivado loads `xsim.dir/<snapshot>/xsimk.so` relative to its test directory. A run elsewhere needs a copy of `xsim.dir/<snapshot>/` and `dpi/` there, which is what the characterization's run directories get.

How the paths were measured: `readelf -d` on the snapshots, and `grep` for the elaboration directory's path in every snapshot file. Only `build.log` and `xsim.dir/<snapshot>/Compile_Options.txt` keep it; both are text that nothing reads at run time. Restored at another path in the same checkout and into a second checkout at another absolute path, both models passed the flow tests.

#### Timings

The flow tests (`tests/test_xfft_flow.py`) at 256 points (`XFFT_CONFIG=nfft=8`) with the result cache off, one machine, a directory store. Wall time is the whole pytest run, the simulation included:

| Model | Elaborated and stored | Local snapshot reused | Restored, same checkout | Restored, second checkout |
|---|---|---|---|---|
| stand-in (`FFT_MODEL=engine`) | 17 s | 3.4 s | 4.2 s | 11 s |
| real core (`FFT_MODEL=rtl`) | 37 s (with IP generation) | 9.2 s | 10 s | 10 s |

The stored snapshots were 1.3 MB (stand-in) and 3.9 MB (real core). The second checkout of the stand-in takes longer because it has no generated core yet, and the stand-in's key needs the C-model archive that comes with it ([above](#how-it-works)).

### Security

An elaboration entry is native code: the test process loads `xsimk.so` and the DPI library from it. Whoever can write to the store can run code on every machine that reads from it. The sha256 checks protect against damage in the store, not against a writer who stores bad files with matching hashes. A restore writes only the file names a snapshot is made of, and only inside the build directory, but the files themselves are trusted. Give write access only to trusted builders (for example CI with Vivado), and read access to everyone else. Read-only users get hits and store nothing: a failed store is logged and never fails a test.

### Switches

| Variable | Values |
|---|---|
| `SIM_ELAB_CACHE` | `on` (default: reuse the local snapshot or restore a stored one, store a new one), `off` (no lookup, no store: cocotb-vivado's own incremental build, as without the cache), `refresh` (elaborate from scratch, overwrite the entry) |
| `SIM_ELAB_CACHE_URL` | the store, as for `SIM_CACHE_URL`; default: the result cache's store (`SIM_CACHE_URL`, else `.sim_cache`), under `elab/` |

`python -m characterize` takes `--elab-cache` and `--elab-cache-url`; `characterize()` takes `elab_cache=` and `elab_cache_url=`. The two caches are independent: `SIM_CACHE=off SIM_ELAB_CACHE=on` runs every test, on cached snapshots.

### When a snapshot is elaborated again

- Any key input changes: a source, the configuration, the timing constants, the C-model archive, the defines or generics, Vivado (version, build, patches, installation path), cocotb or cocotb-vivado.
- The local snapshot no longer matches its stamp (a file changed or is gone) and the store has no entry, or the entry is damaged: a missing or damaged manifest, a name that is no part of a snapshot or is outside the build directory, a missing file, a hash mismatch. Each is logged as a miss.
- The restored snapshot does not load on this host.
- `SIM_ELAB_CACHE=refresh`, or `WAVES=1` (always built here, never stored).

A complete entry is kept per key: a second build of the same key keeps it, unless it is a refresh. A damaged entry is replaced by the next build of its key (`replacing the damaged entry <key>`). Two builders that store one key at the same time each write their own files, named by content, so the manifest that lands last names a complete set. An entry that does not load on one host is left for the hosts it suits. As for results, the key is computed before the build: a source edited during an elaboration is stored under the key it had at the start.

### Clearing it

- One build directory: delete it (everything under `build/` is safe to delete), or run once with `SIM_ELAB_CACHE=refresh`.
- A directory store: delete `elab/` in it (`.sim_cache/elab/` by default), or single `elab/<key>/` directories. The result entries are not affected.
- An S3 store: delete the objects under `<prefix>/elab/`, or age them out with a lifecycle rule on that prefix. A missing entry is just a miss.
