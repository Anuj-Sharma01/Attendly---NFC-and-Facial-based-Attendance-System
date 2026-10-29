"""SQLite data access for students, NFC tags, lectures and attendance.

The legacy table/column names are retained. Startup performs additive migrations
only, so an existing database and already-recorded attendance remain intact.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

try:
    from .config import PROJECT_DIR
except ImportError:  # pragma: no cover
    from config import PROJECT_DIR

DEFAULT_DB_PATH = PROJECT_DIR / "main.db"
_TAG_SEPARATORS = re.compile(r"[\s:-]+")


def normalize_uid(uid: str) -> str:
    """Normalize a serial-reader UID without changing its hexadecimal content."""
    normalized = _TAG_SEPARATORS.sub("", str(uid or "")).upper()
    if not normalized:
        return ""
    if not re.fullmatch(r"[0-9A-F]+", normalized):
        raise ValueError("NFC tag UID must contain only hexadecimal characters and separators")
    return normalized


def _clean(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _same(a: Any, b: Any) -> bool:
    return _clean(a).casefold() == _clean(b).casefold()


class Database:
    """Small, dependency-free SQLite repository with additive schema migration."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path).expanduser().resolve()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=5.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 5000")
        return con

    @contextmanager
    def _session(self):
        con = self._connect()
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _ensure_column(con: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        names = {row["name"].upper() for row in con.execute(f"PRAGMA table_info({table})")}
        if column.upper() not in names:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def initialize(self) -> None:
        """Create tables/indexes and add columns missing from older databases."""
        with self._session() as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS studentInfo (
                    ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    NAME TEXT NOT NULL,
                    ROLLNO TEXT NOT NULL UNIQUE,
                    DEPARTMENT TEXT NOT NULL DEFAULT '',
                    YEAR TEXT NOT NULL DEFAULT '',
                    CLASS_NAME TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS tagInfo (
                    ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    TAGID TEXT NOT NULL UNIQUE,
                    ROLLNO TEXT NOT NULL UNIQUE
                );

                CREATE TABLE IF NOT EXISTS lectureInfo (
                    ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    LECTURE_DATE TEXT NOT NULL,
                    DEPARTMENT TEXT NOT NULL,
                    YEAR TEXT NOT NULL,
                    CLASS_NAME TEXT NOT NULL,
                    SUBJECT TEXT NOT NULL,
                    START_TIME TEXT NOT NULL,
                    END_TIME TEXT NOT NULL,
                    CREATED_AT TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS attendanceInfo (
                    ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    ROLLNO TEXT NOT NULL,
                    NAME TEXT NOT NULL,
                    DATE DATETIME NOT NULL,
                    STATUS TEXT NOT NULL DEFAULT 'Present',
                    LECTURE_ID INTEGER
                );
                """
            )
            # Additive migration: never drops or rewrites legacy student/attendance data.
            self._ensure_column(con, "studentInfo", "CLASS_NAME", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(con, "attendanceInfo", "LECTURE_ID", "INTEGER")
            con.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_taginfo_tagid ON tagInfo(TAGID);
                CREATE INDEX IF NOT EXISTS idx_taginfo_rollno ON tagInfo(ROLLNO);
                CREATE INDEX IF NOT EXISTS idx_attendance_rollno_date ON attendanceInfo(ROLLNO, DATE);
                CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendanceInfo(DATE);
                CREATE INDEX IF NOT EXISTS idx_attendance_lecture_roll ON attendanceInfo(LECTURE_ID, ROLLNO);
                CREATE INDEX IF NOT EXISTS idx_lecture_date_class ON lectureInfo(LECTURE_DATE, DEPARTMENT, YEAR, CLASS_NAME);
                """
            )

    def add_student(
        self,
        roll_no: str,
        name: str,
        department: str = "",
        year: str = "",
        class_name: str = "",
    ) -> None:
        roll_no, name = _clean(roll_no), _clean(name)
        if not roll_no or not name:
            raise ValueError("Roll number and student name are required")
        with self._session() as con:
            con.execute(
                "INSERT INTO studentInfo (NAME, ROLLNO, DEPARTMENT, YEAR, CLASS_NAME) VALUES (?, ?, ?, ?, ?)",
                (name, roll_no, _clean(department), _clean(year), _clean(class_name)),
            )

    def update_student(
        self,
        roll_no: str,
        *,
        name: str | None = None,
        department: str | None = None,
        year: str | None = None,
        class_name: str | None = None,
    ) -> None:
        """Update student metadata, especially class placement for reporting."""
        roll_no = _clean(roll_no)
        if not roll_no:
            raise ValueError("Roll number is required")
        values = {
            "NAME": name,
            "DEPARTMENT": department,
            "YEAR": year,
            "CLASS_NAME": class_name,
        }
        updates = [(key, _clean(value)) for key, value in values.items() if value is not None]
        if not updates:
            raise ValueError("Provide at least one field to update")
        if "NAME" in dict(updates) and not dict(updates)["NAME"]:
            raise ValueError("Student name cannot be empty")
        with self._session() as con:
            result = con.execute(
                "UPDATE studentInfo SET " + ", ".join(f"{key} = ?" for key, _ in updates)
                + " WHERE ROLLNO = ?",
                [value for _, value in updates] + [roll_no],
            )
            if result.rowcount == 0:
                raise ValueError(f"No student is registered with roll number {roll_no!r}")

    def register_student_with_tag(
        self,
        roll_no: str,
        name: str,
        uid: str,
        department: str = "",
        year: str = "",
        class_name: str = "",
    ) -> None:
        """Create a student and assign their NFC tag in one transaction."""
        roll_no, name = _clean(roll_no), _clean(name)
        uid = normalize_uid(uid)
        department, year, class_name = _clean(department), _clean(year), _clean(class_name)
        if not roll_no or not name:
            raise ValueError("Roll number and student name are required")
        if not uid:
            raise ValueError("NFC tag UID is required")

        with self._session() as con:
            if con.execute("SELECT 1 FROM studentInfo WHERE ROLLNO = ?", (roll_no,)).fetchone():
                raise ValueError(f"Roll number {roll_no!r} is already registered")
            existing_tag = con.execute(
                "SELECT ROLLNO FROM tagInfo WHERE UPPER(REPLACE(REPLACE(REPLACE(TAGID, ':', ''), '-', ''), ' ', '')) = ?",
                (uid,),
            ).fetchone()
            if existing_tag:
                raise ValueError(f"NFC tag {uid!r} is already assigned to {existing_tag['ROLLNO']!r}")
            con.execute(
                "INSERT INTO studentInfo (NAME, ROLLNO, DEPARTMENT, YEAR, CLASS_NAME) VALUES (?, ?, ?, ?, ?)",
                (name, roll_no, department, year, class_name),
            )
            con.execute("INSERT INTO tagInfo (TAGID, ROLLNO) VALUES (?, ?)", (uid, roll_no))

    def bind_tag(self, roll_no: str, uid: str) -> None:
        roll_no, uid = _clean(roll_no), normalize_uid(uid)
        if not roll_no:
            raise ValueError("Roll number is required")
        if not uid:
            raise ValueError("NFC tag UID is required")
        with self._session() as con:
            if con.execute("SELECT 1 FROM studentInfo WHERE ROLLNO = ?", (roll_no,)).fetchone() is None:
                raise ValueError(f"No student is registered with roll number {roll_no!r}")
            existing_uid = con.execute(
                "SELECT ROLLNO FROM tagInfo WHERE UPPER(REPLACE(REPLACE(REPLACE(TAGID, ':', ''), '-', ''), ' ', '')) = ?",
                (uid,),
            ).fetchone()
            if existing_uid is not None:
                if existing_uid["ROLLNO"] == roll_no:
                    return
                raise ValueError("This NFC tag is already assigned to another student")
            existing_roll = con.execute("SELECT TAGID FROM tagInfo WHERE ROLLNO = ?", (roll_no,)).fetchone()
            if existing_roll is not None:
                raise ValueError(
                    f"Student {roll_no!r} already has a tag ({existing_roll['TAGID']}); remove or update that assignment explicitly first"
                )
            con.execute("INSERT INTO tagInfo (TAGID, ROLLNO) VALUES (?, ?)", (uid, roll_no))

    def unbind_tag(self, roll_no: str) -> None:
        roll_no = _clean(roll_no)
        if not roll_no:
            raise ValueError("Roll number is required")
        with self._session() as con:
            result = con.execute("DELETE FROM tagInfo WHERE ROLLNO = ?", (roll_no,))
            if result.rowcount == 0:
                raise ValueError(f"No NFC tag is assigned to roll number {roll_no!r}")

    def list_tags(self) -> list[dict[str, Any]]:
        with self._session() as con:
            rows = con.execute(
                "SELECT t.TAGID, t.ROLLNO, s.NAME FROM tagInfo t LEFT JOIN studentInfo s ON s.ROLLNO=t.ROLLNO ORDER BY t.ROLLNO, t.TAGID"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_student(self, roll_no: str) -> dict[str, Any] | None:
        with self._session() as con:
            row = con.execute(
                "SELECT ID, NAME, ROLLNO, DEPARTMENT, YEAR, CLASS_NAME FROM studentInfo WHERE ROLLNO = ?",
                (_clean(roll_no),),
            ).fetchone()
        return dict(row) if row else None

    def get_student_by_tag(self, uid: str) -> dict[str, Any] | None:
        uid = normalize_uid(uid)
        if not uid:
            return None
        with self._session() as con:
            query = """
                SELECT s.ID, s.NAME, s.ROLLNO, s.DEPARTMENT, s.YEAR, s.CLASS_NAME, t.TAGID
                FROM tagInfo t INNER JOIN studentInfo s ON s.ROLLNO=t.ROLLNO WHERE t.TAGID=? LIMIT 1
            """
            row = con.execute(query, (uid,)).fetchone()
            if row is None:
                row = con.execute(
                    """SELECT s.ID, s.NAME, s.ROLLNO, s.DEPARTMENT, s.YEAR, s.CLASS_NAME, t.TAGID
                       FROM tagInfo t INNER JOIN studentInfo s ON s.ROLLNO=t.ROLLNO
                       WHERE UPPER(REPLACE(REPLACE(REPLACE(t.TAGID, ':', ''), '-', ''), ' ', ''))=? LIMIT 1""",
                    (uid,),
                ).fetchone()
        return dict(row) if row else None

    def verify_tag(self, uid: str) -> bool:
        return self.get_student_by_tag(uid) is not None

    @staticmethod
    def _parse_lecture_date(value: date | str) -> date:
        try:
            return value if isinstance(value, date) else date.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("Lecture date must use YYYY-MM-DD format") from exc

    @staticmethod
    def _parse_time(value: str, label: str) -> str:
        try:
            parsed = datetime.strptime(str(value).strip(), "%H:%M")
        except ValueError as exc:
            raise ValueError(f"{label} must use 24-hour HH:MM format") from exc
        return parsed.strftime("%H:%M")

    def create_lecture(
        self,
        lecture_date: date | str,
        department: str,
        year: str,
        class_name: str,
        subject: str,
        start_time: str,
        end_time: str,
    ) -> int:
        """Create one scheduled lecture/class session and return its numeric ID."""
        day = self._parse_lecture_date(lecture_date)
        department, year, class_name, subject = map(_clean, (department, year, class_name, subject))
        start_time = self._parse_time(start_time, "Start time")
        end_time = self._parse_time(end_time, "End time")
        if not all((department, year, class_name, subject)):
            raise ValueError("Department, year, class/section and subject are required to create a lecture")
        if end_time <= start_time:
            raise ValueError("Lecture end time must be later than its start time")
        with self._session() as con:
            duplicate = con.execute(
                """SELECT ID FROM lectureInfo WHERE LECTURE_DATE=? AND UPPER(DEPARTMENT)=UPPER(?)
                   AND UPPER(YEAR)=UPPER(?) AND UPPER(CLASS_NAME)=UPPER(?) AND UPPER(SUBJECT)=UPPER(?)
                   AND START_TIME=? AND END_TIME=?""",
                (day.isoformat(), department, year, class_name, subject, start_time, end_time),
            ).fetchone()
            if duplicate:
                raise ValueError(f"This lecture already exists with ID {duplicate['ID']}")
            cursor = con.execute(
                """INSERT INTO lectureInfo
                   (LECTURE_DATE, DEPARTMENT, YEAR, CLASS_NAME, SUBJECT, START_TIME, END_TIME)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (day.isoformat(), department, year, class_name, subject, start_time, end_time),
            )
            return int(cursor.lastrowid)

    def get_lecture(self, lecture_id: int) -> dict[str, Any] | None:
        with self._session() as con:
            row = con.execute("SELECT * FROM lectureInfo WHERE ID=?", (int(lecture_id),)).fetchone()
        return dict(row) if row else None

    def list_lectures(
        self,
        on_date: date | str | None = None,
        department: str | None = None,
        year: str | None = None,
        class_name: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = """SELECT l.*, (SELECT COUNT(*) FROM attendanceInfo a WHERE a.LECTURE_ID=l.ID) AS ATTENDANCE_COUNT
                 FROM lectureInfo l WHERE 1=1"""
        params: list[Any] = []
        if on_date is not None:
            sql += " AND l.LECTURE_DATE=?"
            params.append(self._parse_lecture_date(on_date).isoformat())
        for column, value in (("DEPARTMENT", department), ("YEAR", year), ("CLASS_NAME", class_name)):
            if value is not None and _clean(value):
                sql += f" AND UPPER(l.{column})=UPPER(?)"
                params.append(_clean(value))
        sql += " ORDER BY l.LECTURE_DATE, l.START_TIME, l.ID"
        with self._session() as con:
            rows = con.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def delete_lecture(self, lecture_id: int) -> None:
        with self._session() as con:
            if con.execute("SELECT 1 FROM lectureInfo WHERE ID=?", (int(lecture_id),)).fetchone() is None:
                raise ValueError(f"Lecture ID {lecture_id} does not exist")
            if con.execute("SELECT 1 FROM attendanceInfo WHERE LECTURE_ID=? LIMIT 1", (int(lecture_id),)).fetchone():
                raise ValueError("Cannot delete a lecture that already has attendance records")
            con.execute("DELETE FROM lectureInfo WHERE ID=?", (int(lecture_id),))

    def _validate_student_for_lecture(
        self, con: sqlite3.Connection, student: dict[str, Any] | sqlite3.Row, lecture_id: int, timestamp: datetime
    ) -> sqlite3.Row:
        lecture = con.execute("SELECT * FROM lectureInfo WHERE ID=?", (int(lecture_id),)).fetchone()
        if lecture is None:
            raise ValueError(f"Lecture ID {lecture_id} does not exist; create/list a lecture first")
        if lecture["LECTURE_DATE"] != timestamp.date().isoformat():
            raise ValueError(
                f"Lecture ID {lecture_id} is scheduled for {lecture['LECTURE_DATE']}, not {timestamp.date().isoformat()}"
            )
        student_fields = {
            "DEPARTMENT": student["DEPARTMENT"] if "DEPARTMENT" in student.keys() else "",
            "YEAR": student["YEAR"] if "YEAR" in student.keys() else "",
            "CLASS_NAME": student["CLASS_NAME"] if "CLASS_NAME" in student.keys() else "",
        }
        for field, label in (("DEPARTMENT", "department"), ("YEAR", "year"), ("CLASS_NAME", "class/section")):
            if not _clean(student_fields[field]):
                raise ValueError(
                    f"Student {student['ROLLNO']} has no {label} assigned. Update the student record before lecture attendance."
                )
            if not _same(student_fields[field], lecture[field]):
                raise ValueError(
                    f"Student {student['ROLLNO']} belongs to {student_fields[field]!r} for {label}, "
                    f"not this lecture's {lecture[field]!r}"
                )
        return lecture

    def mark_attendance(
        self,
        student: dict[str, Any] | sqlite3.Row | Iterable[Any],
        at: datetime | None = None,
        lecture_id: int | None = None,
    ) -> str:
        """Record attendance once per day (legacy) or once per scheduled lecture.

        When ``lecture_id`` is provided, it is unique per student/lecture and
        the student must belong to that lecture's date, department, year and class.
        """
        if isinstance(student, (dict, sqlite3.Row)):
            data = dict(student)
            roll_no, name = _clean(data.get("ROLLNO")), _clean(data.get("NAME"))
        else:
            values = list(student)
            if len(values) < 3:
                raise ValueError("Student details must contain ID, NAME and ROLLNO")
            name, roll_no = _clean(values[1]), _clean(values[2])
            data = {"NAME": name, "ROLLNO": roll_no}
        if not roll_no or not name:
            raise ValueError("Student name and roll number are required")
        timestamp = at or datetime.now().replace(microsecond=0)
        timestamp_text = timestamp.isoformat(sep=" ", timespec="seconds")
        day = timestamp.date()
        start_text = f"{day.isoformat()} 00:00:00"
        next_start_text = f"{(day + timedelta(days=1)).isoformat()} 00:00:00"

        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            if lecture_id is not None:
                self._validate_student_for_lecture(con, data, lecture_id, timestamp)
                existing = con.execute(
                    "SELECT ID FROM attendanceInfo WHERE ROLLNO=? AND LECTURE_ID=? LIMIT 1",
                    (roll_no, int(lecture_id)),
                ).fetchone()
                if existing:
                    con.commit()
                    return "already_present"
                con.execute(
                    "INSERT INTO attendanceInfo (ROLLNO, NAME, DATE, STATUS, LECTURE_ID) VALUES (?, ?, ?, ?, ?)",
                    (roll_no, name, timestamp_text, "Present", int(lecture_id)),
                )
            else:
                existing = con.execute(
                    "SELECT ID FROM attendanceInfo WHERE ROLLNO=? AND DATE>=? AND DATE<? LIMIT 1",
                    (roll_no, start_text, next_start_text),
                ).fetchone()
                if existing is None:
                    existing = con.execute(
                        "SELECT ID FROM attendanceInfo WHERE ROLLNO=? AND DATE=? LIMIT 1",
                        (roll_no, day.isoformat()),
                    ).fetchone()
                if existing:
                    con.commit()
                    return "already_present"
                con.execute(
                    "INSERT INTO attendanceInfo (ROLLNO, NAME, DATE, STATUS, LECTURE_ID) VALUES (?, ?, ?, ?, NULL)",
                    (roll_no, name, timestamp_text, "Present"),
                )
            con.commit()
            return "marked"
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def list_students(self) -> list[dict[str, Any]]:
        with self._session() as con:
            rows = con.execute(
                "SELECT ID, NAME, ROLLNO, DEPARTMENT, YEAR, CLASS_NAME FROM studentInfo ORDER BY ROLLNO"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_attendance(
        self,
        on_date: date | None = None,
        department: str | None = None,
        year: str | None = None,
        class_name: str | None = None,
        lecture_id: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = """SELECT a.ID, a.ROLLNO, a.NAME, a.DATE, a.STATUS, a.LECTURE_ID,
                    COALESCE(NULLIF(s.DEPARTMENT,''), l.DEPARTMENT, '') AS DEPARTMENT,
                    COALESCE(NULLIF(s.YEAR,''), l.YEAR, '') AS YEAR,
                    COALESCE(NULLIF(s.CLASS_NAME,''), l.CLASS_NAME, '') AS CLASS_NAME,
                    l.SUBJECT, l.LECTURE_DATE, l.START_TIME, l.END_TIME
                 FROM attendanceInfo a
                 LEFT JOIN studentInfo s ON s.ROLLNO=a.ROLLNO
                 LEFT JOIN lectureInfo l ON l.ID=a.LECTURE_ID WHERE 1=1"""
        params: list[Any] = []
        if on_date is not None:
            start_text = f"{on_date.isoformat()} 00:00:00"
            next_text = f"{(on_date + timedelta(days=1)).isoformat()} 00:00:00"
            sql += " AND ((a.DATE>=? AND a.DATE<?) OR a.DATE=?)"
            params.extend((start_text, next_text, on_date.isoformat()))
        if lecture_id is not None:
            sql += " AND a.LECTURE_ID=?"
            params.append(int(lecture_id))
        for expression, value in (
            ("COALESCE(NULLIF(s.DEPARTMENT,''), l.DEPARTMENT, '')", department),
            ("COALESCE(NULLIF(s.YEAR,''), l.YEAR, '')", year),
            ("COALESCE(NULLIF(s.CLASS_NAME,''), l.CLASS_NAME, '')", class_name),
        ):
            if value is not None and _clean(value):
                sql += f" AND UPPER({expression})=UPPER(?)"
                params.append(_clean(value))
        sql += " ORDER BY a.DATE, a.ROLLNO"
        with self._session() as con:
            rows = con.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def student_day_report(self, roll_no: str, on_date: date | str) -> list[dict[str, Any]]:
        """Return every scheduled lecture for one student/date, including absences."""
        day = self._parse_lecture_date(on_date)
        student = self.get_student(roll_no)
        if student is None:
            raise ValueError(f"No student is registered with roll number {_clean(roll_no)!r}")
        missing = [label for field, label in (("DEPARTMENT", "department"), ("YEAR", "year"), ("CLASS_NAME", "class/section")) if not _clean(student.get(field))]
        if missing:
            raise ValueError(
                f"Student {roll_no} is missing {', '.join(missing)} metadata. Use update-student before lecture reports."
            )
        with self._session() as con:
            rows = con.execute(
                """SELECT l.ID AS LECTURE_ID, l.LECTURE_DATE, l.START_TIME, l.END_TIME, l.SUBJECT,
                          CASE WHEN a.ID IS NULL THEN 'Absent' ELSE a.STATUS END AS ATTENDANCE_STATUS,
                          a.DATE AS ATTENDANCE_TIME
                   FROM lectureInfo l
                   LEFT JOIN attendanceInfo a ON a.LECTURE_ID=l.ID AND a.ROLLNO=?
                   WHERE l.LECTURE_DATE=? AND UPPER(l.DEPARTMENT)=UPPER(?)
                     AND UPPER(l.YEAR)=UPPER(?) AND UPPER(l.CLASS_NAME)=UPPER(?)
                   ORDER BY l.START_TIME, l.ID""",
                (student["ROLLNO"], day.isoformat(), student["DEPARTMENT"], student["YEAR"], student["CLASS_NAME"]),
            ).fetchall()
        return [dict(row) for row in rows]

    def class_day_report(
        self, on_date: date | str, department: str, year: str, class_name: str
    ) -> list[dict[str, Any]]:
        """Return the full roster against every scheduled class lecture, including absences."""
        day = self._parse_lecture_date(on_date)
        department, year, class_name = map(_clean, (department, year, class_name))
        if not all((department, year, class_name)):
            raise ValueError("Department, year and class/section are required")
        with self._session() as con:
            rows = con.execute(
                """SELECT l.ID AS LECTURE_ID, l.LECTURE_DATE, l.START_TIME, l.END_TIME, l.SUBJECT,
                          s.ROLLNO, s.NAME,
                          CASE WHEN a.ID IS NULL THEN 'Absent' ELSE a.STATUS END AS ATTENDANCE_STATUS,
                          a.DATE AS ATTENDANCE_TIME
                   FROM lectureInfo l
                   JOIN studentInfo s ON UPPER(s.DEPARTMENT)=UPPER(l.DEPARTMENT)
                     AND UPPER(s.YEAR)=UPPER(l.YEAR) AND UPPER(s.CLASS_NAME)=UPPER(l.CLASS_NAME)
                   LEFT JOIN attendanceInfo a ON a.LECTURE_ID=l.ID AND a.ROLLNO=s.ROLLNO
                   WHERE l.LECTURE_DATE=? AND UPPER(l.DEPARTMENT)=UPPER(?)
                     AND UPPER(l.YEAR)=UPPER(?) AND UPPER(l.CLASS_NAME)=UPPER(?)
                   ORDER BY l.START_TIME, s.ROLLNO""",
                (day.isoformat(), department, year, class_name),
            ).fetchall()
        return [dict(row) for row in rows]


# Backwards-compatible functions for the original scripts.
def verifyTagValid(uid: str, db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    return Database(db_path).verify_tag(uid)


def getStudentDetails(uid: str, db_path: str | Path = DEFAULT_DB_PATH) -> list[tuple[Any, ...]]:
    student = Database(db_path).get_student_by_tag(uid)
    if student is None:
        return []
    return [tuple(student[key] for key in ("ID", "NAME", "ROLLNO", "DEPARTMENT", "YEAR"))]


def updateAttendanceStatus(studentDetails: Iterable[Any], db_path: str | Path = DEFAULT_DB_PATH) -> bool:
    return Database(db_path).mark_attendance(studentDetails) == "marked"
