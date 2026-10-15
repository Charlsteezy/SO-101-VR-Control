"""
Episode recorder for LeRobot dataset collection during teleoperation.
Captures joint observations/actions and USB camera frames at the control rate.
Optionally runs hosted Roboflow OD and stores overlay videos + predictions JSONL.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import threading
from pathlib import Path
from typing import Dict, List, Optional, TextIO

import numpy as np

from ..config import TelegripConfig, JOINT_NAMES, save_config
from .roboflow_inference import RoboflowInferenceService

logger = logging.getLogger(__name__)

JOINT_POS_KEYS = [f"{name}.pos" for name in JOINT_NAMES]
DEFAULT_CAMERA_NAMES = ["front", "wrist", "top"]


class EpisodeRecorder:
    """Records teleop episodes to a LeRobot dataset with USB camera frames."""

    def __init__(self, config: TelegripConfig):
        self.config = config
        self.dataset = None
        self.cameras: Dict[str, object] = {}
        self.is_recording = False
        self.frame_count = 0
        self._cameras_connected = False
        self._camera_lock = threading.Lock()
        self.last_discovered: List[dict] = []

        self.roboflow = RoboflowInferenceService.from_config(config)
        self._predictions_file: Optional[TextIO] = None
        self._predictions_path: Optional[Path] = None
        self._recording_episode_index: int = 0

    @property
    def cameras_connected(self) -> bool:
        return self._cameras_connected and len(self.cameras) > 0

    @property
    def episode_index(self) -> int:
        if self.dataset is None:
            return 0
        return int(getattr(self.dataset, "num_episodes", 0))

    @property
    def _save_overlays(self) -> bool:
        return bool(self.config.roboflow_enabled and self.config.roboflow_save_overlays)

    @property
    def _save_predictions(self) -> bool:
        return bool(self.config.roboflow_enabled and self.config.roboflow_save_predictions)

    def _dataset_expects_overlays(self) -> bool:
        """True when the open dataset schema includes *_overlay image features."""
        if self.dataset is None:
            return self._save_overlays
        features = getattr(self.dataset, "features", None) or {}
        for name in self.cameras:
            if f"observation.images.{name}_overlay" in features:
                return True
        return False

    @property
    def status(self) -> dict:
        return {
            "recording": self.is_recording,
            "episodeIndex": self.episode_index,
            "frameCount": self.frame_count,
            "cameraConnected": self.cameras_connected,
            "recordingEnabled": True,
            "connectedCameras": {
                name: str(getattr(cam, "index_or_path", name))
                for name, cam in self.cameras.items()
            },
            "roboflow": self.roboflow.status(),
        }

    def setup(self) -> bool:
        """Connect cameras and create/resume the LeRobot dataset.

        Camera failures are non-fatal — use search/apply later to recover.
        """
        try:
            self._connect_cameras()
        except Exception as e:
            logger.error(f"Camera connect failed during setup: {e}")
            self.release_cameras()
            return False

        self._start_roboflow()

        try:
            if self.dataset is None:
                self._create_or_load_dataset()
            return self.dataset is not None and self.cameras_connected
        except Exception as e:
            logger.error(f"Dataset setup failed: {e}")
            return False

    def _start_roboflow(self) -> None:
        """Start hosted inference for the currently connected cameras."""
        if not self.config.roboflow_enabled:
            return
        self.roboflow.set_active_cameras(list(self.cameras.keys()))
        self.roboflow.start()

    @staticmethod
    def _sanitize_camera_info(cam_info: dict) -> dict:
        """Make OpenCV find_cameras() output JSON-safe."""
        profile = cam_info.get("default_stream_profile") or {}

        def _num(value, default=0):
            try:
                number = float(value)
                if math.isnan(number) or math.isinf(number):
                    return default
                if number.is_integer():
                    return int(number)
                return number
            except (TypeError, ValueError):
                return default

        cam_id = cam_info.get("id")
        if isinstance(cam_id, Path):
            cam_id = str(cam_id)

        return {
            "name": cam_info.get("name", f"OpenCV Camera @ {cam_id}"),
            "type": cam_info.get("type", "OpenCV"),
            "id": cam_id,
            "backend_api": cam_info.get("backend_api"),
            "default_stream_profile": {
                "width": _num(profile.get("width"), 640),
                "height": _num(profile.get("height"), 480),
                "fps": _num(profile.get("fps"), 30),
                "fourcc": profile.get("fourcc"),
            },
        }

    @staticmethod
    def _can_capture_frame(device_id) -> bool:
        """Return True if the V4L/OpenCV device can deliver at least one frame.

        On Linux, metadata-only nodes (e.g. /dev/video1 next to /dev/video0) often
        open successfully but never produce frames.
        """
        import cv2

        cap = cv2.VideoCapture(device_id)
        try:
            if not cap.isOpened():
                return False
            ok, frame = cap.read()
            return bool(ok and frame is not None)
        except Exception:
            return False
        finally:
            cap.release()

    def discover_cameras(self) -> List[dict]:
        """Release held devices, then scan for OpenCV USB cameras."""
        if self.is_recording:
            raise RuntimeError("Stop recording before searching for cameras")

        with self._camera_lock:
            # Devices must be free or OpenCV often reports nothing / wrong nodes
            self.release_cameras()
            from lerobot.cameras.opencv import OpenCVCamera

            logger.info("🔍 Searching for OpenCV cameras...")
            raw = OpenCVCamera.find_cameras()
            discovered = []
            for info in raw:
                sanitized = self._sanitize_camera_info(info)
                device_id = sanitized["id"]
                if not self._can_capture_frame(device_id):
                    logger.info(f"🔍 Skipping non-capture node: {device_id}")
                    continue
                discovered.append(sanitized)

            self.last_discovered = discovered
            logger.info(f"🔍 Found {len(discovered)} usable OpenCV camera(s)")
            return discovered

    @staticmethod
    def _assignment_from_device(name: str, device: dict) -> dict:
        """Build a camera config entry + assignment record from a discovered device."""
        profile = device.get("default_stream_profile") or {}
        width = int(device.get("width") or profile.get("width") or 640)
        height = int(device.get("height") or profile.get("height") or 480)
        fps_val = device.get("fps") if device.get("fps") is not None else profile.get("fps") or 30
        try:
            fps = int(fps_val) if float(fps_val) > 0 else 30
        except (TypeError, ValueError):
            fps = 30

        cam_id = device.get("id", device.get("index_or_path"))
        return {
            "name": name,
            "id": cam_id,
            "width": width,
            "height": height,
            "fps": fps,
            "index_or_path": cam_id,
        }

    def apply_discovered_cameras(self, devices: Optional[List[dict]] = None) -> dict:
        """Map devices onto front/wrist/top in order (legacy all-selected path)."""
        devices = devices if devices is not None else self.last_discovered
        if not devices:
            raise RuntimeError("No cameras to apply — run search first")

        names = list(DEFAULT_CAMERA_NAMES)
        if len(devices) < len(names):
            names = names[: len(devices)]
        elif len(devices) > len(names):
            logger.info(
                f"Found {len(devices)} cameras; using the first {len(names)} for {names}"
            )

        assignments = [
            self._assignment_from_device(name, device)
            for name, device in zip(names, devices)
        ]
        return self.apply_camera_assignments(assignments, merge=False)

    def apply_camera_assignments(
        self,
        assignments: List[dict],
        *,
        merge: bool = False,
    ) -> dict:
        """Connect specific slot→device assignments.

        Args:
            assignments: [{"name": "front", "id": "/dev/video0", ...}, ...]
            merge: If True, only replace the named slots and keep other connected
                cameras. If False, release everything first and connect only these.
        """
        if self.is_recording:
            raise RuntimeError("Stop recording before reconnecting cameras")
        if not assignments:
            raise RuntimeError("No camera assignments provided")

        slot_names = set(DEFAULT_CAMERA_NAMES)
        built: Dict[str, dict] = {}
        result_assignments = []
        seen_names = set()
        seen_ids = set()

        for raw in assignments:
            name = str(raw.get("name") or "").strip()
            if not name:
                raise RuntimeError("Assignment missing camera name/slot")
            if name not in slot_names:
                raise RuntimeError(
                    f"Unknown camera slot '{name}'. Expected one of: {', '.join(DEFAULT_CAMERA_NAMES)}"
                )
            if name in seen_names:
                raise RuntimeError(f"Duplicate slot assignment: '{name}'")

            entry = self._assignment_from_device(name, raw)
            cam_id = entry["id"]
            if cam_id is None or cam_id == "":
                raise RuntimeError(f"Assignment for '{name}' is missing device id")
            id_key = str(cam_id)
            if id_key in seen_ids:
                raise RuntimeError(f"Device {cam_id} assigned to multiple slots")

            seen_names.add(name)
            seen_ids.add(id_key)
            built[name] = {
                "index_or_path": cam_id,
                "width": entry["width"],
                "height": entry["height"],
                "fps": entry["fps"],
            }
            result_assignments.append({
                "name": name,
                "id": cam_id,
                "width": entry["width"],
                "height": entry["height"],
                "fps": entry["fps"],
            })

        with self._camera_lock:
            if not merge:
                self.release_cameras()
                self.config.cameras = dict(built)
            else:
                # Drop any live camera that owns a device we're about to claim
                claimed = {str(cfg["index_or_path"]) for cfg in built.values()}
                for live_name, live_cam in list(self.cameras.items()):
                    live_id = str(getattr(live_cam, "index_or_path", ""))
                    if live_name in built or live_id in claimed:
                        self._disconnect_one(live_name)

                updated = dict(self.config.cameras or {})
                updated.update(built)
                self.config.cameras = updated

            self._persist_camera_config(self.config.cameras or {})

            errors = []
            connected_now = []
            for name, cam_cfg in built.items():
                try:
                    self.cameras[name] = self._connect_one_camera(name, cam_cfg)
                    connected_now.append(name)
                except Exception as e:
                    errors.append(f"{name}: {e}")
                    logger.error(str(e))

            self._cameras_connected = len(self.cameras) > 0
            if not self.cameras and errors:
                raise RuntimeError(
                    "No cameras connected. " + (" | ".join(errors))
                )
            if errors:
                logger.warning(
                    f"Connected {len(connected_now)}/{len(built)} selected camera(s); some failed"
                )

            self._start_roboflow()
            if self.dataset is None:
                try:
                    self._create_or_load_dataset()
                except Exception as e:
                    logger.warning(
                        f"Dataset not ready after camera apply ({e}). "
                        "Preview/inference will work; recording may fail until fixed."
                    )

        return {
            "assignments": result_assignments,
            "connected": connected_now,
            "cameraConnected": self.cameras_connected,
            "connectedCameras": {
                name: str(getattr(cam, "index_or_path", name))
                for name, cam in self.cameras.items()
            },
        }

    def disconnect_cameras(self, names: List[str]) -> dict:
        """Disconnect one or more named camera slots."""
        if self.is_recording:
            raise RuntimeError("Stop recording before disconnecting cameras")
        if not names:
            raise RuntimeError("No camera names provided")

        disconnected = []
        with self._camera_lock:
            for name in names:
                name = str(name).strip()
                if not name:
                    continue
                if name in self.cameras:
                    self._disconnect_one(name)
                    disconnected.append(name)
                if self.config.cameras and name in self.config.cameras:
                    del self.config.cameras[name]

            self._cameras_connected = len(self.cameras) > 0
            self._persist_camera_config(self.config.cameras or {})
            self._start_roboflow()

        logger.info(
            f"📷 Disconnected camera(s): {', '.join(disconnected) if disconnected else '(none live)'}"
        )
        return {
            "disconnected": disconnected,
            "cameraConnected": self.cameras_connected,
            "connectedCameras": {
                name: str(getattr(cam, "index_or_path", name))
                for name, cam in self.cameras.items()
            },
        }

    def _disconnect_one(self, name: str) -> None:
        """Disconnect a single live camera by slot name."""
        camera = self.cameras.pop(name, None)
        if camera is None:
            return
        try:
            camera.disconnect()
            logger.info(f"📷 Camera '{name}' disconnected")
        except Exception as e:
            logger.warning(f"Error disconnecting camera '{name}': {e}")

    def _persist_camera_config(self, cameras: Dict[str, dict]) -> None:
        """Deep-merge camera settings into config.yaml without wiping other keys."""
        from .. import config as config_module

        try:
            updated = copy.deepcopy(config_module._config_data)
            updated["cameras"] = cameras
            config_module._config_data = updated
            save_config(updated)
            logger.info("💾 Saved camera config to config.yaml")
        except Exception as e:
            logger.warning(f"Could not persist camera config: {e}")

    def _parse_index_or_path(self, value):
        """Accept int indices or /dev/video* paths from config."""
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
            return Path(text)
        return value

    def _connect_one_camera(self, name: str, cam_cfg: dict):
        """Connect a single camera, falling back to device defaults if needed."""
        from lerobot.cameras.opencv import OpenCVCamera, OpenCVCameraConfig

        index_or_path = self._parse_index_or_path(cam_cfg.get("index_or_path", 0))
        width = cam_cfg.get("width")
        height = cam_cfg.get("height")
        fps = cam_cfg.get("fps")

        configs = []
        if width and height and fps:
            configs.append(
                OpenCVCameraConfig(
                    index_or_path=index_or_path,
                    fps=int(fps),
                    width=int(width),
                    height=int(height),
                )
            )
        configs.append(OpenCVCameraConfig(index_or_path=index_or_path))

        last_error = None
        for oc_cfg in configs:
            try:
                camera = OpenCVCamera(oc_cfg)
                camera.connect()
                logger.info(
                    f"📷 Camera '{name}' connected (index_or_path={index_or_path}, "
                    f"{getattr(camera, 'width', '?')}x{getattr(camera, 'height', '?')}"
                    f"@{getattr(camera, 'fps', '?')})"
                )
                return camera
            except Exception as e:
                last_error = e
                logger.warning(f"Camera '{name}' connect attempt failed: {e}")

        raise RuntimeError(f"Failed to connect camera '{name}' at {index_or_path}: {last_error}")
    def _connect_cameras(self) -> None:
        camera_cfgs = self.config.cameras or {}
        if not camera_cfgs:
            raise RuntimeError("No cameras configured in config.yaml")

        connected = {}
        errors = []
        for name, cam_cfg in camera_cfgs.items():
            try:
                connected[name] = self._connect_one_camera(name, cam_cfg)
            except Exception as e:
                errors.append(str(e))
                logger.error(str(e))

        if not connected:
            raise RuntimeError(
                "No cameras connected. Use Search Cameras in the UI. "
                + (" | ".join(errors) if errors else "")
            )

        if errors:
            logger.warning(f"Connected {len(connected)}/{len(camera_cfgs)} cameras; some failed")

        self.cameras = connected
        self._cameras_connected = True

    def release_cameras(self) -> None:
        """Disconnect cameras only (keep dataset open for later)."""
        for name, camera in list(self.cameras.items()):
            try:
                camera.disconnect()
                logger.info(f"📷 Camera '{name}' released")
            except Exception as e:
                logger.warning(f"Error releasing camera '{name}': {e}")
        self.cameras.clear()
        self._cameras_connected = False

    def get_preview_jpeg(self, name: str, max_width: int = 480, quality: int = 70) -> bytes:
        """Capture one JPEG frame from a connected camera for UI preview."""
        import cv2

        if name not in self.cameras:
            raise KeyError(f"Camera '{name}' is not connected")

        camera = self.cameras[name]
        try:
            frame = camera.async_read(timeout_ms=200)
        except Exception:
            frame = camera.read()

        if frame is None:
            raise RuntimeError(f"No frame from camera '{name}'")

        # LeRobot OpenCV cameras default to RGB
        image = np.asarray(frame)

        # Ensure hosted inference is running for live preview (even before recording)
        if self.config.roboflow_enabled and not self.roboflow.is_active:
            self._start_roboflow()

        # Kick async infer + draw cached detections onto the preview
        if self.roboflow.is_active:
            self.roboflow.submit(name, image)
            if self.config.roboflow_preview_overlay:
                image = self.roboflow.annotate(image, camera_name=name)

        # OpenCV encode expects BGR
        if image.ndim == 3 and image.shape[2] == 3:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)

        height, width = image.shape[:2]
        if width > max_width:
            scale = max_width / float(width)
            image = cv2.resize(
                image,
                (max_width, max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )

        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            raise RuntimeError(f"Failed to encode JPEG for camera '{name}'")
        return encoded.tobytes()

    def _build_features(self) -> dict:
        from lerobot.datasets.utils import hw_to_dataset_features

        # Prefer actual camera shapes after connect; fall back to config.
        image_features = {}
        for name, camera in self.cameras.items():
            height = getattr(camera, "height", None) or (self.config.cameras[name].get("height", 480))
            width = getattr(camera, "width", None) or (self.config.cameras[name].get("width", 640))
            shape = (int(height), int(width), 3)
            image_features[name] = shape
            if self._save_overlays:
                image_features[f"{name}_overlay"] = shape

        obs_features = {key: float for key in JOINT_POS_KEYS}
        obs_features.update(image_features)
        action_features = {key: float for key in JOINT_POS_KEYS}

        features = {}
        features.update(hw_to_dataset_features(obs_features, "observation", use_video=True))
        features.update(hw_to_dataset_features(action_features, "action", use_video=True))
        return features

    def _dataset_root(self) -> Path:
        """Return the on-disk dataset directory for this repo_id.

        LeRobotDataset.create() requires this path to not exist yet; only the
        parent `recording.root` directory is created ahead of time.
        """
        parent = Path(self.config.get_absolute_recording_root())
        parent.mkdir(parents=True, exist_ok=True)
        return parent / self.config.recording_repo_id.replace("/", "_")

    def _create_or_load_dataset(self) -> None:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        repo_id = self.config.recording_repo_id
        root = self._dataset_root()
        fps = int(self.config.recording_fps)
        features = self._build_features()
        num_cams = max(len(self.cameras), 1)

        meta_path = root / "meta" / "info.json"
        if meta_path.exists():
            logger.info(f"📂 Resuming LeRobot dataset at {root}")
            self.dataset = LeRobotDataset(repo_id, root=root)
            self.dataset.start_image_writer(
                num_processes=0,
                num_threads=4 * num_cams,
            )
            if self._save_overlays and not self._dataset_expects_overlays():
                logger.warning(
                    "Roboflow overlays enabled, but this dataset has no *_overlay "
                    "image features. Overlays will not be written. Use a new "
                    "recording.repo_id (or delete the dataset) to create a schema "
                    "with overlay streams."
                )
        else:
            logger.info(f"📂 Creating LeRobot dataset repo_id={repo_id} root={root}")
            self.dataset = LeRobotDataset.create(
                repo_id=repo_id,
                fps=fps,
                features=features,
                root=root,
                robot_type=self.config.recording_robot_type,
                use_videos=True,
                image_writer_processes=0,
                image_writer_threads=4 * num_cams,
            )

        logger.info(
            f"✅ Episode recorder ready (episodes so far: {self.episode_index}, "
            f"cameras: {list(self.cameras.keys())})"
        )

    def start(self) -> bool:
        """Begin recording a new episode buffer."""
        if self.dataset is None:
            if not self.setup():
                logger.error("Cannot start recording: recorder not set up")
                return False

        if self.is_recording:
            logger.warning("Already recording an episode")
            return False

        if not self.cameras_connected:
            logger.error("Cannot start recording: cameras not connected")
            return False

        try:
            self.dataset.episode_buffer = self.dataset.create_episode_buffer()
            self.is_recording = True
            self.frame_count = 0
            self._recording_episode_index = self.episode_index
            self._open_predictions_file()
            logger.info(f"🔴 Recording started (next episode index {self._recording_episode_index})")
            return True
        except Exception as e:
            logger.error(f"Failed to start recording: {e}")
            self.is_recording = False
            self._close_predictions_file(delete=True)
            return False

    def add_frame(
        self,
        observation_state: np.ndarray,
        action: np.ndarray,
        images: Optional[Dict[str, np.ndarray]] = None,
    ) -> bool:
        """Append one frame to the current episode buffer."""
        if not self.is_recording or self.dataset is None:
            return False

        try:
            if images is None:
                images = self.read_images()

            # Submit to async hosted inference (non-blocking); overlays use latest cache
            if self.roboflow.is_active:
                for name, image in images.items():
                    self.roboflow.submit(name, image, frame_index=self.frame_count)

            frame = {
                "observation.state": np.asarray(observation_state, dtype=np.float32),
                "action": np.asarray(action, dtype=np.float32),
                "task": self.config.recording_task,
            }
            write_overlays = self._dataset_expects_overlays()
            for name, image in images.items():
                frame[f"observation.images.{name}"] = image
                if write_overlays:
                    overlay = self.roboflow.annotate(image, camera_name=name)
                    frame[f"observation.images.{name}_overlay"] = overlay

            self.dataset.add_frame(frame)
            if self._save_predictions:
                self._append_predictions_line(images.keys())
            self.frame_count += 1
            return True
        except Exception as e:
            logger.error(f"Failed to add recording frame: {e}")
            return False

    def _predictions_dir(self) -> Path:
        return self._dataset_root() / "predictions"

    def _open_predictions_file(self) -> None:
        self._close_predictions_file(delete=False)
        if not self._save_predictions:
            return
        try:
            pred_dir = self._predictions_dir()
            pred_dir.mkdir(parents=True, exist_ok=True)
            path = pred_dir / f"predictions_ep{self._recording_episode_index:06d}.jsonl"
            self._predictions_path = path
            self._predictions_file = open(path, "w", encoding="utf-8")
            logger.info(f"📝 Writing predictions to {path}")
        except Exception as e:
            logger.warning(f"Could not open predictions file: {e}")
            self._predictions_file = None
            self._predictions_path = None

    def _close_predictions_file(self, *, delete: bool = False) -> None:
        if self._predictions_file is not None:
            try:
                self._predictions_file.close()
            except Exception:
                pass
            self._predictions_file = None
        if delete and self._predictions_path is not None:
            try:
                if self._predictions_path.exists():
                    self._predictions_path.unlink()
                    logger.info(f"🗑️ Deleted predictions file {self._predictions_path}")
            except Exception as e:
                logger.warning(f"Could not delete predictions file: {e}")
        self._predictions_path = None

    def _append_predictions_line(self, camera_names) -> None:
        if self._predictions_file is None:
            return
        try:
            cameras_payload = {}
            for name in camera_names:
                pred = self.roboflow.get_latest(name)
                if pred is None:
                    cameras_payload[name] = {
                        "predictions": [],
                        "inference_ms": None,
                        "stale": True,
                        "model_id": self.config.roboflow_model_id,
                    }
                else:
                    # stale if this prediction was produced for an earlier frame
                    stale = (
                        pred.frame_index is not None
                        and pred.frame_index != self.frame_count
                    )
                    cameras_payload[name] = pred.to_dict(stale=stale)

            record = {
                "episode_index": self._recording_episode_index,
                "frame_index": self.frame_count,
                "model_id": self.config.roboflow_model_id,
                "cameras": cameras_payload,
            }
            self._predictions_file.write(json.dumps(record) + "\n")
            # Flush periodically so a crash does not lose the whole episode
            if self.frame_count % 20 == 0:
                self._predictions_file.flush()
        except Exception as e:
            logger.warning(f"Failed to append predictions line: {e}")

    def read_images(self) -> Dict[str, np.ndarray]:
        """Grab the latest frame from each connected camera."""
        images = {}
        for name, camera in self.cameras.items():
            try:
                # async_read waits briefly for a fresh frame without blocking the control loop hard
                images[name] = camera.async_read(timeout_ms=200)
            except Exception as e:
                logger.warning(f"Camera '{name}' read failed: {e}")
                # Fall back to last known / sync read once
                try:
                    images[name] = camera.read()
                except Exception as e2:
                    logger.error(f"Camera '{name}' sync read also failed: {e2}")
                    raise
        return images

    def stop(self) -> bool:
        """Save the current episode to disk."""
        if not self.is_recording or self.dataset is None:
            logger.warning("Stop recording ignored: not currently recording")
            return False

        try:
            if self.frame_count == 0:
                logger.warning("No frames recorded; discarding empty episode")
                self.dataset.clear_episode_buffer(delete_images=True)
                self.is_recording = False
                self._close_predictions_file(delete=True)
                return False

            self.dataset.save_episode()
            if self._predictions_file is not None:
                try:
                    self._predictions_file.flush()
                except Exception:
                    pass
            self._close_predictions_file(delete=False)
            logger.info(
                f"💾 Episode saved ({self.frame_count} frames). "
                f"Total episodes: {self.episode_index}"
            )
            self.is_recording = False
            self.frame_count = 0
            return True
        except Exception as e:
            logger.error(f"Failed to save episode: {e}")
            self.is_recording = False
            self._close_predictions_file(delete=False)
            return False

    def discard(self) -> bool:
        """Clear the current episode buffer without saving."""
        if self.dataset is None:
            return False

        try:
            was_recording = self.is_recording
            discarded_frames = self.frame_count
            if self.dataset.episode_buffer is not None:
                self.dataset.clear_episode_buffer(delete_images=True)
            else:
                self.dataset.episode_buffer = self.dataset.create_episode_buffer()
            self.is_recording = False
            self.frame_count = 0
            self._close_predictions_file(delete=True)
            if was_recording or discarded_frames > 0:
                logger.info(f"🗑️ Discarded episode buffer ({discarded_frames} frames)")
            return True
        except Exception as e:
            logger.error(f"Failed to discard episode: {e}")
            self.is_recording = False
            self.frame_count = 0
            self._close_predictions_file(delete=True)
            return False

    def disconnect(self) -> None:
        """Release cameras and finalize the dataset writers."""
        if self.is_recording:
            try:
                self.discard()
            except Exception:
                pass

        try:
            self.roboflow.stop()
        except Exception as e:
            logger.warning(f"Error stopping Roboflow inference: {e}")

        with self._camera_lock:
            self.release_cameras()

        if self.dataset is not None:
            try:
                if hasattr(self.dataset, "stop_image_writer"):
                    self.dataset.stop_image_writer()
                self.dataset.finalize()
                logger.info("📂 Dataset writers finalized")
            except Exception as e:
                logger.warning(f"Error finalizing dataset: {e}")
            self.dataset = None
