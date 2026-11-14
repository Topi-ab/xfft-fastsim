"""Finding the C model in a generated core (model/cmodel_archive.py), against fake IP directories: no Vivado."""

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "model"))

import cmodel_archive  # noqa: E402


def fake_ip(tmp_path: Path, version: str, extra: tuple = ()) -> Path:
    """A generated IP's directory with the cmodel output product of xfft <version> (e.g. "9_1")."""
    ip = tmp_path / f"ip_{version}" / "xfft_core"
    (ip / "cmodel").mkdir(parents=True)
    for os_name in ("lin64", "nt64"):
        with zipfile.ZipFile(ip / "cmodel" / f"xfft_v{version}_bitacc_cmodel_{os_name}.zip", "w") as z:
            for name in (f"xfft_v{version}_bitacc_cmodel.h", f"libIp_xfft_v{version}_bitacc_cmodel.so",
                         "libgmp.so.11", "libmpfr.so.4", "gmp.h", "mpfr.h", *extra):
                z.writestr(name, "x")
    return ip


def test_names_follow_the_archive(tmp_path):
    for version, prefix in (("9_1", "xilinx_ip_xfft_v9_1_"), ("9_2", "xilinx_ip_xfft_v9_2_")):
        archive = cmodel_archive.find(fake_ip(tmp_path, version))
        assert archive.name == f"xfft_v{version}_bitacc_cmodel_lin64.zip"
        names = cmodel_archive.names(archive)
        assert names == cmodel_archive.CModel(header=f"xfft_v{version}_bitacc_cmodel.h",
                                              library=f"Ip_xfft_v{version}_bitacc_cmodel", api_prefix=prefix,
                                              version=version.replace("_", "."))


def test_other_libraries_in_the_archive_do_not_matter(tmp_path):
    ip = fake_ip(tmp_path, "9_1", extra=("libIp_floating_point_v7_1_bitacc_cmodel.so", "libgmp.so.10"))
    assert cmodel_archive.names(cmodel_archive.find(ip)).library == "Ip_xfft_v9_1_bitacc_cmodel"


def test_unsupported_major_version_is_refused(tmp_path):
    with pytest.raises(cmodel_archive.CModelError, match="9.x"):
        cmodel_archive.names(cmodel_archive.find(fake_ip(tmp_path, "10_0")))


def test_missing_or_ambiguous_archive(tmp_path):
    ip = tmp_path / "ip" / "xfft_core"
    ip.mkdir(parents=True)
    with pytest.raises(cmodel_archive.CModelError, match="cmodel target"):
        cmodel_archive.find(ip)
    ip = fake_ip(tmp_path, "9_1")
    zip_bytes = (ip / "cmodel" / "xfft_v9_1_bitacc_cmodel_lin64.zip").read_bytes()
    (ip / "cmodel" / "other_bitacc_cmodel_lin64.zip").write_bytes(zip_bytes)
    with pytest.raises(cmodel_archive.CModelError, match="found 2"):
        cmodel_archive.find(ip)


def test_archive_without_the_xfft_library(tmp_path):
    ip = tmp_path / "ip" / "xfft_core"
    (ip / "cmodel").mkdir(parents=True)
    with zipfile.ZipFile(ip / "cmodel" / "xfft_v9_1_bitacc_cmodel_lin64.zip", "w") as z:
        z.writestr("xfft_v9_1_bitacc_cmodel.h", "x")
    with pytest.raises(cmodel_archive.CModelError, match="expected one"):
        cmodel_archive.names(cmodel_archive.find(ip))
