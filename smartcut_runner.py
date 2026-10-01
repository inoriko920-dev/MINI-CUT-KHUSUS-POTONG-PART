"""MiniCut SmartCut companion.

MiniCut mengekspor subtitle sebagai file .srt eksternal per part. SmartCut
upstream secara default ikut menyalin subtitle stream internal dari container
sumber. Itu dapat membuat player menampilkan subtitle lama/embedded alih-alih
SRT hasil MiniCut.

Companion ini juga menjaga kontrak frame MiniCut untuk cut exact-PTS. MiniCut
memetakan exact PTS ke indeks frame master tanpa toleransi lalu meneruskannya ke
mode ``--frames`` resmi SmartCut. Setelah ekspor, companion tidak hanya percaya
jumlah packet/PTS: GOP terakhir benar-benar didekode dengan PyAV. Jika dan hanya
jika satu frame akhir terbukti tidak dapat didekode, end boundary diulang dengan
satu source frame tambahan sebagai frame pengorbanan. Retry harus menghasilkan
jumlah frame efektif yang tepat; mismatch lain selalu menjadi hard failure.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
from smartcut.media_container import MediaContainer


_original_media_container_init = MediaContainer.__init__


def _init_without_embedded_subtitles(self, *args, **kwargs):
    _original_media_container_init(self, *args, **kwargs)
    # smart_cut() menentukan stream subtitle output dari panjang list ini.
    # Kosongkan setelah source selesai dianalisis agar embedded subtitle tidak
    # ikut dimux. External .srt per part tetap dibuat oleh MiniCut sendiri.
    self.subtitle_tracks = []


MediaContainer.__init__ = _init_without_embedded_subtitles

from smartcut.__main__ import main as _smartcut_main  # noqa: E402


_TERMINAL_START = {"s", "start"}
_TERMINAL_END = {"e", "end", "-0"}


@dataclass(frozen=True)
class _ExactKeepPlan:
    expected_frames: int
    frame_keep: str
    retry_frame_keep: str | None


@dataclass(frozen=True)
class _OutputDecodeStats:
    packet_frames: int
    tail_packets: int
    tail_decoded: int

    @property
    def tail_missing(self) -> int:
        return max(0, self.tail_packets - self.tail_decoded)

    @property
    def effective_decoded_frames(self) -> int:
        # SmartCut's MediaContainer counts packet PTS. The reproduced R0 bug is
        # a packet at the final edge that exists in the container but does not
        # produce a decoded frame. Decode only the final GOP and subtract that
        # proven tail deficit from the lightweight packet count.
        return max(0, self.packet_frames - self.tail_missing)


def _as_fraction(value: Any) -> Fraction:
    return value if isinstance(value, Fraction) else Fraction(str(value))


def _is_canonical_exact_token(token: str) -> bool:
    """True for MiniCut's normalized Fraction text, not ms fallback decimals."""
    text = str(token).strip().lower()
    if text in _TERMINAL_START or text in _TERMINAL_END:
        return True
    # core.py emits exact PTS via str(Fraction): either integer or n/d.
    # Non-exact fallback timestamps are always fixed decimals (x.xxxxxx), so
    # requiring integer/fraction syntax lets the companion distinguish them.
    if not re.fullmatch(r"-?\d+(?:/\d+)?", text):
        return False
    try:
        Fraction(text)
    except (ValueError, ZeroDivisionError):
        return False
    return True


def _looks_like_minicut_exact_keep(argv: list[str]) -> bool:
    if "--frames" in argv or "--keep" not in argv or len(argv) < 2:
        return False
    try:
        keep_index = argv.index("--keep")
        keep = argv[keep_index + 1]
    except (ValueError, IndexError):
        return False
    values = [item.strip() for item in keep.split(",")]
    if len(values) != 2:
        return False

    # Do not rely on the output filename here. core.export_segments_smartcut()
    # writes each part to a temporary staging path first, so the companion does
    # not necessarily see the final "*_Part-01.mp4" name. Exact MiniCut calls
    # already have an unambiguous contract: exact PTS values are emitted as
    # canonical Fraction text (integer or n/d), while non-exact fallback times
    # are fixed decimal strings. This keeps staging and final export on the same
    # exact-frame validation path.
    non_terminal = [
        value for value in values
        if value.lower() not in _TERMINAL_START | _TERMINAL_END
    ]
    return bool(non_terminal) and all(
        _is_canonical_exact_token(value) for value in values
    )


def _video_time_base(source: MediaContainer) -> Fraction:
    stream = getattr(source, "video_stream", None)
    if stream is None or getattr(stream, "time_base", None) is None:
        raise RuntimeError("Video SmartCut tidak memiliki time_base untuk verifikasi PTS.")
    return _as_fraction(stream.time_base)


