# Attendly — NFC + Facial Attendance Web App

A local-first browser dashboard for managing students, NFC card assignments, face references, lecture schedules and lecture-wise attendance. The app uses SQLite with persistent Docker storage. A small Python hardware bridge runs on the computer connected to the Arduino RC522 reader and forwards NFC UIDs to the web app over localhost.

## What is included

- Admin sign-in for the local dashboard.
- Student enrollment in the browser: student metadata, scan an NFC UID from the hardware bridge, capture three or more webcam photos, validate that each image contains one face, and save all enrollment data together.
- Student directory with department/year/class-section editing.
- Year selectors use **First Year**, **Second Year**, **Third Year**, and **Fourth Year** throughout enrollment, class scheduling, filters, and reports. Existing numeric database values (`1`–`4`) are retained internally so prior records remain compatible.
- Lecture scheduling by date, department, year, class/section, subject and time.
- Live NFC kiosk: scan tag, resolve student, capture three browser-camera frames, verify against the enrolled references in the web service, and mark attendance for a selected lecture.
- Duplicate scan suppression and one check-in per student per lecture.
- Individual daily timetable/status report and complete class roster report with Present/Absent per lecture.
- Recorded check-in search and CSV export for Excel.
- Docker Compose, health check, restart policy, SQLite persistence, and health endpoint.
- Existing CLI tools remain available under `Project/main.py`; the web application does not replace the legacy scripts.

## Architecture

```text
Arduino RC522 --USB serial--> hardware_bridge.py (host PC)
                                    |
                                    | POST UID / shared bridge token
                                    v
                              Docker web app
                         Flask + existing SQLite layer
                          |                     |
                       SQLite             Face verification
                      ./data volume       (reference images)
                          |
                   Browser dashboard <--> webcam/canvas frames
```

The bridge is intentionally **not** inside the Linux container: Windows Docker Desktop cannot reliably expose a laptop's USB COM port as a standard Linux serial device. Webcam access stays in the browser; the server only receives still frames needed for face verification.

## Prerequisites

- Docker Desktop with Docker Compose v2.
- Arduino IDE + a working RC522 sketch that prints a line such as `UID: 045851EA0B1390` at 9600 baud.
- Python 3.10+ on the PC attached to the Arduino (Python 3.12 is recommended) for the hardware bridge only.
- Google Chrome or Microsoft Edge for camera permissions on `http://localhost`.

The web service and face-recognition native dependencies run inside Docker. The host bridge only needs `pyserial` and does **not** need dlib, Visual Studio C++ Build Tools, OpenCV or the face-recognition model package.

## First run (Windows 11 / PowerShell)

1. Extract this project to a short local path, e.g. `E:\Attendly`.
2. In the project root, copy the example environment file:

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

3. Replace `ADMIN_PASSWORD`, `SECRET_KEY`, and `BRIDGE_API_TOKEN` with your own long random values. Keep the bridge token private; use the same token for the host bridge. Each teammate can choose their own values.
4. Start the web service:

   ```powershell
   docker compose up --build -d
   docker compose logs -f attendance-web
   ```

   The first image build downloads the face model data and compiles `dlib` inside Linux, so the first build is significantly slower than later starts.
5. Open **http://localhost:8000** and sign in using the `ADMIN_USERNAME` / `ADMIN_PASSWORD` values in `.env`.
6. In a **second** PowerShell terminal, install the lightweight serial dependency and start the bridge. Replace `COM7` if Windows assigned a different port; copy the token value from `.env`:

   ```powershell
   py -3.12 -m venv .bridge-venv
   .\.bridge-venv\Scripts\Activate.ps1
   python -m pip install -r requirements-bridge.txt
   python hardware_bridge.py --port COM7 --baud-rate 9600 --url http://127.0.0.1:8000
   ```

   The bridge reads `BRIDGE_API_TOKEN` from the project `.env` automatically. Keep this second terminal open during attendance. Close Arduino IDE's Serial Monitor first so the bridge can exclusively open COM7. To stop the bridge, press `Ctrl+C`.
7. In Attendly, enroll students, schedule a lecture for today, select it on the dashboard, click **Start kiosk**, allow browser camera access, then scan a student's card and click **Verify face & mark present**.

## Data location and migration

Runtime data is deliberately excluded from the source ZIP and Git:

- `data/main.db` — SQLite database containing students, tags, lectures, attendance and scan events.
- `data/Photos/<ROLLNO>/face_01.jpg` etc. — biometric reference images.

