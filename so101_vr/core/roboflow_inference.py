"""
Hosted Roboflow object-detection inference for USB camera streams.

Runs asynchronously so the 20 Hz teleop/recording loop is never blocked.
Latest-wins queue + per-camera rate limiting keep hosted API load bounded.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class CameraPrediction:
    """Latest inference result for one camera."""

    predictions: List[dict] = field(default_factory=list)
    inference_ms: float = 0.0
    frame_index: Optional[int] = None
    timestamp: float = 0.0
    model_id: str = ""
    raw_result: Any = None
    error: Optional[str] = None

    def to_dict(self, *, stale: bool = False) -> dict:
        return {
            "predictions": self.predictions,
            "inference_ms": self.inference_ms,
            "frame_index": self.frame_index,
            "timestamp": self.timestamp,
            "model_id": self.model_id,
            "stale": stale,
            "error": self.error,
        }


class RoboflowInferenceService:
    """Async hosted Roboflow OD client with overlay helpers."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        api_key: str = "",
        model_id: str = "",
        api_url: str = "https://serverless.roboflow.com",
        cameras: Optional[List[str]] = None,
        confidence: float = 0.4,
        max_fps_per_camera: float = 4.0,
    ):
        self.enabled = bool(enabled)
        self.api_key = (api_key or "").strip()
        self.model_id = self._normalize_model_id(model_id)
        self.api_url = (api_url or "https://serverless.roboflow.com").rstrip("/")
        self.cameras: Set[str] = set(cameras or [])
        self.confidence = float(confidence)
        self.max_fps_per_camera = max(0.1, float(max_fps_per_camera))
        self._min_interval = 1.0 / self.max_fps_per_camera

        self._client = None
        self._healthy = False
        self._last_error: Optional[str] = None
        self._error_logged = False

        self._pending: Dict[str, tuple] = {}  # cam -> (rgb_frame, frame_idx)
        self._latest: Dict[str, CameraPrediction] = {}
        self._last_submit_time: Dict[str, float] = {}
        self._infer_times: List[float] = []

        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None

        self._box_annotator = None
        self._label_annotator = None
        self._sv = None

    @classmethod
    def from_config(cls, config) -> "RoboflowInferenceService":
        return cls(
            enabled=getattr(config, "roboflow_enabled", False),
            api_key=getattr(config, "roboflow_api_key", ""),
            model_id=getattr(config, "roboflow_model_id", ""),
            api_url=getattr(config, "roboflow_api_url", "https://serverless.roboflow.com"),
            cameras=getattr(config, "roboflow_cameras", None),
            confidence=getattr(config, "roboflow_confidence", 0.4),
            max_fps_per_camera=getattr(config, "roboflow_max_fps_per_camera", 4.0),
        )

    @staticmethod
    def _normalize_model_id(model_id: str) -> str:
        """Normalize to project_id/version (SDK rejects workspace/project/version)."""
        mid = (model_id or "").strip().strip("/")
        if not mid:
            return ""
        parts = [p for p in mid.split("/") if p]
        if len(parts) >= 3:
            # workspace/project/version → project/version
            normalized = f"{parts[-2]}/{parts[-1]}"
            logger.info(
                f"Roboflow model_id normalized: {mid!r} → {normalized!r} "
                "(expected project_id/model_version_id)"
            )
            return normalized
        if len(parts) == 2:
            return f"{parts[0]}/{parts[1]}"
        return mid

    @property
    def is_active(self) -> bool:
        return self.enabled and self._client is not None and bool(self.model_id)

    def start(self) -> bool:
        """Initialize the hosted client and start the background worker."""
        if not self.enabled:
            logger.info("Roboflow inference disabled in config")
            return False

        if self.is_active and self._worker is not None and self._worker.is_alive():
            return True

        if not self.api_key:
            self._last_error = "Missing API key (set ROBOFLOW_API_KEY or roboflow.api_key)"
            logger.warning(f"Roboflow inference not started: {self._last_error}")
            return False

        if not self.model_id:
            self._last_error = "Missing model_id (set roboflow.model_id in config.yaml)"
            logger.warning(f"Roboflow inference not started: {self._last_error}")
            return False

        try:
            from inference_sdk import InferenceHTTPClient, InferenceConfiguration

            self._client = InferenceHTTPClient(
                api_url=self.api_url,
                api_key=self.api_key,
            )
            # Prefer header transport; ignore if this SDK build lacks configure()
            try:
                self._client = self._client.configure(
                    InferenceConfiguration(api_key_transport="header")
                )
            except Exception:
                pass

            try:
                import supervision as sv

                self._sv = sv
                self._box_annotator = sv.BoxAnnotator()
                self._label_annotator = sv.LabelAnnotator()
            except ImportError:
                logger.warning(
                    "supervision not installed — overlays will use OpenCV rectangles"
                )

            self._stop.clear()
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="roboflow-infer",
                daemon=True,
            )
            self._worker.start()
            self._healthy = True
            self._last_error = None
            logger.info(
                f"Roboflow inference started (model={self.model_id}, "
                f"url={self.api_url}, max_fps/cam={self.max_fps_per_camera})"
            )
            return True
        except ImportError:
            self._last_error = "inference-sdk not installed (pip install -e '.[roboflow]')"
            logger.warning(f"Roboflow inference not started: {self._last_error}")
            self._client = None
            return False
        except Exception as e:
            self._last_error = str(e)
            logger.error(f"Roboflow inference failed to start: {e}")
            self._client = None
            return False

    def stop(self) -> None:
        """Stop the worker thread."""
        self._stop.set()
        self._wake.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2.0)
        self._worker = None
        self._client = None
        self._healthy = False
        logger.info("Roboflow inference stopped")

    def set_active_cameras(self, camera_names: List[str]) -> None:
        """Restrict inference to the intersection of configured and connected cams."""
        configured = self.cameras or set(camera_names)
        self.cameras = set(camera_names) & configured if configured else set(camera_names)

    def submit(
        self,
        camera_name: str,
        frame_rgb: np.ndarray,
        frame_index: Optional[int] = None,
    ) -> bool:
        """Queue a frame for inference (non-blocking, latest-wins, rate-limited)."""
        if not self.is_active:
            return False
        if self.cameras and camera_name not in self.cameras:
            return False
        if frame_rgb is None:
            return False

        now = time.monotonic()
        with self._lock:
            last = self._last_submit_time.get(camera_name, 0.0)
            if now - last < self._min_interval:
                return False
            # Only rate-limit on accept; copy for thread safety
            self._pending[camera_name] = (np.asarray(frame_rgb).copy(), frame_index)
            self._last_submit_time[camera_name] = now

        self._wake.set()
        return True

    def get_latest(self, camera_name: str) -> Optional[CameraPrediction]:
        with self._lock:
            pred = self._latest.get(camera_name)
            return pred

    def get_all_latest(self) -> Dict[str, CameraPrediction]:
        with self._lock:
            return dict(self._latest)

    def annotate(
        self,
        frame_rgb: np.ndarray,
        camera_name: Optional[str] = None,
        prediction: Optional[CameraPrediction] = None,
    ) -> np.ndarray:
        """Return an RGB copy of the frame with detection overlays drawn."""
        image = np.asarray(frame_rgb)
        if image.ndim != 3 or image.shape[2] != 3:
            return image.copy() if hasattr(image, "copy") else np.array(image)

        if prediction is None and camera_name is not None:
            prediction = self.get_latest(camera_name)

        if prediction is None or not prediction.predictions:
            return image.copy()

        return self._draw_predictions(image, prediction.predictions)

    def status(self) -> dict:
        with self._lock:
            pending = len(self._pending)
            n = len(self._infer_times)
            fps_estimate = 0.0
            if n >= 2:
                span = self._infer_times[-1] - self._infer_times[0]
                if span > 0:
                    fps_estimate = (n - 1) / span
            elif n == 1:
                fps_estimate = 0.0

        return {
            "enabled": self.enabled,
            "active": self.is_active,
            "healthy": self._healthy and self.is_active,
            "modelId": self.model_id,
            "lastError": self._last_error,
            "pending": pending,
            "fpsEstimate": round(fps_estimate, 2),
            "cameras": sorted(self.cameras),
        }

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.25)
            self._wake.clear()
            if self._stop.is_set():
                break

            while not self._stop.is_set():
                job = self._pop_pending()
                if job is None:
                    break
                camera_name, frame_rgb, frame_index = job
                self._run_infer(camera_name, frame_rgb, frame_index)

    def _pop_pending(self) -> Optional[tuple]:
        with self._lock:
            if not self._pending:
                return None
            # Prefer the oldest pending submission by rotating keys
            camera_name = next(iter(self._pending))
            frame_rgb, frame_index = self._pending.pop(camera_name)
            return camera_name, frame_rgb, frame_index

    def _run_infer(
        self,
        camera_name: str,
        frame_rgb: np.ndarray,
        frame_index: Optional[int],
    ) -> None:
        import cv2

        t0 = time.perf_counter()
        try:
            # Hosted API / OpenCV convention: BGR
            bgr = cv2.cvtColor(np.asarray(frame_rgb), cv2.COLOR_RGB2BGR)
            result = self._client.infer(bgr, model_id=self.model_id)
            predictions = self._normalize_predictions(result)
            # Confidence filter
            predictions = [
                p
                for p in predictions
                if float(p.get("confidence", 0.0)) >= self.confidence
            ]
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            pred = CameraPrediction(
                predictions=predictions,
                inference_ms=round(elapsed_ms, 1),
                frame_index=frame_index,
                timestamp=time.time(),
                model_id=self.model_id,
                raw_result=result,
            )
            with self._lock:
                self._latest[camera_name] = pred
                self._infer_times.append(time.monotonic())
                if len(self._infer_times) > 60:
                    self._infer_times = self._infer_times[-60:]
            self._healthy = True
            self._last_error = None
            self._error_logged = False
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self._healthy = False
            self._last_error = str(e)
            if not self._error_logged:
                logger.error(f"Roboflow infer failed ({camera_name}): {e}")
                self._error_logged = True
            with self._lock:
                prev = self._latest.get(camera_name)
                self._latest[camera_name] = CameraPrediction(
                    predictions=list(prev.predictions) if prev else [],
                    inference_ms=round(elapsed_ms, 1),
                    frame_index=frame_index,
                    timestamp=time.time(),
                    model_id=self.model_id,
                    error=str(e),
                )

    @staticmethod
    def _normalize_predictions(result: Any) -> List[dict]:
        """Extract a flat list of prediction dicts from SDK responses."""
        if result is None:
            return []
        if isinstance(result, list):
            # Some SDK versions return a list of image results
            if result and isinstance(result[0], dict) and "predictions" in result[0]:
                out: List[dict] = []
                for item in result:
                    out.extend(RoboflowInferenceService._normalize_predictions(item))
                return out
            return [p for p in result if isinstance(p, dict)]
        if isinstance(result, dict):
            preds = result.get("predictions", result.get("predictions_list", []))
            if isinstance(preds, list):
                return [p for p in preds if isinstance(p, dict)]
        return []

    def _draw_predictions(self, frame_rgb: np.ndarray, predictions: List[dict]) -> np.ndarray:
        """Draw boxes/labels onto an RGB frame copy."""
        import cv2

        annotated = frame_rgb.copy()

        if self._sv is not None and self._box_annotator is not None:
            try:
                # supervision expects BGR for cv2-style drawing; convert round-trip
                bgr = cv2.cvtColor(annotated, cv2.COLOR_RGB2BGR)
                result = {"predictions": predictions}
                detections = self._sv.Detections.from_inference(result)
                labels = [
                    f"{p.get('class', '?')} {float(p.get('confidence', 0)):.2f}"
                    for p in predictions
                ]
                bgr = self._box_annotator.annotate(bgr, detections)
                if self._label_annotator is not None and labels:
                    bgr = self._label_annotator.annotate(bgr, detections, labels=labels)
                return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            except Exception as e:
                logger.debug(f"supervision annotate failed, falling back to OpenCV: {e}")

        # OpenCV fallback (Roboflow boxes are center-x/y + width/height in pixels)
        for p in predictions:
            try:
                cx = float(p["x"])
                cy = float(p["y"])
                w = float(p["width"])
                h = float(p["height"])
                x1 = int(cx - w / 2)
                y1 = int(cy - h / 2)
                x2 = int(cx + w / 2)
                y2 = int(cy + h / 2)
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
                label = f"{p.get('class', '?')} {float(p.get('confidence', 0)):.2f}"
                cv2.putText(
                    annotated,
                    label,
                    (x1, max(0, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (0, 255, 0),
                    1,
                    cv2.LINE_AA,
                )
            except Exception:
                continue
        return annotated
