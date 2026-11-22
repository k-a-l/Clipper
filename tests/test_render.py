"""End-to-end render with the real FFmpeg (skipped when FFmpeg isn't installed)."""

import asyncio
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _make_source(path):
    """10 s of 1280x720 test pattern with a 440 Hz tone (so the clip must have audible sound)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)


def _mean_volume(path) -> float:
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    line = next(ln for ln in err.splitlines() if "mean_volume" in ln)
    return float(line.split("mean_volume:")[1].split("dB")[0])


@pytest.mark.parametrize("layout", ["center", "full", "cam_top"])
def test_vertical_render_keeps_sound(tmp_path, layout):
    from clipper.config import Facecam, RenderOptions, Settings
    from clipper.modules.video_editor import ClipSource, Effects, VideoEditor

    src_file = tmp_path / "src.mp4"
    _make_source(src_file)
    s = Settings(video_encoder="libx264", loudnorm=False)
    opts = RenderOptions(layout=layout, quality="standard", story=False, voiceover=False,
                         facecam=Facecam(x=0.0, y=0.0, w=0.3, h=0.3) if layout == "cam_top" else None)
    out = tmp_path / "out.mp4"
    fx = Effects(highlight=5.0, progress=True, zoom=True)
    asyncio.run(VideoEditor(s).render_vertical(ClipSource.from_file(src_file, 1.0, 8.0), out, None, opts, fx))

    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout
    assert "video,1080,1920" in probe
    assert "audio" in probe
    assert _mean_volume(out) > -40  # the tone survived the render
