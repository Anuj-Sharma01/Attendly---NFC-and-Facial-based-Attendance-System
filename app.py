from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from threading import RLock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask, Response, abort, flash, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

from Project.dbAccess import Database, normalize_uid

BASE_DIR = Path(__file__).resolve().parent
ROLL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$")
UID_LABEL_RE = re.compile(r"\bUID\b\s*[:=]\s*(.*)$", re.IGNORECASE)
IMAGE_DATA_RE = re.compile(r"^data:image/(jpeg|jpg|png);base64,([A-Za-z0-9+/=\r\n]+)$", re.IGNORECASE)
YEAR_OPTIONS = (("1", "First Year"), ("2", "Second Year"), ("3", "Third Year"), ("4", "Fourth Year"))
YEAR_LABELS = dict(YEAR_OPTIONS)
YEAR_CODES = {label.casefold(): code for code, label in YEAR_OPTIONS}


def normalize_year(value: Any, *, allow_empty: bool = False) -> str:
    """Normalize a year label or numeric year to the legacy numeric DB code."""
    raw = str(value or "").strip()
    if not raw and allow_empty:
        return ""
    normalized = raw.casefold()
    if normalized in YEAR_LABELS:
        return normalized
    if normalized in YEAR_CODES:
        return YEAR_CODES[normalized]
    if normalized.startswith("year ") and normalized[5:].strip() in YEAR_LABELS:
        return normalized[5:].strip()
    choices = ", ".join(label for _, label in YEAR_OPTIONS)
    raise ValueError(f"Year must be one of: {choices}.")


def display_year(value: Any) -> str:
    """Display historical numeric years with human-friendly labels."""
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        code = normalize_year(raw)
        return YEAR_LABELS[code]
    except ValueError:
        # Preserve historical out-of-range numeric values visibly instead of hiding them.
        return f"Legacy year {raw}" if raw.isdigit() else raw


def year_code(value: Any) -> str:
    """Return a numeric code for selecting existing records in edit forms."""
    try:
        return normalize_year(value, allow_empty=True)
    except ValueError:
        return ""
_FACE_CACHE: dict[tuple[str, str], tuple[tuple[tuple[str, int, int], ...], list[Any]]] = {}
_FACE_CACHE_LOCK = RLock()


def local_now(app: Flask) -> datetime:
    try:
        tz = ZoneInfo(app.config["TIMEZONE"])
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).replace(tzinfo=None, microsecond=0)


def _db(app: Flask) -> Database:
    return app.extensions["attendance_db"]


