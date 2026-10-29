from pathlib import Path

import pytest

from Project.dbAccess import Database
from Project import enrollment


def test_parse_reader_line_supports_raw_and_labeled_uids():
    assert enrollment.parse_reader_line(b"04:6F:4F:EA\r\n") == "046F4FEA"
    assert enrollment.parse_reader_line("Card UID: 04 6F 4F EA") == "046F4FEA"
    assert enrollment.parse_reader_line("UID=aa-bb-cc-dd") == "AABBCCDD"
    assert enrollment.parse_reader_line("") == ""
    with pytest.raises(ValueError, match="hexadecimal"):
        enrollment.parse_reader_line("UID: nope")


def test_guided_enrollment_registers_student_tag_and_multiple_photos(tmp_path, monkeypatch):
    db = Database(tmp_path / "attendance.sqlite3")
    photos = tmp_path / "Photos"
    monkeypatch.setattr(
        enrollment, "read_nfc_uid",
        lambda *_args, **_kwargs: "04AABBCC",
    )

    def fake_capture(output_dir, *, image_count, camera_index):
        output_dir = Path(output_dir)
        paths = []
        for index in range(1, image_count + 1):
            path = output_dir / f"face_{index:02d}.jpg"
            path.write_bytes(b"test image")
            paths.append(path)
        return paths

    monkeypatch.setattr(enrollment, "capture_face_images", fake_capture)
    answers = iter(["CE42", "Ada Lovelace", "CE", "2", "CE-A", "y"])
    messages = []
    result = enrollment.enroll_interactive(
        db,
        photos_dir=photos,
        serial_port="FAKE",
        image_count=3,
        input_fn=lambda _prompt: next(answers),
        output=messages.append,
    )

    student = db.get_student_by_tag("04AABBCC")
    assert student is not None
    assert student["ROLLNO"] == "CE42"
    assert student["NAME"] == "Ada Lovelace"
    assert result["PHOTO_COUNT"] == 3
    assert sorted(path.name for path in (photos / "CE42").glob("*.jpg")) == [
        "face_01.jpg", "face_02.jpg", "face_03.jpg"
    ]
    assert len(db.list_students()) == 1


def test_cancelled_enrollment_does_not_create_database_records(tmp_path, monkeypatch):
    db = Database(tmp_path / "attendance.sqlite3")
    photos = tmp_path / "Photos"
    monkeypatch.setattr(enrollment, "read_nfc_uid", lambda *_args, **_kwargs: "AABBCCDD")

    def fake_capture(output_dir, *, image_count, camera_index):
        path = Path(output_dir) / "face_01.jpg"
        path.write_bytes(b"test image")
        return [path]

    monkeypatch.setattr(enrollment, "capture_face_images", fake_capture)
    answers = iter(["CE42", "Ada Lovelace", "CE", "2", "CE-A", "n"])
    with pytest.raises(RuntimeError, match="cancelled"):
        enrollment.enroll_interactive(
            db, photos_dir=photos, serial_port="FAKE", input_fn=lambda _prompt: next(answers),
            output=lambda _message: None,
        )
    assert db.list_students() == []
    assert db.list_tags() == []
    assert list(photos.iterdir()) == []
