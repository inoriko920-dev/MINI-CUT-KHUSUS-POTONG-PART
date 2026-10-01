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


class SmartCutPacketDurationSeedTests(unittest.TestCase):
    def test_duration_seed_uses_master_pts_delta_without_rounding(self):
        media = SimpleNamespace(video_frame_times_pts=[0, 512, 1024, 1536])
        self.assertEqual(
            smartcut_runner._master_frame_duration_in_output_ticks(
                media,
                Fraction(1, 12288),
                Fraction(1, 12288),
            ),
            512,
        )

    def test_duration_seed_converts_exactly_to_output_time_base(self):
        media = SimpleNamespace(video_frame_times_pts=[0, 1001, 2002, 3003])
        self.assertEqual(
            smartcut_runner._master_frame_duration_in_output_ticks(
                media,
                Fraction(1, 30000),
                Fraction(1, 90000),
            ),
            3003,
        )

    def test_duration_seed_uses_most_common_positive_delta(self):
        media = SimpleNamespace(video_frame_times_pts=[0, 1000, 2001, 3001, 4001])
        self.assertEqual(
            smartcut_runner._master_frame_duration_in_output_ticks(
                media,
                Fraction(1, 1000),
                Fraction(1, 1000),
            ),
            1000,
        )

    def test_duration_seed_refuses_fractional_output_tick(self):
        media = SimpleNamespace(video_frame_times_pts=[0, 1, 2])
        self.assertIsNone(
            smartcut_runner._master_frame_duration_in_output_ticks(
                media,
                Fraction(1, 3),
                Fraction(1, 2),
            )
        )

    def test_duration_seed_requires_two_master_pts(self):
        media = SimpleNamespace(video_frame_times_pts=[123])
        self.assertIsNone(
            smartcut_runner._master_frame_duration_in_output_ticks(
                media,
                Fraction(1, 24),
                Fraction(1, 24),
            )
        )


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

    def test_main_uses_official_frame_mode_and_accepts_matching_decode_count(self):
        source = _FakeSource()
        runs: list[tuple[str, str]] = []

        def fake_run_and_count(argv, keep, output):
            runs.append((keep, str(output)))
            return 97

        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_and_count_decoded",
            side_effect=fake_run_and_count,
        ):
            smartcut_runner.main()

        self.assertEqual(runs, [("0,96", ".movie_Part-01.minicut-stage-abc.mp4")])

    def test_main_rejects_decoded_output_frame_count_mismatch(self):
        source = _FakeSource()
        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_and_count_decoded",
            return_value=96,
        ):
            with self.assertRaisesRegex(RuntimeError, "jumlah frame terdecode"):
                smartcut_runner.main()


if __name__ == "__main__":
    unittest.main()
