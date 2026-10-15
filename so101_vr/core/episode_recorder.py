"""
Episode recorder for LeRobot dataset collection during teleoperation.
Captures joint observations/actions and USB camera frames at the control rate.
"""

from __future__ import annotations

import copy
import logging
import math
import threading
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from ..config import TelegripConfig, JOINT_NAMES, save_config

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

    @property
    def cameras_connected(self) -> bool:
        return self._cameras_connected and len(self.cameras) > 0

    @property
    def episode_index(self) -> int:
        if self.dataset is None:
            return 0
        return int(getattr(self.dataset, "num_episodes", 0))

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

        try:
            if self.dataset is None:
                self._create_or_load_dataset()
            return self.dataset is not None and self.cameras_connected
        except Exception as e:
            logger.error(f"Dataset setup failed: {e}")
            return False

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

    def apply_discovered_cameras(self, devices: Optional[List[dict]] = None) -> dict:
        """Map discovered devices onto front/wrist/top, save config, and connect."""
        if self.is_recording:
            raise RuntimeError("Stop recording before reconnecting cameras")

        devices = devices if devices is not None else self.last_discovered
        if not devices:
            raise RuntimeError("No cameras to apply — run search first")

        names = list((self.config.cameras or {}).keys()) or list(DEFAULT_CAMERA_NAMES)
        if len(devices) < len(names):
            # Use as many named slots as we have devices
            names = names[: len(devices)]
            logger.warning(
                f"Found {len(devices)} camera(s); configuring only: {', '.join(names)}"
            )
        elif len(devices) > len(names):
            logger.info(
                f"Found {len(devices)} cameras; using the first {len(names)} for {names}"
            )

        new_cameras: Dict[str, dict] = {}
        assignments = []
        for name, device in zip(names, devices):
            profile = device.get("default_stream_profile") or {}
            width = int(profile.get("width") or 640)
            height = int(profile.get("height") or 480)
            fps_val = profile.get("fps") or 30
            try:
                fps = int(fps_val) if float(fps_val) > 0 else 30
            except (TypeError, ValueError):
                fps = 30

            cam_id = device.get("id")
            new_cameras[name] = {
                "index_or_path": cam_id,
                "width": width,
                "height": height,
                "fps": fps,
            }
            assignments.append({"name": name, "id": cam_id, "width": width, "height": height, "fps": fps})

        with self._camera_lock:
            self.release_cameras()
            self.config.cameras = new_cameras
            self._persist_camera_config(new_cameras)

            self._connect_cameras()
            if self.dataset is None:
                self._create_or_load_dataset()

        return {
            "assignments": assignments,
            "cameraConnected": self.cameras_connected,
        }

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

        # LeRobot OpenCV cameras default to RGB; OpenCV encode expects BGR
        image = np.asarray(frame)
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
            image_features[name] = (int(height), int(width), 3)

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
            logger.info(f"🔴 Recording started (next episode index {self.episode_index})")
            return True
        except Exception as e:
            logger.error(f"Failed to start recording: {e}")
            self.is_recording = False
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

            frame = {
                "observation.state": np.asarray(observation_state, dtype=np.float32),
                "action": np.asarray(action, dtype=np.float32),
                "task": self.config.recording_task,
            }
            for name, image in images.items():
                frame[f"observation.images.{name}"] = image

            self.dataset.add_frame(frame)
            self.frame_count += 1
            return True
        except Exception as e:
            logger.error(f"Failed to add recording frame: {e}")
            return False

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
                return False

            self.dataset.save_episode()
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
            if was_recording or discarded_frames > 0:
                logger.info(f"🗑️ Discarded episode buffer ({discarded_frames} frames)")
            return True
        except Exception as e:
            logger.error(f"Failed to discard episode: {e}")
            self.is_recording = False
            self.frame_count = 0
            return False

    def disconnect(self) -> None:
        """Release cameras and finalize the dataset writers."""
        if self.is_recording:
            try:
                self.discard()
            except Exception:
                pass

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
