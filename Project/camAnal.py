"""Camera-based verification against consented reference photos.

Recognition is deliberately performed only after an NFC tag resolves to a known
student. This module is not a liveness detector and must not be treated as a
high-assurance biometric security boundary.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # support both package and direct-script imports
    from .config import Settings
except ImportError:  # pragma: no cover
    from config import Settings

logger = logging.getLogger(__name__)
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _face_libraries() -> tuple[Any, Any]:
    try:
        import cv2
        import face_recognition as fr
    except ImportError as exc:
        dependency = "opencv-python" if exc.name == "cv2" else "face-recognition (and dlib)"
        raise RuntimeError(
            f"Missing optional camera dependency: {dependency}. "
            "Install the packages listed in requirements.txt; see README.md for setup notes."
        ) from exc
    return cv2, fr


def _photo_student_name(path: Path) -> str:
    # Supports both "Anuj Sharma.jpg" and "Anuj_Sharma.jpg".
    return path.stem.replace("_", " ").strip()


@dataclass(frozen=True, slots=True)
class KnownFace:
    name: str
    encoding: Any
    roll_no: str | None = None


class FaceVerifier:
    """Loads face encodings once, then uses a short live session per NFC scan."""

    def __init__(
        self,
        photos_dir: str | Path | None = None,
        *,
        tolerance: float | None = None,
        frame_scale: float | None = None,
        required_matches: int | None = None,
        students: list[dict[str, Any]] | None = None,
    ) -> None:
        self.settings = Settings.from_env()
        self.photos_dir = Path(photos_dir or self.settings.photos_dir).expanduser().resolve()
        self.students_by_roll = {
            str(student.get("ROLLNO", "")).strip().casefold(): student
            for student in (students or [])
            if str(student.get("ROLLNO", "")).strip()
        }
        self.tolerance = self.settings.face_tolerance if tolerance is None else tolerance
        self.frame_scale = self.settings.frame_scale if frame_scale is None else frame_scale
        self.required_matches = (
            self.settings.required_face_matches if required_matches is None else required_matches
        )
        if not 0.0 < self.tolerance <= 1.0:
            raise ValueError("Face tolerance must be between 0 and 1")
        if not 0.1 <= self.frame_scale <= 1.0:
            raise ValueError("Frame scale must be between 0.1 and 1.0")
        if self.required_matches < 1:
            raise ValueError("Required match count must be at least 1")
        self.cv2, self.fr = _face_libraries()
        self.known_faces = self._load_known_faces()
        # Keep encodings in a flat list to avoid rebuilding it for every face/frame.
        self.known_encodings = [face.encoding for face in self.known_faces]

    def _load_known_faces(self) -> list[KnownFace]:
        if not self.photos_dir.is_dir():
            raise FileNotFoundError(
                f"Reference photo folder not found: {self.photos_dir}. "
                "Run the enrollment command or create the folder and add consented face photos."
            )

        known: list[KnownFace] = []
        files = sorted(
            p for p in self.photos_dir.rglob("*")
            if p.is_file()
            and p.suffix.lower() in _IMAGE_SUFFIXES
            and not any(part.startswith(".") for part in p.relative_to(self.photos_dir).parts)
        )
        for image_path in files:
            relative = image_path.relative_to(self.photos_dir)
            roll_no: str | None = None
            name: str | None = None
            if len(relative.parts) > 1:
                # New enrollment format: Photos/<ROLLNO>/face_01.jpg. The roll
                # number binds multiple reference images to one unambiguous person.
                roll_no = relative.parts[0]
                student = self.students_by_roll.get(roll_no.casefold())
                if student is None:
                    logger.debug("Skipping photos for unregistered roll number %s", roll_no)
                    continue
                name = str(student.get("NAME", "")).strip()
            else:
                # Backward compatibility for existing flat files named after a student.
                name = _photo_student_name(image_path)

            try:
                image = self.fr.load_image_file(str(image_path))
                encodings = self.fr.face_encodings(image, model="small")
            except Exception as exc:  # corrupt or unsupported image; keep other photos usable
                logger.warning("Skipping unreadable reference photo %s: %s", image_path.name, exc)
                continue
            if len(encodings) != 1:
                logger.warning(
                    "Skipping %s: expected exactly one face, found %d", image_path.name, len(encodings)
                )
                continue
            if not name:
                logger.warning("Skipping photo with an empty student name: %s", image_path.name)
                continue
            known.append(KnownFace(name=name, encoding=encodings[0], roll_no=roll_no))

        if not known:
            raise RuntimeError(
                f"No usable reference faces found in {self.photos_dir}. Enroll a student or add "
                "JPG/PNG photos containing exactly one clearly visible face per image."
            )

        logger.info("Loaded %d reference face image(s) from %s", len(known), self.photos_dir)
        return known

    def verify(
        self,
        expected_name: str,
        *,
        camera_index: int | None = None,
        timeout_seconds: float | None = None,
        display: bool = True,
        expected_roll_no: str | None = None,
    ) -> bool:
        """Verify the expected student over several processed frames, or time out."""
        expected = expected_name.strip().casefold()
        if not expected:
            raise ValueError("Expected student name cannot be empty")
        target_faces: list[KnownFace]
        if expected_roll_no:
            # Prefer roll-number-scoped photos. Fall back to a legacy flat photo
            # only if no enrolled photos exist for this student.
            target_faces = [
                face for face in self.known_faces
                if face.roll_no is not None and face.roll_no.casefold() == expected_roll_no.casefold()
            ]
            if not target_faces:
                target_faces = [
                    face for face in self.known_faces
                    if face.roll_no is None and face.name.casefold() == expected
                ]
        else:
            target_faces = [face for face in self.known_faces if face.name.casefold() == expected]

        if not target_faces:
            logger.error(
                "No reference image matches student %r (%s). Enroll face images or add a legacy photo.",
                expected_name, expected_roll_no or "no roll number supplied",
            )
            return False
        target_encodings = [face.encoding for face in target_faces]

        timeout = self.settings.face_timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("Camera timeout must be positive")
        cam_index = self.settings.camera_index if camera_index is None else camera_index
        cv2 = self.cv2
        cap = cv2.VideoCapture(cam_index)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(
                f"Could not open camera index {cam_index}. Close other camera apps or set CAMERA_INDEX."
            )

        # A modest frame size improves camera throughput and lowers detection latency.
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        deadline = time.monotonic() + timeout
        matching_frames = 0
        frame_count = 0
        detections: list[tuple[tuple[int, int, int, int], str, float | None]] = []
        matched = False
        try:
            while time.monotonic() < deadline:
                ok, frame = cap.read()
                if not ok or frame is None:
                    if display and (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                        break
                    continue

                frame_count += 1
                if frame_count % 2 == 0:
                    small = cv2.resize(
                        frame, (0, 0), fx=self.frame_scale, fy=self.frame_scale,
                        interpolation=cv2.INTER_AREA,
                    )
                    rgb_small = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                    locations = self.fr.face_locations(rgb_small, number_of_times_to_upsample=0, model="hog")
                    encodings = self.fr.face_encodings(rgb_small, locations, model="small")
                    current_detections: list[tuple[tuple[int, int, int, int], str, float | None]] = []
                    expected_seen = False
                    for location, encoding in zip(locations, encodings):
                        distances = self.fr.face_distance(target_encodings, encoding)
                        best_index = int(distances.argmin()) if len(distances) else -1
                        distance = float(distances[best_index]) if best_index >= 0 else None
                        name = "Unknown"
                        if best_index >= 0 and distance is not None and distance <= self.tolerance:
                            name = target_faces[best_index].name
                            if name.casefold() == expected:
                                expected_seen = True
                        current_detections.append((location, name, distance))
                    detections = current_detections
                    matching_frames = matching_frames + 1 if expected_seen else 0
                    if matching_frames >= self.required_matches:
                        matched = True

                if display:
                    scale = 1.0 / self.frame_scale
                    for (top, right, bottom, left), name, distance in detections:
                        top, right, bottom, left = [int(v * scale) for v in (top, right, bottom, left)]
                        color = (0, 160, 0) if name.casefold() == expected else (0, 0, 220)
                        cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
                        label = f"{name}" + (f" ({distance:.2f})" if distance is not None else "")
                        cv2.putText(frame, label, (left, max(22, top - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
                    status = "Identity verified" if matched else f"Verifying {expected_name}... (q/Esc to cancel)"
                    cv2.putText(frame, status, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
                    cv2.imshow("NFC Attendance - Face Verification", frame)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (ord("q"), 27):
                        break
                if matched:
                    break
            return matched
        finally:
            cap.release()
            if display:
                cv2.destroyAllWindows()


def verify_student_face(
    expected_name: str,
    *,
    photos_dir: str | Path | None = None,
    camera_index: int | None = None,
    timeout_seconds: float | None = None,
    expected_roll_no: str | None = None,
    students: list[dict[str, Any]] | None = None,
) -> bool:
    """Convenience wrapper; long-running applications should reuse FaceVerifier."""
    verifier = FaceVerifier(photos_dir=photos_dir, students=students)
    return verifier.verify(
        expected_name,
        camera_index=camera_index,
        timeout_seconds=timeout_seconds,
        expected_roll_no=expected_roll_no,
    )
