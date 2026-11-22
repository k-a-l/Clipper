"""Fast checks of the pure logic (no network, no FFmpeg)."""

import random
import subprocess
import sys


def test_settings_load_on_a_fresh_machine(isolated_home):
    from clipper.config import Settings

    s = Settings()
    assert s.storage_dir == isolated_home / "storage"
    assert s.all_sources() == []  # nothing configured: streams are added from the dashboard
    assert s.log_file is not None and s.log_file.is_relative_to(s.storage_dir)
    assert s.tts_python.is_relative_to(s.storage_dir)


def test_env_file_in_clipper_home_is_read(isolated_home):
    (isolated_home / ".env").write_text("MIN_POST_SPACING_MINUTES=200\n")
    from clipper.config import Settings

    assert Settings().min_post_spacing_minutes == 200


def test_random_clip_length_stays_in_range():
    from clipper.config import ClipStyle, Settings
    from clipper.modules.pipeline import window_for

    s, rng = Settings(), random.Random(1)
    totals = [sum(window_for(s, ClipStyle(clip_seconds=90), rng)) for _ in range(300)]
    assert min(totals) >= 30 and max(totals) <= 90
    assert len(set(totals)) > 20  # actually varies
    assert sum(window_for(s, ClipStyle(clip_seconds=45, random_length=False))) == 45


def test_hooks_rarely_end_with_a_question():
    from clipper.modules.metadata import ClipContext, MetadataGenerator

    g = MetadataGenerator(random.Random(7))
    transcript = "Is it Stacey? What are you doing? That was insane! Why do I follow Billy? We got him."
    hooks = [g.make_hook(ClipContext("c", "Streamer", "u", transcript=transcript)) for _ in range(200)]
    assert sum("?" in h for h in hooks) < 30


def test_small_webcam_is_widened_but_capped():
    from clipper.modules.facecam import widen_for_sharpness

    x, y, w, h = widen_for_sharpness((0.08, 0.42, 0.11, 0.15), 1920, 1080, 1440)
    assert 0.2 <= w <= 0.3  # ≤ 3× upscale at 1080p
    x, y, w, h = widen_for_sharpness((0.08, 0.42, 0.11, 0.15), 640, 360, 1440)
    assert w <= 0.11 * 2.5 + 1e-6  # a low-res source never swallows the frame
    assert widen_for_sharpness((0.0, 0.0, 0.5, 0.5), 1920, 1080, 1440) == (0.0, 0.0, 0.5, 0.5)


def test_vod_format_prefers_sdr_and_highest_resolution():
    from clipper.modules.vod import _format

    f = _format(4320)
    assert f.startswith("bv*[height<=4320][dynamic_range=SDR]")


def test_ended_stream_url_detection():
    from clipper.modules.daemon import Daemon

    rx = Daemon._VIDEO_URL
    assert rx.search("https://www.youtube.com/watch?v=abc")
    assert rx.search("https://youtu.be/abc")
    assert rx.search("https://www.twitch.tv/videos/123")
    assert not rx.search("https://www.twitch.tv/somechannel")


def test_single_instance_lock(tmp_path):
    from clipper.modules.utils import daemon_running, try_lock

    lock = tmp_path / "daemon.lock"
    with open(lock, "a+") as fh:
        assert try_lock(fh)
        assert daemon_running(lock)
    assert not daemon_running(lock)


def test_cli_help_and_gen_key():
    out = subprocess.run([sys.executable, "-m", "clipper", "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "ui" in out.stdout
    key = subprocess.run([sys.executable, "-m", "clipper", "gen-key"], capture_output=True, text=True)
    assert key.returncode == 0 and len(key.stdout.strip()) == 44