def _init_web_tables(db: Database) -> None:
    with db._session() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS web_scan_events (
                ID INTEGER PRIMARY KEY AUTOINCREMENT,
                UID TEXT NOT NULL,
                CREATED_AT TEXT NOT NULL,
                STATUS TEXT NOT NULL DEFAULT 'pending',
                MESSAGE TEXT NOT NULL DEFAULT '',
                ROLLNO TEXT,
                LECTURE_ID INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_web_scan_created ON web_scan_events(CREATED_AT);
            CREATE INDEX IF NOT EXISTS idx_web_scan_status ON web_scan_events(STATUS, ID);
            """
        )


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.jinja_env.filters["year_label"] = display_year
    app.jinja_env.filters["year_code"] = year_code
    data_dir = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "development-only-change-me"),
        ADMIN_USERNAME=os.getenv("ADMIN_USERNAME", "admin"),
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD", "change-this-before-running"),
        BRIDGE_API_TOKEN=os.getenv("BRIDGE_API_TOKEN", "development-bridge-token-change-me"),
        DATABASE=os.getenv("ATTENDANCE_DB", str(data_dir / "main.db")),
        PHOTOS_DIR=os.getenv("ATTENDANCE_PHOTOS_DIR", str(data_dir / "Photos")),
        TIMEZONE=os.getenv("APP_TIMEZONE", "Asia/Kolkata"),
        FACE_TOLERANCE=float(os.getenv("FACE_TOLERANCE", "0.48")),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)

    if app.config["SECRET_KEY"] == "development-only-change-me" or "replace-with" in str(app.config["SECRET_KEY"]).lower():
        app.logger.warning("SECRET_KEY looks like a development placeholder. Replace it in .env before team use.")
    if app.config["ADMIN_PASSWORD"] == "change-this-before-running" or str(app.config["ADMIN_PASSWORD"]).startswith("ChangeThis-"):
        app.logger.warning("ADMIN_PASSWORD looks like a sample value. Change it in .env before team use.")
    if "replace-with" in str(app.config["BRIDGE_API_TOKEN"]).lower():
        app.logger.warning("BRIDGE_API_TOKEN looks like a placeholder. Replace it in .env before team use.")

    Path(app.config["PHOTOS_DIR"]).mkdir(parents=True, exist_ok=True)
    db = Database(app.config["DATABASE"])
    _init_web_tables(db)
    app.extensions["attendance_db"] = db

    @app.context_processor
    def inject_globals():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_urlsafe(24)
        return {"csrf_token": session["_csrf_token"], "today": local_now(app).date().isoformat(), "year_options": YEAR_OPTIONS}

    @app.before_request
    def protect_routes():
        endpoint = request.endpoint or ""
        public = {"login", "health", "hardware_scan", "static"}
        if endpoint not in public and not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required. Sign in and retry."}), 401
            return redirect(url_for("login", next=request.path))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and endpoint not in {"login", "hardware_scan"}:
            sent = request.headers.get("X-CSRFToken") or request.form.get("csrf_token", "")
            expected = session.get("_csrf_token", "")
            if not sent or not expected or not hmac.compare_digest(sent, expected):
                if request.path.startswith("/api/") or request.is_json:
                    return jsonify({"error": "Your page token expired. Refresh the page and try again."}), 400
                abort(400, description="Your page token expired. Refresh the page and try again.")

    @app.errorhandler(413)
    def too_large(_exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Upload too large. Capture fewer or smaller images."}), 413
        return "Upload too large. Capture fewer or smaller images.", 413

    @app.errorhandler(400)
    def bad_request(exc):
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"error": getattr(exc, "description", "Bad request")}), 400
        return render_template("error.html", code=400, message=getattr(exc, "description", "Bad request")), 400

    @app.get("/healthz")
    def health():
        return jsonify({"status": "ok", "app": "NFC Attendance", "time": local_now(app).isoformat(sep=" ")})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("logged_in"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if hmac.compare_digest(username, str(app.config["ADMIN_USERNAME"])) and hmac.compare_digest(
                password, str(app.config["ADMIN_PASSWORD"])
            ):
                session.clear()
                session["logged_in"] = True
                session["username"] = username
                session["_csrf_token"] = secrets.token_urlsafe(24)
                next_url = request.args.get("next", "")
                if not next_url.startswith("/") or next_url.startswith("//"):
                    next_url = url_for("dashboard")
                return redirect(next_url)
            flash("Username or password is incorrect.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard():
        today = local_now(app).date().isoformat()
        students = _db(app).list_students()
        lectures = _db(app).list_lectures(on_date=today)
        attendance = _db(app).list_attendance(on_date=date.fromisoformat(today))
        recent = attendance[-8:][::-1]
        by_class = {}
        for lecture in lectures:
            key = f"{lecture['DEPARTMENT']} · {display_year(lecture['YEAR'])} · {lecture['CLASS_NAME']}"
            by_class[key] = by_class.get(key, 0) + 1
        return render_template(
            "dashboard.html", title="Dashboard", active="dashboard", students=students,
            lectures=lectures, present_count=len(attendance), recent=recent,
            class_count=len(by_class), attendance_count=len(attendance),
        )

    @app.get("/students")
    def students_page():
        students = _db(app).list_students()
        tags = {item["ROLLNO"]: item["TAGID"] for item in _db(app).list_tags()}
        for student in students:
            student["TAGID"] = tags.get(student["ROLLNO"], "")
        return render_template("students.html", title="Students", active="students", students=students)

    @app.post("/students/<roll_no>/update")
    def update_student_page(roll_no: str):
        try:
            _db(app).update_student(
                roll_no,
                name=request.form.get("name"),
                department=request.form.get("department"),
                year=normalize_year(request.form.get("year")),
                class_name=request.form.get("class_name"),
            )
            flash(f"Updated student {roll_no}.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("students_page"))

    @app.get("/lectures")
    def lectures_page():
        chosen_date = request.args.get("date") or local_now(app).date().isoformat()
        try:
            date.fromisoformat(chosen_date)
        except ValueError:
            chosen_date = local_now(app).date().isoformat()
        department = request.args.get("department", "")
        try:
            year = normalize_year(request.args.get("year", ""), allow_empty=True)
        except ValueError:
            year = ""
        class_name = request.args.get("class_name", "")
        lectures = _db(app).list_lectures(chosen_date, department, year, class_name)
        return render_template(
            "lectures.html", title="Lectures", active="lectures", lectures=lectures,
            filter_date=chosen_date, department=department, year=year, class_name=class_name,
        )

    @app.post("/lectures/<int:lecture_id>/delete")
    def delete_lecture_page(lecture_id: int):
        try:
            _db(app).delete_lecture(lecture_id)
            flash(f"Deleted lecture {lecture_id}.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("lectures_page", date=request.form.get("date") or local_now(app).date().isoformat()))

    @app.get("/reports")
    def reports_page():
        chosen_date = request.args.get("date") or local_now(app).date().isoformat()
        try:
            date.fromisoformat(chosen_date)
        except ValueError:
            chosen_date = local_now(app).date().isoformat()
        mode = request.args.get("mode", "class")
        department = request.args.get("department", "").strip()
        try:
            year = normalize_year(request.args.get("year", ""), allow_empty=True)
        except ValueError:
            year = ""
        class_name = request.args.get("class_name", "").strip()
        roll_no = request.args.get("roll_no", "").strip()
        rows: list[dict[str, Any]] = []
        error = ""
        if mode == "student" and roll_no:
            try:
                rows = _db(app).student_day_report(roll_no, chosen_date)
            except ValueError as exc:
                error = str(exc)
        elif mode == "class" and department and year and class_name:
            rows = _db(app).class_day_report(chosen_date, department, year, class_name)
        elif mode == "checkins":
            rows = _db(app).list_attendance(date.fromisoformat(chosen_date), department, year, class_name)
        present = sum(1 for row in rows if row.get("ATTENDANCE_STATUS", row.get("STATUS")) == "Present")
        return render_template(
            "reports.html", title="Reports", active="reports", rows=rows, mode=mode,
            filter_date=chosen_date, department=department, year=year, class_name=class_name,
            roll_no=roll_no, present=present, error=error,
        )

    @app.get("/reports/export.csv")
    def export_report():
        chosen_date = request.args.get("date") or local_now(app).date().isoformat()
        try:
            day = date.fromisoformat(chosen_date)
        except ValueError:
            abort(400, description="Date must be YYYY-MM-DD.")
        mode = request.args.get("mode", "class")
        department = request.args.get("department", "").strip()
        try:
            year = normalize_year(request.args.get("year", ""), allow_empty=True)
        except ValueError:
            abort(400, description="Year must be First Year, Second Year, Third Year or Fourth Year.")
        class_name = request.args.get("class_name", "").strip()
        roll_no = request.args.get("roll_no", "").strip()
        try:
            if mode == "student":
                if not roll_no:
                    abort(400, description="Enter a student roll number to export an individual report.")
                rows = _db(app).student_day_report(roll_no, day)
                columns = ["LECTURE_DATE", "LECTURE_ID", "START_TIME", "END_TIME", "SUBJECT", "ATTENDANCE_STATUS", "ATTENDANCE_TIME"]
            elif mode == "class":
                if not all((department, year, class_name)):
                    abort(400, description="Department, year and class/section are required for a class report.")
                rows = _db(app).class_day_report(day, department, year, class_name)
                columns = ["LECTURE_DATE", "DEPARTMENT", "YEAR", "CLASS_NAME", "LECTURE_ID", "START_TIME", "END_TIME", "SUBJECT", "ROLLNO", "NAME", "ATTENDANCE_STATUS", "ATTENDANCE_TIME"]
            else:
                rows = _db(app).list_attendance(day, department or None, year or None, class_name or None)
                columns = ["DATE", "DEPARTMENT", "YEAR", "CLASS_NAME", "LECTURE_ID", "SUBJECT", "ROLLNO", "NAME", "STATUS"]
        except ValueError as exc:
            abort(400, description=str(exc))
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([
                display_year(row.get(key, "")) if key == "YEAR" and row.get(key, "") else (row.get(key, "") or "")
                for key in columns
            ])
        output.seek(0)
        return Response(
            "\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="attendance-{mode}-{day.isoformat()}.csv"'},
        )

    @app.get("/api/scans/cursor")
    def scan_cursor():
        with _db(app)._session() as con:
            row = con.execute("SELECT COALESCE(MAX(ID), 0) AS MAX_ID FROM web_scan_events").fetchone()
        return jsonify({"cursor": int(row["MAX_ID"])})

    @app.get("/api/scans")
    def get_scans():
        try:
            after_id = max(0, int(request.args.get("after_id", "0")))
        except ValueError:
            return jsonify({"error": "after_id must be an integer"}), 400
        with _db(app)._session() as con:
            events = con.execute(
                "SELECT ID, UID, CREATED_AT, STATUS, MESSAGE, ROLLNO, LECTURE_ID FROM web_scan_events WHERE ID>? ORDER BY ID LIMIT 25",
                (after_id,),
            ).fetchall()
        out = []
        for event in events:
            item = dict(event)
            student = _db(app).get_student_by_tag(item["UID"])
            item["student"] = student
            out.append(item)
        return jsonify({"events": out})

    @app.post("/api/hardware/scan")
    def hardware_scan():
        sent_token = request.headers.get("X-Bridge-Token", "")
        if not sent_token or not hmac.compare_digest(sent_token, str(app.config["BRIDGE_API_TOKEN"])):
            return jsonify({"error": "Invalid bridge token"}), 401
        payload = request.get_json(silent=True) or {}
        candidate = str(payload.get("uid", "")).strip()
        label = UID_LABEL_RE.search(candidate)
        if label:
            candidate = label.group(1).strip()
        try:
            uid = normalize_uid(candidate)
        except ValueError:
            return jsonify({"error": "UID must be hexadecimal, optionally separated by colons or dashes."}), 400
        if not uid:
            return jsonify({"error": "UID is required."}), 400
        now = local_now(app)
        now_text = now.isoformat(sep=" ", timespec="seconds")
        with _db(app)._session() as con:
            latest = con.execute(
                "SELECT ID FROM web_scan_events WHERE UID=? AND CREATED_AT>=? ORDER BY ID DESC LIMIT 1",
                (uid, (now - timedelta(seconds=2)).isoformat(sep=" ", timespec="seconds")),
            ).fetchone()
            if latest:
                return jsonify({"accepted": True, "duplicate": True, "scan_id": int(latest["ID"])})
            student = _db(app).get_student_by_tag(uid)
            status = "pending" if student else "unknown_tag"
            message = "" if student else "This NFC tag is not assigned to a registered student."
            cursor = con.execute(
                "INSERT INTO web_scan_events (UID, CREATED_AT, STATUS, MESSAGE, ROLLNO) VALUES (?, ?, ?, ?, ?)",
                (uid, now_text, status, message, student["ROLLNO"] if student else None),
            )
            scan_id = int(cursor.lastrowid)
        return jsonify({"accepted": True, "duplicate": False, "scan_id": scan_id, "status": status}), 201

    @app.post("/api/students")
    def api_create_student():
        payload = request.get_json(silent=True) or {}
        roll_no = str(payload.get("roll_no", "")).strip()
        name = str(payload.get("name", "")).strip()
        department = str(payload.get("department", "")).strip()
        year = str(payload.get("year", "")).strip()
        class_name = str(payload.get("class_name", "")).strip()
        uid = str(payload.get("uid", "")).strip()
        images = payload.get("photos", [])
        if not ROLL_RE.fullmatch(roll_no):
            return jsonify({"error": "Roll number is required and may contain letters, digits, dots, underscores and dashes only (max 50 characters)."}), 400
        if not all((name, department, year, class_name, uid)):
            return jsonify({"error": "Name, department, year, class/section and scanned NFC UID are required."}), 400
        try:
            year = normalize_year(year)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not isinstance(images, list) or len(images) < 3 or len(images) > 8:
            return jsonify({"error": "Capture between 3 and 8 face photos before enrolling."}), 400
        try:
            uid = normalize_uid(uid)
        except ValueError:
            return jsonify({"error": "The NFC UID is invalid. Scan the card again."}), 400
        photo_root = Path(app.config["PHOTOS_DIR"]).resolve()
        target_dir = photo_root / roll_no
        if target_dir.exists():
            return jsonify({"error": f"A photo folder already exists for {roll_no}. Check for an existing enrollment before retrying."}), 409
        staging_root = photo_root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix="enroll-", dir=staging_root))
        try:
            decoded_images = [_decode_photo(item) for item in images]
            _validate_face_images(decoded_images)
            for index, (raw, canonical) in enumerate(decoded_images, start=1):
                (stage_dir / f"face_{index:02d}.jpg").write_bytes(canonical)
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            stage_dir.rename(target_dir)
            try:
                _db(app).register_student_with_tag(roll_no, name, uid, department, year, class_name)
            except Exception:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise
        except ValueError as exc:
            shutil.rmtree(stage_dir, ignore_errors=True)
            if target_dir.exists() and not _db(app).get_student(roll_no):
                shutil.rmtree(target_dir, ignore_errors=True)
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            shutil.rmtree(stage_dir, ignore_errors=True)
            if target_dir.exists() and not _db(app).get_student(roll_no):
                shutil.rmtree(target_dir, ignore_errors=True)
            return jsonify({"error": str(exc)}), 503
        except Exception as exc:
            shutil.rmtree(stage_dir, ignore_errors=True)
            if target_dir.exists() and not _db(app).get_student(roll_no):
                shutil.rmtree(target_dir, ignore_errors=True)
            app.logger.exception("Student enrollment failed")
            return jsonify({"error": str(exc) or "Enrollment failed."}), 409
        return jsonify({"ok": True, "message": f"Enrolled {name} ({roll_no}) and saved {len(images)} face photos.", "roll_no": roll_no}), 201

    @app.post("/api/lectures")
    def api_create_lecture():
        payload = request.get_json(silent=True) or {}
        try:
            lecture_id = _db(app).create_lecture(
                payload.get("date", ""), payload.get("department", ""), normalize_year(payload.get("year", "")),
                payload.get("class_name", ""), payload.get("subject", ""),
                payload.get("start_time", ""), payload.get("end_time", ""),
            )
            return jsonify({"ok": True, "lecture_id": lecture_id, "message": "Lecture created."}), 201
        except (ValueError, sqlite3.IntegrityError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/verify")
    def api_verify():
        payload = request.get_json(silent=True) or {}
        try:
            scan_id = int(payload.get("scan_id"))
            lecture_id = int(payload.get("lecture_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "Choose a scheduled lecture and scan an NFC card first."}), 400
        frames = payload.get("frames")
        if not isinstance(frames, list) or len(frames) < 2 or len(frames) > 5:
            return jsonify({"error": "Capture 3 clear camera frames for verification."}), 400
        with _db(app)._session() as con:
            event_row = con.execute("SELECT * FROM web_scan_events WHERE ID=?", (scan_id,)).fetchone()
        if not event_row:
            return jsonify({"error": "Scan event not found. Scan the card again."}), 404
        event = dict(event_row)
        if event["STATUS"] != "pending":
            return jsonify({"error": event["MESSAGE"] or f"This scan has already been processed ({event['STATUS']})."}), 409
        try:
            scanned_at = datetime.fromisoformat(event["CREATED_AT"])
        except ValueError:
            scanned_at = local_now(app)
        if (local_now(app) - scanned_at).total_seconds() > 180:
            _set_scan_status(_db(app), scan_id, "expired", "Scan expired; present the NFC card again.")
            return jsonify({"error": "That scan expired. Present the NFC card again."}), 409
        student = _db(app).get_student_by_tag(event["UID"])
        if not student:
            _set_scan_status(_db(app), scan_id, "unknown_tag", "NFC tag is not assigned to a student.")
            return jsonify({"error": "This NFC tag is not assigned to a student."}), 404
        lecture = _db(app).get_lecture(lecture_id)
        if not lecture:
            return jsonify({"error": "Selected lecture no longer exists. Refresh the page."}), 404
        if lecture["LECTURE_DATE"] != local_now(app).date().isoformat():
            return jsonify({"error": "Attendance can only be checked into a lecture scheduled for today."}), 400
        if not _student_matches_lecture(student, lecture):
            _set_scan_status(_db(app), scan_id, "class_mismatch", "Student does not belong to the selected lecture's class.")
            return jsonify({"error": f"{student['ROLLNO']} belongs to {student['DEPARTMENT']} / {display_year(student['YEAR'])} / {student['CLASS_NAME']}, not this lecture's class."}), 400
        try:
            matches, details = _verify_face_frames(
                roll_no=student["ROLLNO"], student_name=student["NAME"], frames=frames,
                photos_dir=Path(app.config["PHOTOS_DIR"]), tolerance=float(app.config["FACE_TOLERANCE"]),
            )
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if matches < 2:
            # Keep the recent scan pending so the operator can retry without rescanning.
            _set_scan_status(_db(app), scan_id, "pending", "Face verification failed. Improve lighting and retry.")
            return jsonify({"ok": False, "status": "face_failed", "error": "Face verification failed. Improve lighting, face the camera and retry.", "debug": details}), 403
        try:
            result = _db(app).mark_attendance(student, at=local_now(app), lecture_id=lecture_id)
        except ValueError as exc:
            _set_scan_status(_db(app), scan_id, "rejected", str(exc))
            return jsonify({"error": str(exc)}), 400
        status = "verified" if result == "marked" else "already_present"
        message = (f"Attendance marked for {student['NAME']} ({student['ROLLNO']})." if result == "marked"
                   else f"{student['NAME']} is already marked present for this lecture.")
        with _db(app)._session() as con:
            con.execute(
                "UPDATE web_scan_events SET STATUS=?, MESSAGE=?, ROLLNO=?, LECTURE_ID=? WHERE ID=?",
                (status, message, student["ROLLNO"], lecture_id, scan_id),
            )
        return jsonify({"ok": True, "status": status, "message": message, "student": student, "lecture": lecture})

    return app


def _set_scan_status(db: Database, scan_id: int, status: str, message: str) -> None:
    with db._session() as con:
        con.execute("UPDATE web_scan_events SET STATUS=?, MESSAGE=? WHERE ID=?", (status, message, scan_id))


def _student_matches_lecture(student: dict[str, Any], lecture: dict[str, Any]) -> bool:
    for field in ("DEPARTMENT", "YEAR", "CLASS_NAME"):
        left = str(student.get(field, "")).strip().casefold()
        right = str(lecture.get(field, "")).strip().casefold()
        if field == "YEAR":
            try:
                left, right = normalize_year(left), normalize_year(right)
            except ValueError:
                pass
        if left != right:
            return False
    return True


def _decode_photo(data_url: Any) -> tuple[bytes, bytes]:
    if not isinstance(data_url, str):
        raise ValueError("Every captured image must be an image data URL.")
    match = IMAGE_DATA_RE.match(data_url)
    if not match:
        raise ValueError("Captured photo format is invalid. Use the in-app camera capture button.")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise ValueError("Could not decode a captured photo.") from exc
    if len(raw) > 2_500_000:
        raise ValueError("A face photo is too large. Capture at the default camera resolution.")
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(raw)).convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=92, optimize=True)
        canonical = output.getvalue()
    except Exception as exc:
        raise ValueError("Captured photo could not be read. Enable camera access and capture again.") from exc
    return raw, canonical


def _validate_face_images(images: list[tuple[bytes, bytes]]) -> None:
    try:
        import face_recognition
    except ImportError as exc:
        raise RuntimeError("Face verification is unavailable in the web container. Rebuild the Docker image with its dependencies.") from exc
    for index, (_raw, canonical) in enumerate(images, start=1):
        array = face_recognition.load_image_file(io.BytesIO(canonical))
        locations = face_recognition.face_locations(array, model="hog")
        if len(locations) != 1:
            raise ValueError(f"Photo {index}: detected {len(locations)} faces. Capture an image with exactly one clear face.")
        encodings = face_recognition.face_encodings(array, known_face_locations=locations, model="small")
        if len(encodings) != 1:
            raise ValueError(f"Photo {index}: the face could not be encoded. Improve lighting and capture again.")


def _decode_frame(data_url: Any) -> bytes:
    if not isinstance(data_url, str):
        raise ValueError("Invalid camera frame.")
    match = IMAGE_DATA_RE.match(data_url)
    if not match:
        raise ValueError("Invalid camera frame format.")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise ValueError("Unable to decode camera frame.") from exc
    if not raw or len(raw) > 1_500_000:
        raise ValueError("Camera frame is empty or too large.")
    return raw


def _verify_face_frames(
    *, roll_no: str, student_name: str, frames: list[Any], photos_dir: Path, tolerance: float
) -> tuple[int, dict[str, Any]]:
    try:
        import face_recognition
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Face verification dependencies are missing from the app container.") from exc
    student_dir = photos_dir / roll_no
    reference_files = sorted(
        [p for p in student_dir.glob("*") if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}]
    ) if student_dir.is_dir() else []
    if not reference_files:
        expected = re.sub(r"\s+", " ", student_name.replace("_", " ")).strip().casefold()
        reference_files = [
            path for path in photos_dir.iterdir() if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
            and re.sub(r"\s+", " ", path.stem.replace("_", " ")).strip().casefold() == expected
        ]
    cache_key = (str(photos_dir.resolve()), roll_no.casefold())
    try:
        fingerprint = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in reference_files)
    except OSError as exc:
        raise RuntimeError(f"Unable to read reference photos for {roll_no}: {exc}") from exc
    with _FACE_CACHE_LOCK:
        cached = _FACE_CACHE.get(cache_key)
        reference_encodings = cached[1] if cached and cached[0] == fingerprint else None
    if reference_encodings is None:
        computed = []
        for path in reference_files:
            try:
                image = face_recognition.load_image_file(str(path))
                encodings = face_recognition.face_encodings(image, model="small")
                if len(encodings) == 1:
                    computed.append(encodings[0])
            except Exception:
                app_log = logging.getLogger("attendance.faces")
                app_log.warning("Skipping unreadable reference photo %s", path, exc_info=True)
        reference_encodings = computed
        with _FACE_CACHE_LOCK:
            if len(_FACE_CACHE) >= 256 and cache_key not in _FACE_CACHE:
                _FACE_CACHE.pop(next(iter(_FACE_CACHE)))
            _FACE_CACHE[cache_key] = (fingerprint, reference_encodings)
    if not reference_encodings:
        raise RuntimeError(f"No usable reference photos found for {roll_no}. Re-enroll the student with clear face photos.")
    matches = 0
    distances: list[float] = []
    for frame in frames:
        raw = _decode_frame(frame)
        image = face_recognition.load_image_file(io.BytesIO(raw))
        locations = face_recognition.face_locations(image, model="hog")
        if len(locations) != 1:
            distances.append(1.0)
            continue
        encodings = face_recognition.face_encodings(image, known_face_locations=locations, model="small")
        if len(encodings) != 1:
            distances.append(1.0)
            continue
        scores = face_recognition.face_distance(reference_encodings, encodings[0])
        best = float(np.min(scores)) if len(scores) else 1.0
        distances.append(best)
        if best <= tolerance:
            matches += 1
    return matches, {"matched_frames": matches, "total_frames": len(frames), "distances": [round(v, 3) for v in distances]}


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
