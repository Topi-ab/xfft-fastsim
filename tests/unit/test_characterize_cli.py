"""The characterization's command line and API around the bench, and the .xci mapping: no Vivado.

    pytest tests/unit/test_characterize_cli.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import characterize  # noqa: E402
from characterize import api, cli, parts  # noqa: E402
from characterize import xci as xci_reader  # noqa: E402

import xfft_config  # noqa: E402
import xfft_timing  # noqa: E402


def run_cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "characterize", *args], cwd=REPO, capture_output=True, text=True,
                          env={**os.environ, "XFFT_CONFIG": "", **(env or {})})


def test_help_and_dry_run():
    r = run_cli("--help")
    assert r.returncode == 0 and "exit codes" in r.stdout and "--xci" in r.stdout
    r = run_cli("--nfft", "8", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "n256_d16_t16_conv_xc7a35tcpg236-1_100mhz" in r.stdout and "135 cases" in r.stdout


def test_configuration_options_override_the_base():
    p = cli.parser()
    cfg = cli.config_of(p.parse_args(["--config", "rounding=truncation", "--points", "512", "--clock-mhz", "250"]), p)
    assert cfg == xfft_config.XfftConfig(nfft=9, rounding="truncation", clock_mhz=250)


@pytest.mark.parametrize("args", [["--points", "100", "--dry-run"], ["--nfft", "2", "--dry-run"],
                                  ["--xci", "x.xci", "--nfft", "8"], ["--allow-differences", "--dry-run"],
                                  ["--rounding", "nearest", "--dry-run"], ["--nfft", "8", "--dry-run", "nosuchcase"]])
def test_bad_arguments_exit_2(args):
    assert run_cli(*args).returncode == api.EXIT_USAGE


def test_extra_cases_and_unknown_cases():
    cfg = xfft_config.XfftConfig(nfft=8)
    assert len(api.case_names(cfg)) == 135
    assert len(api.case_names(cfg, extra=True)) > 135
    assert api.case_names(cfg, ["ideal", "gap_1"]) == ["ideal", "gap_1"]
    with pytest.raises(api.CharacterizeError):
        api.case_names(cfg, ["nosuchcase"])


def test_resolve_config_and_out_path():
    assert api.resolve_config("nfft=8") == api.resolve_config({"nfft": 8}) == xfft_config.XfftConfig(nfft=8)
    assert api.resolve_config(None) == xfft_config.XfftConfig()
    with pytest.raises(characterize.CharacterizeError):
        api.resolve_config("nfft=99")
    assert api.out_path("out", "t") == Path("out/t.json")
    assert api.out_path("a/b.json", "t") == Path("a/b.json")


def test_characterize_needs_vivado(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(characterize.CharacterizeError, match="settings64.sh"):
        characterize.characterize("nfft=8", log=None)


def properties(**config) -> xci_reader.IpProperties:
    """The properties Vivado reads from the .xci of the bench's 512-point core, with overrides."""
    base = {"Component_Name": "xfft_0", "transform_length": "512", "input_width": "18", "phase_factor_width": "18",
            "rounding_modes": "truncation", "target_clock_frequency": "250", "scaling_options": "scaled",
            "implementation_options": "pipelined_streaming_io", "data_format": "fixed_point",
            "output_ordering": "natural_order", "throttle_scheme": "nonrealtime", "xk_index": "true",
            "ovflo": "true", "aresetn": "true", "aclken": "false", "run_time_configurable_transform_length": "false",
            "cyclic_prefix_insertion": "false", "channels": "1", "super_sample_rates": "1",
            "memory_options_data": "block_ram", "S_AXIS_DATA.INSERT_VIP": "0"}
    return xci_reader.IpProperties(xci=Path("x.xci"), vlnv="xilinx.com:ip:xfft:9.1", part="xc7a35tcpg236-1",
                                   config={**base, **config}, newest_xfft="xilinx.com:ip:xfft:9.1", vivado="2025.2")


