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

    def test_part_before_boundary_maps_half_open_range_and_has_recovery_form(self):
        source = _FakeSource()
        plan = smartcut_runner._build_exact_keep_plan(source, "start,97/24")
        self.assertEqual(plan.expected_frames, 97)
        self.assertEqual(plan.frame_keep, "0,96")
        self.assertEqual(plan.recovery_frame_keep, "0,97")

    def test_part_after_boundary_starts_at_k_and_keeps_remaining_frames(self):
        source = _FakeSource()
        plan = smartcut_runner._build_exact_keep_plan(source, "97/24,end")
        self.assertEqual(plan.expected_frames, 200 - 97)
        self.assertEqual(plan.frame_keep, "97,-1")
        self.assertIsNone(plan.recovery_frame_keep)

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

    def test_main_uses_normal_exact_frame_form_when_decode_count_matches(self):
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

    def test_main_retries_one_extra_input_frame_when_decode_is_short_by_one(self):
        source = _FakeSource()
        keeps: list[str] = []

        def fake_run_and_count(argv, keep, output):
            keeps.append(keep)
            return 96 if len(keeps) == 1 else 97

        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_and_count_decoded",
            side_effect=fake_run_and_count,
        ):
            smartcut_runner.main()

        self.assertEqual(keeps, ["0,96", "0,97"])

    def test_main_rejects_decode_mismatch_that_is_not_exactly_one_short(self):
        source = _FakeSource()
        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_and_count_decoded",
            return_value=95,
        ):
            with self.assertRaisesRegex(RuntimeError, "jumlah frame terdecode"):
                smartcut_runner.main()

    def test_main_rejects_retry_when_recovery_does_not_match_expected_count(self):
        source = _FakeSource()
        with patch.object(sys, "argv", self._argv()), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_and_count_decoded",
            side_effect=[96, 98],
        ):
            with self.assertRaisesRegex(RuntimeError, "retry boundary exact"):
                smartcut_runner.main()

    def test_terminal_end_cannot_retry_past_source_tail(self):
        source = _FakeSource()
        with patch.object(sys, "argv", self._argv("97/24,end")), patch(
            "smartcut_runner.MediaContainer",
            return_value=source,
        ), patch(
            "smartcut_runner._run_and_count_decoded",
            return_value=102,
        ) as run_count:
            with self.assertRaisesRegex(RuntimeError, "jumlah frame terdecode"):
                smartcut_runner.main()
        run_count.assert_called_once()


if __name__ == "__main__":
    unittest.main()
