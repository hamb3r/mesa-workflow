"""Unit tests for compiler and Meson error log parsing."""

from __future__ import annotations

from mesa_canary.log_parser import normalize_mesa_path, parse_build_log


def test_parse_msvc_16936_error() -> None:
    log = r"""
[120/410] Compiling C++ object src/gallium/drivers/d3d12/libd3d12.a.p/d3d12_video_enc.cpp.obj
FAILED: src/gallium/drivers/d3d12/libd3d12.a.p/d3d12_video_enc.cpp.obj
D:\a\_temp\mesa-work\mesa\src\gallium\drivers\d3d12\d3d12_video_enc.cpp(2028): error C2664: 'void d3d12_video_encoder_references_manager_interface::get_current_frame_picture_control_data(D3D12_VIDEO_ENCODER_PICTURE_CONTROL_CODEC_DATA &)': cannot convert argument 1 from 'const D3D12_VIDEO_ENCODER_PICTURE_CONTROL_CODEC_DATA' to 'D3D12_VIDEO_ENCODER_PICTURE_CONTROL_CODEC_DATA &'
D:\a\_temp\mesa-work\mesa\subprojects\DirectX-Headers-1.0\include\directx\d3d12video.h(5123): note: see declaration of 'D3D12_VIDEO_ENCODER_PICTURE_CONTROL_CODEC_DATA'
D:\a\_temp\mesa-work\mesa\src\gallium\drivers\d3d12\d3d12_video_enc_h264.cpp(412): error C2440: '=': cannot convert from 'const D3D12_VIDEO_ENCODER_PICTURE_CONTROL_CODEC_DATA_H264 *' to 'D3D12_VIDEO_ENCODER_PICTURE_CONTROL_CODEC_DATA_H264 *'
"""
    parsed = parse_build_log(log, source_root=r"D:\a\_temp\mesa-work\mesa")
    assert parsed.failed_files == (
        "src/gallium/drivers/d3d12/d3d12_video_enc.cpp",
        "src/gallium/drivers/d3d12/d3d12_video_enc_h264.cpp",
    )
    assert "error C2664" in parsed.summary
    assert "d3d12video.h" in parsed.excerpt
    # Subproject header must NOT pollute Mesa failed_files
    assert all(not f.startswith("subprojects/") for f in parsed.failed_files)


def test_parse_mingw_gcc_and_clang_and_linux_errors() -> None:
    log = """
../mesa/src/gallium/drivers/d3d12/d3d12_video_enc_hevc.cpp:310:15: error: invalid conversion from 'const D3D12_VIDEO_ENCODER_PICTURE_CONTROL_CODEC_DATA_HEVC*' to 'D3D12_VIDEO_ENCODER_PICTURE_CONTROL_CODEC_DATA_HEVC*' [-fpermissive]
  310 |    pData = &picParams;
      |            ^~~~~~~~~~
../mesa/src/microsoft/vulkan/dzn_dxcore.cpp:42:9: error: reference to 'Status' is ambiguous
C:/Program Files (x86)/Windows Kits/10/Include/10.0.26100.0/um/mfapi.h(99,1): error: unrelated sdk header
"""
    parsed = parse_build_log(log)
    assert "src/gallium/drivers/d3d12/d3d12_video_enc_hevc.cpp" in parsed.failed_files
    assert "src/microsoft/vulkan/dzn_dxcore.cpp" in parsed.failed_files
    assert len(parsed.failed_files) == 2


def test_parse_meson_configure_error() -> None:
    log = """
mesa/meson.build:712:18: ERROR: Dependency "DirectX-Headers" not found, tried pkgconfig and cmake
"""
    parsed = parse_build_log(log, source_root="mesa")
    assert parsed.failed_files == ("meson.build",)
    assert "DirectX-Headers" in parsed.summary


def test_normalize_mesa_path_variants() -> None:
    assert normalize_mesa_path(r"..\src\gallium\drivers\d3d12\d3d12_video_enc.cpp") == (
        "src/gallium/drivers/d3d12/d3d12_video_enc.cpp"
    )
    assert normalize_mesa_path("/tmp/work/mesa/src/microsoft/vulkan/dzn_dxcore.cpp") == (
        "src/microsoft/vulkan/dzn_dxcore.cpp"
    )
    assert normalize_mesa_path("subprojects/DirectX-Headers-1.0/include/directx/d3d12.h") is None
    assert normalize_mesa_path("/usr/include/X11/Xlib.h") is None
