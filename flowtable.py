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
import json
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
    parser.add_argument(
        "--table-diameter-mm",
        type=float,
        help="The largest real diameter of the white reference disk in millimetres.",
    )
    parser.add_argument("--output", type=Path, default=Path("captures"))
    parser.add_argument(
        "--dark-threshold",
        type=int,
        default=115,
        help="Pixels darker than this grayscale value are considered concrete (0-255).",
    )
    parser.add_argument(
        "--table-threshold",
        type=int,
        default=150,
        help="Pixels brighter than this value are used to find the white reference disk.",
    )
    parser.add_argument("--calibration-file", type=Path, default=Path("calibration.json"))
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Save the reference disk's largest diameter for later measurements.",
    )
    parser.add_argument(
        "--warmup-seconds", type=float, default=2.0, help="Camera warm-up time."
    )
    return parser.parse_args()


def load_calibration(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"table_diameter_mm": 297.0}
    try:
        with path.open() as file:
            data = json.load(file)
        return {"table_diameter_mm": float(data["table_diameter_mm"]), **data}
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read calibration file {path}: {error}") from error


def save_calibration(path: Path, table_diameter_mm: float, ellipse: tuple[tuple[float, float], tuple[float, float], float]) -> None:
    center, axes, angle = ellipse
    data = {
        "table_diameter_mm": table_diameter_mm,
        "calibrated_at_utc": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "ellipse_center_px": [round(value, 3) for value in center],
        "ellipse_axes_px": [round(value, 3) for value in axes],
        "ellipse_angle_deg": round(angle, 3),
    }
    with path.open("w") as file:
        json.dump(data, file, indent=2)
        file.write("\n")


def find_table_ellipse(image: np.ndarray, threshold: int) -> tuple[tuple[float, float], tuple[float, float], float]:
    """Find the white reference disk, which may appear elliptical in perspective."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, bright = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    candidates = [contour for contour in contours if len(contour) >= 5 and cv2.contourArea(contour) > 10_000]
    if not candidates:
        raise RuntimeError(
            "Den vita referensskivan syns inte tydligt. Rikta kameran rakt ned och "
            "se till att hela skivan ryms i bilden."
        )
    return cv2.fitEllipse(max(candidates, key=cv2.contourArea))


def table_geometry(ellipse: tuple[tuple[float, float], tuple[float, float], float], diameter_mm: float) -> dict[str, object]:
    """Normalize OpenCV's ellipse representation to major/minor table axes."""
    center, (axis_a, axis_b), angle = ellipse
    if axis_a >= axis_b:
        major_px, minor_px, major_angle = axis_a, axis_b, angle
    else:
        major_px, minor_px, major_angle = axis_b, axis_a, angle + 90
    radians = np.deg2rad(major_angle)
    major_direction = np.array([np.cos(radians), np.sin(radians)])
    minor_direction = np.array([-np.sin(radians), np.cos(radians)])
    return {
        "center": np.array(center),
        "ellipse": ellipse,
        "major_px": major_px,
        "minor_px": minor_px,
        "major_direction": major_direction,
        "minor_direction": minor_direction,
        "major_px_per_mm": major_px / diameter_mm,
        "minor_px_per_mm": minor_px / diameter_mm,
    }


def contour_in_mm(contour: np.ndarray, geometry: dict[str, object]) -> np.ndarray:
    """Convert image points to table-plane millimetres using the reference ellipse."""
    points = contour.reshape(-1, 2).astype(np.float32) - geometry["center"]
    major = points @ geometry["major_direction"] / geometry["major_px_per_mm"]
    minor = points @ geometry["minor_direction"] / geometry["minor_px_per_mm"]
    return np.column_stack((major, minor)).astype(np.float32).reshape(-1, 1, 2)


def measure(image: np.ndarray, table_diameter_mm: float, threshold: int, table_threshold: int) -> dict[str, object]:
    geometry = table_geometry(find_table_ellipse(image, table_threshold), table_diameter_mm)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    table_mask = np.zeros(gray.shape, dtype=np.uint8)
    center, axes, angle = geometry["ellipse"]
    cv2.ellipse(
        table_mask,
        tuple(map(int, center)),
        tuple(int(value * 0.96 / 2) for value in axes),
        angle,
        0,
        360,
        255,
        -1,
    )
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

    physical_contour = contour_in_mm(contour, geometry)
    (_, _), (axis_a, axis_b), angle = cv2.fitEllipse(physical_contour)
    diameter_max_mm = max(axis_a, axis_b)
    diameter_min_mm = min(axis_a, axis_b)
    area_mm2 = cv2.contourArea(physical_contour)
    equivalent_diameter_mm = (4 * area_mm2 / np.pi) ** 0.5
    return {
        "table_ellipse": geometry["ellipse"],
        "pixels_per_mm": (geometry["major_px_per_mm"] * geometry["minor_px_per_mm"]) ** 0.5,
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
    center, axes, angle = result["table_ellipse"]
    cv2.ellipse(
        rendered,
        tuple(map(int, center)),
        tuple(int(value / 2) for value in axes),
        angle,
        0,
        360,
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


def render_calibration(image: np.ndarray, ellipse: tuple[tuple[float, float], tuple[float, float], float], diameter_mm: float) -> np.ndarray:
    rendered = image.copy()
    center, axes, angle = ellipse
    cv2.ellipse(rendered, tuple(map(int, center)), tuple(int(value / 2) for value in axes), angle, 0, 360, (255, 120, 0), 3)
    label = f"Kalibrerad: storsta diameter {diameter_mm:.1f} mm"
    cv2.putText(rendered, label, (30, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 5)
    cv2.putText(rendered, label, (30, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
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
    calibration = load_calibration(args.calibration_file)
    table_diameter_mm = args.table_diameter_mm or float(calibration["table_diameter_mm"])
    if table_diameter_mm <= 0:
        sys.exit("--table-diameter-mm must be positive.")
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
    if args.calibrate:
        try:
            ellipse = find_table_ellipse(image, args.table_threshold)
        except RuntimeError as error:
            sys.exit(f"Saved raw image to {raw_path}; calibration failed: {error}")
        save_calibration(args.calibration_file, table_diameter_mm, ellipse)
        calibration_path = args.output / f"{captured_at}_calibration.jpg"
        cv2.imwrite(str(calibration_path), render_calibration(image, ellipse, table_diameter_mm))
        print(f"Calibration image: {calibration_path}")
        print(f"Saved maximum table diameter: {table_diameter_mm:.1f} mm")
        return
    try:
        result = measure(image, table_diameter_mm, args.dark_threshold, args.table_threshold)
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
