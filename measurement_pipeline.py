"""Asynchronous capture/analysis pipeline for FlowTable strike measurements.

The capture thread owns the camera. It crops to the calibrated ROI and puts
only that ROI in an unbounded priority queue. OpenCV and disk storage run in
worker threads, so a slow analysis never delays the next camera capture.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, PriorityQueue
import threading, time
from typing import Any, Callable
import cv2
from picamera2 import Picamera2
from flowtable import measure, render_result

@dataclass(order=True)
class Frame:
    priority: int
    frame_id: int
    roi: Any = field(compare=False)
    ellipse: Any = field(compare=False)
    metadata: dict[str, Any] = field(compare=False)

def crop_roi(image: Any, ellipse: Any) -> tuple[Any, Any]:
    (x, y), axes, angle = ellipse
    radius = int((axes[0] ** 2 + axes[1] ** 2) ** .5 * .525)
    h, w = image.shape[:2]
    left, top = max(0, int(x-radius)), max(0, int(y-radius))
    right, bottom = min(w, int(x+radius)), min(h, int(y+radius))
    return image[top:bottom, left:right].copy(), ((x-left, y-top), axes, angle)

class MeasurementPipeline:
    """Long-lived Pi camera plus analysis workers; no queue size limit means no dropped frames."""
    def __init__(self, *, table_diameter_mm: float, ellipse: Any, output: Path, threshold: int = 115,
                 workers: int = 1, on_complete: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.table_diameter_mm, self.ellipse, self.output, self.threshold = table_diameter_mm, ellipse, output, threshold
        self.on_complete = on_complete
        self.queue: PriorityQueue[Frame] = PriorityQueue()
        self._camera_lock, self._next_id, self._closed = threading.Lock(), 0, False
        self.camera = Picamera2()
        self.camera.configure(self.camera.create_still_configuration(main={"size": (3280, 2464)}))
        self.camera.start()
        self.output.mkdir(parents=True, exist_ok=True)
        self.workers = [threading.Thread(target=self._analyse, daemon=True, name=f"flow-analysis-{i}") for i in range(workers)]
        for worker in self.workers: worker.start()

    def capture(self, *, test_id: str, strike_count: int, time_since_previous_strike_s: float | None = None,
                motor_position: float | None = None, is_final: bool = False) -> dict[str, Any]:
        """Capture and enqueue immediately; it intentionally does not wait for OpenCV."""
        with self._camera_lock:
            started = time.perf_counter()
            image = self.camera.capture_array("main")
            captured_at = datetime.now(timezone.utc).isoformat()
            capture_ms = (time.perf_counter()-started)*1000
        crop_started = time.perf_counter()
        roi, roi_ellipse = crop_roi(image, self.ellipse)
        with self._camera_lock:
            frame_id, self._next_id = self._next_id, self._next_id+1
        metadata = {"timestamp": captured_at, "test_id": test_id, "frame_id": frame_id,
                    "strike_count": strike_count, "time_since_previous_strike_s": time_since_previous_strike_s,
                    "motor_position": motor_position, "is_final": is_final, "capture_ms": round(capture_ms,2),
                    "crop_ms": round((time.perf_counter()-crop_started)*1000,2), "queued_at": time.monotonic()}
        self.queue.put(Frame(0 if is_final else 1, frame_id, roi, roi_ellipse, metadata))
        return metadata

    def _analyse(self) -> None:
        while not self._closed:
            try: frame = self.queue.get(timeout=.2)
            except Empty: continue
            started = time.perf_counter()
            try:
                result = measure(frame.roi, self.table_diameter_mm, self.threshold, 150, frame.ellipse)
                stamp = frame.metadata["timestamp"].replace(":", "-")
                roi_path, annotated_path = self.output/f"{stamp}_{frame.frame_id:04d}_roi.jpg", self.output/f"{stamp}_{frame.frame_id:04d}_result.jpg"
                cv2.imwrite(str(roi_path), frame.roi); cv2.imwrite(str(annotated_path), render_result(frame.roi, result))
                payload = {**frame.metadata, **result["timings_ms"], "queue_wait_ms": round((started-frame.metadata["queued_at"])*1000,2),
                           "total_pipeline_ms": round((time.perf_counter()-started)*1000,2), "roi_image": str(roi_path), "result_image": str(annotated_path),
                           "status": "completed" if result["contour"] is not None else "no_material_detected",
                           "area_cm2": float(result["area_cm2"]) if result["contour"] is not None else None,
                           "equivalent_diameter_mm": float(result["equivalent_diameter_mm"]) if result["contour"] is not None else None,
                           "diameter_max_mm": float(result["diameter_max_mm"]) if result["contour"] is not None else None,
                           "diameter_min_mm": float(result["diameter_min_mm"]) if result["contour"] is not None else None}
                if self.on_complete: self.on_complete(payload)
            finally: self.queue.task_done()

    def close(self) -> None:
        self._closed = True; self.camera.stop()
