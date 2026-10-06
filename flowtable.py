#!/usr/bin/env python3
"""Capture and measure concrete spread on a circular flow table.

The program is intended for a Raspberry Pi Camera Module mounted above a
known, light-coloured circular table.  It saves the original image, a marked
result image, and one row of measurements in CSV format for every capture.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

try:
    import cv2
    import numpy as np
    from picamera2 import Picamera2
except ImportError as error:
    sys.exit(
        f"Missing Python package: {error.name}. "
        "On Raspberry Pi OS run: sudo apt install python3-opencv python3-picamera2"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table-diameter-mm", type=float, default=300.0)
    parser.add_argument("--output", type=Path, default=Path("captures"))
    parser.add_argument(
        "--dark-threshold",
        type=int,
        default=115,
        help="Pixels darker than this grayscale value are considered concrete (0-255).",
    )
    parser.add_argument(
        "--warmup-seconds", type=float, default=2.0, help="Camera warm-up time."
    )
    return parser.parse_args()


def find_table_circle(image: np.ndarray) -> tuple[int, int, int]:
    """Return the largest visible circle as x, y, radius in pixels."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (9, 9), 2)
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=gray.shape[0] // 2,
        param1=100,
        param2=35,
        minRadius=int(min(gray.shape[:2]) * 0.20),
        maxRadius=int(min(gray.shape[:2]) * 0.49),
    )
    if circles is None:
        raise RuntimeError("Could not find the circular table edge.")
    x, y, radius = max(np.round(circles[0]).astype(int), key=lambda c: c[2])
    return int(x), int(y), int(radius)


def measure(image: np.ndarray, table_diameter_mm: float, threshold: int) -> dict[str, object]:
    center_x, center_y, table_radius_px = find_table_circle(image)
    px_per_mm = (2 * table_radius_px) / table_diameter_mm
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    table_mask = np.zeros(gray.shape, dtype=np.uint8)
    cv2.circle(table_mask, (center_x, center_y), int(table_radius_px * 0.96), 255, -1)
    concrete_mask = cv2.inRange(gray, 0, threshold)
    concrete_mask = cv2.bitwise_and(concrete_mask, table_mask)
    concrete_mask = cv2.morphologyEx(
        concrete_mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
    )
    concrete_mask = cv2.morphologyEx(
        concrete_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)),
    )

    contours, _ = cv2.findContours(concrete_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise RuntimeError("No dark concrete area found; adjust --dark-threshold or lighting.")
    contour = max(contours, key=cv2.contourArea)
    area_px = cv2.contourArea(contour)
    if area_px < 100:
        raise RuntimeError("Detected concrete area is too small.")

    (_, _), (axis_a, axis_b), angle = cv2.fitEllipse(contour)
    diameter_max_mm = max(axis_a, axis_b) / px_per_mm
    diameter_min_mm = min(axis_a, axis_b) / px_per_mm
    area_mm2 = area_px / (px_per_mm**2)
    equivalent_diameter_mm = (4 * area_mm2 / np.pi) ** 0.5
    return {
        "table_center_x_px": center_x,
        "table_center_y_px": center_y,
        "table_radius_px": table_radius_px,
        "pixels_per_mm": px_per_mm,
        "area_mm2": area_mm2,
        "area_cm2": area_mm2 / 100,
        "equivalent_diameter_mm": equivalent_diameter_mm,
        "diameter_max_mm": diameter_max_mm,
        "diameter_min_mm": diameter_min_mm,
        "ellipse_angle_deg": angle,
        "concrete_mask": concrete_mask,
        "contour": contour,
    }


def render_result(image: np.ndarray, result: dict[str, object]) -> np.ndarray:
    rendered = image.copy()
    contour = result["contour"]
    cv2.drawContours(rendered, [contour], -1, (0, 220, 0), 3)
    cv2.circle(
        rendered,
        (int(result["table_center_x_px"]), int(result["table_center_y_px"])),
        int(result["table_radius_px"]),
        (255, 120, 0),
        2,
    )
    lines = [
        f"Area: {result['area_cm2']:.1f} cm2",
        f"Deq: {result['equivalent_diameter_mm']:.1f} mm",
        f"Dmax/Dmin: {result['diameter_max_mm']:.1f} / {result['diameter_min_mm']:.1f} mm",
    ]
    for index, line in enumerate(lines):
        cv2.putText(rendered, line, (30, 45 + index * 36), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (20, 20, 20), 5)
        cv2.putText(rendered, line, (30, 45 + index * 36), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    return rendered


def append_csv(path: Path, captured_at: str, result: dict[str, object]) -> None:
    fields = ["captured_at_utc", "area_cm2", "equivalent_diameter_mm", "diameter_max_mm", "diameter_min_mm", "pixels_per_mm"]
    new_file = not path.exists()
    with path.open("a", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow({"captured_at_utc": captured_at, **{field: result[field] for field in fields[1:]}})


def main() -> None:
    args = parse_args()
    if not 0 <= args.dark_threshold <= 255:
        sys.exit("--dark-threshold must be between 0 and 255.")
    args.output.mkdir(parents=True, exist_ok=True)

    camera = Picamera2()
    camera.configure(camera.create_still_configuration(main={"size": (3280, 2464)}))
    camera.start()
    time.sleep(args.warmup_seconds)
    image = camera.capture_array("main")
    camera.stop()

    captured_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = args.output / f"{captured_at}_raw.jpg"
    cv2.imwrite(str(raw_path), image)
    try:
        result = measure(image, args.table_diameter_mm, args.dark_threshold)
    except RuntimeError as error:
        sys.exit(f"Saved raw image to {raw_path}; measurement failed: {error}")

    result_path = args.output / f"{captured_at}_measured.jpg"
    cv2.imwrite(str(result_path), render_result(image, result))
    append_csv(args.output / "measurements.csv", captured_at, result)
    print(f"Raw image: {raw_path}")
    print(f"Result image: {result_path}")
    print(f"Area: {result['area_cm2']:.1f} cm2")
    print(f"Equivalent diameter: {result['equivalent_diameter_mm']:.1f} mm")
    print(f"Max/min diameter: {result['diameter_max_mm']:.1f} / {result['diameter_min_mm']:.1f} mm")


if __name__ == "__main__":
    main()
