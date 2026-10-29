from datetime import date, datetime

import pytest

from Project.dbAccess import Database, normalize_uid


def make_db(tmp_path):
    return Database(tmp_path / "attendance.sqlite3")


def test_database_initializes_and_student_tag_lookup_works(tmp_path):
    db = make_db(tmp_path)
    db.add_student("CE42", "Ada Lovelace", "CE", "2")
    db.bind_tag("CE42", "04:6f-4fea 0b1390")

    student = db.get_student_by_tag("046f4fea0b1390")
    assert student is not None
    assert student["NAME"] == "Ada Lovelace"
    assert student["ROLLNO"] == "CE42"
    assert db.verify_tag("04:6F:4F:EA:0B:13:90") is True
    assert db.verify_tag("DEADBEEF") is False


def test_attendance_is_recorded_only_once_per_day(tmp_path):
    db = make_db(tmp_path)
    db.add_student("CE42", "Ada Lovelace")
    student = {"ROLLNO": "CE42", "NAME": "Ada Lovelace"}
    first = datetime(2026, 10, 22, 9, 0, 0)
    second = datetime(2026, 10, 22, 15, 30, 0)
    following_day = datetime(2026, 10, 23, 9, 0, 0)

    assert db.mark_attendance(student, first) == "marked"
    assert db.mark_attendance(student, second) == "already_present"
    assert db.mark_attendance(student, following_day) == "marked"
    assert len(db.list_attendance()) == 2
    assert len(db.list_attendance(first.date())) == 1


def test_tag_cannot_be_assigned_to_another_student(tmp_path):
    db = make_db(tmp_path)
    db.add_student("CE42", "Ada Lovelace")
    db.add_student("CE43", "Grace Hopper")
    db.bind_tag("CE42", "AABBCCDD")

    with pytest.raises(ValueError, match="already assigned"):
        db.bind_tag("CE43", "AA:BB:CC:DD")
    with pytest.raises(ValueError, match="already has a tag"):
        db.bind_tag("CE42", "00112233")


def test_binding_requires_an_existing_student(tmp_path):
    db = make_db(tmp_path)
    with pytest.raises(ValueError, match="No student is registered"):
        db.bind_tag("MISSING", "AABBCCDD")


def test_values_are_parameterized_and_uid_validation_is_explicit(tmp_path):
    db = make_db(tmp_path)
    # SQL-looking text is stored as data; it cannot alter the query.
    db.add_student("CE' OR 1=1 --", "Student Name")
    assert len(db.list_students()) == 1
    assert normalize_uid(" 04:6f-4f ea\n") == "046F4FEA"
    with pytest.raises(ValueError, match="hexadecimal"):
        normalize_uid("not-a-valid-uid!")


def test_existing_legacy_schema_is_opened_without_dropping_records(tmp_path):
    import sqlite3

    legacy_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy_path) as con:
        con.executescript(
            """
            CREATE TABLE studentInfo(ID INTEGER PRIMARY KEY AUTOINCREMENT, NAME VARCHAR(20), ROLLNO VARCHAR(10), DEPARTMENT VARCHAR(20), YEAR VARCHAR(10));
            CREATE TABLE tagInfo(ID INTEGER PRIMARY KEY AUTOINCREMENT, TAGID VARCHAR(20), ROLLNO VARCHAR(10));
            CREATE TABLE attendanceInfo(ID INTEGER PRIMARY KEY AUTOINCREMENT, ROLLNO VARCHAR(10), NAME VARCHAR(20), DATE DATETIME, STATUS VARCHAR(10));
            INSERT INTO studentInfo(NAME, ROLLNO, DEPARTMENT, YEAR) VALUES ('Ada Lovelace', 'CE42', 'CE', '2');
            INSERT INTO tagInfo(TAGID, ROLLNO) VALUES ('04:AA:BB:CC', 'CE42');
            INSERT INTO attendanceInfo(ROLLNO, NAME, DATE, STATUS) VALUES ('CE42', 'Ada Lovelace', '2026-10-21 09:00:00', 'Present');
            """
        )

    db = Database(legacy_path)
    assert db.get_student_by_tag("04AABBCC")["NAME"] == "Ada Lovelace"
    assert len(db.list_attendance()) == 1
    assert db.mark_attendance({"ROLLNO": "CE42", "NAME": "Ada Lovelace"}, datetime(2026, 10, 21, 13, 0)) == "already_present"


def test_date_only_legacy_attendance_blocks_duplicate_and_lists_by_date(tmp_path):
    import sqlite3
    from datetime import date

    path = tmp_path / "date-only.sqlite3"
    db = Database(path)
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO attendanceInfo (ROLLNO, NAME, DATE, STATUS) VALUES (?, ?, ?, ?)",
            ("CE42", "Ada Lovelace", "2026-10-22", "Present"),
        )
    assert db.mark_attendance(
        {"ROLLNO": "CE42", "NAME": "Ada Lovelace"}, datetime(2026, 10, 22, 11, 0)
    ) == "already_present"
    assert len(db.list_attendance(date(2026, 10, 22))) == 1


def test_tag_assignment_can_be_listed_and_explicitly_removed(tmp_path):
    db = make_db(tmp_path)
    db.add_student("CE42", "Ada Lovelace")
    db.bind_tag("CE42", "AABBCCDD")
    assert db.list_tags() == [{"TAGID": "AABBCCDD", "ROLLNO": "CE42", "NAME": "Ada Lovelace"}]

    db.unbind_tag("CE42")
    assert db.list_tags() == []
    with pytest.raises(ValueError, match="No NFC tag is assigned"):
        db.unbind_tag("CE42")