def test_xci_maps_to_the_configuration():
    cfg, unsupported = xci_reader.map_config(properties())
    assert unsupported == []
    assert cfg == xfft_config.XfftConfig(nfft=9, input_width=18, twiddle_width=18, rounding="truncation",
                                         part="xc7a35tcpg236-1", clock_mhz=250, module="xfft_0")


@pytest.mark.parametrize("override,word", [({"implementation_options": "radix_4_burst_io"}, "architecture"),
                                           ({"scaling_options": "block_floating_point"}, "scaling"),
                                           ({"throttle_scheme": "realtime"}, "throttling"),
                                           ({"run_time_configurable_transform_length": "true"}, "transform length"),
                                           ({"transform_length": "500"}, "power of two"),
                                           ({"target_clock_frequency": "245.76"}, "whole MHz"),
                                           ({"rounding_modes": "nearest"}, "rounding")])
def test_xci_reports_unsupported_options(override, word):
    cfg, unsupported = xci_reader.map_config(properties(**override))
    assert cfg is None and any(word in u for u in unsupported), unsupported


def test_xci_of_another_major_version_is_unsupported():
    p = properties()
    p = xci_reader.IpProperties(**{**p.__dict__, "vlnv": "xilinx.com:ip:xfft:10.0"})
    assert any("9.x" in u for u in xci_reader.map_config(p)[1])


def test_xci_differences_from_the_generated_core():
    user = properties(memory_options_data="distributed_ram", Component_Name="xfft_core")
    generated = properties(Component_Name="xfft_core", **{"S_AXIS_DATA.INSERT_VIP": "1"})
    assert xci_reader.differences(user, generated) == [
        "CONFIG.memory_options_data: the .xci has distributed_ram, the generated core block_ram"]
    older = xci_reader.IpProperties(**{**user.__dict__, "vlnv": "xilinx.com:ip:xfft:9.0",
                                       "config": generated.config})
    assert len(xci_reader.differences(older, generated)) == 1


def test_reader_output_is_parsed():
    text = ("vivado\t2025.2\nip\t/a.xci\nnewest_xfft\txilinx.com:ip:xfft:9.1\nIPDEF\txilinx.com:ip:xfft:9.1\n"
            "PART\txc7a35tcpg236-1\nNAME\txfft_core\nCONFIG.transform_length\t256\nCONFIG.Component_Name\txfft_core\n")
    p, = xci_reader.parse_properties(text, [Path("/a.xci")])
    assert (p.vlnv, p.part, p.version, p.vivado) == ("xilinx.com:ip:xfft:9.1", "xc7a35tcpg236-1", "9.1", "2025.2")
    assert p.config == {"transform_length": "256", "Component_Name": "xfft_core"}


def test_a_characterization_json_in_timing_is_picked_up(tmp_path, monkeypatch):
    cfg = xfft_config.XfftConfig(nfft=12)
    monkeypatch.setattr(xfft_timing, "TIMING_DIR", tmp_path)
    monkeypatch.setattr(xfft_config, "BUILD", tmp_path / "build")
    with pytest.raises(LookupError):
        xfft_timing.constants(cfg)
    values = {name: i + 1 for i, name in enumerate(xfft_timing.NAMES)}
    (tmp_path / f"{cfg.tag}.json").write_text(json.dumps({"tag": cfg.tag, "constants": values}))
    assert xfft_timing.constants(cfg) == values
    (tmp_path / f"{cfg.tag}.json").write_text(json.dumps(values))       # a flat dict works too
    assert xfft_timing.constants(cfg) == values


def test_shipped_timing_files_are_complete():
    for path in sorted((REPO / "timing").glob("*.json")):
        doc = json.loads(path.read_text())
        assert doc["tag"] == path.stem and "problems" not in doc, path
        cfg = xfft_config.XfftConfig(**doc["config"])
        assert cfg.tag == path.stem and xfft_timing.constants(cfg) == doc["constants"], path


VERSAL = ["xcvc1902-vsva2197-2MP-e-S", "xcve2802-vsvh1760-2MP-e-S", "xcvm1802-vsva2197-2MP-e-S",
          "xcvp1202-vsva2785-2MP-e-S", "xcvh1582-vsva3697-2MP-e-S", "xcvr1652", "xqvc1902-vsqa2197-2MP-e-S",
          "xavm1102", "XCVC1902", "xc2ve3858", "xc2vm3558"]
