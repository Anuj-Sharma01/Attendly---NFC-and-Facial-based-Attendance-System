"""Central configuration for the NFC + face-recognition attendance app."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path = PROJECT_DIR / "main.db"
    photos_dir: Path = PROJECT_DIR / "Photos"
    serial_port: str = "COM8"
    baud_rate: int = 9600
    camera_index: int = 0
    face_timeout_seconds: float = 15.0
    face_tolerance: float = 0.48
    frame_scale: float = 0.25
    required_face_matches: int = 3
    rfid_cooldown_seconds: float = 3.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(os.getenv("ATTENDANCE_DB", str(PROJECT_DIR / "main.db"))).expanduser(),
            photos_dir=Path(os.getenv("ATTENDANCE_PHOTOS_DIR", str(PROJECT_DIR / "Photos"))).expanduser(),
            serial_port=os.getenv("NFC_SERIAL_PORT", "COM8").strip(),
            baud_rate=_env_int("NFC_BAUD_RATE", 9600),
            camera_index=_env_int("CAMERA_INDEX", 0),
            face_timeout_seconds=_env_float("FACE_TIMEOUT_SECONDS", 15.0),
            face_tolerance=_env_float("FACE_TOLERANCE", 0.48),
            frame_scale=_env_float("FACE_FRAME_SCALE", 0.25),
            required_face_matches=_env_int("REQUIRED_FACE_MATCHES", 3),
            rfid_cooldown_seconds=_env_float("RFID_COOLDOWN_SECONDS", 3.0),
        )

    def validate(self) -> None:
        if not self.serial_port:
            raise ValueError("NFC_SERIAL_PORT cannot be empty")
        if self.baud_rate <= 0:
            raise ValueError("NFC_BAUD_RATE must be positive")
        if self.camera_index < 0:
            raise ValueError("CAMERA_INDEX cannot be negative")
        if self.face_timeout_seconds <= 0:
            raise ValueError("FACE_TIMEOUT_SECONDS must be positive")
        if not 0.0 < self.face_tolerance <= 1.0:
            raise ValueError("FACE_TOLERANCE must be between 0 and 1")
        if not 0.1 <= self.frame_scale <= 1.0:
            raise ValueError("FACE_FRAME_SCALE must be between 0.1 and 1.0")
        if self.required_face_matches < 1:
            raise ValueError("REQUIRED_FACE_MATCHES must be at least 1")
        if self.rfid_cooldown_seconds < 0:
            raise ValueError("RFID_COOLDOWN_SECONDS cannot be negative")
