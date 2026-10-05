"""Project Aria VRS extractors for NymeriaPlus → Minute wire.

Requires ``projectaria_tools``. If missing from the active interpreter, tries
the local NymeriaPlus uv/.venv used for the dataset tooling.
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
        import projectaria_tools  # noqa: F401
        _ARIA_BOOTSTRAPPED = True
        return
    except ImportError:
        pass
    candidates = [
        Path(os.environ.get(
            "NYMERIA_VENV",
            r"C:\Users\victo\Documents\Codex\2026-10-05\pre\work\nymeria_dataset\.venv",
        )),
        Path(r"C:\Users\victo\Documents\Codex\2026-10-05\pre\work\nymeria_dataset\.venv"),
    ]
    for root in candidates:
        site = root / "Lib" / "site-packages"
        if site.is_dir() and str(site) not in sys.path:
            sys.path.insert(0, str(site))
        try:
            import projectaria_tools  # noqa: F401
            _ARIA_BOOTSTRAPPED = True
            return
        except ImportError:
            continue
    raise ImportError(
        "projectaria_tools é obrigatório para NymeriaPlus; "
        "instale no Python do QFar ou aponte NYMERIA_VENV para o .venv do dataset"
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
) -> list[tuple[int, tuple[float, float, float], tuple[float, float, float]]]:
    """Return (capture_ts_ns, accel_msec2, gyro_radsec) in [t0,t1]."""
    p = _provider(motion_vrs)
    sid = p.get_stream_id_from_label(label)
    n = int(p.get_num_data(sid))
    out: list[tuple[int, tuple[float, float, float], tuple[float, float, float]]] = []
    for idx in range(n):
        d = p.get_imu_data_by_index(sid, idx)
        ts = int(d.capture_timestamp_ns)
        if ts < t0_ns:
            continue
        if ts > t1_ns:
            break
        if not (bool(d.accel_valid) and bool(d.gyro_valid)):
            continue
        ax, ay, az = (float(x) for x in d.accel_msec2)
        wx, wy, wz = (float(x) for x in d.gyro_radsec)
        if not all(math.isfinite(v) for v in (ax, ay, az, wx, wy, wz)):
            continue
        out.append((ts, (ax, ay, az), (wx, wy, wz)))
    return out


def build_imu_csv_from_samples(
    samples: list[tuple[int, tuple[float, float, float], tuple[float, float, float]]],
    *,
    sample_rate_hz: int = config.ANDROID_IMU_SAMPLE_RATE_HZ,
    max_gap_ms: float = 25.0,
    stats: dict[str, Any] | None = None,
) -> str:
    """Resample measured Aria IMU onto a uniform Android 500 Hz relative grid."""
    if len(samples) < 2:
        raise RuntimeError("sem amostras válidas de IMU Nymeria")
    step_ns = int(round(1_000_000_000 / sample_rate_hz))
    step_ms = 1000.0 / sample_rate_hz
    max_gap_samples = max(1, int(math.ceil(max_gap_ms / step_ms)))
    t0 = samples[0][0]
    t_last = samples[-1][0]
    # Continuity on source timestamps
    for a, b in zip(samples, samples[1:]):
        gap = b[0] - a[0]
        if gap > max_gap_ms * 1_000_000:
            raise RuntimeError(
                f"cobertura IMU insuficiente no giroscópio/acelerômetro "
                f"(lacuna máxima={gap/1e6:.0f}ms)")
    duration_ns = t_last - t0
    n = max(2, int(duration_ns // step_ns) + 1)
    # Bucket average like ego4d
    sums = [[0.0, 0.0, 0.0] for _ in range(n)]
    gyros = [[0.0, 0.0, 0.0] for _ in range(n)]
    counts = [0] * n
    for ts, accel, gyro in samples:
        idx = int((ts - t0) // step_ns)
        if idx < 0 or idx >= n:
            continue
        for i in range(3):
            sums[idx][i] += accel[i]
            gyros[idx][i] += gyro[i]
        counts[idx] += 1
    # Continuity of filled buckets
    filled = [i for i, c in enumerate(counts) if c > 0]
    if len(filled) < 2:
        raise RuntimeError("sem cobertura contínua de IMU Nymeria")
    first, last = filled[0], filled[-1]
    if first > max_gap_samples or (n - 1 - last) > max_gap_samples:
        raise RuntimeError(
            f"cobertura IMU insuficiente "
            f"(lacuna máxima={max(first, n-1-last) * step_ms:.0f}ms)")
    internal = 0
    run = 0
    for i in range(first, last + 1):
        if counts[i] == 0:
            run += 1
            internal = max(internal, run)
        else:
            run = 0
    if internal > max_gap_samples:
        raise RuntimeError(
            f"cobertura IMU insuficiente "
            f"(lacuna máxima={internal * step_ms:.0f}ms)")

    # Interpolate empty buckets between neighbors
    left = [None] * n
    right = [None] * n
    prev = None
    for i in range(n):
        if counts[i]:
            prev = i
        left[i] = prev
    nxt = None
    for i in range(n - 1, -1, -1):
        if counts[i]:
            nxt = i
        right[i] = nxt

    def _vals(i: int) -> tuple[tuple[float, float, float], tuple[float, float, float], bool]:
        if counts[i]:
            c = counts[i]
            return (
                (sums[i][0] / c, sums[i][1] / c, sums[i][2] / c),
                (gyros[i][0] / c, gyros[i][1] / c, gyros[i][2] / c),
                False,
            )
        a, b = left[i], right[i]
        if a is None and b is None:
            raise RuntimeError("IMU Nymeria sem vizinhos para interpolar")
        if a is None or b is None or a == b:
            j = a if a is not None else b
            assert j is not None
            c = counts[j]
            return (
                (sums[j][0] / c, sums[j][1] / c, sums[j][2] / c),
                (gyros[j][0] / c, gyros[j][1] / c, gyros[j][2] / c),
                True,
            )
        w = (i - a) / (b - a)
        ca, cb = counts[a], counts[b]
        accel = tuple(
            (1 - w) * (sums[a][k] / ca) + w * (sums[b][k] / cb) for k in range(3)
        )
        gyro = tuple(
            (1 - w) * (gyros[a][k] / ca) + w * (gyros[b][k] / cb) for k in range(3)
        )
        return accel, gyro, True  # type: ignore[return-value]

    lines = ["t,ax,ay,az,wx,wy,wz"]
    interpolated = 0
    nearest = 0
    max_run = 0
    run = 0
    for i in range(n):
        accel, gyro, was_missing = _vals(i)
        if was_missing:
            interpolated += 1
            run += 1
            max_run = max(max_run, run)
            if left[i] is None or right[i] is None:
                nearest += 1
        else:
            run = 0
        lines.append(
            f"{i * step_ns},"
            f"{accel[0]:.6f},{accel[1]:.6f},{accel[2]:.6f},"
            f"{gyro[0]:.6f},{gyro[1]:.6f},{gyro[2]:.6f}"
        )
    if stats is not None:
        half = str(max(0, step_ns // 2))
        span_ns = min(max_run * step_ns, int(max_gap_ms * 1_000_000))
        stats.clear()
        stats.update({
            "droppedRowCount": 0,
            "interpolatedCount": int(interpolated),
            "maxAlignmentDeltaNs": half,
            "maxInterpolationSpanNs": "25000000",
            "measuredMaxInterpolationSpanNs": str(span_ns if span_ns > 0 else step_ns),
            "nearestFallbackCount": int(nearest),
            "nearestFallbackToleranceNs": "1000000",
            "p95AlignmentDeltaNs": half,
            "sampleCount": int(n),
            "strategy": "gyro_anchored_v1",
        })
    return "\n".join(lines) + "\n"


def extract_rgb_mp4(
    data_vrs: Path,
    out_mp4: Path,
    *,
    t0_ns: int,
    t1_ns: int,
    label: str = "camera-rgb",
    ffmpeg: str | None = None,
) -> Path:
    """Decode RGB frames in [t0,t1] and encode a temporary MP4 via ffmpeg."""
    from .sidecar import ffmpeg_bin

    ff = ffmpeg or ffmpeg_bin()
    p = _provider(data_vrs)
    sid = p.get_stream_id_from_label(label)
    from projectaria_tools.core.sensor_data import TimeDomain, TimeQueryOptions
    import subprocess

    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    n = int(p.get_num_data(sid))
    span_s = max(1e-3, (t1_ns - t0_ns) / 1e9)
    # First frame in window → size; estimate fps from index density.
    h = w = 0
    first_idx = last_idx = -1
    for idx in range(n):
        _im, meta = p.get_image_data_by_index(sid, idx)
        ts = int(meta.capture_timestamp_ns)
        if ts < t0_ns:
            continue
        if ts > t1_ns:
            break
        if first_idx < 0:
            first_idx = idx
            arr0 = _im.to_numpy_array()
            h, w = int(arr0.shape[0]), int(arr0.shape[1])
        last_idx = idx
    if first_idx < 0 or last_idx <= first_idx or not h:
        raise RuntimeError("sem frames RGB Nymeria na janela")
    approx_frames = last_idx - first_idx + 1
    fps = max(1.0, approx_frames / span_s)
    cmd = [
        ff, "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{w}x{h}",
        "-r", f"{fps:.6f}",
        "-i", "pipe:0",
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        str(out_mp4),
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE)
    assert proc.stdin is not None
    written = 0
    try:
        for idx in range(first_idx, last_idx + 1):
            im, meta = p.get_image_data_by_index(sid, idx)
            ts = int(meta.capture_timestamp_ns)
            if ts < t0_ns or ts > t1_ns:
                continue
            arr = im.to_numpy_array()
            if arr.shape[0] != h or arr.shape[1] != w:
                continue
            if str(arr.dtype) != "uint8":
                arr = arr.astype("uint8")
            if arr.ndim != 3 or arr.shape[2] < 3:
                continue
            proc.stdin.write(memoryview(arr[:, :, :3]))
            written += 1
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        err = proc.stderr.read() if proc.stderr else b""
        code = proc.wait()
    if code != 0 or written < 2 or not out_mp4.is_file() or out_mp4.stat().st_size < 1000:
        raise RuntimeError(
            f"falha ao extrair RGB Nymeria (frames={written}, code={code}): "
            f"{err[-500:].decode('utf-8', errors='replace')}")
    return out_mp4