NOT_VERSAL = ["xcvu9p-flga2104-2L-e", "xc7vx485tffg1761-2", "xc7vx485t", "xczu4eg-sfvc784-1-e", "xczu4eg",
              "xc7a35tcpg236-1", "xc7a35t", "xcku040-ffva1156-2-e", "xc7z020clg400-1", "xcku5p-ffvb676-2-e"]


@pytest.mark.parametrize("part", VERSAL)
def test_versal_parts_are_refused_by_name(part):
    assert parts.is_versal(part)
    with pytest.raises(characterize.CharacterizeError, match="Versal is not supported"):
        api.check_part(xfft_config.XfftConfig(part=part))


@pytest.mark.parametrize("part", NOT_VERSAL)
def test_other_families_are_not_refused(part):
    assert not parts.is_versal(part)
    api.check_part(xfft_config.XfftConfig(part=part))


def test_vivados_family_of_the_part_decides():
    assert parts.is_versal("xcvu9p-flga2104-2L-e", architecture="versal")
    assert parts.is_versal("x", family="versalpremium")
    assert not parts.is_versal("xcvc1902", architecture="virtexuplus", family="virtexuplus")
    assert not parts.is_versal("xcvu9p-flga2104-2L-e", architecture="virtexuplus")


def test_characterize_refuses_versal_before_vivado(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(characterize.CharacterizeError, match="Versal is not supported.*all-zero"):
        characterize.characterize({"part": "xcvc1902-vsva2197-2MP-e-S"}, log=None)
    with pytest.raises(characterize.CharacterizeError, match="settings64.sh"):
        characterize.characterize({"part": "xcvu9p-flga2104-2L-e"}, log=None)


def test_cli_refuses_versal():
    r = run_cli("--part", "xcvc1902-vsva2197-2MP-e-S", "--dry-run")
    assert r.returncode == api.EXIT_USAGE and "Versal is not supported" in r.stderr, (r.stdout, r.stderr)
    r = run_cli("--dry-run", env={"XFFT_CONFIG": "part=xcve2802-vsvh1760-2MP-e-S"})
    assert r.returncode == api.EXIT_USAGE and "Versal is not supported" in r.stderr
    for part in ("xcvu9p-flga2104-2L-e", "xc7vx485tffg1761-2", "xczu4eg-sfvc784-1-e", "xc7a35tcpg236-1"):
        r = run_cli("--part", part, "--dry-run")
        assert r.returncode == api.EXIT_OK, (part, r.stderr)


def test_xci_of_a_versal_part_is_refused_before_generating(monkeypatch):
    import generate_ip

    def generate(cfg):
        raise AssertionError("generated a core for a Versal part")

    p = properties()
    versal = xci_reader.IpProperties(**{**p.__dict__, "part": "xcvc1902-vsva2197-2MP-e-S"})
    by_vivado = xci_reader.IpProperties(**{**p.__dict__, "architecture": "versal", "family": "versal"})
    monkeypatch.setattr(generate_ip, "generate", generate)
    for props in (versal, by_vivado):
        monkeypatch.setattr(xci_reader, "read_properties", lambda *xcis, log=None, props=props: [props])
        with pytest.raises(xci_reader.XciError, match="Versal is not supported"):
            xci_reader.read_config(Path("x.xci"))
        with pytest.raises(xci_reader.XciError, match="Versal is not supported"):
            xci_reader.config_from_xci("x.xci")


def test_reader_output_gives_the_part_family():
    text = ("vivado\t2025.2\nip\t/a.xci\nIPDEF\txilinx.com:ip:xfft:9.1\nPART\txcvc1902-vsva2197-2MP-e-S\n"
            "ARCHITECTURE\tversal\nFAMILY\tversal\n")
    p, = xci_reader.parse_properties(text, [Path("/a.xci")])
    assert (p.architecture, p.family) == ("versal", "versal")
