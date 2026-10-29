"""CLI for enrollment, lecture setup and lecture-wise attendance reporting."""
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

try:
    from .config import Settings
    from .dbAccess import Database
    from .nfcData import run_attendance
    from .enrollment import enroll_interactive
except ImportError:  # pragma: no cover
    from config import Settings
    from dbAccess import Database
    from nfcData import run_attendance
    from enrollment import enroll_interactive


def build_parser() -> argparse.ArgumentParser:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(
        description="NFC + face-recognition attendance system with lecture-wise reporting",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=settings.db_path, help="SQLite database file")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init-db", help="Create or safely migrate the database schema")

    enroll = commands.add_parser("enroll", help="Register a student, scan NFC card and capture face images")
    enroll.add_argument("--photos", type=Path, default=settings.photos_dir, help="Face-photo root folder")
    enroll.add_argument("--port", default=settings.serial_port, help="Arduino/NFC serial port")
    enroll.add_argument("--baud-rate", type=int, default=settings.baud_rate)
    enroll.add_argument("--camera-index", type=int, default=settings.camera_index)
    enroll.add_argument("--image-count", type=int, default=3)
    enroll.add_argument("--nfc-timeout", type=float, default=60.0)

    add_student = commands.add_parser("add-student", help="Register a student without automatic image capture")
    add_student.add_argument("roll_no")
    add_student.add_argument("name")
    add_student.add_argument("--department", default="")
    add_student.add_argument("--year", default="")
    add_student.add_argument("--class-name", default="", help="Class/section, e.g. IT-B")

    update = commands.add_parser("update-student", help="Update student details or class placement")
    update.add_argument("roll_no")
    update.add_argument("--name")
    update.add_argument("--department")
    update.add_argument("--year")
    update.add_argument("--class-name", help="Class/section, e.g. IT-B")

    bind = commands.add_parser("bind-tag", help="Assign an NFC UID to an existing student")
    bind.add_argument("roll_no")
    bind.add_argument("uid")
    commands.add_parser("students", help="List registered students")
    commands.add_parser("tags", help="List NFC tag assignments")
    unbind = commands.add_parser("unbind-tag", help="Remove an NFC tag assignment")
    unbind.add_argument("roll_no")

    lecture = commands.add_parser("lecture", help="Create/list/delete scheduled lecture sessions")
    lecture_commands = lecture.add_subparsers(dest="lecture_command", required=True)
    lecture_add = lecture_commands.add_parser("add", help="Create a lecture session")
    lecture_add.add_argument("--date", default=date.today().isoformat(), help="Lecture date, YYYY-MM-DD")
    lecture_add.add_argument("--department", required=True)
    lecture_add.add_argument("--year", required=True)
    lecture_add.add_argument("--class-name", required=True, help="Class/section, e.g. IT-B")
    lecture_add.add_argument("--subject", required=True, help="Subject or lecture title")
    lecture_add.add_argument("--start-time", required=True, help="24-hour time HH:MM")
    lecture_add.add_argument("--end-time", required=True, help="24-hour time HH:MM")

    lecture_list = lecture_commands.add_parser("list", help="List scheduled lecture sessions")
    lecture_list.add_argument("--date", help="Filter date, YYYY-MM-DD")
    lecture_list.add_argument("--department")
    lecture_list.add_argument("--year")
    lecture_list.add_argument("--class-name")
    lecture_delete = lecture_commands.add_parser("delete", help="Delete a lecture without attendance records")
    lecture_delete.add_argument("lecture_id", type=int)

    attendance = commands.add_parser("attendance", help="List recorded attendance check-ins")
    attendance.add_argument("--date", dest="attendance_date", help="Filter date, YYYY-MM-DD")
    attendance.add_argument("--department")
    attendance.add_argument("--year")
    attendance.add_argument("--class-name")
    attendance.add_argument("--lecture-id", type=int)
    attendance.add_argument("--csv", type=Path, help="Export displayed records to a CSV file")

    student_report = commands.add_parser(
        "student-report", help="Show one student's scheduled lectures and attendance for a day"
    )
    student_report.add_argument("roll_no")
    student_report.add_argument("--date", default=date.today().isoformat(), help="Date, YYYY-MM-DD")
    student_report.add_argument("--csv", type=Path, help="Export report to CSV")

    class_report = commands.add_parser(
        "class-report", help="Show the full roster's Present/Absent status for every lecture on a date"
    )
    class_report.add_argument("--date", default=date.today().isoformat(), help="Date, YYYY-MM-DD")
    class_report.add_argument("--department", required=True)
    class_report.add_argument("--year", required=True)
    class_report.add_argument("--class-name", required=True)
    class_report.add_argument("--csv", type=Path, help="Export report to CSV")

    run = commands.add_parser("run", help="Start the NFC and camera attendance listener")
    run.add_argument("--photos", type=Path, default=settings.photos_dir, help="Reference face photo folder")
    run.add_argument("--port", default=settings.serial_port)
    run.add_argument("--baud-rate", type=int, default=settings.baud_rate)
    run.add_argument("--camera-index", type=int, default=settings.camera_index)
    run.add_argument("--face-timeout", type=float, default=settings.face_timeout_seconds)
    run.add_argument("--lecture-id", type=int, help="Record check-ins for this scheduled lecture; omit for legacy once-per-day mode")
    return parser


