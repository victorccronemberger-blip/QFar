"""Project Aria VRS extractors for NymeriaPlus → Minute wire.

Requires the bundled Gen 1 ``projectaria_tools`` SDK. A development environment
may explicitly select another installation with ``NYMERIA_VENV``.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Any

from . import config

_ARIA_BOOTSTRAPPED = False


def _bootstrap_projectaria() -> None:
    global _ARIA_BOOTSTRAPPED
    if _ARIA_BOOTSTRAPPED:
        return
    try:
        from projectaria_tools.core import data_provider  # noqa: F401
        from projectaria_tools.core.sensor_data import TimeDomain
        _ = TimeDomain.DEVICE_TIME
        _ARIA_BOOTSTRAPPED = True
        return
    except ImportError:
        pass
    override = os.environ.get("NYMERIA_VENV", "").strip()
    candidates = [Path(override).expanduser()] if override else []
    for root in candidates:
        site = root / "Lib" / "site-packages"
        if site.is_dir() and str(site) not in sys.path:
            sys.path.insert(0, str(site))
        try:
            from projectaria_tools.core import data_provider  # noqa: F401
            from projectaria_tools.core.sensor_data import TimeDomain
            _ = TimeDomain.DEVICE_TIME
            _ARIA_BOOTSTRAPPED = True
            return
        except ImportError:
            continue
    raise ImportError(
        "SDK Project Aria Gen 1 ausente ou incompatível; repare a instalação "
        "do QMoney. Em desenvolvimento, instale as dependências do projeto."
    )


def _provider(path: Path):
    _bootstrap_projectaria()
    from projectaria_tools.core import data_provider
    return data_provider.create_vrs_data_provider(str(path))


def imu_stream_span_ns(motion_vrs: Path, label: str = "imu-right") -> tuple[int, int]:
    p = _provider(motion_vrs)
    sid = p.get_stream_id_from_label(label)
    from projectaria_tools.core.sensor_data import TimeDomain
    return (
        int(p.get_first_time_ns(sid, TimeDomain.DEVICE_TIME)),
        int(p.get_last_time_ns(sid, TimeDomain.DEVICE_TIME)),
    )


def rgb_stream_span_ns(data_vrs: Path, label: str = "camera-rgb") -> tuple[int, int]:
    p = _provider(data_vrs)
    sid = p.get_stream_id_from_label(label)
    from projectaria_tools.core.sensor_data import TimeDomain
    return (
        int(p.get_first_time_ns(sid, TimeDomain.DEVICE_TIME)),
        int(p.get_last_time_ns(sid, TimeDomain.DEVICE_TIME)),
    )


def read_imu_samples(
    motion_vrs: Path,
    *,
    t0_ns: int,
    t1_ns: int,
    label: str = "imu-right",
    stats: dict[str, Any] | None = None,
) -> list[tuple[int, tuple[float, float, float], tuple[float, float, float]]]:
    """Read DEVICE_TIME IMU, retaining real neighbors around both endpoints."""
    from bisect import bisect_left, bisect_right

    _validate_window_ns(t0_ns, t1_ns)
    p = _provider(motion_vrs)
    sid = p.get_stream_id_from_label(label)
    n = int(p.get_num_data(sid))
    timestamps = _stream_timestamps(p, sid)
    lo = bisect_left(timestamps, t0_ns - 25_000_000) if timestamps else 0
    hi = bisect_right(timestamps, t1_ns + 25_000_000) if timestamps else n
    out: list[tuple[int, tuple[float, float, float], tuple[float, float, float]]] = []
    dropped = 0
    dropped_timestamps: list[int] = []
    previous_ts: int | None = None
    for idx in range(lo, hi):
        d = p.get_imu_data_by_index(sid, idx)
        ts = int(d.capture_timestamp_ns)
        if ts < t0_ns - 25_000_000:
            continue
        if ts > t1_ns + 25_000_000:
            break
        if previous_ts is not None and ts <= previous_ts:
            raise RuntimeError("timestamps IMU Nymeria não são estritamente crescentes")
        previous_ts = ts
        if timestamps and ts != timestamps[idx]:
            raise RuntimeError("DEVICE_TIME da IMU diverge do timestamp de captura")
        if not (bool(d.accel_valid) and bool(d.gyro_valid)):
            if t0_ns <= ts <= t1_ns:
                dropped += 1
                dropped_timestamps.append(ts)
            continue
        ax, ay, az = (float(x) for x in d.accel_msec2)
        wx, wy, wz = (float(x) for x in d.gyro_radsec)
        if not all(math.isfinite(v) for v in (ax, ay, az, wx, wy, wz)):
            if t0_ns <= ts <= t1_ns:
                dropped += 1
                dropped_timestamps.append(ts)
            continue
        # Only the last valid predecessor and first valid successor are needed.
        if ts < t0_ns:
            out[:] = [(ts, (ax, ay, az), (wx, wy, wz))]
            continue
        out.append((ts, (ax, ay, az), (wx, wy, wz)))
        if ts >= t1_ns:
            break
    if stats is not None:
        stats.clear()
        stats.update({"droppedRowCount": dropped, "sourceRowCount": len(out),
                      "droppedRowTimestampsNs": dropped_timestamps})
    return out


def _validate_window_ns(t0_ns: int, t1_ns: int) -> None:
    if (type(t0_ns) is not int or type(t1_ns) is not int
            or not 0 <= t0_ns < t1_ns):
        raise ValueError("janela DEVICE_TIME Nymeria inválida")


def _stream_timestamps(provider: Any, stream_id: Any) -> list[int]:
    """Use the SDK timestamp index without decoding a whole VRS."""
    if not hasattr(provider, "get_timestamps_ns"):
        return []
    from projectaria_tools.core.sensor_data import TimeDomain
    timestamps = [int(value) for value in provider.get_timestamps_ns(
        stream_id, TimeDomain.DEVICE_TIME)]
    if any(b <= a for a, b in zip(timestamps, timestamps[1:])):
        raise RuntimeError("timestamps VRS Nymeria não são estritamente crescentes")
    return timestamps


def build_imu_csv_from_samples(
    samples: list[tuple[int, tuple[float, float, float], tuple[float, float, float]]],
    *,
    sample_rate_hz: int = config.ANDROID_IMU_SAMPLE_RATE_HZ,
    max_gap_ms: float = 25.0,
    t0_ns: int | None = None,
    duration_ms: int | None = None,
    source_dropped_row_count: int = 0,
    source_dropped_timestamps_ns: list[int] | None = None,
    stats: dict[str, Any] | None = None,
    stats_windows_ms: list[tuple[int, int]] | None = None,
) -> str:
    """Interpolate measured IMU at 500 Hz relative to the first selected RGB.

    DEVICE_TIME stays the source clock. Only the delivery layer adds the
    account's elapsedRealtime uptime. Source neighbors must span at most
    25 ms; an unbracketed edge allows at most 1 ms of nearest fallback.
    """
    from array import array
    from bisect import bisect_left, bisect_right

    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz deve ser inteiro positivo")
    if (not math.isfinite(max_gap_ms) or not 0 < max_gap_ms <= 25
            or type(source_dropped_row_count) is not int
            or source_dropped_row_count < 0):
        raise ValueError("limite de interpolação ou contador IMU inválido")
    if len(samples) < 2:
        raise RuntimeError("sem amostras válidas de IMU Nymeria")
    timestamps: list[int] = []
    for ts, accel, gyro in samples:
        if (type(ts) is not int or ts < 0 or len(accel) != 3 or len(gyro) != 3
                or not all(math.isfinite(value) for value in (*accel, *gyro))
                or (timestamps and ts <= timestamps[-1])):
            raise RuntimeError("amostras IMU Nymeria inválidas ou fora de ordem")
        timestamps.append(ts)
    origin = timestamps[0] if t0_ns is None else t0_ns
    if type(origin) is not int or origin < 0:
        raise ValueError("origem DEVICE_TIME da IMU inválida")
    if duration_ms is not None:
        if type(duration_ms) is not int or duration_ms <= 0:
            raise ValueError("duration_ms deve ser inteiro positivo")
        end_ns = origin + duration_ms * 1_000_000
    else:
        end_ns = timestamps[-1]
    if end_ns <= origin:
        raise RuntimeError("janela IMU Nymeria sem duração")
    step_ns = int(round(1_000_000_000 / sample_rate_hz))
    if step_ns <= 0:
        raise ValueError("sample_rate_hz fora da precisão de nanossegundos")
    n = (end_ns - origin) // step_ns + 1
    max_span_ns = int(round(max_gap_ms * 1_000_000))
    fallback_tolerance_ns = 1_000_000
    if source_dropped_timestamps_ns is not None:
        if (not isinstance(source_dropped_timestamps_ns, list)
                or any(type(ts) is not int or ts < 0 for ts in source_dropped_timestamps_ns)):
            raise ValueError("timestamps de linhas IMU descartadas inválidos")
        source_dropped_row_count = sum(
            origin <= ts <= end_ns for ts in source_dropped_timestamps_ns)
    windows: list[dict[str, Any]] = []
    window_starts_ns: list[int] = []
    if stats_windows_ms is not None:
        if (stats is None or not isinstance(stats_windows_ms, list)
                or (source_dropped_row_count and source_dropped_timestamps_ns is None)):
            raise ValueError("diagnósticos por chunk exigem stats e descartes temporizados")
        previous_end = 0
        for window in stats_windows_ms:
            if (not isinstance(window, (list, tuple)) or len(window) != 2
                    or any(type(value) is not int for value in window)
                    or not 0 <= window[0] < window[1] <= (end_ns - origin) / 1e6
                    or window[0] < previous_end):
                raise ValueError("janela de diagnóstico IMU inválida")
            previous_end = window[1]
            window_starts_ns.append(window[0] * 1_000_000)
            windows.append({"start_ms": window[0], "end_ms": window[1],
                            "deltas": array("Q"), "interpolated": 0,
                            "nearest": 0, "max_span": 0})
    left_index = max(0, bisect_left(timestamps, origin) - 1)
    right_index = min(len(timestamps) - 1, bisect_left(timestamps, end_ns))
    source_max_gap = max(
        (b - a for a, b in zip(timestamps[left_index:right_index],
                              timestamps[left_index + 1:right_index + 1])),
        default=0)
    if source_max_gap > max_span_ns:
        raise RuntimeError(
            "cobertura IMU insuficiente no giroscópio/acelerômetro "
            f"(lacuna máxima={source_max_gap / 1e6:.3f}ms)")
    deltas = array("Q")
    interpolated = nearest = max_measured_span = 0
    lines = ["t,ax,ay,az,wx,wy,wz"]
    for index in range(n):
        relative_ns = index * step_ns
        target_ns = origin + relative_ns
        right = bisect_left(timestamps, target_ns)
        was_interpolated = was_nearest = False
        measured_span = 0
        if right < len(samples) and timestamps[right] == target_ns:
            accel, gyro = samples[right][1:]
            delta = 0
        elif right == 0 or right == len(samples):
            neighbor = 0 if right == 0 else len(samples) - 1
            delta = abs(timestamps[neighbor] - target_ns)
            if delta > fallback_tolerance_ns:
                raise RuntimeError(
                    "cobertura IMU insuficiente na borda do vídeo "
                    f"(distância={delta / 1e6:.3f}ms)")
            accel, gyro = samples[neighbor][1:]
            nearest += 1
            was_nearest = True
        else:
            left = right - 1
            span = timestamps[right] - timestamps[left]
            if span > max_span_ns:
                raise RuntimeError("lacuna IMU Nymeria ultrapassa 25ms")
            weight = (target_ns - timestamps[left]) / span
            accel = tuple((1 - weight) * samples[left][1][axis]
                          + weight * samples[right][1][axis] for axis in range(3))
            gyro = tuple((1 - weight) * samples[left][2][axis]
                         + weight * samples[right][2][axis] for axis in range(3))
            delta = min(target_ns - timestamps[left], timestamps[right] - target_ns)
            max_measured_span = max(max_measured_span, span)
            interpolated += 1
            was_interpolated = True
            measured_span = span
        deltas.append(delta)
        if windows:
            window_index = bisect_right(window_starts_ns, relative_ns) - 1
            if window_index >= 0 and relative_ns < windows[window_index]["end_ms"] * 1_000_000:
                measured_window = windows[window_index]
                measured_window["deltas"].append(delta)
                measured_window["interpolated"] += int(was_interpolated)
                measured_window["nearest"] += int(was_nearest)
                measured_window["max_span"] = max(measured_window["max_span"], measured_span)
        lines.append(
            f"{relative_ns},"
            f"{accel[0]:.6f},{accel[1]:.6f},{accel[2]:.6f},"
            f"{gyro[0]:.6f},{gyro[1]:.6f},{gyro[2]:.6f}")
    def diagnostics(alignment: Any, interpolations: int, fallbacks: int,
                    observed_span: int, dropped_rows: int) -> dict[str, Any]:
        ordered = sorted(alignment)
        return {
            "droppedRowCount": dropped_rows,
            "interpolatedCount": interpolations,
            "maxAlignmentDeltaNs": str(max(alignment, default=0)),
            "maxInterpolationSpanNs": "25000000",
            "measuredMaxInterpolationSpanNs": str(observed_span),
            "nearestFallbackCount": fallbacks,
            "nearestFallbackToleranceNs": str(fallback_tolerance_ns),
            "p95AlignmentDeltaNs": str(ordered[max(0, math.ceil(len(ordered) * .95) - 1)] if ordered else 0),
            "sampleCount": len(alignment),
            "strategy": "gyro_anchored_v1",
        }

    if stats is not None:
        stats.clear()
        stats.update(diagnostics(deltas, interpolated, nearest,
                                 max_measured_span, source_dropped_row_count))
        stats.update({
            "sourceTimeDomain": "DEVICE_TIME",
            "sourceOriginNs": str(origin),
            "sourceRowCount": len(samples),
            "measuredMaxSourceGapNs": str(source_max_gap),
        })
        if stats_windows_ms is not None:
            stats["windows"] = [
                {"start_ms": window["start_ms"], "end_ms": window["end_ms"],
                 **diagnostics(
                     window["deltas"], window["interpolated"], window["nearest"],
                     window["max_span"], sum(
                         origin + window["start_ms"] * 1_000_000 <= ts
                         < origin + window["end_ms"] * 1_000_000
                         for ts in (source_dropped_timestamps_ns or [])))}
                for window in windows]
    return "\n".join(lines) + "\n"


def _write_rgb_png(path: Path, rgb: Any) -> None:
    """Write a temporary RGB PNG using only numpy from the Aria SDK."""
    import struct
    import zlib

    if (rgb.ndim != 3 or rgb.shape[2] not in (3, 4)
            or str(rgb.dtype) != "uint8" or min(rgb.shape[:2]) < 2):
        raise RuntimeError("frame RGB Nymeria inválido")
    height, width = map(int, rgb.shape[:2])
    pixels = rgb[:, :, :3].tobytes(order="C")
    stride = width * 3
    scanlines = b"".join(b"\x00" + pixels[row:row + stride]
                         for row in range(0, len(pixels), stride))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines, level=1))
        + chunk(b"IEND", b""))


def extract_rgb_mp4(
    data_vrs: Path,
    out_mp4: Path,
    *,
    t0_ns: int,
    t1_ns: int,
    label: str = "camera-rgb",
    ffmpeg: str | None = None,
    output_args: list[str] | None = None,
    max_frame_gap_ms: float | None = None,
    stats: dict[str, Any] | None = None,
) -> Path:
    """Export captured RGB as VFR, preserving DEVICE_TIME deltas within 1µs.

    The final packet ends at t1. No duplicate frame or estimated frame rate
    fills a missing image. Publication follows a measured PTS check, so a
    failed extraction preserves an existing output file.
    """
    from bisect import bisect_left
    import subprocess
    import tempfile
    import uuid
    import numpy as np
    from .sidecar import _extract_frame_pts, ffmpeg_bin, probe_video

    _validate_window_ns(t0_ns, t1_ns)
    if max_frame_gap_ms is not None and (
            not math.isfinite(max_frame_gap_ms) or max_frame_gap_ms <= 0):
        raise ValueError("max_frame_gap_ms deve ser positivo")
    p = _provider(data_vrs)
    sid = p.get_stream_id_from_label(label)
    timestamps = _stream_timestamps(p, sid)
    count = int(p.get_num_data(sid))
    lo = bisect_left(timestamps, t0_ns) if timestamps else 0
    hi = bisect_left(timestamps, t1_ns) if timestamps else count
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp_mp4 = out_mp4.with_name(f".{out_mp4.stem}-{uuid.uuid4().hex}.tmp.mp4")
    source_stat = data_vrs.stat()
    captured: list[int] = []
    shape: tuple[int, int] | None = None
    try:
        with tempfile.TemporaryDirectory(
                prefix=".nymeria-frames-", dir=out_mp4.parent) as scratch:
            scratch_dir = Path(scratch)
            for idx in range(lo, hi):
                image, meta = p.get_image_data_by_index(sid, idx)
                ts = int(meta.capture_timestamp_ns)
                if ts < t0_ns:
                    continue
                if ts >= t1_ns:
                    break
                if ((captured and ts <= captured[-1])
                        or (timestamps and ts != timestamps[idx])):
                    raise RuntimeError("timestamps RGB Nymeria inválidos")
                rgb = image.to_numpy_array()
                current_shape = tuple(map(int, rgb.shape[:2]))
                if shape is not None and current_shape != shape:
                    raise RuntimeError("dimensões RGB Nymeria mudaram no recorte")
                shape = current_shape
                # Aria Gen 1 cameras are mounted sideways. Match the SDK's
                # vrs_to_mp4 presentation; IMU stays in its recorded axes.
                _write_rgb_png(scratch_dir / f"frame-{len(captured):06d}.png",
                               np.rot90(rgb, -1))
                captured.append(ts)
            if len(captured) < 2:
                raise RuntimeError("sem frames RGB Nymeria na janela")
            gaps = [b - a for a, b in zip(captured, captured[1:])]
            edge_gap = max(captured[0] - t0_ns, t1_ns - captured[-1])
            if (max_frame_gap_ms is not None
                    and max(max(gaps), edge_gap) > max_frame_gap_ms * 1_000_000):
                raise RuntimeError("cobertura RGB Nymeria insuficiente (lacuna de frames)")
            relative_us = [int(round((ts - captured[0]) / 1000)) for ts in captured]
            if any(b <= a for a, b in zip(relative_us, relative_us[1:])):
                raise RuntimeError("timestamps RGB abaixo da precisão do MP4")
            duration_us = int(round((t1_ns - captured[0]) / 1000))
            tail_us = duration_us - relative_us[-1]
            if tail_us <= 0:
                raise RuntimeError("duração do último frame RGB inválida")
            manifest = ["ffconcat version 1.0"]
            for index, timestamp in enumerate(relative_us):
                next_timestamp = (relative_us[index + 1]
                                  if index + 1 < len(relative_us) else duration_us)
                manifest.extend([
                    f"file frame-{index:06d}.png", "option framerate 1000000",
                    f"duration {(next_timestamp - timestamp) / 1e6:.6f}"])
            manifest_path = scratch_dir / "frames.ffconcat"
            manifest_path.write_text("\n".join(manifest) + "\n", encoding="ascii")
            # safe=0 is required for the framerate option. Every filename in
            # this manifest is generated here, relative to our private folder.
            args = (output_args if output_args is not None else [
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-bf", "0", "-pix_fmt", "yuv420p", "-movflags", "+faststart"])
            cmd = [
                ffmpeg or ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                "-safe", "0", "-f", "concat", "-i", str(manifest_path), *args,
                "-fps_mode", "passthrough", "-enc_time_base", "1:1000000",
                "-bsf:v", f"setts=duration=if(eq(N\\,{len(captured) - 1})\\,{tail_us}\\,DURATION)",
                "-video_track_timescale", "1000000", "-f", "mp4", str(tmp_mp4)]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=3600,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            if result.returncode != 0:
                raise RuntimeError(
                    f"falha ao extrair RGB Nymeria: {result.stderr[-500:]}")
            measured = _extract_frame_pts(tmp_mp4)
            expected = [ts * 1000 for ts in relative_us]
            if (len(measured) != len(expected)
                    or any(abs(frame[0] - ts) > 1000
                           for frame, ts in zip(measured, expected))):
                raise RuntimeError("PTS do MP4 não preservam capturas RGB Nymeria")
            probe = probe_video(tmp_mp4)
            if abs(int(probe.get("duration_ms") or 0) - duration_us / 1000) > 1:
                raise RuntimeError("duração do MP4 diverge da janela RGB medida")
            current_stat = data_vrs.stat()
            if (current_stat.st_size, current_stat.st_mtime_ns) != (
                    source_stat.st_size, source_stat.st_mtime_ns):
                raise RuntimeError("VRS RGB alterado durante extração")
            tmp_mp4.replace(out_mp4)
            if stats is not None:
                stats.clear()
                stats.update({
                    "sourceTimeDomain": "DEVICE_TIME",
                    "firstCaptureTimestampNs": str(captured[0]),
                    "lastCaptureTimestampNs": str(captured[-1]),
                    "sourceEndTimestampNs": str(t1_ns),
                    "durationNs": str(duration_us * 1000),
                    "frameCount": len(captured),
                    "measuredMaxFrameGapNs": str(max(gaps)),
                    "measuredLeadingFrameGapNs": str(captured[0] - t0_ns),
                    "measuredTrailingFrameGapNs": str(t1_ns - captured[-1]),
                    "ptsPrecisionNs": "1000",
                    "measuredPts": True,
                    "presentationRotationDegreesClockwise": 90,
                })
            return out_mp4
    finally:
        tmp_mp4.unlink(missing_ok=True)
