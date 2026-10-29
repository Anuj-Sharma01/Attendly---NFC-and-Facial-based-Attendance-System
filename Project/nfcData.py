"""NFC listener and orchestration for attendance check-in."""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

try:  # support both package and direct-script imports
    from .camAnal import FaceVerifier
    from .config import Settings
    from .dbAccess import Database, normalize_uid
except ImportError:  # pragma: no cover
    from camAnal import FaceVerifier
    from config import Settings
    from dbAccess import Database, normalize_uid

logger = logging.getLogger(__name__)

# The included Arduino sketch emits lines such as "UID: 046F4FEA" and a
# startup banner ("NFC reader ready"). Normalize these the same way as the
# guided enrollment reader so enrollment and attendance accept the same format.
_SERIAL_UID_PREFIX = re.compile(r"^(?:CARD\s+)?UID\s*[:=]\s*(.*?)\s*$", re.IGNORECASE)
_SERIAL_STATUS_LINES = {"nfc reader ready", "reader ready", "ready"}


def _serial_factory() -> Any:
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError(
            "pyserial is not installed. Run 'python -m pip install -r requirements.txt'."
        ) from exc
    return serial.Serial


def process_uid(
    uid: str,
    database: Database,
    face_check: Callable[[str], bool],
    lecture_id: int | None = None,
) -> str:
    """Process one tag scan. Return a concise machine-readable outcome."""
    normalized = normalize_uid(uid)
    if not normalized:
        return "empty"
    student = database.get_student_by_tag(normalized)
    if student is None:
        logger.warning("Unknown or unassigned NFC tag: %s", normalized)
        return "unknown_tag"

    logger.info("NFC tag accepted for roll number %s; checking face", student["ROLLNO"])
    try:
        # The application verifier accepts the full student record so enrollment
        # photos can be matched by roll number, not display name. Plain callables
        # remain supported for tests and integrations that expect a name string.
        student_verifier = getattr(face_check, "verify_student", None)
        verified = student_verifier(student) if callable(student_verifier) else face_check(student["NAME"])
    except Exception:
        logger.exception("Face verification failed for roll number %s", student["ROLLNO"])
        return "face_error"
    if not verified:
        logger.warning("Face verification failed for roll number %s", student["ROLLNO"])
        return "face_rejected"

    try:
        result = database.mark_attendance(student, lecture_id=lecture_id)
    except ValueError as exc:
        logger.warning("Attendance was not recorded for %s: %s", student["ROLLNO"], exc)
        return "not_eligible"
    if result == "marked":
        if lecture_id is None:
            logger.info("Attendance marked (daily mode): %s (%s)", student["NAME"], student["ROLLNO"])
        else:
            logger.info("Attendance marked for lecture %s: %s (%s)", lecture_id, student["NAME"], student["ROLLNO"])
    else:
        if lecture_id is None:
            logger.info("Attendance already recorded today: %s (%s)", student["NAME"], student["ROLLNO"])
        else:
            logger.info("Attendance already recorded for lecture %s: %s (%s)", lecture_id, student["NAME"], student["ROLLNO"])
    return result


def run_attendance(
    *,
    database: Database | None = None,
    settings: Settings | None = None,
    serial_factory: Callable[..., Any] | None = None,
    face_check: Callable[[str], bool] | None = None,
    lecture_id: int | None = None,
) -> None:
    """Run the hardware loop until Ctrl+C, with injected dependencies for testing."""
    settings = settings or Settings.from_env()
    settings.validate()
    database = database or Database(settings.db_path)

    if face_check is None:
        # Keep loaded encodings in memory for all scans rather than reading every
        # student photo from disk each time a tag is presented.
        verifier = FaceVerifier(
            photos_dir=settings.photos_dir,
            students=database.list_students(),
            tolerance=settings.face_tolerance,
            frame_scale=settings.frame_scale,
            required_matches=settings.required_face_matches,
        )

        class _StudentFaceCheck:
            def __call__(self, name: str) -> bool:
                # Backward-compatible direct call for integrations passing a name.
                return verifier.verify(
                    name, camera_index=settings.camera_index,
                    timeout_seconds=settings.face_timeout_seconds,
                )

            def verify_student(self, student: dict[str, Any]) -> bool:
                return verifier.verify(
                    student["NAME"], expected_roll_no=student["ROLLNO"],
                    camera_index=settings.camera_index,
                    timeout_seconds=settings.face_timeout_seconds,
                )

        face_check = _StudentFaceCheck()
    if lecture_id is not None:
        lecture = database.get_lecture(lecture_id)
        if lecture is None:
            raise ValueError(f"Lecture ID {lecture_id} does not exist. Create/list lectures before starting attendance.")
        logger.info(
            "Lecture mode: ID %s | %s | %s %s %s | %s-%s",
            lecture["ID"], lecture["SUBJECT"], lecture["DEPARTMENT"], lecture["YEAR"],
            lecture["CLASS_NAME"], lecture["START_TIME"], lecture["END_TIME"],
        )
    else:
        logger.warning("No --lecture-id supplied: running legacy daily attendance mode (one check-in per day).")
    open_serial = serial_factory or _serial_factory()
    logger.info("Opening NFC reader on %s at %d baud", settings.serial_port, settings.baud_rate)

    seen_at: dict[str, float] = {}
    try:
        with open_serial(settings.serial_port, settings.baud_rate, timeout=1) as reader:
            logger.info("Ready. Present a registered NFC tag; press Ctrl+C to stop.")
            while True:
                raw = reader.readline()
                if not raw:
                    continue
                if isinstance(raw, bytes):
                    raw_uid = raw.decode("utf-8", errors="replace").strip()
                else:
                    raw_uid = str(raw).strip()
                if not raw_uid:
                    continue
                if raw_uid.casefold() in _SERIAL_STATUS_LINES:
                    logger.debug("Ignoring NFC reader status line: %r", raw_uid)
                    continue
                # Accept both plain hexadecimal UIDs and the labels printed by
                # the supplied Arduino sketch, e.g. "UID: 046F4FEA" or
                # "Card UID: 04:6F:4F:EA".
                label_match = _SERIAL_UID_PREFIX.fullmatch(raw_uid)
                if label_match:
                    raw_uid = label_match.group(1).strip()
                try:
                    uid = normalize_uid(raw_uid)
                except ValueError:
                    logger.warning("Ignoring malformed NFC reader value: %r", raw_uid)
                    continue
                if not uid:
                    continue
                now = time.monotonic()
                previous = seen_at.get(uid)
                if previous is not None and now - previous < settings.rfid_cooldown_seconds:
                    # Refresh on repeated reads so a tag held against the reader
                    # does not trigger another camera session when a slow scan ends.
                    seen_at[uid] = now
                    continue
                seen_at[uid] = now
                # Bound memory use even for readers presented with many unknown tags.
                seen_at = {tag: ts for tag, ts in seen_at.items() if now - ts < max(30.0, settings.rfid_cooldown_seconds * 4)}
                process_uid(uid, database, face_check, lecture_id=lecture_id)
    except KeyboardInterrupt:
        logger.info("Attendance listener stopped by user")
    except OSError as exc:
        raise RuntimeError(
            f"Could not open/read NFC serial port {settings.serial_port!r}: {exc}. "
            "Check the configured COM port and close any serial monitor using it."
        ) from exc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_attendance()