The `./data` folder is mounted into `/app/data` inside the container. Rebuilding or replacing the Docker image does not remove this folder. Back up `data/main.db` and `data/Photos` regularly. Face reference images are sensitive biometric data; restrict file access and do not commit or casually share them.

### Migrate an existing CLI installation

**Stop the old listener first and back up the old database and images.** From the web-app project root, copy your existing files into the web app's `data` folder before the first `docker compose up`:

```powershell
New-Item -ItemType Directory -Force .\data\Photos | Out-Null
Copy-Item "E:\NFC-and-Facial-based-Attendance-System-main\Project\main.db" ".\data\main.db"
Copy-Item "E:\NFC-and-Facial-based-Attendance-System-main\Project\Photos\*" ".\data\Photos" -Recurse -Force
```

Change the source path to the real old project folder. If `data/main.db` already contains data, stop and back it up before copying over it. On startup, schema updates are additive; they do not drop old records. Old attendance rows with no lecture ID remain in the check-ins report, but the app cannot infer which scheduled lecture those historical rows belonged to.

For a teammate starting from scratch, do not copy anyone else's database or face photos; simply start the app and create their own roster. To hand off an existing roster, transfer the SQLite database and photo folder through a controlled, private channel.

## Live kiosk behaviour

1. Create today's lecture under **Lecture schedule** (department, year and class must exactly match students' roster metadata).
2. Start `hardware_bridge.py` on the computer attached to the reader.
3. Open the dashboard; choose the live lecture and press **Start kiosk**. Approve webcam access.
4. Scan a registered card. The student's record appears in the kiosk panel.
5. Ask the student to face the camera and press **Verify face & mark present**. Three frames are sent to the local web service; at least two must match an enrolled reference within `FACE_TOLERANCE`.
6. The attendance record includes the scan time and selected `LECTURE_ID`. Repeated scans for the same student/lecture do not create another check-in.

If face matching fails, improve lighting and recapture reference images rather than immediately loosening the threshold. There is no liveness detection in this version, so do not use it as high-assurance identity proof.

## Reports

- **Attendance reports → Class roster**: choose date, department, year, and class/section (e.g. `IT-B`) to list every scheduled lecture for every student, including absences.
- **Student's full day**: switch report type and enter their roll number to list the day's scheduled lectures and check-in time per lecture.
- **Recorded check-ins**: view only the records that were actually written.
- Select **Export CSV** to download a UTF-8 CSV that Excel can open.

Absent is derived from a student roster joined against a scheduled lecture. Therefore, absence reporting is accurate only when the lecture schedule and student class metadata are set up correctly.

## Configuration

| Variable | Purpose |
|---|---|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Dashboard login credentials |
| `SECRET_KEY` | Flask session signing key; set a random secret |
| `BRIDGE_API_TOKEN` | Shared token for host-to-web serial bridge API |
| `APP_TIMEZONE` | Date/time used for attendance; defaults to `Asia/Kolkata` |
| `FACE_TOLERANCE` | Face distance threshold; default `0.48` |

The service binds to `127.0.0.1:8000` by default through Compose, so it is local to that PC. To share a single server with other machines, intentionally change the port binding and put it behind HTTPS/authenticated network access; browsers generally require HTTPS for webcam use on non-localhost hosts. Do not expose the unauthenticated bridge port/token or biometric data to the public internet.

## Troubleshooting

- **`port is busy` / COM access denied:** close Arduino Serial Monitor and any other tool using COM7. Restart the bridge.
- **Bridge gets HTTP 401:** make sure its `BRIDGE_API_TOKEN` exactly matches the value in `.env`; restart Compose after changing `.env`.
- **Dashboard receives no scan:** verify that the bridge logs `Tag <UID> forwarded`, that Docker is healthy, and that the UID is printed on its own serial line.
- **Camera cannot start:** use Chrome/Edge at `http://localhost:8000`, allow the camera, and close Zoom/Teams/other camera apps.
- **No reference photos:** check `data/Photos/<ROLLNO>/` and ensure each student's saved images contain one clear face.
- **Container startup/build fails:** inspect `docker compose logs --tail=200 attendance-web`; `docker compose up --build` rebuilds the image.
- **Check service health:** browse to `http://localhost:8000/healthz`.

## Development checks

The web routes, bridge UID parser, and old SQLite functions can be exercised using the included tests. The web image has a health check. To stop the stack without deleting saved data:

```powershell
docker compose down
```

Do not run `docker compose down -v` if you want to preserve named volumes; this project uses a bind-mounted `./data` directory which you should back up directly.