def _exact_frame_index(source: MediaContainer, exact_time: str) -> int:
    target = Fraction(str(exact_time).strip())
    pts_values = getattr(source, "video_frame_times_pts", None)
    if pts_values is None:
        raise RuntimeError("Daftar PTS frame master SmartCut tidak tersedia.")
    time_base = _video_time_base(source)
    start_time = _as_fraction(getattr(source, "start_time", Fraction(0)))
    target_absolute = target + start_time

    # Compare in the integer PTS domain whenever possible. This avoids float
    # tolerance and guarantees MiniCut never silently maps to a nearby frame.
    pts_fraction = target_absolute / time_base
    if pts_fraction.denominator != 1:
        raise RuntimeError(
            f"Exact PTS {target} tidak berada pada grid time_base video {time_base}."
        )
    target_pts = pts_fraction.numerator
    for index, raw_pts in enumerate(pts_values):
        if int(raw_pts) == target_pts:
            return index
    raise RuntimeError(
        f"Exact PTS {target} tidak ditemukan pada frame master SmartCut."
    )


def _build_exact_keep_plan(source: MediaContainer, keep: str) -> _ExactKeepPlan:
    values = [item.strip() for item in keep.split(",")]
    if len(values) != 2:
        raise RuntimeError("MiniCut exact keep harus memiliki satu start dan satu end.")
    start_raw, end_raw = values
    total_frames = len(getattr(source, "video_frame_times_pts", []))
    if total_frames <= 0:
        raise RuntimeError("Video tidak memiliki frame master untuk diverifikasi.")

    if start_raw.lower() in _TERMINAL_START:
        start_index = 0
    else:
        start_index = _exact_frame_index(source, start_raw)

    retry_frame_keep: str | None = None
    if end_raw.lower() in _TERMINAL_END:
        end_exclusive = total_frames
        end_frame_inclusive = -1
    else:
        end_exclusive = _exact_frame_index(source, end_raw)
        end_frame_inclusive = end_exclusive - 1
        # A retry is permitted only when another source frame really exists.
        # That next frame is never accepted as content; it is exposed solely as
        # a sacrificial frame if decode verification proves SmartCut discarded
        # the desired final frame at the exact boundary.
        if end_exclusive < total_frames:
            retry_frame_keep = f"{start_index},{end_exclusive}"

    expected = end_exclusive - start_index
    if expected <= 0:
        raise RuntimeError(
            f"Rentang exact SmartCut tidak valid: start={start_raw}, end={end_raw}."
        )

    # SmartCut --frames treats the second number as the final INCLUDED frame and
    # internally converts it to the next frame's timestamp. Therefore [K, L)
    # maps exactly to "K,L-1". -1 is SmartCut's documented final-frame token.
    frame_keep = f"{start_index},{end_frame_inclusive}"
    return _ExactKeepPlan(
        expected_frames=expected,
        frame_keep=frame_keep,
        retry_frame_keep=retry_frame_keep,
    )


def _output_decode_stats(path: Path) -> _OutputDecodeStats:
    """Count packet PTS cheaply, but decode the final GOP for truth.

    SmartCut/MediaContainer inventory is packet based. A malformed/truncated
    final GOP can therefore report N packet timestamps while a real decoder only
    yields N-1 frames. We scan packets (cheap), remember only the final GOP, then
    reopen and seek to its keyframe so normal production validation decodes a
    bounded tail rather than an entire movie part.
    """
    packet_pts: list[int] = []
    tail_pts: list[int] = []
    last_keyframe_pts: int | None = None

    container = av.open(str(path), mode="r")
    try:
        if not container.streams.video:
            raise RuntimeError("Output SmartCut tidak memiliki video stream.")
        stream = container.streams.video[0]
        for packet in container.demux(stream):
            if packet.pts is None:
                continue
            pts = int(packet.pts)
            packet_pts.append(pts)
            if packet.is_keyframe:
                last_keyframe_pts = pts
                tail_pts = [pts]
            elif last_keyframe_pts is None:
                # Provisional pre-keyframe tail. It is discarded as soon as a
                # real keyframe is encountered; if the file has no keyframe at
                # all, full decode below is the safe fallback.
                tail_pts.append(pts)
            else:
                tail_pts.append(pts)
    finally:
        container.close()

    if not packet_pts:
        raise RuntimeError("Output SmartCut tidak memiliki packet video ber-PTS.")
    if not tail_pts:
        tail_pts = list(packet_pts)

    wanted = Counter(tail_pts)
    decoded = Counter()
    decoder = av.open(str(path), mode="r")
    try:
        if not decoder.streams.video:
            raise RuntimeError("Output SmartCut tidak memiliki video stream saat decode.")
        stream = decoder.streams.video[0]
        if last_keyframe_pts is not None:
            # PyAV interprets the offset in stream.time_base when stream= is
            # supplied and seeks backward to a keyframe. Starting at/before the
            # remembered final keyframe gives the decoder all references needed.
            decoder.seek(
                int(last_keyframe_pts),
                stream=stream,
                backward=True,
                any_frame=False,
            )
        for frame in decoder.decode(stream):
            if frame.pts is None:
                continue
            pts = int(frame.pts)
            if pts in wanted:
                decoded[pts] += 1
    finally:
        decoder.close()

    matched = sum(
        min(count, decoded.get(pts, 0))
        for pts, count in wanted.items()
    )
    return _OutputDecodeStats(
        packet_frames=len(packet_pts),
        tail_packets=len(tail_pts),
        tail_decoded=matched,
    )


