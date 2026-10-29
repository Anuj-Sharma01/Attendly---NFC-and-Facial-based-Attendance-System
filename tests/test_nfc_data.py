from datetime import datetime

from Project.dbAccess import Database
from Project.nfcData import process_uid


def test_process_uid_requires_tag_and_face_match(tmp_path):
    db = Database(tmp_path / "attendance.sqlite3")
    db.add_student("CE42", "Ada Lovelace")
    db.bind_tag("CE42", "AABBCCDD")

    assert process_uid("11223344", db, lambda _: True) == "unknown_tag"
    assert process_uid("AABBCCDD", db, lambda _: False) == "face_rejected"
    assert db.list_attendance() == []

    assert process_uid("AABBCCDD", db, lambda name: name == "Ada Lovelace") == "marked"
    assert process_uid("AABBCCDD", db, lambda _: True) == "already_present"
    assert len(db.list_attendance()) == 1


def test_process_uid_normalizes_serial_values_and_handles_empty_uid(tmp_path):
    db = Database(tmp_path / "attendance.sqlite3")
    assert process_uid("", db, lambda _: True) == "empty"


def test_listener_marks_attendance_from_serial_input_without_hardware(tmp_path):
    from Project.config import Settings
    from Project.nfcData import run_attendance

    db = Database(tmp_path / "attendance.sqlite3")
    db.add_student("CE42", "Ada Lovelace")
    db.bind_tag("CE42", "AABBCCDD")

    class FakeReader:
        def __init__(self):
            self.lines = [b"AABBCCDD\n", b"AABBCCDD\n", b"AABBCCDD\n"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def readline(self):
            if self.lines:
                return self.lines.pop(0)
            raise KeyboardInterrupt

    settings = Settings(
        db_path=tmp_path / "attendance.sqlite3",
        photos_dir=tmp_path / "Photos",
        serial_port="FAKE",
        face_timeout_seconds=1,
    )
    face_calls = []

    def face_check(name):
        face_calls.append(name)
        return name == "Ada Lovelace"

    run_attendance(
        database=db,
        settings=settings,
        serial_factory=lambda *args, **kwargs: FakeReader(),
        face_check=face_check,
    )
    assert len(db.list_attendance()) == 1
    assert face_calls == ["Ada Lovelace"]


def test_listener_accepts_arduino_uid_label_and_ignores_startup_banner(tmp_path):
    from Project.config import Settings
    from Project.nfcData import run_attendance

    db = Database(tmp_path / "attendance.sqlite3")
    db.add_student("CE42", "Ada Lovelace")
    db.bind_tag("CE42", "045851EA0B1390")

    class FakeReader:
        def __init__(self):
            self.lines = [
                b"NFC reader ready\r\n",
                b"UID: 045851EA0B1390\r\n",
            ]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def readline(self):
            if self.lines:
                return self.lines.pop(0)
            raise KeyboardInterrupt

    settings = Settings(
        db_path=tmp_path / "attendance.sqlite3",
        photos_dir=tmp_path / "Photos",
        serial_port="FAKE",
        face_timeout_seconds=1,
        rfid_cooldown_seconds=0,
    )
    calls = []
    run_attendance(
        database=db,
        settings=settings,
        serial_factory=lambda *args, **kwargs: FakeReader(),
        face_check=lambda name: calls.append(name) or True,
    )

    assert calls == ["Ada Lovelace"]
    assert len(db.list_attendance()) == 1


def test_listener_marks_attendance_for_selected_lecture(tmp_path):
    from datetime import date
    from Project.config import Settings
    from Project.nfcData import run_attendance

    db = Database(tmp_path / "attendance.sqlite3")
    db.add_student("IT42", "Ada Lovelace", "IT", "2", "IT-B")
    lecture_id = db.create_lecture(date.today(), "IT", "2", "IT-B", "DBMS", "09:00", "10:00")

    class FakeReader:
        def __init__(self):
            self.lines = [b"UID: AABBCCDD\r\n"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def readline(self):
            if self.lines:
                return self.lines.pop(0)
            raise KeyboardInterrupt

    db.bind_tag("IT42", "AABBCCDD")
    settings = Settings(
        db_path=tmp_path / "attendance.sqlite3", photos_dir=tmp_path / "Photos",
        serial_port="FAKE", face_timeout_seconds=1, rfid_cooldown_seconds=0,
    )
    run_attendance(
        database=db, settings=settings,
        serial_factory=lambda *args, **kwargs: FakeReader(),
        face_check=lambda _name: True, lecture_id=lecture_id,
    )
    rows = db.list_attendance(date.today())
    assert len(rows) == 1
    assert rows[0]["LECTURE_ID"] == lecture_id
    assert rows[0]["SUBJECT"] == "DBMS"
