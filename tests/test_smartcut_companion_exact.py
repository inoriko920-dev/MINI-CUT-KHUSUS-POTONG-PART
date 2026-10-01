from __future__ import annotations

import sys
import unittest
from fractions import Fraction
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


class SmartCutExactFramePlanTests(unittest.TestCase):
    def test_exact_boundary_maps_to_master_frame_index_without_tolerance(self):
        source = _FakeSource()
        self.assertEqual(
            smartcut_runner._exact_frame_index(source, "97/24"),
            97,
        )
        with self.assertRaisesRegex(RuntimeError, "grid time_base"):
            smartcut_runner._exact_frame_index(source, "1/100")

    def test_part_before_boundary_maps_half_open_range_and_prepares_guarded_retry(self):
        source = _FakeSource()
        plan = smartcut_runner._build_exact_keep_plan(source, "start,97/24")
        self.assertEqual(plan.expected_frames, 97)
        self.assertEqual(plan.frame_keep, "0,96")
        # Retry is not automatic. This candidate is used only when decoded-tail
        # verification proves the normal export lost exactly one final frame.
        self.assertEqual(plan.retry_frame_keep, "0,97")

    def test_part_after_boundary_starts_at_k_and_terminal_end_has_no_retry(self):
        source = _FakeSource()
        plan = smartcut_runner._build_exact_keep_plan(source, "97/24,end")
        self.assertEqual(plan.expected_frames, 200 - 97)
        self.assertEqual(plan.frame_keep, "97,-1")
        self.assertIsNone(plan.retry_frame_keep)

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

    def test_decode_stats_subtract_only_proven_missing_tail_frames(self):
        stats = smartcut_runner._OutputDecodeStats(
            packet_frames=97,
            tail_packets=20,
            tail_decoded=19,
        )
        self.assertEqual(stats.tail_missing, 1)
        self.assertEqual(stats.effective_decoded_frames, 96)


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

    def test_main_accepts_exact_output_without_retry(self):
        source = _FakeSource()
        runs: list[list[str]] = []

        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_upstream",
            side_effect=lambda args: runs.append(list(args)),
        ), patch(
            "smartcut_runner._output_decode_stats",
            return_value=smartcut_runner._OutputDecodeStats(97, 20, 20),
        ):
            smartcut_runner.main()

        self.assertEqual(len(runs), 1)
        self.assertEqual(
            runs[0][runs[0].index("--keep") + 1],
            "0,96",
        )
        self.assertIn("--frames", runs[0])

    def test_main_retries_one_extra_source_frame_only_after_decode_proof(self):
        source = _FakeSource()
        runs: list[list[str]] = []
        stats = [
            # Packet inventory says 97 but final GOP decodes only 96.
            smartcut_runner._OutputDecodeStats(97, 20, 19),
            # Retry exposes one sacrificial packet; exactly 97 remain decodable.
            smartcut_runner._OutputDecodeStats(98, 21, 20),
        ]

        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_upstream",
            side_effect=lambda args: runs.append(list(args)),
        ), patch(
            "smartcut_runner._output_decode_stats",
            side_effect=stats,
        ):
            smartcut_runner.main()

        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0][runs[0].index("--keep") + 1], "0,96")
        self.assertEqual(runs[1][runs[1].index("--keep") + 1], "0,97")
        self.assertIn("--frames", runs[1])

    def test_main_does_not_retry_unrelated_mismatch(self):
        source = _FakeSource()
        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_upstream",
        ), patch(
            "smartcut_runner._output_decode_stats",
            return_value=smartcut_runner._OutputDecodeStats(95, 20, 20),
        ):
            with self.assertRaisesRegex(RuntimeError, "verifikasi decode"):
                smartcut_runner.main()

    def test_main_does_not_extend_past_terminal_end(self):
        source = _FakeSource()
        runs: list[list[str]] = []
        with patch.object(sys, "argv", self._argv("97/24,end")), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_upstream",
            side_effect=lambda args: runs.append(list(args)),
        ), patch(
            "smartcut_runner._output_decode_stats",
            return_value=smartcut_runner._OutputDecodeStats(103, 20, 19),
        ):
            with self.assertRaisesRegex(RuntimeError, "verifikasi decode"):
                smartcut_runner.main()
        self.assertEqual(len(runs), 1)

    def test_retry_must_itself_match_exact_decoded_contract(self):
        source = _FakeSource()
        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_upstream",
        ), patch(
            "smartcut_runner._output_decode_stats",
            side_effect=[
                smartcut_runner._OutputDecodeStats(97, 20, 19),
                smartcut_runner._OutputDecodeStats(98, 21, 21),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "retry satu-frame"):
                smartcut_runner.main()


if __name__ == "__main__":
    unittest.main()
