"""Guided enrollment: student details, NFC tag scan and face photo capture."""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

try:
    from .camAnal import _face_libraries
    from .dbAccess import Database, normalize_uid
except ImportError:  # pragma: no cover - support direct-script imports
    from camAnal import _face_libraries
    from dbAccess import Database, normalize_uid

logger = logging.getLogger(__name__)
_SAFE_ROLL_NO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$")
_UID_LABEL = re.compile(r"\bUID\b\s*[:=]\s*(.*)$", re.IGNORECASE)


def parse_reader_line(line: str | bytes) -> str:
    """Parse a raw UID or common ``UID: ...`` serial-reader output line."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    candidate = str(line).strip()
    if not candidate:
        return ""
    label_match = _UID_LABEL.search(candidate)
    if label_match:
        candidate = label_match.group(1).strip()
    return normalize_uid(candidate)


def read_nfc_uid(
    port: str,
    baud_rate: int = 9600,
    timeout_seconds: float = 60.0,
    *,
    serial_factory: Callable[..., Any] | None = None,
    status: Callable[[str], None] = print,
) -> str:
    """Wait for one tag, returning the first valid normalized UID or timing out."""
    if not port.strip():
        raise ValueError("Serial port cannot be empty")
    if baud_rate <= 0:
        raise ValueError("Baud rate must be positive")
    if timeout_seconds <= 0:
        raise ValueError("NFC scan timeout must be positive")

    if serial_factory is None:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError(
                "pyserial is not installed. Run 'python -m pip install -r requirements.txt'."
            ) from exc
        serial_factory = serial.Serial

    status(f"Opening NFC reader on {port} at {baud_rate} baud...")
    status("Present one unused NFC card/tag now. Press Ctrl+C to cancel.")
    deadline = time.monotonic() + timeout_seconds
    try:
        with serial_factory(port, baud_rate, timeout=1) as reader:
            while time.monotonic() < deadline:
                raw = reader.readline()
                if not raw:
                    continue
                try:
                    uid = parse_reader_line(raw)
                except ValueError:
                    logger.debug("Ignoring non-UID serial line: %r", raw)
                    continue
                if uid:
                    status(f"Read NFC UID: {uid}")
                    return uid
    except OSError as exc:
        raise RuntimeError(
            f"Could not open/read NFC serial port {port!r}: {exc}. "
            "Check the COM port and close Arduino Serial Monitor or any other app using it."
        ) from exc
    raise TimeoutError(
        f"No valid NFC UID was received within {timeout_seconds:g} seconds. "
        "Check the reader firmware, serial port, baud rate, and newline output."
    )


def capture_face_images(
    output_dir: str | Path,
    *,
    image_count: int = 3,
    camera_index: int = 0,
) -> list[Path]:
    """Capture a requested number of images, saving only frames with exactly one face.

    In the camera window, press Space to capture a valid frame; press q/Esc to stop.
    At least one image is required. The returned image paths are inside output_dir.
    """
    if image_count < 1:
        raise ValueError("Image count must be at least 1")
    if camera_index < 0:
        raise ValueError("Camera index cannot be negative")

    cv2, fr = _face_libraries()
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(
            f"Could not open camera index {camera_index}. Close other camera apps or change --camera-index."
        )

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    saved: list[Path] = []
    face_count = 0
    try:
        while len(saved) < image_count:
            ok, frame = cap.read()
            if not ok or frame is None:
                continue

            # Detect on a half-size frame for responsiveness; scale locations for preview.
            small = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
            rgb_small = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            locations = fr.face_locations(rgb_small, number_of_times_to_upsample=0, model="hog")
            face_count = len(locations)
            preview = frame.copy()
            for top, right, bottom, left in locations:
                top, right, bottom, left = (int(v * 2) for v in (top, right, bottom, left))
                cv2.rectangle(preview, (left, top), (right, bottom), (40, 180, 40), 2)

            prompt = (
                f"Faces detected: {face_count} | Captured: {len(saved)}/{image_count} | "
                "SPACE = capture, Q/ESC = finish"
            )
            cv2.putText(
                preview, prompt, (12, 28), cv2.FONT_HERSHEY_SIMPLEX,
                0.55, (255, 255, 255), 2,
            )
            if face_count != 1:
                hint = "Keep exactly one face visible" if face_count != 1 else ""
                cv2.putText(preview, hint, (12, 54), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 230), 2)
            cv2.imshow("Student Enrollment - Face Capture", preview)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == 32:  # Space
                if face_count != 1:
                    print("Capture skipped: the frame must contain exactly one visible face.")
                    continue
                path = destination / f"face_{len(saved) + 1:02d}.jpg"
                if not cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92]):
                    raise OSError(f"Could not save captured photo to {path}")
                saved.append(path)
                print(f"Captured face image {len(saved)}/{image_count}: {path.name}")
        if not saved:
            raise RuntimeError("No face images were captured. Enrollment was cancelled.")
        if len(saved) < image_count:
            print(f"Captured {len(saved)} image(s); requested {image_count}. Continuing with captured images.")
        return saved
    finally:
        cap.release()
        cv2.destroyAllWindows()


def enroll_interactive(
    database: Database,
    *,
    photos_dir: str | Path,
    serial_port: str,
    baud_rate: int = 9600,
    camera_index: int = 0,
    image_count: int = 3,
    nfc_timeout: float = 60,
    serial_factory: Callable[..., Any] | None = None,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run the full guided workflow and commit student, tag and face images."""
    if image_count < 1:
        raise ValueError("Image count must be at least 1")
    if camera_index < 0:
        raise ValueError("Camera index cannot be negative")
    if nfc_timeout <= 0:
        raise ValueError("NFC scan timeout must be positive")
    output("\n=== Student enrollment ===")
    roll_no = input_fn("Roll number / student ID: ").strip()
    name = input_fn("Full name: ").strip()
    department = input_fn("Department (optional): ").strip()
    year = input_fn("Year (e.g. 2): ").strip()
    class_name = input_fn("Class / section (e.g. IT-B): ").strip()
    if not roll_no or not name:
        raise ValueError("Roll number and full name are required")
    if not _SAFE_ROLL_NO.fullmatch(roll_no):
        raise ValueError(
            "Roll number must start with a letter/digit and contain only letters, digits, dot, underscore or hyphen (max 50 chars)."
        )
    if any(str(student["ROLLNO"]).casefold() == roll_no.casefold() for student in database.list_students()):
        raise ValueError(f"Roll number {roll_no!r} is already registered")

    uid = read_nfc_uid(
        serial_port, baud_rate, nfc_timeout,
        serial_factory=serial_factory, status=output,
    )
    normalized_existing = {
        normalize_uid(str(tag.get("TAGID", "")))
        for tag in database.list_tags()
        if tag.get("TAGID")
    }
    if uid in normalized_existing:
        raise ValueError(f"NFC tag {uid!r} is already assigned. Use an unassigned tag.")

    photo_root = Path(photos_dir).expanduser().resolve()
    photo_root.mkdir(parents=True, exist_ok=True)
    final_dir = photo_root / roll_no
    if final_dir.exists():
        raise ValueError(
            f"A face-photo folder already exists for {roll_no!r}: {final_dir}. "
            "Review or archive it before enrolling this roll number."
        )

    staging_dir = Path(tempfile.mkdtemp(prefix=".enroll-", dir=photo_root))
    moved_to_final = False
    try:
        output(f"\nNext, capture up to {image_count} face images for {name}.")
        images = capture_face_images(
            staging_dir, image_count=image_count, camera_index=camera_index,
        )
        confirmation = input_fn(
            f"\nRegister {name} ({roll_no}), NFC UID {uid}, with {len(images)} image(s)? [Y/n]: "
        ).strip().casefold()
        if confirmation not in ("", "y", "yes"):
            raise RuntimeError("Enrollment cancelled; no student or tag record was created")

        # Rename within the same volume so the staged photos appear together.
        os.replace(staging_dir, final_dir)
        moved_to_final = True
        try:
            database.register_student_with_tag(
                roll_no, name, uid, department=department, year=year, class_name=class_name,
            )
        except Exception:
            shutil.rmtree(final_dir, ignore_errors=True)
            moved_to_final = False
            raise
        result = {
            "ROLLNO": roll_no,
            "NAME": name,
            "TAGID": uid,
            "DEPARTMENT": department,
            "YEAR": year,
            "CLASS_NAME": class_name,
            "PHOTOS_DIR": str(final_dir),
            "PHOTO_COUNT": len(images),
        }
        output("\nEnrollment completed successfully.")
        output(f"Student: {name} ({roll_no})")
        output(f"NFC UID: {uid}")
        output(f"Face images: {len(images)} saved to {final_dir}")
        return result
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        # Normally cleared by the error path around the database transaction.
        if moved_to_final and not (photo_root / roll_no).exists():
            shutil.rmtree(final_dir, ignore_errors=True)