def test_register_student_with_tag_is_atomic_and_normalizes_uid(tmp_path):
    db = make_db(tmp_path)
    db.register_student_with_tag("CE42", "Ada Lovelace", "04:AA-BB:CC", "CE", "2")
    student = db.get_student_by_tag("04AABBCC")
    assert student["NAME"] == "Ada Lovelace"
    assert student["DEPARTMENT"] == "CE"

    with pytest.raises(ValueError, match="already assigned"):
        db.register_student_with_tag("CE43", "Grace Hopper", "04AABBCC")
    assert [row["ROLLNO"] for row in db.list_students()] == ["CE42"]

    with pytest.raises(ValueError, match="already registered"):
        db.register_student_with_tag("CE42", "Different Name", "11223344")
    assert db.get_student_by_tag("11223344") is None


def test_lecture_attendance_allows_multiple_lectures_same_day_but_blocks_duplicates(tmp_path):
    from datetime import date

    db = make_db(tmp_path)
    db.add_student("IT42", "Ada Lovelace", "IT", "2", "IT-B")
    day = date(2026, 10, 22)
    first = db.create_lecture(day, "IT", "2", "IT-B", "DBMS", "09:00", "10:00")
    second = db.create_lecture(day, "IT", "2", "IT-B", "Operating Systems", "10:00", "11:00")
    student = db.get_student("IT42")

    assert db.mark_attendance(student, datetime(2026, 10, 22, 9, 12), lecture_id=first) == "marked"
    assert db.mark_attendance(student, datetime(2026, 10, 22, 9, 14), lecture_id=first) == "already_present"
    assert db.mark_attendance(student, datetime(2026, 10, 22, 10, 9), lecture_id=second) == "marked"
    assert len(db.list_attendance(day)) == 2

    report = db.student_day_report("IT42", day)
    assert [row["SUBJECT"] for row in report] == ["DBMS", "Operating Systems"]
    assert [row["ATTENDANCE_STATUS"] for row in report] == ["Present", "Present"]


def test_class_report_shows_absent_students_for_each_lecture(tmp_path):
    from datetime import date

    db = make_db(tmp_path)
    db.add_student("IT42", "Ada Lovelace", "IT", "2", "IT-B")
    db.add_student("IT43", "Grace Hopper", "IT", "2", "IT-B")
    db.add_student("IT44", "Alan Turing", "IT", "2", "IT-A")
    lecture_id = db.create_lecture(date(2026, 10, 22), "IT", "2", "IT-B", "DBMS", "09:00", "10:00")
    db.mark_attendance(db.get_student("IT42"), datetime(2026, 10, 22, 9, 11), lecture_id=lecture_id)

    rows = db.class_day_report("2026-10-22", "IT", "2", "IT-B")
    assert len(rows) == 2
    by_roll = {row["ROLLNO"]: row["ATTENDANCE_STATUS"] for row in rows}
    assert by_roll == {"IT42": "Present", "IT43": "Absent"}


def test_lecture_attendance_requires_matching_student_class_and_date(tmp_path):
    db = make_db(tmp_path)
    db.add_student("IT42", "Ada Lovelace", "IT", "2", "IT-A")
    lecture_id = db.create_lecture("2026-10-22", "IT", "2", "IT-B", "DBMS", "09:00", "10:00")

    with pytest.raises(ValueError, match="class/section"):
        db.mark_attendance(db.get_student("IT42"), datetime(2026, 10, 22, 9, 5), lecture_id=lecture_id)
    with pytest.raises(ValueError, match="scheduled for"):
        db.mark_attendance(db.get_student("IT42") | {"CLASS_NAME": "IT-B"}, datetime(2026, 10, 23, 9, 5), lecture_id=lecture_id)


def test_legacy_database_gets_class_and_lecture_columns_without_losing_records(tmp_path):
    import sqlite3

    path = tmp_path / "old-schema.sqlite3"
    with sqlite3.connect(path) as con:
        con.executescript(
            """
            CREATE TABLE studentInfo(ID INTEGER PRIMARY KEY AUTOINCREMENT, NAME TEXT, ROLLNO TEXT, DEPARTMENT TEXT, YEAR TEXT);
            CREATE TABLE tagInfo(ID INTEGER PRIMARY KEY AUTOINCREMENT, TAGID TEXT, ROLLNO TEXT);
            CREATE TABLE attendanceInfo(ID INTEGER PRIMARY KEY AUTOINCREMENT, ROLLNO TEXT, NAME TEXT, DATE DATETIME, STATUS TEXT);
            INSERT INTO studentInfo(NAME, ROLLNO, DEPARTMENT, YEAR) VALUES ('Ada', 'IT42', 'IT', '2');
            INSERT INTO attendanceInfo(ROLLNO, NAME, DATE, STATUS) VALUES ('IT42', 'Ada', '2026-10-22 09:00:00', 'Present');
            """
        )
    db = Database(path)
    assert db.get_student("IT42")["CLASS_NAME"] == ""
    assert len(db.list_attendance(date.fromisoformat("2026-10-22"))) == 1
    db.update_student("IT42", class_name="IT-B")
    assert db.get_student("IT42")["CLASS_NAME"] == "IT-B"
