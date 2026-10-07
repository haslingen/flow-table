#!/usr/bin/env python3
"""Capture a FlowTable image sequence without blocking on image analysis.

The producer owns the camera and only timestamps and queues native-resolution
frames.  A background worker performs measurement and storage.  The final
frame has higher queue priority, so its result is produced before any queued
older frames.  A review MP4 is assembled only after all frames are analysed.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from queue import PriorityQueue
import threading
import time
from typing import Any

import cv2

from flowtable import (
    load_calibration,
    measure,
    render_result,
    saved_ellipse,
)
from picamera2 import Picamera2


@dataclass(order=True)
class FrameJob:
    priority: int
    frame_id: int
    image: Any = field(compare=False)
    metadata: dict[str, Any] = field(compare=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frequency-hz", type=float, default=1.0)
    parser.add_argument("--duration-s", type=float, default=15.0)
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--strike-count", type=int, default=0)
    parser.add_argument("--output-root", type=Path, default=Path("data"))
    parser.add_argument("--calibration-file", type=Path, default=Path("calibration.json"))
    parser.add_argument("--dark-threshold", type=int, default=115)
    parser.add_argument("--table-threshold", type=int, default=150)
    return parser.parse_args()


def write_state(path: Path, **values: Any) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as file:
        json.dump(values, file, indent=2)
        file.write("\n")
    temporary.replace(path)


def create_preview(frame_paths: list[Path], destination: Path, frequency_hz: float) -> str | None:
    """Create a review-only MP4 in capture order; originals remain authoritative."""
    if not frame_paths:
        return None
    first = cv2.imread(str(frame_paths[0]))
    if first is None:
        return None
    height, width = first.shape[:2]
    scale = min(1.0, 1280 / width)
    size = (round(width * scale), round(height * scale))
    writer = cv2.VideoWriter(str(destination), cv2.VideoWriter_fourcc(*"mp4v"), frequency_hz, size)
    if not writer.isOpened():
        return None
    try:
        for path in frame_paths:
            image = cv2.imread(str(path))
            if image is not None:
                writer.write(cv2.resize(image, size) if image.shape[:2] != (size[1], size[0]) else image)
    finally:
        writer.release()
    return str(destination)


def calibrated_roi(image: Any, ellipse: tuple[tuple[float, float], tuple[float, float], float]) -> tuple[Any, tuple[tuple[float, float], tuple[float, float], float]]:
    """Crop around the fixed calibrated disk and translate its ellipse to ROI coordinates."""
    (center_x, center_y), axes, angle = ellipse
    radius = int((axes[0] ** 2 + axes[1] ** 2) ** 0.5 / 2 * 1.05)
    height, width = image.shape[:2]
    left, top = max(0, int(center_x - radius)), max(0, int(center_y - radius))
    right, bottom = min(width, int(center_x + radius)), min(height, int(center_y + radius))
    return image[top:bottom, left:right], ((center_x - left, center_y - top), axes, angle)


def main() -> None:
    args = parse_args()
    if not 0.1 <= args.frequency_hz <= 10 or not 1 <= args.duration_s <= 3600:
        raise SystemExit("Frequency must be 0.1–10 Hz and duration 1–3600 seconds.")

    calibration = load_calibration(args.calibration_file)
    table_diameter_mm = float(calibration["table_diameter_mm"])
    reference_ellipse = saved_ellipse(calibration)
    if reference_ellipse is None:
        raise SystemExit("No usable calibration exists. Calibrate the white disk first.")

    started = datetime.now().astimezone()
    directory = args.output_root / started.strftime("%Y-%m-%d") / f"film_{args.test_id}"
    raw_directory, roi_directory, result_directory = directory / "raw", directory / "roi", directory / "result"
    raw_directory.mkdir(parents=True, exist_ok=True)
    roi_directory.mkdir(parents=True, exist_ok=True)
    result_directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "status.json"
    queue: PriorityQueue[FrameJob] = PriorityQueue()
    records: list[dict[str, Any]] = []
    record_lock = threading.Lock()
    worker_error: list[str] = []
    done = threading.Event()

    def analyse() -> None:
        while not done.is_set() or not queue.empty():
            try:
                job = queue.get(timeout=0.1)
            except Exception:
                continue
            started_analysis = time.perf_counter()
            try:
                raw_path = raw_directory / f"frame_{job.frame_id:04d}.jpg"
                roi_path = roi_directory / f"frame_{job.frame_id:04d}.jpg"
                result_path = result_directory / f"frame_{job.frame_id:04d}.jpg"
                cv2.imwrite(str(raw_path), job.image)
                roi, roi_ellipse = calibrated_roi(job.image, reference_ellipse)
                cv2.imwrite(str(roi_path), roi)
                result = measure(roi, table_diameter_mm, args.dark_threshold, args.table_threshold, roi_ellipse)
                cv2.imwrite(str(result_path), render_result(roi, result))
                record = {
                    **job.metadata,
                    "raw_image": str(raw_path),
                    "roi_image": str(roi_path),
                    "result_image": str(result_path),
                    "analysis_ms": round((time.perf_counter() - started_analysis) * 1000, 1),
                    "status": "completed" if result["contour"] is not None else "no_material_detected",
                    "area_cm2": float(result["area_cm2"]) if result["contour"] is not None else None,
                    "equivalent_diameter_mm": float(result["equivalent_diameter_mm"]) if result["contour"] is not None else None,
                    "diameter_max_mm": float(result["diameter_max_mm"]) if result["contour"] is not None else None,
                    "diameter_min_mm": float(result["diameter_min_mm"]) if result["contour"] is not None else None,
                }
                with record_lock:
                    records.append(record)
                    write_state(state_path, status="recording", test_id=args.test_id, captured_frames=captured[0], analysed_frames=len(records))
            except Exception as error:  # Record worker errors rather than losing the capture sequence.
                worker_error.append(str(error))
            finally:
                queue.task_done()

    captured = [0]
    worker = threading.Thread(target=analyse, name="flowtable-analysis", daemon=True)
    worker.start()
    write_state(state_path, status="recording", test_id=args.test_id, captured_frames=0, analysed_frames=0)
    camera = Picamera2()
    camera.configure(camera.create_still_configuration(main={"size": (3280, 2464)}))
    camera.start()
    try:
        interval = 1 / args.frequency_hz
        started_monotonic = time.monotonic()
        frame_count = max(1, round(args.duration_s * args.frequency_hz))
        for frame_id in range(frame_count):
            deadline = started_monotonic + frame_id * interval
            time.sleep(max(0, deadline - time.monotonic()))
            capture_start = time.perf_counter()
            image = camera.capture_array("main")
            captured[0] += 1
            timestamp = datetime.now(timezone.utc).isoformat()
            queue.put(FrameJob(
                priority=0 if frame_id == frame_count - 1 else 1,
                frame_id=frame_id,
                image=image,
                metadata={
                    "test_id": args.test_id,
                    "frame_id": frame_id,
                    "captured_at_utc": timestamp,
                    "strike_count": args.strike_count,
                    "capture_frequency_hz": args.frequency_hz,
                    "is_final": frame_id == frame_count - 1,
                    "capture_ms": round((time.perf_counter() - capture_start) * 1000, 1),
                },
            ))
    finally:
        camera.stop()
        done.set()
        worker.join()

    ordered = sorted(records, key=lambda item: item["frame_id"])
    preview = create_preview([Path(item["result_image"]) for item in ordered], directory / "review.mp4", args.frequency_hz)
    manifest = {
        "schema_version": 1,
        "test_id": args.test_id,
        "started_at_local": started.isoformat(),
        "capture_frequency_hz": args.frequency_hz,
        "requested_duration_s": args.duration_s,
        "preview_video": preview,
        "frames": ordered,
        "errors": worker_error,
    }
    with (directory / "manifest.json").open("w") as file:
        json.dump(manifest, file, indent=2)
        file.write("\n")
    write_state(state_path, status="completed", test_id=args.test_id, captured_frames=captured[0], analysed_frames=len(ordered), preview_video=preview, errors=worker_error)
    print(directory)


if __name__ == "__main__":
    main()
