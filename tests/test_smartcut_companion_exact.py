from __future__ import annotations

import sys
import unittest
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import patch

import smartcut_runner


class _FakeStream:
    time_base = Fraction(1, 24)


class _FakeSource:
    def __init__(self):
        self.video_stream = _FakeStream()
        self.start_time = Fraction(10, 1)
        # First displayed frame is absolute PTS 10s. There are 200 frames.
        self.video_frame_times_pts = list(range(240, 440))
        self.closed = False

    def close(self):
        self.closed = True


class SmartCutPacketDurationBackportTests(unittest.TestCase):
    def test_positive_duration_becomes_typical_duration(self):
        cutter = SimpleNamespace(typical_frame_duration=None)
        packet = SimpleNamespace(duration=1001)
        smartcut_runner._ensure_packet_duration(cutter, packet)
        self.assertEqual(cutter.typical_frame_duration, 1001)
        self.assertEqual(packet.duration, 1001)

    def test_zero_duration_uses_previous_positive_duration(self):
        cutter = SimpleNamespace(typical_frame_duration=1001)
        packet = SimpleNamespace(duration=0)
        smartcut_runner._ensure_packet_duration(cutter, packet)
        self.assertEqual(packet.duration, 1001)

    def test_none_duration_uses_previous_positive_duration(self):
        cutter = SimpleNamespace(typical_frame_duration=512)
        packet = SimpleNamespace(duration=None)
        smartcut_runner._ensure_packet_duration(cutter, packet)
        self.assertEqual(packet.duration, 512)

    def test_missing_duration_stays_missing_without_known_typical_duration(self):
        cutter = SimpleNamespace(typical_frame_duration=None)
        packet = SimpleNamespace(duration=None)
        smartcut_runner._ensure_packet_duration(cutter, packet)
        self.assertIsNone(packet.duration)


class SmartCutExactFramePlanTests(unittest.TestCase):
    def test_exact_boundary_maps_to_master_frame_index_without_tolerance(self):
        source = _FakeSource()
        self.assertEqual(
            smartcut_runner._exact_frame_index(source, "97/24"),
            97,
        )
        with self.assertRaisesRegex(RuntimeError, "grid time_base"):
            smartcut_runner._exact_frame_index(source, "1/100")

    def test_part_before_boundary_maps_half_open_range_to_inclusive_frame_cli(self):
        source = _FakeSource()
        plan = smartcut_runner._build_exact_keep_plan(source, "start,97/24")
        self.assertEqual(plan.expected_frames, 97)
        self.assertEqual(plan.frame_keep, "0,96")

    def test_part_after_boundary_starts_at_k_and_keeps_remaining_frames(self):
        source = _FakeSource()
        plan = smartcut_runner._build_exact_keep_plan(source, "97/24,end")
        self.assertEqual(plan.expected_frames, 200 - 97)
        self.assertEqual(plan.frame_keep, "97,-1")

    def test_exact_fraction_syntax_activates_hardening_on_staging_output(self):
        final_name = [
            "movie.mp4",
            "movie_Part-01.mp4",
            "--keep",
            "start,97/24",
        ]
        staging_name = [
            "movie.mp4",
            ".movie_Part-01.minicut-stage-abc.mp4",
            "--keep",
            "start,97/24",
        ]
        rounded_fallback = [
            "movie.mp4",
            ".movie_Part-01.minicut-stage-abc.mp4",
            "--keep",
            "start,4.041667",
        ]
        self.assertTrue(
            smartcut_runner._looks_like_minicut_exact_keep(final_name)
        )
        self.assertTrue(
            smartcut_runner._looks_like_minicut_exact_keep(staging_name)
        )
        self.assertFalse(
            smartcut_runner._looks_like_minicut_exact_keep(rounded_fallback)
        )


class SmartCutExactFrameExecutionTests(unittest.TestCase):
    def _argv(self, keep: str = "start,97/24") -> list[str]:
        return [
            "MiniCut SmartCut.exe",
            "movie.mp4",
            ".movie_Part-01.minicut-stage-abc.mp4",
            "--keep",
            keep,
            "--log-level",
            "warning",
        ]

    def test_main_uses_official_frame_mode_and_accepts_clean_output(self):
        source = _FakeSource()
        runs: list[list[str]] = []
        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_upstream",
            side_effect=lambda args: runs.append(list(args)),
        ), patch(
            "smartcut_runner._video_frame_count",
            return_value=97,
        ), patch(
            "smartcut_runner._discard_video_packet_count",
            return_value=0,
        ):
            smartcut_runner.main()

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][runs[0].index("--keep") + 1], "0,96")
        self.assertIn("--frames", runs[0])

    def test_main_rejects_output_frame_count_mismatch(self):
        source = _FakeSource()
        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_upstream",
        ), patch(
            "smartcut_runner._video_frame_count",
            return_value=96,
        ):
            with self.assertRaisesRegex(RuntimeError, "jumlah frame"):
                smartcut_runner.main()

    def test_main_rejects_discard_flag_even_when_packet_count_matches(self):
        source = _FakeSource()
        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_upstream",
        ), patch(
            "smartcut_runner._video_frame_count",
            return_value=97,
        ), patch(
            "smartcut_runner._discard_video_packet_count",
            return_value=1,
        ):
            with self.assertRaisesRegex(RuntimeError, "discard"):
                smartcut_runner.main()


if __name__ == "__main__":
    unittest.main()