def _run_upstream(argv: list[str]) -> None:
    previous = list(sys.argv)
    try:
        sys.argv = [previous[0], *argv]
        _smartcut_main()
    finally:
        sys.argv = previous


def _replace_keep_with_frames(argv: list[str], keep: str) -> list[str]:
    rewritten = list(argv)
    index = rewritten.index("--keep")
    rewritten[index + 1] = keep
    if "--frames" not in rewritten:
        rewritten.append("--frames")
    return rewritten


def _initial_output_is_exact(stats: _OutputDecodeStats, expected: int) -> bool:
    return (
        stats.packet_frames == expected
        and stats.tail_missing == 0
        and stats.effective_decoded_frames == expected
    )


def _retry_output_is_exact(stats: _OutputDecodeStats, expected: int) -> bool:
    # Ideal retry shape for the reproduced SmartCut bug: one deliberately extra
    # packet/frame is exposed, the broken tail consumes exactly one, and exactly
    # N desired frames remain decodable. Some codecs/muxers may clip the extra
    # packet themselves; the ordinary exact N/N shape is safe too.
    sacrificed_one = (
        stats.packet_frames == expected + 1
        and stats.tail_missing == 1
        and stats.effective_decoded_frames == expected
    )
    clipped_to_exact = _initial_output_is_exact(stats, expected)
    return sacrificed_one or clipped_to_exact


def main() -> None:
    argv = list(sys.argv[1:])
    if not _looks_like_minicut_exact_keep(argv):
        _run_upstream(argv)
        return

    keep_index = argv.index("--keep")
    keep = argv[keep_index + 1]
    source = MediaContainer(argv[0])
    try:
        plan = _build_exact_keep_plan(source, keep)
    finally:
        source.close()

    exact_frame_argv = _replace_keep_with_frames(argv, plan.frame_keep)
    _run_upstream(exact_frame_argv)

    output = Path(argv[1])
    stats = _output_decode_stats(output)
    if _initial_output_is_exact(stats, plan.expected_frames):
        return

    # Never apply a blanket +/- one-frame correction. Retry is allowed only for
    # the single reproduced signature: packet inventory is exactly right, the
    # decoded final GOP proves exactly one frame missing, and a source frame
    # exists after the requested end boundary to act as a disposable cushion.
    can_retry_one = (
        plan.retry_frame_keep is not None
        and stats.packet_frames == plan.expected_frames
        and stats.tail_missing == 1
        and stats.effective_decoded_frames == plan.expected_frames - 1
    )
    if can_retry_one:
        retry_argv = _replace_keep_with_frames(argv, plan.retry_frame_keep)
        try:
            output.unlink(missing_ok=True)
        except OSError:
            pass
        _run_upstream(retry_argv)
        retry_stats = _output_decode_stats(output)
        if _retry_output_is_exact(retry_stats, plan.expected_frames):
            return
        raise RuntimeError(
            "SmartCut retry satu-frame tidak menghasilkan boundary exact: "
            f"packet={retry_stats.packet_frames}, "
            f"tail_missing={retry_stats.tail_missing}, "
            f"decoded_efektif={retry_stats.effective_decoded_frames}, "
            f"seharusnya={plan.expected_frames}."
        )

    raise RuntimeError(
        "SmartCut menghasilkan boundary yang tidak lolos verifikasi decode: "
        f"packet={stats.packet_frames}, tail_missing={stats.tail_missing}, "
        f"decoded_efektif={stats.effective_decoded_frames}, "
        f"seharusnya={plan.expected_frames}."
    )


if __name__ == "__main__":
    main()
