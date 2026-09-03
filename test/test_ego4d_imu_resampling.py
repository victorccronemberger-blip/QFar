from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from moneymin import ego4d


def _write_imu(path: Path, *, accel_gap: bool = False) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow([
            "component_idx", "component_timestamp_ms", "canonical_timestamp_ms",
            "gyro_x", "gyro_y", "gyro_z", "accl_x", "accl_y", "accl_z",
        ])
        for timestamp in range(0, 1001, 10):
            accel_present = timestamp % 20 == 0
            if accel_gap and 100 < timestamp < 900:
                accel_present = False
            accel = ("1", "2", "3") if accel_present else ("", "", "")
            writer.writerow([
                0, timestamp, timestamp, "0.1", "0.2", "0.3", *accel,
            ])


class Ego4dImuResamplingTests(unittest.TestCase):
    def test_resamples_sensors_with_different_native_rates(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "imu.csv"
            _write_imu(source)

            result = ego4d.build_imu_csv(
                source, (0.0, 1.0), duration_ms=1000)
            rows = result.strip().splitlines()

        self.assertEqual(rows[0], "t,ax,ay,az,wx,wy,wz")
        self.assertEqual(
            len(rows), 502)  # cabeçalho + 500 Hz inclusivo em 1 segundo
        self.assertEqual(
            rows[1],
            "0,1.000000,2.000000,3.000000,0.100000,0.200000,0.300000",
        )

    def test_rejects_a_real_gap_in_either_sensor(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "imu-gap.csv"
            _write_imu(source, accel_gap=True)

            with self.assertRaisesRegex(
                    RuntimeError, "acelerômetro.*lacuna máxima"):
                ego4d.build_imu_csv(
                    source, (0.0, 1.0), duration_ms=1000,
                    validate_only=True)