def _print_table(rows: list[dict], columns: list[tuple[str, str]]) -> None:
    if not rows:
        print("No records found.")
        return
    widths = [max(len(label), *(len(str(row.get(key, "") if row.get(key) is not None else "")) for row in rows))
              for key, label in columns]
    print("  ".join(label.ljust(width) for (_, label), width in zip(columns, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(row.get(key, "") if row.get(key) is not None else "").ljust(width)
                         for (key, _), width in zip(columns, widths)))


def _output_rows(rows: list[dict], columns: list[tuple[str, str]], csv_path: Path | None = None) -> None:
    if csv_path is None:
        _print_table(rows, columns)
        return
    csv_path = csv_path.expanduser().resolve()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in columns])
        for row in rows:
            writer.writerow([row.get(key, "") if row.get(key) is not None else "" for key, _ in columns])
    print(f"Exported {len(rows)} row(s) to {csv_path}")


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        database = Database(args.db)
        if args.command == "init-db":
            print(f"Database is ready (migrations applied safely): {database.path}")
        elif args.command == "enroll":
            enroll_interactive(
                database, photos_dir=args.photos, serial_port=args.port, baud_rate=args.baud_rate,
                camera_index=args.camera_index, image_count=args.image_count, nfc_timeout=args.nfc_timeout,
            )
        elif args.command == "add-student":
            database.add_student(args.roll_no, args.name, args.department, args.year, args.class_name)
            print(f"Registered {args.name} ({args.roll_no}) in {args.class_name or '(class not set)' }.")
        elif args.command == "update-student":
            database.update_student(
                args.roll_no, name=args.name, department=args.department, year=args.year, class_name=args.class_name,
            )
            print(f"Updated student {args.roll_no}.")
        elif args.command == "bind-tag":
            database.bind_tag(args.roll_no, args.uid)
            print(f"NFC tag {args.uid} is assigned to {args.roll_no}.")
        elif args.command == "students":
            _output_rows(database.list_students(), [
                ("ROLLNO", "Roll no."), ("NAME", "Name"), ("DEPARTMENT", "Department"),
                ("YEAR", "Year"), ("CLASS_NAME", "Class/section"),
            ])
        elif args.command == "tags":
            _output_rows(database.list_tags(), [("ROLLNO", "Roll no."), ("NAME", "Name"), ("TAGID", "NFC UID")])
        elif args.command == "unbind-tag":
            database.unbind_tag(args.roll_no)
            print(f"Removed the NFC tag assignment for {args.roll_no}.")
        elif args.command == "lecture":
            if args.lecture_command == "add":
                lecture_id = database.create_lecture(
                    args.date, args.department, args.year, args.class_name,
                    args.subject, args.start_time, args.end_time,
                )
                print(
                    f"Created lecture ID {lecture_id}: {args.subject}, {args.date} "
                    f"{args.start_time}-{args.end_time}, {args.department} year {args.year} {args.class_name}."
                )
            elif args.lecture_command == "list":
                _output_rows(database.list_lectures(args.date, args.department, args.year, args.class_name), [
                    ("ID", "ID"), ("LECTURE_DATE", "Date"), ("START_TIME", "Start"),
                    ("END_TIME", "End"), ("SUBJECT", "Subject"), ("DEPARTMENT", "Department"),
                    ("YEAR", "Year"), ("CLASS_NAME", "Class/section"), ("ATTENDANCE_COUNT", "Marked"),
                ])
            elif args.lecture_command == "delete":
                database.delete_lecture(args.lecture_id)
                print(f"Deleted lecture ID {args.lecture_id}.")
        elif args.command == "attendance":
            rows = database.list_attendance(
                _date(args.attendance_date), args.department, args.year, args.class_name, args.lecture_id,
            )
            _output_rows(rows, [
                ("DATE", "Attendance time"), ("ROLLNO", "Roll no."), ("NAME", "Name"),
                ("DEPARTMENT", "Department"), ("YEAR", "Year"), ("CLASS_NAME", "Class/section"),
                ("LECTURE_ID", "Lecture ID"), ("SUBJECT", "Subject"), ("STATUS", "Status"),
            ], args.csv)
        elif args.command == "student-report":
            report = database.student_day_report(args.roll_no, args.date)
            student = database.get_student(args.roll_no)
            if not report:
                print(f"No scheduled lectures found for {args.roll_no} on {args.date}.")
                print("Check the student's department/year/class and create that day's lecture sessions.")
            else:
                present = sum(row["ATTENDANCE_STATUS"] == "Present" for row in report)
                print(f"{student['NAME']} ({student['ROLLNO']}) — {args.date}: {present}/{len(report)} lectures attended")
                _output_rows(report, [
                    ("LECTURE_ID", "Lecture ID"), ("START_TIME", "Start"), ("END_TIME", "End"),
                    ("SUBJECT", "Subject"), ("ATTENDANCE_STATUS", "Status"), ("ATTENDANCE_TIME", "Scanned at"),
                ], args.csv)
        elif args.command == "class-report":
            report = database.class_day_report(args.date, args.department, args.year, args.class_name)
            if not report:
                print(f"No class/lecture records found for {args.department}, year {args.year}, {args.class_name} on {args.date}.")
                print("Add lecture sessions and ensure students are assigned to the same department/year/class.")
            else:
                present = sum(row["ATTENDANCE_STATUS"] == "Present" for row in report)
                print(
                    f"{args.department} | Year {args.year} | {args.class_name} | {args.date}: "
                    f"{present}/{len(report)} student-lecture check-ins present"
                )
                _output_rows(report, [
                    ("START_TIME", "Start"), ("END_TIME", "End"), ("SUBJECT", "Subject"),
                    ("ROLLNO", "Roll no."), ("NAME", "Name"), ("ATTENDANCE_STATUS", "Status"),
                    ("ATTENDANCE_TIME", "Scanned at"), ("LECTURE_ID", "Lecture ID"),
                ], args.csv)
        elif args.command == "run":
            env_settings = Settings.from_env()
            settings = Settings(
                db_path=args.db, photos_dir=args.photos, serial_port=args.port,
                baud_rate=args.baud_rate, camera_index=args.camera_index,
                face_timeout_seconds=args.face_timeout, face_tolerance=env_settings.face_tolerance,
                frame_scale=env_settings.frame_scale, required_face_matches=env_settings.required_face_matches,
                rfid_cooldown_seconds=env_settings.rfid_cooldown_seconds,
            )
            settings.validate()
            run_attendance(database=database, settings=settings, lecture_id=args.lecture_id)
        return 0
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        return 130
    except (ValueError, sqlite3.Error, OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
