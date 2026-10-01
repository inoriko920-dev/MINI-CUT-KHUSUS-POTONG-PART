"""MiniCut SmartCut companion.

MiniCut mengekspor subtitle sebagai file .srt eksternal per part. SmartCut
upstream secara default ikut menyalin subtitle stream internal dari container
sumber. Itu dapat membuat player menampilkan subtitle lama/embedded alih-alih
SRT hasil MiniCut.

Companion ini juga menjaga kontrak frame MiniCut untuk cut exact-PTS. MiniCut
memetakan exact PTS ke indeks frame master tanpa toleransi lalu meneruskannya ke
mode ``--frames`` resmi SmartCut.

SmartCut 1.7 memiliki bug MP4 di ujung segmen: packet terakhir hasil recode dapat
memiliki duration kosong/0 sehingga muxer membuat edit-list yang menandai frame
terakhir sebagai discard. Upstream memperbaikinya setelah 1.7 dengan menyimpan
duration packet valid sebelumnya dan menggunakannya pada packet yang duration-nya
hilang. MiniCut membackport fix kecil itu di companion ini tanpa memodifikasi
paket SmartCut yang terpasang.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
from smartcut.media_container import MediaContainer
from smartcut.cut_video import VideoCutter


_original_media_container_init = MediaContainer.__init__
_original_fix_packet_timestamps = VideoCutter._fix_packet_timestamps


def _init_without_embedded_subtitles(self, *args, **kwargs):
    _original_media_container_init(self, *args, **kwargs)
    # smart_cut() menentukan stream subtitle output dari panjang list ini.
    # Kosongkan setelah source selesai dianalisis agar embedded subtitle tidak
    # ikut dimux. External .srt per part tetap dibuat oleh MiniCut sendiri.
    self.subtitle_tracks = []


def _ensure_packet_duration(cutter: Any, packet: Any) -> None:
    """Backport upstream post-1.7 fix for missing final packet duration.

    SmartCut upstream commit 9d8dbae57d1c added this exact policy because MP4
    muxers can create an edit-list that discards the last frame when duration is
    missing/zero. Keep the last known positive duration and apply it only when
    the current packet has no valid duration.
    """
    duration = getattr(packet, "duration", None)
    if duration is not None and int(duration) > 0:
        cutter.typical_frame_duration = int(duration)
        return
    typical = getattr(cutter, "typical_frame_duration", None)
    if typical is not None and int(typical) > 0:
        packet.duration = int(typical)


def _fix_packet_timestamps_with_duration(self, packet):
    _original_fix_packet_timestamps(self, packet)
    _ensure_packet_duration(self, packet)


MediaContainer.__init__ = _init_without_embedded_subtitles
VideoCutter._fix_packet_timestamps = _fix_packet_timestamps_with_duration

from smartcut.__main__ import main as _smartcut_main  # noqa: E402


_TERMINAL_START = {"s", "start"}
_TERMINAL_END = {"e", "end", "-0"}


@dataclass(frozen=True)
class _ExactKeepPlan:
    expected_frames: int
    frame_keep: str


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

    # core.export_segments_smartcut() writes parts to a temporary staging path,
    # so do not rely on the final filename. Exact MiniCut calls are identified
    # by canonical Fraction syntax; fallback millisecond times use decimals.
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

    if end_raw.lower() in _TERMINAL_END:
        end_exclusive = total_frames
        end_frame_inclusive = -1
    else:
        end_exclusive = _exact_frame_index(source, end_raw)
        end_frame_inclusive = end_exclusive - 1

    expected = end_exclusive - start_index
    if expected <= 0:
        raise RuntimeError(
            f"Rentang exact SmartCut tidak valid: start={start_raw}, end={end_raw}."
        )

    # SmartCut --frames treats the second number as the final INCLUDED frame and
    # internally converts it to the next frame's timestamp. Therefore [K, L)
    # maps exactly to "K,L-1". -1 is SmartCut's documented final-frame token.
    return _ExactKeepPlan(
        expected_frames=expected,
        frame_keep=f"{start_index},{end_frame_inclusive}",
    )


def _video_frame_count(path: Path) -> int:
    media = MediaContainer(str(path))
    try:
        return len(getattr(media, "video_frame_times_pts", []))
    finally:
        media.close()


def _discard_video_packet_count(path: Path) -> int:
    """Return video packets that the container marks as discard."""
    container = av.open(str(path), mode="r")
    try:
        if not container.streams.video:
            raise RuntimeError("Output SmartCut tidak memiliki video stream.")
        stream = container.streams.video[0]
        return sum(1 for packet in container.demux(stream) if packet.is_discard)
    finally:
        container.close()


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
    actual = _video_frame_count(output)
    if actual != plan.expected_frames:
        raise RuntimeError(
            "SmartCut menghasilkan jumlah frame yang tidak sesuai boundary exact: "
            f"hasil {actual}, seharusnya {plan.expected_frames}."
        )

    discarded = _discard_video_packet_count(output)
    if discarded:
        raise RuntimeError(
            "SmartCut menghasilkan packet video ber-flag discard; "
            f"jumlah={discarded}. Boundary exact tidak dapat dianggap valid."
        )


if __name__ == "__main__":
    main()
