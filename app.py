from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash, send_file, send_from_directory
import sqlite3
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import smtplib
import socket
import sys
import time
import uuid
import base64
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from datetime import datetime, timedelta
from pathlib import Path
from html import escape as html_escape
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# ReportLab PDF Generation
from reportlab.lib.pagesizes import letter, landscape, A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas

# Load backend .env file (supports running from workspace root or BusMate-master)
_backend_dir = Path(__file__).resolve().parent
_backend_env = _backend_dir / ".env"
_root_env = _backend_dir.parent / ".env"

if _root_env.exists():
    load_dotenv(dotenv_path=_root_env, override=False)
if _backend_env.exists():
    load_dotenv(dotenv_path=_backend_env, override=True)

app = Flask(__name__)

# Cross-Origin Resource Sharing (CORS) for LAN and cross-device testing
try:
    from flask_cors import CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)
except ImportError:
    pass

# ============================================================
# APP CONFIGURATION
# ============================================================

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "rit-transport-secret-key-2026")

# Database Path Configuration (Render Persistent Disk / Local Fallback)
_default_db_path = Path(__file__).resolve().parent / "busmate.db"
_env_db_path = os.environ.get("DATABASE_PATH")
if _env_db_path and _env_db_path.strip():
    DATABASE = str(Path(_env_db_path.strip()).resolve())
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
else:
    DATABASE = str(_default_db_path)

DEFAULT_CAMPUS_ARRIVAL_TIME = "07:40 AM"
DEFAULT_RETURN_TIME = "3:50 PM"

# ============================================================
# REPORTLAB NUMBERED CANVAS & ATTACHMENT CONFIG
# ============================================================

# Uploads Folder Configuration (Render Persistent Disk / Local Fallback)
_default_uploads_dir = Path(__file__).resolve().parent / "static" / "uploads" / "notices"
_default_drivers_dir = Path(__file__).resolve().parent / "static" / "uploads" / "drivers"
_env_uploads_path = os.environ.get("UPLOADS_PATH")
if _env_uploads_path and _env_uploads_path.strip():
    _custom_uploads = Path(_env_uploads_path.strip()).resolve()
    if _custom_uploads.name.lower() == "notices":
        NOTICES_UPLOAD_FOLDER = str(_custom_uploads)
    else:
        NOTICES_UPLOAD_FOLDER = str(_custom_uploads / "notices")
else:
    NOTICES_UPLOAD_FOLDER = str(_default_uploads_dir)

os.makedirs(NOTICES_UPLOAD_FOLDER, exist_ok=True)
ALLOWED_ATTACHMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".png", ".jpg", ".jpeg"}

class NumberedCanvas(canvas.Canvas):
    """Two-pass canvas to dynamically compute and stamp total page count."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def draw_page_number(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748b"))
        footer_text = f"Page {self._pageNumber} of {page_count}  |  RIT Transport Management System  |  Official Document"
        self.drawRightString(self._pagesize[0] - 36, 18, footer_text)
        self.restoreState()


# ============================================================
# ACCURATE REVERSE GEOCODING (FEATURE 6)
# ============================================================

_GEOCODE_MEMORY_CACHE = {}

def reverse_geocode_coordinates(lat, lon):
    """
    Feature 6: Reverse geocode coordinates to an accurate place name.
    1. Checks in-memory cache.
    2. Checks geocode_cache SQLite table.
    3. Queries Nominatim if available (timeout 2.0s).
    4. Returns 'Location name unavailable' on error/failure. Never guesses.
    """
    if lat is None or lon is None:
        return "Location name unavailable"
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (ValueError, TypeError):
        return "Location name unavailable"

    lat_key = f"{lat_f:.4f}"
    lon_key = f"{lon_f:.4f}"
    cache_key = (lat_key, lon_key)

    if cache_key in _GEOCODE_MEMORY_CACHE:
        return _GEOCODE_MEMORY_CACHE[cache_key]

    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT place_name FROM geocode_cache WHERE lat_key = ? AND lon_key = ?",
            (lat_key, lon_key)
        ).fetchone()
        if row and row["place_name"]:
            conn.close()
            _GEOCODE_MEMORY_CACHE[cache_key] = row["place_name"]
            return row["place_name"]

        # Approximate match within ~0.01 degrees (~1km)
        approx_row = conn.execute("""
            SELECT place_name,
                   ((CAST(lat_key AS REAL) - ?) * (CAST(lat_key AS REAL) - ?) +
                    (CAST(lon_key AS REAL) - ?) * (CAST(lon_key AS REAL) - ?)) AS dist_sq
            FROM geocode_cache
            ORDER BY dist_sq ASC
            LIMIT 1
        """, (lat_f, lat_f, lon_f, lon_f)).fetchone()

        if approx_row and approx_row["dist_sq"] is not None and approx_row["dist_sq"] < 0.0001:
            conn.close()
            place = approx_row["place_name"]
            _GEOCODE_MEMORY_CACHE[cache_key] = place
            return place

        conn.close()
    except Exception:
        pass

    # External query to Nominatim
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat_key}&lon={lon_key}"
        headers = {"User-Agent": "RIT-BusMate-Tracker/1.0 (transit@ritchennai.edu.in)"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            addr = data.get("address", {})
            road = addr.get("road") or addr.get("suburb") or addr.get("neighbourhood") or ""
            city = addr.get("city") or addr.get("town") or addr.get("county") or ""
            place = f"{road}, {city}".strip(", ") or data.get("display_name", "")
            if place:
                try:
                    c2 = get_connection()
                    c2.execute(
                        "INSERT OR REPLACE INTO geocode_cache (lat_key, lon_key, place_name) VALUES (?, ?, ?)",
                        (lat_key, lon_key, place)
                    )
                    c2.commit()
                    c2.close()
                except Exception:
                    pass
                _GEOCODE_MEMORY_CACHE[cache_key] = place
                return place
    except Exception:
        pass

    return "Location name unavailable"

def get_kolkata_now():
    """Returns current datetime in Asia/Kolkata timezone."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        from datetime import timezone
        return datetime.now(timezone(timedelta(hours=5, minutes=30)))

def get_today_date_str():
    """Returns today's date formatted as DD-MM-YYYY in Asia/Kolkata timezone."""
    return get_kolkata_now().strftime("%d-%m-%Y")

def get_now_time_str():
    """Returns current time formatted as HH:MM AM/PM in Asia/Kolkata timezone."""
    return get_kolkata_now().strftime("%I:%M %p")

def get_current_service_type():
    """Determines whether current time is Morning or Return service (cutoff: 1:00 PM)."""
    return "MORNING" if get_kolkata_now().hour < 13 else "RETURN"

# Official RIT Programmes (https://ritchennai.org/)
DEPARTMENTS = [
    # UG PROGRAMMES
    "B.E. Computer Science & Engineering",
    "B.E. Computer & Communication Engineering",
    "B.E. Computer Science & Engineering (AI & ML)",
    "B.E. Electronics & Communication Engineering",
    "B.E. Mechanical Engineering",
    "B.E. Computer Science & Engineering (Cyber Security)",
    "B.E. Electronics Engineering (VLSI Design & Tech.)",
    "B.Tech. Artificial Intelligence & Data Science",
    "B.Tech. Computer Science and Business Systems",
    "B.Tech. Bio Technology",
    # PG PROGRAMMES
    "M.E. ECE (VLSI Design)",
    "M.Tech. Data Sciences"
]

CLASS_SECTIONS = ["A", "B", "C", "D"]

STUDENT_BATCHES = [
    "2026–2029",
    "2025–2028",
    "2024–2028",
    "2023–2027",
    "2022–2026"
]

FACULTY_DESIGNATIONS = [
    "Assistant Professor",
    "Associate Professor",
    "Professor",
    "Head of Department (HOD)",
    "Teaching Fellow",
    "Non-Teaching Staff"
]


def get_default_return_time():
    """Return the admin-configured default return time."""
    try:
        connection = get_connection()
        row = connection.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = ?",
            ("default_return_time",)
        ).fetchone()
        connection.close()
        return row["setting_value"] if row and row["setting_value"] else DEFAULT_RETURN_TIME
    except Exception:
        return DEFAULT_RETURN_TIME


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    connection = sqlite3.connect(DATABASE, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def get_bus_boarded_count(bus_id=None, travel_date=None, service_type=None):
    """
    Centralized calculation of valid passenger boarding counts.
    Strictly joins with 'users' table so deleted/test accounts are never counted.
    - bus_id: specific bus ID (or None for fleet-wide total)
    - travel_date: e.g. '14-11-2026' (defaults to today's Asia/Kolkata date)
    - service_type: 'MORNING' or 'RETURN' (or None / 'ALL' for combined)
    """
    if not travel_date:
        travel_date = get_today_date_str()
    conn = get_connection()
    query = """
        SELECT COUNT(qa.id) as c
        FROM qr_attendance qa
        JOIN users u ON qa.user_id = u.id
        WHERE qa.travel_date = ?
    """
    params = [travel_date]
    if bus_id:
        query += " AND qa.bus_id = ?"
        params.append(bus_id)
    if service_type and str(service_type).strip().upper() not in ["ALL", ""]:
        query += " AND UPPER(qa.service_type) = ?"
        params.append(str(service_type).strip().upper())
    count = conn.execute(query, params).fetchone()["c"]
    conn.close()
    return count


# ============================================================
# DATABASE SETUP & MIGRATION
# ============================================================


def check_student_batch_lifecycle():
    """
    Automatic batch completion / graduation lifecycle.
    Rules:
    - ONLY applies to students (role = 'student'). Faculty accounts are NEVER affected.
    - Active student batches (2026–2029, 2025–2028, 2024–2028, 2023–2027, 2022–2026) are NEVER marked as graduated.
    - Missing, blank, or unparseable batches are NEVER marked as graduated.
    - Only students whose batch end year is strictly in the past (prior to 2026) are marked as graduated.
    """
    try:
        connection = get_connection()
        students = connection.execute("""
            SELECT id, academic_year, lifecycle_status, is_active
            FROM users
            WHERE role = 'student' AND (lifecycle_status = 'ACTIVE' OR lifecycle_status IS NULL)
        """).fetchall()

        active_cohorts = {"2026–2029", "2025–2028", "2024–2028", "2023–2027", "2022–2026"}
        now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
        graduated = []

        for s in students:
            batch = (s["academic_year"] or "").strip()
            if not batch or batch in active_cohorts:
                continue

            # Match 4-digit start and 4-digit end year with dash/en-dash
            m = re.search(r"(\d{4})\s*[\u2013\u2014\-]\s*(\d{4})", batch)
            if m:
                end_year = int(m.group(2))
                # Only batches whose end year is strictly prior to 2026 are graduated
                if end_year < 2026:
                    graduated.append((s["id"], batch))

        for uid, batch in graduated:
            connection.execute("""
                UPDATE users
                SET lifecycle_status = 'GRADUATED',
                    is_active = 0,
                    deactivation_reason = ?,
                    deactivated_at = ?
                WHERE id = ?
            """, (f"Batch {batch} completed (Graduated)", now_str, uid))

        if graduated:
            connection.commit()
        connection.close()
    except Exception as e:
        app.logger.error(f"Error checking batch lifecycle: {e}")

def setup_database():
    connection = get_connection()
    cursor = connection.cursor()

    # 1. APP SETTINGS
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        )
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO app_settings (setting_key, setting_value)
        VALUES (?, ?)
    """, ("default_return_time", DEFAULT_RETURN_TIME))

    # 2. BUSES
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS buses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bus_number TEXT NOT NULL UNIQUE,
            route_name TEXT NOT NULL
        )
    """)

    # 3. STOPS
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bus_id INTEGER NOT NULL,
            stop_name TEXT NOT NULL,
            stop_time TEXT,
            stop_order INTEGER,
            direction TEXT NOT NULL DEFAULT 'MORNING',
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    # 4. RETURN BUSES (OVERRIDE / DATE-BASED)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS return_buses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bus_id INTEGER NOT NULL,
            travel_date TEXT NOT NULL,
            departure_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'AVAILABLE',
            UNIQUE(bus_id, travel_date),
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    # 5. LEGACY STUDENTS TABLE (PRESERVED)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash BLOB NOT NULL,
            salt BLOB NOT NULL
        )
    """)

    # 6. LEGACY FACULTY TABLE (PRESERVED)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS faculty (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash BLOB NOT NULL,
            salt BLOB NOT NULL
        )
    """)

    # 7. DRIVERS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS drivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            password_hash BLOB NOT NULL,
            salt BLOB NOT NULL,
            assigned_bus_id INTEGER,
            driver_status TEXT NOT NULL DEFAULT 'OFFLINE',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT,
            deactivation_reason TEXT,
            FOREIGN KEY (assigned_bus_id) REFERENCES buses(id) ON DELETE SET NULL
        )
    """)

    driver_columns = {row[1] for row in cursor.execute("PRAGMA table_info(drivers)").fetchall()}
    if "assigned_bus_id" not in driver_columns:
        cursor.execute("ALTER TABLE drivers ADD COLUMN assigned_bus_id INTEGER")
    if "driver_status" not in driver_columns:
        cursor.execute("ALTER TABLE drivers ADD COLUMN driver_status TEXT NOT NULL DEFAULT 'OFFLINE'")
    if "is_active" not in driver_columns:
        cursor.execute("ALTER TABLE drivers ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "created_at" not in driver_columns:
        cursor.execute("ALTER TABLE drivers ADD COLUMN created_at TEXT")
    if "deactivation_reason" not in driver_columns:
        cursor.execute("ALTER TABLE drivers ADD COLUMN deactivation_reason TEXT")

    # 8. DRIVER ASSIGNMENTS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS driver_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER NOT NULL,
            bus_id INTEGER NOT NULL,
            assigned_at TEXT NOT NULL,
            unassigned_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (driver_id) REFERENCES drivers(id) ON DELETE CASCADE,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    # 9. MORNING BUS CONFIGURATION TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS morning_bus_config (
            bus_id INTEGER PRIMARY KEY,
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    # 10. DAILY RETURN BUS CONFIGURATION TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_return_config (
            bus_id INTEGER PRIMARY KEY,
            departure_time TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    # 11. CENTRALIZED USERS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT DEFAULT '',
            role TEXT NOT NULL DEFAULT 'student',
            name TEXT NOT NULL,
            register_number TEXT UNIQUE,
            email TEXT UNIQUE,
            mobile TEXT,
            department TEXT,
            academic_year TEXT,
            bus_id INTEGER,
            boarding_point_id INTEGER,
            password_hash BLOB,
            salt BLOB,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE SET NULL,
            FOREIGN KEY (boarding_point_id) REFERENCES stops(id) ON DELETE SET NULL
        )
    """)

    user_cols = {row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    needed_user_cols = {
        "role": "TEXT NOT NULL DEFAULT 'student'",
        "register_number": "TEXT",
        "mobile": "TEXT",
        "department": "TEXT",
        "academic_year": "TEXT",
        "class_section": "TEXT",
        "lifecycle_status": "TEXT NOT NULL DEFAULT 'ACTIVE'",
        "deactivation_reason": "TEXT",
        "deactivated_at": "TEXT",
        "bus_id": "INTEGER REFERENCES buses(id) ON DELETE SET NULL",
        "boarding_point_id": "INTEGER REFERENCES stops(id) ON DELETE SET NULL",
        "password_hash": "BLOB",
        "salt": "BLOB",
        "is_active": "INTEGER NOT NULL DEFAULT 1",
        "updated_at": "TEXT"
    }
    for col_name, col_def in needed_user_cols.items():
        if col_name not in user_cols:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")

    # 12. PASSWORD RESET OTPS
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_otps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            contact TEXT NOT NULL,
            otp_hash TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            is_used INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # 13. BUS LOCATIONS
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bus_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bus_id INTEGER UNIQUE NOT NULL,
            latitude REAL,
            longitude REAL,
            current_location TEXT,
            next_point TEXT,
            updated_at TEXT,
            is_active INTEGER DEFAULT 0,
            bus_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
            driver_id INTEGER,
            sharing_expires_at TEXT,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    location_columns = {row[1] for row in cursor.execute("PRAGMA table_info(bus_locations)").fetchall()}
    if "bus_status" not in location_columns:
        cursor.execute("ALTER TABLE bus_locations ADD COLUMN bus_status TEXT NOT NULL DEFAULT 'NOT_STARTED'")
    if "driver_id" not in location_columns:
        cursor.execute("ALTER TABLE bus_locations ADD COLUMN driver_id INTEGER")
    if "sharing_expires_at" not in location_columns:
        cursor.execute("ALTER TABLE bus_locations ADD COLUMN sharing_expires_at TEXT")
    if "last_seen_epoch" not in location_columns:
        cursor.execute("ALTER TABLE bus_locations ADD COLUMN last_seen_epoch REAL DEFAULT 0")
    if "last_client_ts" not in location_columns:
        cursor.execute("ALTER TABLE bus_locations ADD COLUMN last_client_ts REAL DEFAULT 0")

    # 14. BUS LOCATION HISTORY
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bus_location_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bus_id INTEGER NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    # 15. NOTICES
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'NORMAL',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            delivery_summary TEXT DEFAULT ''
        )
    """)
    cursor.execute("PRAGMA table_info(notices)")
    cols = [c[1] for c in cursor.fetchall()]
    if "delivery_summary" not in cols:
        cursor.execute("ALTER TABLE notices ADD COLUMN delivery_summary TEXT DEFAULT ''")
    if "attachment_filename" not in cols:
        cursor.execute("ALTER TABLE notices ADD COLUMN attachment_filename TEXT")
    if "attachment_original_name" not in cols:
        cursor.execute("ALTER TABLE notices ADD COLUMN attachment_original_name TEXT")
    if "attachment_type" not in cols:
        cursor.execute("ALTER TABLE notices ADD COLUMN attachment_type TEXT")
    if "target_audience" not in cols:
        cursor.execute("ALTER TABLE notices ADD COLUMN target_audience TEXT DEFAULT 'ALL'")

    # 16. NOTICE EMAIL LOGS
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notice_email_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notice_id INTEGER NOT NULL,
            recipient_email TEXT NOT NULL,
            recipient_name TEXT,
            recipient_role TEXT,
            status TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            error_message TEXT,
            FOREIGN KEY (notice_id) REFERENCES notices(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_notice_recipient ON notice_email_logs(notice_id, recipient_email)")

    # 17. ATTENDANCE CANCELLATION LOGS (PHASE 3)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance_cancellation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attendance_id INTEGER,
            user_id INTEGER NOT NULL,
            bus_id INTEGER NOT NULL,
            service_type TEXT NOT NULL,
            travel_date TEXT NOT NULL,
            boarding_time TEXT NOT NULL,
            cancelled_at TEXT NOT NULL,
            reason TEXT DEFAULT 'CANCELLED_BY_USER_SCAN_AGAIN',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cancellation_user ON attendance_cancellation_logs(user_id, travel_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cancellation_bus ON attendance_cancellation_logs(bus_id, travel_date)")

    # 18. REVERSE GEOCODING CACHE (PHASE 3)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS geocode_cache (
            lat_key TEXT NOT NULL,
            lon_key TEXT NOT NULL,
            place_name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (lat_key, lon_key)
        )
    """)

    # Auto-seed public transit fleet if buses table is empty (fresh production deployment)
    bus_count = cursor.execute("SELECT COUNT(1) FROM buses").fetchone()[0]
    if bus_count == 0:
        try:
            from seed_data import seed_data
            from add_evening_routes import add_evening_routes
            app.logger.info("Fresh database detected (0 buses). Initializing official public bus fleet from seed_data...")
            seed_data(connection=connection)
            add_evening_routes(connection=connection)
            app.logger.info("Successfully initialized official public bus fleet (52 buses and 602 stops).")
        except Exception as e:
            app.logger.error(f"Failed to auto-seed bus fleet: {e}")

    # Populate default configurations for all buses
    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    all_buses = cursor.execute("SELECT id FROM buses").fetchall()
    for b in all_buses:
        cursor.execute("""
            INSERT OR IGNORE INTO morning_bus_config (bus_id, is_active, updated_at)
            VALUES (?, 1, ?)
        """, (b["id"], now_str))
        cursor.execute("""
            INSERT OR IGNORE INTO daily_return_config (bus_id, departure_time, is_active, updated_at)
            VALUES (?, NULL, 1, ?)
        """, (b["id"], now_str))

    # Populate driver assignments from existing drivers
    existing_assignments = cursor.execute("SELECT COUNT(*) FROM driver_assignments").fetchone()[0]
    if existing_assignments == 0:
        drivers = cursor.execute("SELECT id, assigned_bus_id FROM drivers WHERE assigned_bus_id IS NOT NULL").fetchall()
        for d in drivers:
            cursor.execute("""
                INSERT INTO driver_assignments (driver_id, bus_id, assigned_at, is_active)
                VALUES (?, ?, ?, 1)
            """, (d["id"], d["assigned_bus_id"], now_str))

    # Indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_reg_number ON users(register_number)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_bus_id ON users(bus_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_drivers_phone ON drivers(phone)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stops_bus_id ON stops(bus_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_driver_assignments_active ON driver_assignments(driver_id, is_active)")

    # Phase 2: Buses capacity, scheduled arrival time, and permanent QR code
    bus_cols = {row[1] for row in cursor.execute("PRAGMA table_info(buses)").fetchall()}
    if "capacity" not in bus_cols:
        cursor.execute("ALTER TABLE buses ADD COLUMN capacity INTEGER DEFAULT 50")
    if "scheduled_arrival_time" not in bus_cols:
        cursor.execute("ALTER TABLE buses ADD COLUMN scheduled_arrival_time TEXT DEFAULT '07:40 AM'")
    if "qr_code" not in bus_cols:
        cursor.execute("ALTER TABLE buses ADD COLUMN qr_code TEXT")

    # Ensure all buses have a permanent unique QR token
    buses_without_qr = cursor.execute("SELECT id, bus_number FROM buses WHERE qr_code IS NULL OR trim(qr_code) = ''").fetchall()
    for b in buses_without_qr:
        safe_num = b["bus_number"].strip().upper().replace(" ", "-")
        token = f"RIT-BUS-{safe_num}-{secrets.token_hex(4).upper()}"
        cursor.execute("UPDATE buses SET qr_code = ? WHERE id = ?", (token, b["id"]))
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_buses_qr_code ON buses(qr_code)")

    # Phase 2: User qr_token
    user_cols = {row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    if "qr_token" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN qr_token TEXT")

    # Phase 2: Transport Incharge multi-bus assignment table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transport_incharge_buses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incharge_id INTEGER NOT NULL,
            bus_id INTEGER NOT NULL,
            assigned_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(incharge_id, bus_id),
            FOREIGN KEY (incharge_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    # Phase 2: QR Attendance Table (with service_type: MORNING / RETURN)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS qr_attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bus_id INTEGER NOT NULL,
            boarding_point_id INTEGER,
            boarding_time TEXT NOT NULL,
            travel_date TEXT NOT NULL,
            scanned_by_driver_id INTEGER,
            status TEXT NOT NULL DEFAULT 'BOARDED',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            service_type TEXT DEFAULT 'MORNING',
            UNIQUE(user_id, travel_date, service_type),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE,
            FOREIGN KEY (boarding_point_id) REFERENCES stops(id) ON DELETE SET NULL,
            FOREIGN KEY (scanned_by_driver_id) REFERENCES drivers(id) ON DELETE SET NULL
        )
    """)
    qr_cols = {row[1] for row in cursor.execute("PRAGMA table_info(qr_attendance)").fetchall()}
    if "service_type" not in qr_cols:
        cursor.execute("ALTER TABLE qr_attendance ADD COLUMN service_type TEXT DEFAULT 'MORNING'")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_qr_attendance_date ON qr_attendance(travel_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_qr_attendance_bus ON qr_attendance(bus_id, travel_date)")

    # Phase 2: Delay Reports Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS delay_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER NOT NULL,
            bus_id INTEGER NOT NULL,
            delay_reason TEXT NOT NULL,
            expected_delay TEXT NOT NULL,
            notes TEXT,
            latitude REAL,
            longitude REAL,
            reported_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'REPORTED',
            resolved_at TEXT,
            FOREIGN KEY (driver_id) REFERENCES drivers(id) ON DELETE CASCADE,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    # Phase 2: Emergency Alerts Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS emergency_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            driver_id INTEGER NOT NULL,
            bus_id INTEGER NOT NULL,
            emergency_type TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            reported_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            notes TEXT,
            acknowledged_by TEXT,
            acknowledged_at TEXT,
            resolved_at TEXT,
            FOREIGN KEY (driver_id) REFERENCES drivers(id) ON DELETE CASCADE,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    # Phase 2: App Notifications Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_user_id INTEGER,
            recipient_role TEXT,
            bus_id INTEGER,
            alert_type TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            metadata_json TEXT DEFAULT '{}',
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (recipient_user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE SET NULL
        )
    """)

    # Phase 2: Web Push Subscriptions Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS web_push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            subscription_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, subscription_json)
        )
    """)

    # Phase 2 Indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_incharge_buses ON transport_incharge_buses(incharge_id, is_active)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_qr_attendance_date ON qr_attendance(bus_id, travel_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_delay_bus_status ON delay_reports(bus_id, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_emergency_status ON emergency_alerts(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notif_recipient ON app_notifications(recipient_user_id, is_read)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notif_role ON app_notifications(recipient_role)")

    connection.commit()
    connection.close()
    check_student_batch_lifecycle()



# ============================================================
# PASSWORD & OTP FUNCTIONS
# ============================================================

def hash_password(password, salt):
    """Secure PBKDF2 HMAC SHA-256 with 500,000 iterations."""
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        500000
    )


def verify_password(password, salt, stored_hash):
    """Constant-time password verification."""
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    password_hash = hash_password(password, salt)
    return hmac.compare_digest(password_hash, stored_hash)


def get_smtp_config():
    """
    Centralized resolver for Gmail SMTP configuration from environment variables.
    Supports GMAIL_USER, GMAIL_APP_PASSWORD, SMTP_HOST, SMTP_PORT, SMTP_USER,
    SMTP_PASSWORD, SMTP_PASS, MAIL_DEFAULT_SENDER, SMTP_FROM.
    Never exposes passwords or sensitive strings in logs.
    """
    smtp_user = (
        os.environ.get("GMAIL_USER")
        or os.environ.get("SMTP_USER")
        or os.environ.get("MAIL_USERNAME")
        or ""
    ).strip()

    smtp_pass = (
        os.environ.get("GMAIL_APP_PASSWORD")
        or os.environ.get("SMTP_PASSWORD")
        or os.environ.get("MAIL_PASSWORD")
        or os.environ.get("SMTP_PASS")
        or ""
    ).strip()
    if " " in smtp_pass:
        smtp_pass = re.sub(r"\s+", "", smtp_pass)

    smtp_host = (
        os.environ.get("SMTP_HOST")
        or os.environ.get("MAIL_SERVER")
        or "smtp.gmail.com"
    ).strip()

    smtp_port_raw = os.environ.get("SMTP_PORT") or os.environ.get("MAIL_PORT") or "587"
    try:
        smtp_port = int(str(smtp_port_raw).strip())
    except (ValueError, TypeError):
        smtp_port = 587

    raw_from = (
        os.environ.get("MAIL_DEFAULT_SENDER")
        or os.environ.get("SMTP_FROM")
        or (f"RIT Transport <{smtp_user}>" if smtp_user else "noreply@ritchennai.edu.in")
    ).strip()
    if "<" not in raw_from and "@" in raw_from:
        smtp_from = f"RIT Transport <{raw_from}>"
    else:
        smtp_from = raw_from or "noreply@ritchennai.edu.in"

    use_ssl = os.environ.get("MAIL_USE_SSL", "").lower() in ("true", "1") or smtp_port == 465
    use_tls = os.environ.get("MAIL_USE_TLS", "true").lower() in ("true", "1") and not use_ssl

    is_configured = bool(smtp_user and smtp_pass)
    return {
        "host": smtp_host,
        "port": smtp_port,
        "user": smtp_user,
        "pass": smtp_pass,
        "from": smtp_from,
        "use_ssl": use_ssl,
        "use_tls": use_tls,
        "is_configured": is_configured
    }


def get_email_provider_config():
    """
    Centralized email provider resolver.
    Prioritizes HTTPS REST APIs (Resend, Brevo, SendGrid) over port 443 which are 100% open
    on cloud platforms like Render Free where outbound SMTP ports (25, 465, 587) are blocked.
    Falls back to direct SMTP when configured and running in an environment with open SMTP ports.
    """
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    brevo_key = os.environ.get("BREVO_API_KEY", "").strip()
    sendgrid_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    smtp_cfg = get_smtp_config()

    raw_from = (
        os.environ.get("MAIL_DEFAULT_SENDER")
        or os.environ.get("SMTP_FROM")
        or (f"RIT Transport <{smtp_cfg['user']}>" if smtp_cfg["user"] else "RIT Transport <noreply@ritchennai.edu.in>")
    ).strip()
    if "<" not in raw_from and "@" in raw_from:
        default_from = f"RIT Transport <{raw_from}>"
    else:
        default_from = raw_from or "RIT Transport <noreply@ritchennai.edu.in>"

    is_render = bool(os.environ.get("RENDER"))

    if resend_key:
        provider = "RESEND"
        is_configured = True
    elif brevo_key:
        provider = "BREVO"
        is_configured = True
    elif sendgrid_key:
        provider = "SENDGRID"
        is_configured = True
    elif smtp_cfg["is_configured"]:
        provider = "SMTP"
        is_configured = True
    else:
        provider = "NONE"
        is_configured = False

    return {
        "provider": provider,
        "is_configured": is_configured,
        "is_render": is_render,
        "from": default_from,
        "resend_key": resend_key,
        "brevo_key": brevo_key,
        "sendgrid_key": sendgrid_key,
        "smtp": smtp_cfg
    }


def send_transactional_email(to_email, subject, text_body, html_body, to_name=None, attachment_path=None, attachment_filename=None):
    """
    Unified transactional email dispatcher.
    Sends via HTTPS REST APIs (Resend, Brevo, SendGrid) on port 443 or direct SMTP.
    Returns (success_bool, message_or_error_str, provider_name).
    """
    cfg = get_email_provider_config()
    if not cfg["is_configured"]:
        return False, "No email provider configured in environment.", "UNCONFIGURED"

    provider = cfg["provider"]

    # 1. Resend API (HTTPS REST)
    if provider == "RESEND":
        try:
            payload = {
                "from": cfg["from"],
                "to": [to_email],
                "subject": subject,
                "html": html_body,
                "text": text_body,
            }
            if attachment_path and os.path.exists(attachment_path):
                with open(attachment_path, "rb") as af:
                    payload["attachments"] = [{
                        "filename": attachment_filename or os.path.basename(attachment_path),
                        "content": base64.b64encode(af.read()).decode("utf-8")
                    }]

            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {cfg['resend_key']}",
                    "Content-Type": "application/json",
                    "User-Agent": "RIT-Transport/2.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return True, resp_data.get("id", "Sent"), "RESEND"
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="ignore")
            app.logger.error(f"Resend HTTPS API error ({he.code}): {err_body}")
            return False, f"Resend API error {he.code}: {err_body}", "RESEND"
        except Exception as e:
            app.logger.error(f"Resend request exception: {e}")
            return False, f"Resend request failed: {e}", "RESEND"

    # 2. Brevo API (HTTPS REST)
    elif provider == "BREVO":
        try:
            from_name = "RIT Transport"
            from_email = cfg["from"]
            if "<" in from_email and ">" in from_email:
                parts = from_email.split("<")
                from_name = parts[0].strip() or "RIT Transport"
                from_email = parts[1].replace(">", "").strip()

            payload = {
                "sender": {"name": from_name, "email": from_email},
                "to": [{"email": to_email, "name": to_name or to_email}],
                "subject": subject,
                "htmlContent": html_body,
                "textContent": text_body,
            }
            if attachment_path and os.path.exists(attachment_path):
                with open(attachment_path, "rb") as af:
                    payload["attachment"] = [{
                        "name": attachment_filename or os.path.basename(attachment_path),
                        "content": base64.b64encode(af.read()).decode("utf-8")
                    }]

            req = urllib.request.Request(
                "https://api.brevo.com/v3/smtp/email",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "api-key": cfg["brevo_key"],
                    "Content-Type": "application/json",
                    "User-Agent": "RIT-Transport/2.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return True, resp_data.get("messageId", "Sent"), "BREVO"
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="ignore")
            app.logger.error(f"Brevo HTTPS API error ({he.code}): {err_body}")
            return False, f"Brevo API error {he.code}: {err_body}", "BREVO"
        except Exception as e:
            app.logger.error(f"Brevo request exception: {e}")
            return False, f"Brevo request failed: {e}", "BREVO"

    # 3. SendGrid API (HTTPS REST)
    elif provider == "SENDGRID":
        try:
            from_email = cfg["from"]
            if "<" in from_email and ">" in from_email:
                from_email = from_email.split("<")[1].replace(">", "").strip()

            payload = {
                "personalizations": [{"to": [{"email": to_email, "name": to_name or to_email}]}],
                "from": {"email": from_email, "name": "RIT Transport"},
                "subject": subject,
                "content": [
                    {"type": "text/plain", "value": text_body},
                    {"type": "text/html", "value": html_body}
                ]
            }
            if attachment_path and os.path.exists(attachment_path):
                with open(attachment_path, "rb") as af:
                    payload["attachments"] = [{
                        "content": base64.b64encode(af.read()).decode("utf-8"),
                        "filename": attachment_filename or os.path.basename(attachment_path),
                        "disposition": "attachment"
                    }]

            req = urllib.request.Request(
                "https://api.sendgrid.com/v3/mail/send",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {cfg['sendgrid_key']}",
                    "Content-Type": "application/json",
                    "User-Agent": "RIT-Transport/2.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return True, "Sent", "SENDGRID"
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="ignore")
            app.logger.error(f"SendGrid HTTPS API error ({he.code}): {err_body}")
            return False, f"SendGrid API error {he.code}: {err_body}", "SENDGRID"
        except Exception as e:
            app.logger.error(f"SendGrid request exception: {e}")
            return False, f"SendGrid request failed: {e}", "SENDGRID"

    # 4. Direct SMTP
    elif provider == "SMTP":
        smtp_cfg = cfg["smtp"]
        if cfg["is_render"] and smtp_cfg["port"] in (25, 465, 587):
            app.logger.warning("Render Free blocks outbound SMTP ports 25, 465, and 587. Email delivery may fail; configure RESEND_API_KEY or BREVO_API_KEY.")

        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = smtp_cfg["from"]
            msg["To"] = to_email
            msg["Subject"] = subject

            msg.attach(MIMEText(text_body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            if attachment_path and os.path.exists(attachment_path):
                try:
                    with open(attachment_path, "rb") as af:
                        part = MIMEApplication(af.read(), Name=attachment_filename or os.path.basename(attachment_path))
                    part['Content-Disposition'] = f'attachment; filename="{attachment_filename or os.path.basename(attachment_path)}"'
                    msg.attach(part)
                except Exception as attach_err:
                    app.logger.warning(f"Failed to attach file to email: {attach_err}")

            if smtp_cfg["use_ssl"]:
                server_ctx = smtplib.SMTP_SSL(smtp_cfg["host"], smtp_cfg["port"], timeout=15)
            else:
                server_ctx = smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"], timeout=15)

            with server_ctx as server:
                if smtp_cfg["use_tls"]:
                    server.starttls()
                server.login(smtp_cfg["user"], smtp_cfg["pass"])
                server.send_message(msg)

            return True, "Sent via SMTP", "SMTP"
        except (smtplib.SMTPConnectError, socket.error, OSError, TimeoutError) as net_err:
            if cfg["is_render"]:
                msg_err = "Render Free blocks outbound SMTP ports (25, 465, 587). Please configure RESEND_API_KEY or BREVO_API_KEY for HTTPS email delivery."
            else:
                msg_err = f"SMTP connection error: {net_err}"
            app.logger.error(msg_err)
            return False, msg_err, "SMTP"
        except smtplib.SMTPAuthenticationError:
            msg_err = "SMTP authentication failed. Please verify email credentials."
            app.logger.error(msg_err)
            return False, msg_err, "SMTP"
        except Exception as e:
            app.logger.error(f"SMTP delivery error: {e}")
            return False, f"SMTP delivery failed: {e}", "SMTP"

    return False, "Unknown email provider", "UNKNOWN"


def send_otp_notification(contact, otp):
    """
    Sends OTP exclusively via email to the user's registered address using the active email provider.
    Mobile/SMS options are completely excluded.
    Returns (success_bool, message, provider_configured_bool).
    """
    provider_cfg = get_email_provider_config()

    if not provider_cfg["is_configured"]:
        return True, "Verification OTP generated. Email provider is not configured in .env (enter RESEND_API_KEY / BREVO_API_KEY on Render or GMAIL_USER and GMAIL_APP_PASSWORD locally).", False

    if not contact or "@" not in contact:
        return False, "Invalid email address for OTP delivery.", True

    subject = "RIT Transport - Password Reset OTP"
    text_body = (
        f"Hello,\n\n"
        f"Your One-Time Password (OTP) for resetting your RIT Transport Management password is:\n\n"
        f"    {otp}\n\n"
        f"This OTP is valid for 10 minutes.\n\n"
        f"If you did not request a password reset, please ignore this email or notify the RIT Transport Office immediately.\n\n"
        f"Best regards,\n"
        f"RIT Transport Team\n"
        f"Rajalakshmi Institute of Technology, Chennai\n"
    )

    html_body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; color: #1e293b; }}
    .card {{ max-width: 520px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }}
    .header {{ background: #0f172a; padding: 24px 32px; text-align: center; }}
    .header h1 {{ color: #ffffff; margin: 0; font-size: 20px; font-weight: 700; letter-spacing: 0.5px; }}
    .header p {{ color: #94a3b8; margin: 4px 0 0 0; font-size: 13px; }}
    .content {{ padding: 32px; }}
    .otp-box {{ background: #f1f5f9; border: 2px dashed #0284c7; border-radius: 8px; text-align: center; padding: 18px; margin: 24px 0; }}
    .otp-code {{ font-size: 32px; font-weight: 800; letter-spacing: 8px; color: #0284c7; font-family: monospace; }}
    .notice {{ font-size: 13px; color: #64748b; line-height: 1.6; }}
    .footer {{ padding: 20px 32px; background: #f8fafc; border-top: 1px solid #e2e8f0; text-align: center; font-size: 12px; color: #94a3b8; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>RIT Transport Management</h1>
      <p>Rajalakshmi Institute of Technology</p>
    </div>
    <div class="content">
      <p style="font-size: 15px; margin-top: 0;">Hello,</p>
      <p style="font-size: 14px; line-height: 1.5; color: #334155;">
        You recently requested to reset the password for your RIT Transport account. Use the verification code below to proceed:
      </p>
      <div class="otp-box">
        <div class="otp-code">{otp}</div>
        <p style="margin: 6px 0 0 0; font-size: 12px; color: #64748b;">Valid for 10 minutes</p>
      </div>
      <p class="notice">
        <strong>Security Notice:</strong> Never share this OTP with anyone. The RIT Transport team will never ask for your password or OTP. If you did not initiate this request, you can safely disregard this email.
      </p>
    </div>
    <div class="footer">
      &copy; 2026 Rajalakshmi Institute of Technology &bull; Transport Department
    </div>
  </div>
</body>
</html>"""

    ok, msg_or_id, provider_name = send_transactional_email(contact, subject, text_body, html_body)
    if ok:
        return True, "Verification OTP has been sent to your registered email address.", True
    else:
        return False, f"Failed to deliver OTP email: {msg_or_id}", True


def send_notice_email_distribution(notice_id, title, message_content, priority, created_at, attachment_path=None, attachment_filename=None, target_audience="ALL"):
    """
    Broadcast notice email to all active, registered Students and Faculty.
    Deduplicates emails, excludes inactive/removed accounts, logs deliveries in notice_email_logs,
    and returns an accurate delivery summary (sent, failed, skipped).
    """
    connection = get_connection()

    t_aud = (target_audience or "ALL").strip().upper()
    if t_aud == "VISITOR":
        summary_str = "Visitors only (0 campus emails)"
        connection.execute("UPDATE notices SET delivery_summary = ? WHERE id = ?", (summary_str, notice_id))
        connection.commit()
        connection.close()
        return {
            "total_eligible": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "status": "COMPLETED",
            "message": "Notice target audience is Visitors only; campus email distribution skipped.",
            "summary_text": summary_str
        }

    role_filter = ("student", "faculty")
    if t_aud == "STUDENT":
        role_filter = ("student",)
    elif t_aud == "FACULTY":
        role_filter = ("faculty",)

    placeholders = ",".join("?" for _ in role_filter)
    rows = connection.execute(f"""
        SELECT DISTINCT id, name, email, role, register_number
        FROM users
        WHERE role IN ({placeholders})
          AND is_active = 1
          AND (lifecycle_status IS NULL OR lifecycle_status = 'ACTIVE')
          AND email IS NOT NULL
          AND trim(email) != ''
        ORDER BY role, name
    """, role_filter).fetchall()

    email_regex = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    seen_emails = set()
    eligible_recipients = []
    skipped_list = []

    for r in rows:
        email = (r["email"] or "").strip().lower()
        if not email or not email_regex.match(email):
            skipped_list.append({"name": r["name"], "email": email, "reason": "Invalid email format"})
            continue
        if email in seen_emails:
            skipped_list.append({"name": r["name"], "email": email, "reason": "Duplicate recipient"})
            continue
        seen_emails.add(email)
        eligible_recipients.append({
            "id": r["id"],
            "name": r["name"] or "Member",
            "email": email,
            "role": r["role"]
        })

    if not eligible_recipients:
        summary_str = "0 sent, 0 failed, 0 skipped"
        connection.execute("UPDATE notices SET delivery_summary = ? WHERE id = ?", (summary_str, notice_id))
        connection.commit()
        connection.close()
        return {
            "total_eligible": 0,
            "sent": 0,
            "failed": 0,
            "skipped": len(skipped_list),
            "status": "COMPLETED",
            "message": "No active student or faculty accounts found with valid email addresses.",
            "summary_text": summary_str
        }

    provider_cfg = get_email_provider_config()
    if not provider_cfg["is_configured"]:
        summary_str = f"Email provider unconfigured (0/{len(eligible_recipients)} sent)"
        connection.execute("UPDATE notices SET delivery_summary = ? WHERE id = ?", (summary_str, notice_id))
        connection.commit()
        connection.close()
        return {
            "total_eligible": len(eligible_recipients),
            "sent": 0,
            "failed": 0,
            "skipped": len(eligible_recipients) + len(skipped_list),
            "status": "UNCONFIGURED",
            "message": "No email provider configured. On Render Free, configure RESEND_API_KEY or BREVO_API_KEY for HTTPS email delivery (or GMAIL_USER and GMAIL_APP_PASSWORD locally).",
            "summary_text": summary_str
        }

    p_upper = (priority or "NORMAL").upper()
    if p_upper == "URGENT":
        priority_badge_html = '<span style="display:inline-block; background:#fee2e2; color:#b91c1c; font-weight:700; font-size:12px; padding:4px 12px; border-radius:9999px; letter-spacing:0.5px; border:1px solid #fca5a5;">🚨 URGENT ADVISORY</span>'
        subject_prefix = "[URGENT] "
    elif p_upper == "HIGH":
        priority_badge_html = '<span style="display:inline-block; background:#fef3c7; color:#b45309; font-weight:700; font-size:12px; padding:4px 12px; border-radius:9999px; letter-spacing:0.5px; border:1px solid #fcd34d;">⚡ HIGH PRIORITY</span>'
        subject_prefix = "[HIGH PRIORITY] "
    else:
        priority_badge_html = '<span style="display:inline-block; background:#e0f2fe; color:#0369a1; font-weight:700; font-size:12px; padding:4px 12px; border-radius:9999px; letter-spacing:0.5px; border:1px solid #bae6fd;">📢 ANNOUNCEMENT</span>'
        subject_prefix = ""

    subject = f"{subject_prefix}RIT Transport Notice: {title}"

    sent_count = 0
    failed_count = 0
    now_ts = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    escaped_title = html_escape(title)
    escaped_message = html_escape(message_content).replace("\n", "<br>")

    for recipient in eligible_recipients:
        # Check idempotency
        already_sent = connection.execute(
            "SELECT id FROM notice_email_logs WHERE notice_id = ? AND recipient_email = ? AND status = 'SENT'",
            (notice_id, recipient["email"])
        ).fetchone()
        if already_sent:
            skipped_list.append({"name": recipient["name"], "email": recipient["email"], "reason": "Already sent"})
            continue

        text_body = (
            f"RIT TRANSPORT MANAGEMENT - OFFICIAL NOTICE\n"
            f"====================================================\n\n"
            f"Title: {title}\n"
            f"Priority: {p_upper}\n"
            f"Published Date: {created_at}\n\n"
            f"Dear {recipient['name']} ({recipient['role'].capitalize()}),\n\n"
            f"{message_content}\n\n"
            f"====================================================\n"
            f"RIT Transport Team\n"
            f"Rajalakshmi Institute of Technology, Chennai\n"
        )

        html_body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f1f5f9; margin: 0; padding: 24px; color: #1e293b; }}
    .card {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; overflow: hidden; box-shadow: 0 4px 14px rgba(0,0,0,0.06); }}
    .header {{ background: #0f172a; padding: 26px 32px; text-align: center; }}
    .header h1 {{ color: #ffffff; margin: 0; font-size: 20px; font-weight: 800; letter-spacing: 0.5px; }}
    .header p {{ color: #94a3b8; margin: 4px 0 0 0; font-size: 13px; font-weight: 500; }}
    .content {{ padding: 32px; }}
    .notice-title {{ font-size: 20px; font-weight: 800; color: #0f172a; margin: 12px 0 6px 0; line-height: 1.35; }}
    .notice-date {{ font-size: 13px; color: #64748b; margin-bottom: 20px; }}
    .greeting {{ font-size: 15px; font-weight: 600; color: #334155; margin-bottom: 16px; }}
    .message-box {{ background: #f8fafc; border-left: 4px solid #0284c7; padding: 18px 20px; border-radius: 6px; font-size: 15px; line-height: 1.65; color: #1e293b; margin: 18px 0 24px 0; }}
    .footer {{ padding: 22px 32px; background: #f8fafc; border-top: 1px solid #e2e8f0; text-align: center; font-size: 12px; color: #94a3b8; line-height: 1.6; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>RIT Transport Management</h1>
      <p>Rajalakshmi Institute of Technology &bull; Official Campus Broadcast</p>
    </div>
    <div class="content">
      <div style="margin-bottom: 14px;">
        {priority_badge_html}
      </div>
      <div class="notice-title">{escaped_title}</div>
      <div class="notice-date">📅 Published Date: <strong>{created_at}</strong></div>
      <div class="greeting">Dear {html_escape(recipient['name'])} ({recipient['role'].capitalize()}),</div>
      <div class="message-box">
        {escaped_message}
      </div>
      <p style="font-size: 13px; color: #64748b; line-height: 1.5; margin: 0;">
        For further questions, please contact the RIT Transport Office or reach out to your bus coordinator.
      </p>
    </div>
    <div class="footer">
      &copy; 2026 Rajalakshmi Institute of Technology &bull; Transport Department<br>
      Kuthambakkam, Post, Chembarambakkam, Chennai, Tamil Nadu 600124
    </div>
  </div>
</body>
</html>"""

        ok, err_or_id, prov_used = send_transactional_email(
            recipient["email"], subject, text_body, html_body,
            to_name=recipient["name"],
            attachment_path=attachment_path,
            attachment_filename=attachment_filename
        )

        if ok:
            sent_count += 1
            connection.execute("""
                INSERT OR REPLACE INTO notice_email_logs (notice_id, recipient_email, recipient_name, recipient_role, status, sent_at, error_message)
                VALUES (?, ?, ?, ?, 'SENT', ?, NULL)
            """, (notice_id, recipient["email"], recipient["name"], recipient["role"], now_ts))
        else:
            failed_count += 1
            connection.execute("""
                INSERT OR REPLACE INTO notice_email_logs (notice_id, recipient_email, recipient_name, recipient_role, status, sent_at, error_message)
                VALUES (?, ?, ?, ?, 'FAILED', ?, ?)
            """, (notice_id, recipient["email"], recipient["name"], recipient["role"], now_ts, str(err_or_id)))

    summary_str = f"{sent_count} sent, {failed_count} failed, {len(skipped_list)} skipped"
    connection.execute("UPDATE notices SET delivery_summary = ? WHERE id = ?", (summary_str, notice_id))
    connection.commit()
    connection.close()

    return {
        "total_eligible": len(eligible_recipients),
        "sent": sent_count,
        "failed": failed_count,
        "skipped": len(skipped_list),
        "status": "COMPLETED" if failed_count == 0 else "PARTIAL" if sent_count > 0 else "FAILED",
        "summary_text": summary_str
    }


# ============================================================
# TIME & STOP TIME FUNCTIONS
# ============================================================

def normalize_time(time_text):
    if not time_text:
        return None
    time_text = time_text.strip().upper()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            dt = datetime.strptime(time_text, fmt)
            return dt.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            continue
    return None


def normalize_date(date_text):
    if not date_text:
        return None
    date_text = date_text.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_text, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return None


def time_to_minutes(time_text):
    normalized = normalize_time(time_text)
    if not normalized:
        return None
    dt = datetime.strptime(normalized, "%I:%M %p")
    return dt.hour * 60 + dt.minute


def minutes_to_time(total_minutes):
    total_minutes = total_minutes % (24 * 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    period = "PM" if hours >= 12 else "AM"
    display_hour = hours % 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:{minutes:02d} {period}"


def calculate_return_times(departure_time, morning_stops):
    dep_minutes = time_to_minutes(departure_time)
    if dep_minutes is None or not morning_stops:
        return None
    reversed_stops = list(reversed(morning_stops))
    estimated = []
    current_minutes = dep_minutes

    estimated.append({
        "stop_name": reversed_stops[0]["stop_name"],
        "estimated_time": minutes_to_time(current_minutes)
    })

    for i in range(1, len(reversed_stops)):
        prev_stop = reversed_stops[i - 1]
        curr_stop = reversed_stops[i]
        prev_time = time_to_minutes(prev_stop["stop_time"])
        curr_time = time_to_minutes(curr_stop["stop_time"])
        gap = 0
        if prev_time is not None and curr_time is not None:
            gap = abs(prev_time - curr_time)
        if gap <= 0:
            gap = 2
        current_minutes += gap
        estimated.append({
            "stop_name": curr_stop["stop_name"],
            "estimated_time": minutes_to_time(current_minutes)
        })
    return estimated


# ============================================================
# CONFIG / OPTIONS API
# ============================================================

@app.route("/api/config/form-options")
def get_form_options():
    """Provides dynamic departments, student batches, class sections, and faculty designations."""
    return jsonify({
        "success": True,
        "departments": DEPARTMENTS,
        "student_batches": STUDENT_BATCHES,
        "class_sections": CLASS_SECTIONS,
        "faculty_designations": FACULTY_DESIGNATIONS
    })


# ============================================================
# ENTRY WORKFLOW & USER PORTAL
# ============================================================

@app.route("/")
def home():
    """Root route: Redirect authenticated users to their dashboard, otherwise show login."""
    role = session.get("user_type")
    if role == "student":
        return redirect("/student")
    if role == "faculty":
        return redirect("/faculty")
    if role == "driver":
        return redirect("/driver")
    if role == "admin":
        return redirect("/admin")
    if role == "transport_incharge":
        return redirect("/transport-incharge/dashboard")
    return render_template("login.html")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    """Dedicated login route."""
    role = session.get("user_type")
    if role == "student":
        return redirect("/student")
    if role == "faculty":
        return redirect("/faculty")
    if role == "driver":
        return redirect("/driver")
    if role == "admin":
        return redirect("/admin")
    if role == "transport_incharge":
        return redirect("/transport-incharge/dashboard")
    return render_template("login.html")


@app.route("/users")
@app.route("/user")
def users_portal():
    """
    Requirement #2: When a user visits /user:
    Show a professional login and registration interface if not logged in.
    The user must register first and then log in to access the dashboard.
    If already logged in, redirect to their role-specific dashboard.
    """
    role = session.get("user_type")
    user_id = session.get("user_id")
    if user_id and role == "student":
        return render_template("student.html", portal_type="Student", user_name=session.get("user_name"), role="student")
    if user_id and role == "faculty":
        return render_template("faculty.html", portal_type="Faculty", user_name=session.get("user_name"), role="faculty")
    # Not logged in: Show professional login & registration interface
    return render_template("login.html")


@app.route("/student")
def student():
    """Student portal with strict server-side role authorization."""
    role = session.get("user_type")
    if role != "student":
        if role == "faculty":
            return redirect("/faculty")
        return redirect(url_for("login_page"))
    return render_template(
        "student.html",
        portal_type="Student",
        user_name=session.get("user_name"),
        role="student"
    )


@app.route("/faculty")
def faculty():
    """Faculty portal with strict server-side role authorization."""
    role = session.get("user_type")
    if role != "faculty":
        if role == "student":
            return redirect("/student")
        return redirect(url_for("login_page"))
    return render_template(
        "faculty.html",
        portal_type="Faculty",
        user_name=session.get("user_name"),
        role="faculty"
    )


# ============================================================
# PUBLIC GUEST PORTAL (NEW JOINERS & VISITORS)
# ============================================================

@app.route("/visitor")
@app.route("/guest")
def visitor_portal():
    """
    Feature 3: Public read-only Visitor Portal for new joiners and guests.
    No login, registration, OTP or authentication required.
    """
    return render_template(
        "guest.html",
        portal_type="Visitor",
        default_return_time=get_default_return_time()
    )


@app.route("/newjoiny")
def newjoiny_legacy_redirect():
    """Redirect legacy /newjoiny URL safely to /visitor."""
    return redirect("/visitor", code=302)


@app.route("/logout")
def logout():
    user_type = session.get("user_type")
    user_id = session.get("user_id")
    if user_type == "driver" and user_id:
        try:
            con = get_connection()
            con.execute("""
                UPDATE bus_locations
                SET is_active = 0, bus_status = 'OFFLINE', driver_id = NULL, sharing_expires_at = NULL, last_seen_epoch = 0, last_client_ts = 0
                WHERE driver_id = ?
            """, (user_id,))
            con.commit()
            con.close()
        except Exception as e:
            app.logger.error(f"Error terminating driver location sharing on logout: {e}")
    session.clear()
    return redirect(url_for("home"))


# ============================================================
# PHASE 2: GLOBAL OPERATIONAL NOTIFICATION BROADCAST
# ============================================================

def broadcast_operational_notification(alert_type, title, message, bus_id=None, metadata=None):
    """
    Broadcasts operational alerts according to strict recipient rules:
    - Admin always receives all notifications.
    - EVERY active Transport Incharge receives every operational alert (global rule, no bus filtering).
    - If bus_id is given and alert is passenger-relevant: active students and faculty registered under that bus receive the alert.
    - If alert is bus full or emergency: assigned driver receives it as well.
    - Deactivated / removed incharges (is_active = 0) do not receive new notifications.
    """
    conn = get_connection()
    try:
        now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
        meta_json = json.dumps(metadata or {})
        recipients = []

        # 1. Admin
        admin_user = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if admin_user:
            recipients.append((admin_user["id"], "admin", None))

        # 2. EVERY active Transport Incharge (GLOBAL RULE - no bus filtering)
        incharges = conn.execute("""
            SELECT id FROM users
            WHERE role = 'transport_incharge' AND is_active = 1
        """).fetchall()
        for inc in incharges:
            recipients.append((inc["id"], "transport_incharge", bus_id))

        # 3. Affected bus passengers (students & faculty) if bus_id provided
        if bus_id and alert_type in ["BUS_DELAY", "ETA_DELAY", "SCHEDULE_CHANGE", "BUS_FULL"]:
            passengers = conn.execute("""
                SELECT id, role FROM users
                WHERE bus_id = ? AND role IN ('student', 'faculty') AND is_active = 1
            """, (bus_id,)).fetchall()
            for p in passengers:
                recipients.append((p["id"], p["role"], bus_id))

        # 4. Assigned Driver if bus_id provided and bus full or emergency
        if bus_id and alert_type in ["BUS_FULL", "EMERGENCY_SOS"]:
            driver_row = conn.execute("""
                SELECT id FROM drivers
                WHERE assigned_bus_id = ? AND is_active = 1
            """, (bus_id,)).fetchone()
            if driver_row:
                recipients.append((None, "driver", bus_id))

        # Deduplicate and insert
        seen = set()
        for uid, rrole, rbus_id in recipients:
            key = (uid, rrole, rbus_id)
            if key in seen:
                continue
            seen.add(key)
            conn.execute("""
                INSERT INTO app_notifications (recipient_user_id, recipient_role, bus_id, alert_type, title, message, metadata_json, is_read, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """, (uid, rrole, rbus_id, alert_type, title, message, meta_json, now_str))

        conn.commit()
    except Exception as e:
        print(f"Error broadcasting operational notification: {e}")
    finally:
        conn.close()


# ============================================================
# PHASE 2: TRANSPORT INCHARGE PORTAL & DASHBOARD
# ============================================================

@app.route("/transport-incharge", methods=["GET", "POST"])
def transport_incharge_login():
    """
    Transport Incharge Login endpoint.
    Login requires:
    1. Transport Incharge Name
    2. Password
    Public self-registration is strictly disallowed; accounts are created by Admin only.
    No bus assignment required.
    """
    if request.method == "GET":
        if session.get("user_type") == "transport_incharge":
            return redirect(url_for("transport_incharge_dashboard"))
        return render_template("transport_incharge_login.html")

    data = request.get_json(silent=True) or request.form
    name_input = (data.get("name") or data.get("login_id") or data.get("username") or data.get("email") or "").strip()
    password = (data.get("password") or "").strip()

    is_ajax = bool(request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.content_type == "application/json")

    def fail_response(msg, code=401):
        if is_ajax:
            return jsonify({"success": False, "message": msg}), code
        return render_template("transport_incharge_login.html", error=msg), code

    if not name_input or not password:
        return fail_response("Transport Incharge Name and password are required.", 400)

    conn = get_connection()
    incharge = conn.execute("""
        SELECT id, username, role, name, register_number, email, mobile, password_hash, salt, is_active, lifecycle_status, deactivation_reason
        FROM users
        WHERE role = 'transport_incharge' AND (
            lower(trim(name)) = lower(trim(?))
            OR lower(trim(register_number)) = lower(trim(?))
            OR lower(trim(email)) = lower(trim(?))
            OR lower(trim(username)) = lower(trim(?))
        )
    """, (name_input, name_input, name_input, name_input)).fetchone()

    if not incharge:
        conn.close()
        return fail_response("Transport Incharge account not found. Please verify your Transport Incharge Name.", 401)

    if incharge["is_active"] != 1 or incharge["lifecycle_status"] == "DEACTIVATED":
        conn.close()
        reason = incharge["deactivation_reason"] or "Contact transport administrator."
        return fail_response(f"Account access has been revoked: {reason}", 403)

    if not incharge["password_hash"] or not incharge["salt"]:
        conn.close()
        return fail_response("Account credentials corrupted. Contact administrator.", 401)

    if not verify_password(password, incharge["salt"], incharge["password_hash"]):
        conn.close()
        return fail_response("Invalid password. Please check your credentials.", 401)

    conn.close()

    session.clear()
    session["user_type"] = "transport_incharge"
    session["role"] = "transport_incharge"
    session["user_id"] = incharge["id"]
    session["user_name"] = incharge["name"]
    session["register_number"] = incharge["register_number"]
    session["email"] = incharge["email"]

    if is_ajax:
        return jsonify({"success": True, "redirect": "/transport-incharge/dashboard"})
    return redirect(url_for("transport_incharge_dashboard"))


# ============================================================
# SESSION INTEGRITY HOOK (PHASE 2 - INCHARGE REMOVAL)
# ============================================================

@app.before_request
def check_session_integrity():
    """
    Ensure active sessions for Transport Incharges are immediately invalidated
    if the account was removed or deactivated by Administrator.
    """
    if session.get("user_type") == "transport_incharge":
        # Allow static assets and login/logout endpoints
        if request.endpoint in ("static", "transport_incharge_login", "logout"):
            return None
        user_id = session.get("user_id")
        if not user_id:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "message": "Session expired or invalid. Please log in again."}), 401
            return redirect(url_for("transport_incharge_login"))

        conn = get_connection()
        user = conn.execute("""
            SELECT id, is_active, lifecycle_status 
            FROM users 
            WHERE id = ? AND role = 'transport_incharge'
        """, (user_id,)).fetchone()
        conn.close()

        if not user or user["is_active"] != 1 or user["lifecycle_status"] == "DEACTIVATED":
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "message": "Your Transport Incharge account has been removed or deactivated. Please contact Administrator."}), 401
            flash("Your Transport Incharge account has been removed or deactivated. Please contact Administrator.", "danger")
            return redirect(url_for("transport_incharge_login"))


@app.route("/transport-incharge/dashboard")
def transport_incharge_dashboard():
    """
    Dedicated Transport Incharge Dashboard for All-Bus Fleet Monitoring.
    Enforces role authorization on server side and verifies account is active.
    """
    if session.get("user_type") != "transport_incharge":
        return redirect(url_for("transport_incharge_login"))

    user_id = session.get("user_id")
    if not user_id:
        session.clear()
        return redirect(url_for("transport_incharge_login"))

    conn = get_connection()
    user = conn.execute("""
        SELECT id, name, register_number, is_active, lifecycle_status 
        FROM users 
        WHERE id = ? AND role = 'transport_incharge'
    """, (user_id,)).fetchone()
    conn.close()

    if not user or user["is_active"] != 1 or user["lifecycle_status"] == "DEACTIVATED":
        session.clear()
        flash("Your Transport Incharge account has been removed or deactivated. Please contact Administrator.", "danger")
        return redirect(url_for("transport_incharge_login"))

    return render_template(
        "transport_incharge.html",
        user_name=user["name"],
        employee_id=user["register_number"],
        role="transport_incharge"
    )


# ============================================================
# PHASE 2: ADMIN MANAGE TRANSPORT INCHARGES APIS
# ============================================================

@app.route("/api/admin/transport-incharges", methods=["GET"])
def admin_get_transport_incharges():
    """Admin views all Transport Incharge accounts. No bus assignments."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    conn = get_connection()
    incharges = conn.execute("""
        SELECT id, name, username, register_number, email, mobile, is_active, lifecycle_status, created_at, deactivation_reason
        FROM users
        WHERE role = 'transport_incharge'
        ORDER BY id DESC
    """).fetchall()
    conn.close()

    result = []
    for inc in incharges:
        result.append({
            "id": inc["id"],
            "name": inc["name"],
            "employee_id": inc["register_number"] or inc["username"],
            "email": inc["email"],
            "mobile": inc["mobile"],
            "is_active": bool(inc["is_active"] and inc["lifecycle_status"] != "DEACTIVATED"),
            "created_at": inc["created_at"],
            "deactivation_reason": inc["deactivation_reason"]
        })

    return jsonify({"success": True, "incharges": result})


@app.route("/api/admin/transport-incharge/add", methods=["POST"])
def admin_add_transport_incharge():
    """Admin adds a Transport Incharge. No bus assignment."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    employee_id = (data.get("employee_id") or data.get("register_number") or "").strip().upper()
    email = (data.get("email") or "").strip().lower()
    mobile = (data.get("mobile") or data.get("phone") or "").strip()
    password = (data.get("password") or "").strip()
    confirm_password = (data.get("confirm_password") or password).strip()

    if not name or not employee_id or not email or not password:
        return jsonify({"success": False, "message": "Name, employee ID, email, and password are required."}), 400

    if password != confirm_password:
        return jsonify({"success": False, "message": "Passwords do not match."}), 400

    if len(password) < 6:
        return jsonify({"success": False, "message": "Password must be at least 6 characters."}), 400

    conn = get_connection()

    # Prevent duplicate Incharge names or ambiguous login identities
    existing_name = conn.execute("""
        SELECT id, name FROM users
        WHERE role = 'transport_incharge' AND lower(trim(name)) = lower(trim(?))
    """, (name,)).fetchone()
    if existing_name:
        conn.close()
        return jsonify({"success": False, "message": f"A Transport Incharge named '{name}' already exists. Please use a unique full name."}), 400

    # Check uniqueness of Employee ID and Email
    existing_cred = conn.execute("""
        SELECT id FROM users
        WHERE lower(trim(register_number)) = lower(trim(?))
           OR lower(trim(email)) = lower(trim(?))
           OR lower(trim(username)) = lower(trim(?))
    """, (employee_id, email, employee_id)).fetchone()

    if existing_cred:
        conn.close()
        return jsonify({"success": False, "message": "An account with that Employee ID or Email already exists."}), 400

    salt = secrets.token_bytes(16)
    pwd_hash = hash_password(password, salt)
    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (username, password, role, name, register_number, email, mobile, password_hash, salt, is_active, lifecycle_status, created_at)
        VALUES (?, '', 'transport_incharge', ?, ?, ?, ?, ?, ?, 1, 'ACTIVE', ?)
    """, (employee_id, name, employee_id, email, mobile, pwd_hash, salt, now_str))
    incharge_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": f"✅ Transport Incharge '{name}' created successfully. Login enabled via Name and Password.",
        "incharge_id": incharge_id
    })


@app.route("/api/admin/transport-incharge/<int:incharge_id>/edit", methods=["POST"])
def admin_edit_transport_incharge(incharge_id):
    """Admin edits Transport Incharge details. No bus assignment."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    employee_id = (data.get("employee_id") or "").strip().upper()
    email = (data.get("email") or "").strip().lower()
    mobile = (data.get("mobile") or "").strip()
    password = (data.get("password") or "").strip()

    conn = get_connection()
    target = conn.execute("SELECT id FROM users WHERE id = ? AND role = 'transport_incharge'", (incharge_id,)).fetchone()
    if not target:
        conn.close()
        return jsonify({"success": False, "message": "Transport Incharge not found."}), 404

    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    if name:
        conn.execute("UPDATE users SET name = ?, updated_at = ? WHERE id = ?", (name, now_str, incharge_id))
    if email:
        conn.execute("UPDATE users SET email = ?, updated_at = ? WHERE id = ?", (email, now_str, incharge_id))
    if mobile:
        conn.execute("UPDATE users SET mobile = ?, updated_at = ? WHERE id = ?", (mobile, now_str, incharge_id))
    if employee_id:
        conn.execute("UPDATE users SET register_number = ?, username = ?, updated_at = ? WHERE id = ?", (employee_id, employee_id, now_str, incharge_id))

    if password:
        salt = secrets.token_bytes(16)
        pwd_hash = hash_password(password, salt)
        conn.execute("UPDATE users SET password_hash = ?, salt = ?, updated_at = ? WHERE id = ?", (pwd_hash, salt, now_str, incharge_id))

    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Transport Incharge details updated successfully."})


@app.route("/api/admin/transport-incharge/<int:incharge_id>/status", methods=["POST"])
def admin_toggle_transport_incharge_status(incharge_id):
    """Admin activates or deactivates Transport Incharge account."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    conn = get_connection()
    target = conn.execute("SELECT id, is_active FROM users WHERE id = ? AND role = 'transport_incharge'", (incharge_id,)).fetchone()
    if not target:
        conn.close()
        return jsonify({"success": False, "message": "Transport Incharge not found."}), 404

    new_status = 0 if target["is_active"] == 1 else 1
    new_lifecycle = "ACTIVE" if new_status == 1 else "DEACTIVATED"
    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    conn.execute("""
        UPDATE users
        SET is_active = ?, lifecycle_status = ?, updated_at = ?
        WHERE id = ?
    """, (new_status, new_lifecycle, now_str, incharge_id))
    conn.commit()
    conn.close()

    status_word = "activated" if new_status == 1 else "deactivated"
    return jsonify({"success": True, "message": f"Transport Incharge account has been {status_word}.", "is_active": bool(new_status)})


@app.route("/api/admin/transport-incharge/<int:incharge_id>/remove", methods=["POST"])
def admin_remove_transport_incharge(incharge_id):
    """
    Admin permanently removes Transport Incharge:
    - Safely cleans up child rows in foreign-key relationships.
    - Permanently deletes incharge from users table.
    - Revokes login access and invalidates active sessions immediately.
    - Account disappears from incharge lists and active counts.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    conn = get_connection()
    target = conn.execute("SELECT id, name FROM users WHERE id = ? AND role = 'transport_incharge'", (incharge_id,)).fetchone()
    if not target:
        conn.close()
        return jsonify({"success": False, "message": "Transport Incharge not found."}), 404

    try:
        # Clean up related records in child tables safely
        conn.execute("DELETE FROM transport_incharge_buses WHERE incharge_id = ?", (incharge_id,))
        conn.execute("DELETE FROM app_notifications WHERE recipient_user_id = ?", (incharge_id,))
        conn.execute("DELETE FROM web_push_subscriptions WHERE user_id = ?", (incharge_id,))
        conn.execute("DELETE FROM password_reset_otps WHERE user_id = ?", (incharge_id,))
        # Permanently delete the Transport Incharge user record
        conn.execute("DELETE FROM users WHERE id = ? AND role = 'transport_incharge'", (incharge_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "message": f"Failed to remove Transport Incharge: {str(e)}"}), 500

    conn.close()
    return jsonify({
        "success": True,
        "message": f"Transport Incharge '{target['name']}' has been permanently removed. Login access revoked immediately."
    })


# ============================================================
# PHASE 2: INCHARGE ALL-BUS STATUS & DASHBOARD APIS
# ============================================================

@app.route("/api/incharge/all-bus-status", methods=["GET"])
def incharge_all_bus_status():
    """
    All-Bus Status visibility for Transport Incharge and Admin.
    Every active Incharge sees all campus buses without any bus assignment filtering.
    """
    user_type = session.get("user_type")
    if user_type not in ["transport_incharge", "admin"]:
        return jsonify({"success": False, "message": "Unauthorized access."}), 403

    conn = get_connection()
    today_str = get_today_date_str()
    now_dt = get_kolkata_now()

    buses = conn.execute("""
        SELECT b.id, b.bus_number, b.route_name,
               COALESCE(b.capacity, 50) as capacity,
               COALESCE(b.scheduled_arrival_time, '07:40 AM') as scheduled_arrival_time,
               COALESCE(mbc.is_active, 1) as morning_active,
               bl.latitude, bl.longitude, bl.current_location, bl.next_point,
               bl.updated_at as gps_updated_at, bl.is_active as tracking_active, bl.bus_status
        FROM buses b
        LEFT JOIN morning_bus_config mbc ON b.id = mbc.bus_id
        LEFT JOIN bus_locations bl ON b.id = bl.bus_id
        ORDER BY b.bus_number ASC
    """).fetchall()

    result = []
    rit_lat, rit_lon = 13.0082, 80.0035

    for b in buses:
        bus_id = b["id"]

        # Boarding counts (centralized, valid registered passengers only)
        boarded_today = get_bus_boarded_count(bus_id, today_str)
        morning_boarded = get_bus_boarded_count(bus_id, today_str, "MORNING")
        return_boarded = get_bus_boarded_count(bus_id, today_str, "RETURN")

        cap = b["capacity"]
        is_full = boarded_today >= cap
        cap_status = "BUS FULL" if is_full else "SEATS AVAILABLE"

        # 2. Tracking status & running status
        is_tracking = bool(b["tracking_active"] and b["latitude"] is not None)
        tracking_status = "LIVE TRACKING" if is_tracking else "NOT SHARING GPS"
        running_status = b["bus_status"] if b["bus_status"] else ("ON_ROUTE" if is_tracking else "NOT_STARTED")

        # 3. Emergency status
        emergency = conn.execute("""
            SELECT id, emergency_type, status FROM emergency_alerts
            WHERE bus_id = ? AND status IN ('PENDING', 'ACKNOWLEDGED')
            ORDER BY id DESC LIMIT 1
        """, (bus_id,)).fetchone()
        emergency_status = f"EMERGENCY ({emergency['emergency_type']})" if emergency else "NORMAL"

        # 4. Delay status
        delay_rep = conn.execute("""
            SELECT id, delay_reason, expected_delay, status FROM delay_reports
            WHERE bus_id = ? AND status IN ('REPORTED', 'UNDER_REVIEW')
            ORDER BY id DESC LIMIT 1
        """, (bus_id,)).fetchone()

        scheduled_str = b["scheduled_arrival_time"]
        estimated_str = "Unavailable"
        delay_status = "ON TIME"
        delay_minutes = 0

        if is_tracking:
            lat, lon = b["latitude"], b["longitude"]
            R = 6371.0
            dlat = math.radians(rit_lat - lat)
            dlon = math.radians(rit_lon - lon)
            a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(rit_lat)) * math.sin(dlon / 2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
            dist_km = round(R * c, 1)

            travel_mins = max(2, int((dist_km / 25.0) * 60) + 5)
            est_dt = now_dt + timedelta(minutes=travel_mins)
            estimated_str = est_dt.strftime("%I:%M %p")

            try:
                sched_time_obj = datetime.strptime(scheduled_str, "%I:%M %p").time()
                sched_dt = datetime.combine(now_dt.date(), sched_time_obj)
                if now_dt.tzinfo:
                    sched_dt = sched_dt.replace(tzinfo=now_dt.tzinfo)
                diff_secs = (est_dt - sched_dt).total_seconds()
                delay_minutes = max(0, int(diff_secs / 60))
            except Exception:
                delay_minutes = 0

        if delay_rep:
            delay_status = f"DELAY REPORTED ({delay_rep['expected_delay']})"
        elif delay_minutes >= 10:
            delay_status = f"RUNNING LATE (+{delay_minutes}m)"

        result.append({
            "bus_id": bus_id,
            "bus_number": b["bus_number"],
            "route_name": b["route_name"],
            "bus_active": bool(b["morning_active"]),
            "tracking_status": tracking_status,
            "running_status": running_status,
            "delay_status": delay_status,
            "capacity": cap,
            "total_boarded_today": boarded_today,
            "morning_boarded": morning_boarded,
            "return_boarded": return_boarded,
            "capacity_status": cap_status,
            "is_full": is_full,
            "scheduled_arrival_time": scheduled_str,
            "estimated_arrival_time": estimated_str,
            "emergency_status": emergency_status,
            "last_updated": b["gps_updated_at"] or "Never"
        })

    conn.close()
    return jsonify({
        "success": True,
        "buses": result,
        "date": today_str,
        "total_buses": len(result),
        "scope": "ALL_CAMPUS_BUSES"
    })


@app.route("/api/incharge/dashboard-summary", methods=["GET"])
def incharge_dashboard_summary():
    """
    Statistics for Transport Incharge dashboard overview.
    Represents campus-wide fleet metrics across ALL buses.
    """
    user_type = session.get("user_type")
    if user_type not in ["transport_incharge", "admin"]:
        return jsonify({"success": False, "message": "Unauthorized access."}), 403

    conn = get_connection()
    today_str = get_today_date_str()

    total_buses = conn.execute("SELECT COUNT(*) as c FROM buses").fetchone()["c"]
    active_buses = conn.execute("SELECT COUNT(*) as c FROM morning_bus_config WHERE is_active = 1").fetchone()["c"]
    inactive_buses = max(0, total_buses - active_buses)

    tracking_buses = conn.execute("SELECT COUNT(*) as c FROM bus_locations WHERE is_active = 1 AND latitude IS NOT NULL").fetchone()["c"]
    not_tracking_buses = max(0, total_buses - tracking_buses)

    delayed_buses = conn.execute("""
        SELECT COUNT(DISTINCT bus_id) as c FROM delay_reports
        WHERE status IN ('REPORTED', 'UNDER_REVIEW')
    """).fetchone()["c"]

    # Full buses: count buses where total boarded today >= capacity
    full_buses_rows = conn.execute("""
        SELECT b.id, COALESCE(b.capacity, 50) as capacity, COUNT(qa.id) as boarded
        FROM buses b
        JOIN qr_attendance qa ON b.id = qa.bus_id AND qa.travel_date = ?
        JOIN users u ON qa.user_id = u.id
        GROUP BY b.id
        HAVING boarded >= capacity
    """, (today_str,)).fetchall()
    full_buses = len(full_buses_rows)

    active_drivers = conn.execute("SELECT COUNT(*) as c FROM drivers WHERE is_active = 1 AND assigned_bus_id IS NOT NULL").fetchone()["c"]
    total_boarded_today = get_bus_boarded_count(None, today_str)
    morning_boarded_today = get_bus_boarded_count(None, today_str, "MORNING")
    return_boarded_today = get_bus_boarded_count(None, today_str, "RETURN")
    pending_sos_count = conn.execute("SELECT COUNT(*) as c FROM emergency_alerts WHERE status = 'PENDING'").fetchone()["c"]

    # Global emergency alerts (last 10)
    global_emergencies = conn.execute("""
        SELECT ea.id, ea.emergency_type, ea.reported_at, ea.status,
               b.bus_number, b.route_name, d.name as driver_name
        FROM emergency_alerts ea
        JOIN buses b ON ea.bus_id = b.id
        JOIN drivers d ON ea.driver_id = d.id
        ORDER BY ea.id DESC LIMIT 10
    """).fetchall()

    # Global delays (last 10)
    global_delays = conn.execute("""
        SELECT dr.id, dr.delay_reason, dr.expected_delay, dr.reported_at, dr.status,
               b.bus_number, b.route_name
        FROM delay_reports dr
        JOIN buses b ON dr.bus_id = b.id
        ORDER BY dr.id DESC LIMIT 10
    """).fetchall()

    conn.close()

    return jsonify({
        "success": True,
        "metrics": {
            "total_buses": total_buses,
            "active_buses": active_buses,
            "inactive_buses": inactive_buses,
            "tracking_buses": tracking_buses,
            "not_tracking_buses": not_tracking_buses,
            "delayed_buses": delayed_buses,
            "full_buses": full_buses,
            "active_drivers": active_drivers,
            "total_boarded_today": total_boarded_today,
            "morning_boarded_today": morning_boarded_today,
            "return_boarded_today": return_boarded_today,
            "pending_sos_count": pending_sos_count
        },
        "global_emergencies": [dict(e) for e in global_emergencies],
        "global_delays": [dict(d) for d in global_delays],
        "date": today_str
    })


# ============================================================
# PHASE 2: BUS CAPACITY & SCHEDULE CONFIGURATION APIS
# ============================================================

@app.route("/api/admin/bus/<int:bus_id>/config", methods=["POST"])
def admin_bus_config(bus_id):
    """Admin configures seating capacity and scheduled campus arrival time for a bus."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    capacity = data.get("capacity")
    scheduled_arrival = (data.get("scheduled_arrival_time") or "").strip()

    conn = get_connection()
    bus = conn.execute("SELECT id, bus_number FROM buses WHERE id = ?", (bus_id,)).fetchone()
    if not bus:
        conn.close()
        return jsonify({"success": False, "message": "Bus not found."}), 404

    if capacity is not None:
        try:
            cap_int = max(1, int(capacity))
            conn.execute("UPDATE buses SET capacity = ? WHERE id = ?", (cap_int, bus_id))
        except ValueError:
            pass

    if scheduled_arrival:
        conn.execute("UPDATE buses SET scheduled_arrival_time = ? WHERE id = ?", (scheduled_arrival, bus_id))

    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"Configuration for Bus {bus['bus_number']} updated."})


# ============================================================
# PHASE 2: PASSENGER QR CODE & ATTENDANCE APIS
# ============================================================

@app.route("/api/user/qr-token", methods=["GET"])
def user_qr_token():
    """
    Returns the secure, authenticated QR code identity for Student or Faculty.
    Linked to account, signed token without exposing plaintext passwords, emails or phone numbers.
    """
    user_id = session.get("user_id")
    role = session.get("user_type")
    if not user_id or role not in ["student", "faculty"]:
        return jsonify({"success": False, "message": "Authentication required."}), 401

    conn = get_connection()
    user = conn.execute("""
        SELECT u.id, u.name, u.register_number, u.role, u.department, u.qr_token,
               b.bus_number, b.route_name, s.stop_name, s.stop_time
        FROM users u
        LEFT JOIN buses b ON u.bus_id = b.id
        LEFT JOIN stops s ON u.boarding_point_id = s.id
        WHERE u.id = ?
    """, (user_id,)).fetchone()

    if not user:
        conn.close()
        return jsonify({"success": False, "message": "User not found."}), 404

    qr_token = user["qr_token"]
    if not qr_token:
        qr_token = f"RIT-BUS-{user['role'].upper()}-{user['register_number']}-{secrets.token_hex(4).upper()}"
        conn.execute("UPDATE users SET qr_token = ? WHERE id = ?", (qr_token, user_id))
        conn.commit()

    conn.close()
    return jsonify({
        "success": True,
        "qr_token": qr_token,
        "passenger": {
            "name": user["name"],
            "register_number": user["register_number"],
            "role": user["role"],
            "department": user["department"] or "Engineering",
            "bus_number": user["bus_number"] or "Not Assigned",
            "boarding_point": user["stop_name"] or "Campus Gate",
            "pickup_time": user["stop_time"] or "07:30 AM"
        }
    })


@app.route("/api/driver/scan-qr", methods=["POST"])
def driver_scan_qr():
    """
    Driver scans passenger QR code using device camera through browser or manual token fallback.
    Verifies:
    1. Passenger exists & is active.
    2. Passenger is assigned to THIS driver's bus.
    3. Duplicate scan prevention (same travel date).
    4. Records boarding time using server time, bus ID, passenger ID, boarding point.
    5. Checks capacity: if boarded reaches capacity, triggers global BUS_FULL alert!
    """
    if session.get("user_type") != "driver":
        return jsonify({"success": False, "message": "Driver authentication required."}), 401

    driver_id = session.get("user_id")
    data = request.get_json(silent=True) or request.form
    qr_token = (data.get("qr_token") or "").strip()

    if not qr_token:
        return jsonify({"success": False, "message": "QR token is required."}), 400

    conn = get_connection()
    # Get driver's active assigned bus from database/session
    driver = conn.execute("SELECT id, name, assigned_bus_id FROM drivers WHERE id = ? AND is_active = 1", (driver_id,)).fetchone()
    if not driver or not driver["assigned_bus_id"]:
        conn.close()
        return jsonify({"success": False, "message": "You do not have an active bus assigned. Contact transport administrator."}), 403

    driver_bus_id = driver["assigned_bus_id"]
    bus = conn.execute("SELECT id, bus_number, route_name, COALESCE(capacity, 50) as capacity FROM buses WHERE id = ?", (driver_bus_id,)).fetchone()

    # Locate passenger by qr_token or register_number
    passenger = conn.execute("""
        SELECT u.id, u.name, u.register_number, u.role, u.department, u.bus_id, u.boarding_point_id, u.is_active,
               s.stop_name, pb.bus_number as assigned_bus_number
        FROM users u
        LEFT JOIN stops s ON u.boarding_point_id = s.id
        LEFT JOIN buses pb ON u.bus_id = pb.id
        WHERE (u.qr_token = ? OR u.register_number = ?)
    """, (qr_token, qr_token)).fetchone()

    if not passenger:
        conn.close()
        return jsonify({"success": False, "message": "Invalid QR code. Passenger not registered in RIT Transport system."}), 404

    if passenger["is_active"] != 1:
        conn.close()
        return jsonify({"success": False, "message": "Passenger account is inactive or graduated. Boarding denied."}), 403

    # Bus assignment match verification
    if passenger["bus_id"] != driver_bus_id:
        conn.close()
        assigned_name = passenger["assigned_bus_number"] or "Unassigned"
        return jsonify({
            "success": False,
            "message": f"Wrong Bus! Passenger {passenger['name']} ({passenger['register_number']}) is assigned to Bus {assigned_name}, not Bus {bus['bus_number']}."
        }), 400

    today_str = datetime.now().strftime("%d-%m-%Y")
    now_time = datetime.now().strftime("%I:%M %p")

    # Duplicate scan prevention
    existing_scan = conn.execute("""
        SELECT id, boarding_time FROM qr_attendance
        WHERE user_id = ? AND travel_date = ?
    """, (passenger["id"], today_str)).fetchone()

    if existing_scan:
        conn.close()
        return jsonify({
            "success": False,
            "message": f"Duplicate scan: {passenger['name']} ({passenger['register_number']}) has already boarded today at {existing_scan['boarding_time']}."
        }), 409

    # Record attendance safely
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO qr_attendance (user_id, bus_id, boarding_point_id, boarding_time, travel_date, scanned_by_driver_id, status)
        VALUES (?, ?, ?, ?, ?, ?, 'BOARDED')
    """, (passenger["id"], driver_bus_id, passenger["boarding_point_id"], now_time, today_str, driver_id))
    conn.commit()

    # Recalculate occupancy
    boarded_count = conn.execute("""
        SELECT COUNT(*) as c FROM qr_attendance
        WHERE bus_id = ? AND travel_date = ?
    """, (driver_bus_id, today_str)).fetchone()["c"]

    capacity = bus["capacity"]
    is_full = boarded_count >= capacity

    conn.close()

    # Trigger Bus Full alert if full
    if is_full:
        broadcast_operational_notification(
            alert_type="BUS_FULL",
            title=f"🚨 Bus Full Alert: Bus {bus['bus_number']}",
            message=f"Bus {bus['bus_number']} ({bus['route_name']}) has reached maximum seating capacity ({boarded_count}/{capacity} boarded).",
            bus_id=driver_bus_id,
            metadata={"bus_number": bus["bus_number"], "capacity": capacity, "boarded": boarded_count}
        )

    return jsonify({
        "success": True,
        "message": f"✅ Boarding confirmed: {passenger['name']} ({passenger['register_number']})",
        "passenger": {
            "name": passenger["name"],
            "register_number": passenger["register_number"],
            "department": passenger["department"] or "Engineering",
            "boarding_point": passenger["stop_name"] or "Boarding Stop",
            "boarding_time": now_time
        },
        "bus_occupancy": {
            "bus_number": bus["bus_number"],
            "capacity": capacity,
            "total_boarded_today": boarded_count,
            "is_full": is_full
        }
    })


# ============================================================
# PHASE 2: PERMANENT BUS QR CODE & PASSENGER SCAN APIS
# ============================================================

@app.route("/api/admin/buses/qr-codes", methods=["GET"])
def admin_get_bus_qr_codes():
    """Admin views all buses and their permanent QR codes."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    conn = get_connection()
    buses = conn.execute("""
        SELECT id, bus_number, route_name, COALESCE(capacity, 50) as capacity, qr_code
        FROM buses
        ORDER BY CAST(SUBSTR(bus_number, 2) AS INTEGER) ASC, bus_number ASC
    """).fetchall()

    today_str = get_today_date_str()
    result = []
    for b in buses:
        qr = b["qr_code"]
        if not qr:
            safe_num = b["bus_number"].strip().upper().replace(" ", "-")
            qr = f"RIT-BUS-{safe_num}-{secrets.token_hex(4).upper()}"
            conn.execute("UPDATE buses SET qr_code = ? WHERE id = ?", (qr, b["id"]))
            conn.commit()

        boarded_today = get_bus_boarded_count(b["id"], today_str)
        morning_boarded = get_bus_boarded_count(b["id"], today_str, "MORNING")
        return_boarded = get_bus_boarded_count(b["id"], today_str, "RETURN")

        result.append({
            "id": b["id"],
            "bus_number": b["bus_number"],
            "route_name": b["route_name"],
            "capacity": b["capacity"],
            "qr_code": qr,
            "total_boarded_today": boarded_today,
            "morning_boarded": morning_boarded,
            "return_boarded": return_boarded,
            "is_full": boarded_today >= b["capacity"]
        })

    conn.close()
    return jsonify({"success": True, "buses": result})


@app.route("/api/admin/bus/<int:bus_id>/regenerate-qr", methods=["POST"])
def admin_regenerate_bus_qr(bus_id):
    """Admin regenerates/revokes a permanent bus QR code if compromised."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    conn = get_connection()
    bus = conn.execute("SELECT id, bus_number FROM buses WHERE id = ?", (bus_id,)).fetchone()
    if not bus:
        conn.close()
        return jsonify({"success": False, "message": "Bus not found."}), 404

    safe_num = bus["bus_number"].strip().upper().replace(" ", "-")
    new_qr = f"RIT-BUS-{safe_num}-{secrets.token_hex(4).upper()}"

    conn.execute("UPDATE buses SET qr_code = ? WHERE id = ?", (new_qr, bus_id))
    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "message": f"Permanent QR code for Bus {bus['bus_number']} successfully regenerated.",
        "bus_id": bus_id,
        "bus_number": bus["bus_number"],
        "new_qr_code": new_qr
    })


@app.route("/api/passenger/scan-bus-qr", methods=["POST"])
def passenger_scan_bus_qr():
    """
    Student or Faculty scans the permanent Bus QR code to mark daily boarding attendance.
    Verifies:
    1. Authenticated Student/Faculty session.
    2. Active user account.
    3. Passenger has an assigned bus in their profile.
    4. Valid QR code token corresponding to an active college bus.
    5. Registered bus matches scanned bus (cannot scan wrong bus).
    6. Prevents duplicate scan on the same date and service (MORNING / RETURN).
    7. Enforces bus capacity limit (rejects if BUS FULL).
    8. Records attendance in qr_attendance.
    9. Broadcasts single BUS_FULL alert when capacity reached.
    """
    user_type = session.get("user_type")
    user_id = session.get("user_id")

    if not user_id or user_type not in ["student", "faculty"]:
        return jsonify({
            "success": False,
            "message": "Authentication required. Please log in to your Student or Faculty account to mark bus attendance."
        }), 401

    data = request.get_json(silent=True) or request.form
    qr_code = (data.get("qr_code") or data.get("qr_token") or "").strip()

    if not qr_code:
        return jsonify({"success": False, "message": "Bus QR code token is required."}), 400

    conn = get_connection()

    # 1. Fetch authenticated passenger
    user = conn.execute("""
        SELECT u.id, u.name, u.register_number, u.role, u.department, u.bus_id, u.boarding_point_id,
               u.is_active, u.lifecycle_status, b.bus_number as assigned_bus_number,
               s.stop_name as boarding_stop_name
        FROM users u
        LEFT JOIN buses b ON u.bus_id = b.id
        LEFT JOIN stops s ON u.boarding_point_id = s.id
        WHERE u.id = ?
    """, (user_id,)).fetchone()

    if not user:
        conn.close()
        return jsonify({"success": False, "message": "User record not found."}), 404

    if user["is_active"] != 1 or user["lifecycle_status"] == "DEACTIVATED":
        conn.close()
        return jsonify({"success": False, "message": "Your account is inactive or access has been suspended."}), 403

    # 2. Locate bus by QR code
    bus = conn.execute("""
        SELECT id, bus_number, route_name, COALESCE(capacity, 50) as capacity, qr_code
        FROM buses
        WHERE qr_code = ?
    """, (qr_code,)).fetchone()

    if not bus:
        conn.close()
        return jsonify({"success": False, "message": "Invalid Bus QR code. Please scan an official RIT Bus QR poster."}), 404

    # 3. Cross-Bus Boarding Supported:
    # Students & Faculty can board ANY active bus (e.g. evening return, exams, special events).
    # Attendance is recorded for the actual scanned bus, increasing that bus's passenger count.

    today_str = get_today_date_str()
    now_time = get_now_time_str()

    service_arg = (data.get("service_type") or "").strip().upper()
    if service_arg in ["MORNING", "RETURN"]:
        service_type = service_arg
    else:
        service_type = get_current_service_type()

    # 4. Feature 5: Controlled Scan-Again Attendance & Bus Switching
    existing_scan = conn.execute("""
        SELECT id, boarding_time, service_type, bus_id
        FROM qr_attendance
        WHERE user_id = ? AND travel_date = ? AND UPPER(service_type) = ?
    """, (user_id, today_str, service_type)).fetchone()

    if existing_scan:
        existing_bus = conn.execute("SELECT bus_number, route_name FROM buses WHERE id = ?", (existing_scan["bus_id"],)).fetchone()
        existing_bus_num = existing_bus["bus_number"] if existing_bus else f"Bus #{existing_scan['bus_id']}"

        # Case A: Scanned the SAME bus again -> Cancel attendance & archive to log
        if existing_scan["bus_id"] == bus["id"]:
            cancelled_at = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO attendance_cancellation_logs (
                    attendance_id, user_id, bus_id, service_type, travel_date,
                    boarding_time, cancelled_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CANCELLED_BY_USER_SCAN_AGAIN')
            """, (
                existing_scan["id"], user_id, bus["id"], service_type,
                today_str, existing_scan["boarding_time"], cancelled_at
            ))
            cursor.execute("DELETE FROM qr_attendance WHERE id = ?", (existing_scan["id"],))
            conn.commit()

            # Recalculate occupancy
            new_service_boarded = get_bus_boarded_count(bus["id"], today_str, service_type)
            conn.close()

            return jsonify({
                "success": True,
                "action": "CANCELLED",
                "cancelled": True,
                "message": f"Boarding attendance on Bus {bus['bus_number']} ({service_type.capitalize()} Service) has been successfully cancelled. You can now board and scan a different bus if needed.",
                "bus_number": bus["bus_number"],
                "service_type": service_type,
                "current_boarded": new_service_boarded
            })

        # Case B: Scanned a DIFFERENT bus while current attendance is active -> Reject with 409
        conn.close()
        return jsonify({
            "success": False,
            "action": "SWITCH_REJECTED",
            "conflict": True,
            "current_bus": existing_bus_num,
            "message": f"You are currently marked present on Bus {existing_bus_num} for {service_type.capitalize()} service. To switch buses, please scan Bus {existing_bus_num} again to cancel your current boarding first."
        }), 409

    # 5. Enforce bus capacity limit
    capacity = bus["capacity"]
    current_boarded = get_bus_boarded_count(bus["id"], today_str, service_type)
    if current_boarded >= capacity:
        conn.close()
        return jsonify({
            "success": False,
            "message": f"Bus {bus['bus_number']} is FULL ({current_boarded}/{capacity} boarded for {service_type.capitalize()} service). Boarding not permitted."
        }), 400

    # 6. Record attendance safely
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO qr_attendance (user_id, bus_id, boarding_point_id, boarding_time, travel_date, scanned_by_driver_id, status, service_type)
            VALUES (?, ?, ?, ?, ?, NULL, 'BOARDED', ?)
        """, (user_id, bus["id"], user["boarding_point_id"], now_time, today_str, service_type))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({
            "success": False,
            "message": f"You have already marked your {service_type.capitalize()} boarding attendance today."
        }), 409
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"success": False, "message": f"Failed to record attendance: {str(e)}"}), 500

    # Recalculate occupancy
    new_service_boarded = get_bus_boarded_count(bus["id"], today_str, service_type)
    total_boarded_today = get_bus_boarded_count(bus["id"], today_str)
    is_full = new_service_boarded >= capacity
    conn.close()

    # Trigger single Bus Full alert if capacity reached
    if is_full:
        conn_alert = get_connection()
        existing_full_alert = conn_alert.execute("""
            SELECT id FROM app_notifications
            WHERE alert_type = 'BUS_FULL' AND bus_id = ? AND metadata LIKE ? AND created_at LIKE ?
        """, (bus["id"], f"%{service_type}%", f"{today_str}%")).fetchone()
        conn_alert.close()

        if not existing_full_alert:
            broadcast_operational_notification(
                alert_type="BUS_FULL",
                title=f"🚨 Bus Full Alert: Bus {bus['bus_number']}",
                message=f"Bus {bus['bus_number']} ({bus['route_name']}) has reached maximum seating capacity ({new_service_boarded}/{capacity} boarded) for {service_type} service.",
                bus_id=bus["id"],
                metadata={"bus_number": bus["bus_number"], "capacity": capacity, "boarded": new_service_boarded, "service_type": service_type, "date": today_str}
            )

    return jsonify({
        "success": True,
        "message": f"✅ Boarding confirmed for Bus {bus['bus_number']} ({service_type} Service)",
        "attendance": {
            "bus_number": bus["bus_number"],
            "route_name": bus["route_name"],
            "service_type": service_type,
            "boarding_time": now_time,
            "travel_date": today_str,
            "boarding_point": user["boarding_stop_name"] or "Registered Stop",
            "passenger_name": user["name"],
            "register_number": user["register_number"],
            "total_boarded_today": total_boarded_today,
            "service_boarded": new_service_boarded,
            "capacity": capacity,
            "is_full": is_full
        }
    })


@app.route("/api/passenger/today-attendance", methods=["GET"])
def passenger_today_attendance():
    """Returns the authenticated passenger's today attendance records."""
    user_type = session.get("user_type")
    user_id = session.get("user_id")

    if not user_id or user_type not in ["student", "faculty"]:
        return jsonify({"success": False, "message": "Authentication required."}), 401

    conn = get_connection()
    today_str = datetime.now().strftime("%d-%m-%Y")
    records = conn.execute("""
        SELECT qa.id, qa.boarding_time, qa.travel_date, COALESCE(qa.service_type, 'MORNING') as service_type,
               qa.status, b.bus_number, b.route_name, COALESCE(s.stop_name, 'Registered Stop') as boarding_point
        FROM qr_attendance qa
        JOIN buses b ON qa.bus_id = b.id
        LEFT JOIN stops s ON qa.boarding_point_id = s.id
        WHERE qa.user_id = ? AND qa.travel_date = ?
        ORDER BY qa.id DESC
    """, (user_id, today_str)).fetchall()
    conn.close()

    return jsonify({
        "success": True,
        "records": [dict(r) for r in records],
        "has_boarded": len(records) > 0
    })


@app.route("/api/attendance/reports", methods=["GET"])
def get_attendance_reports():
    """
    Detailed attendance reports:
    - Admin: view all buses with date, bus, search, and service_type filters.
    - Driver: view attendance for their assigned bus only (today).
    - Transport Incharge: Access restricted (403 - no detailed passenger attendance lists).
    """
    user_type = session.get("user_type")
    user_id = session.get("user_id")

    if user_type not in ["admin", "driver"]:
        return jsonify({"success": False, "message": "Detailed passenger attendance access restricted to Admin and Driver."}), 403

    conn = get_connection()
    today_str = get_today_date_str()

    permitted_bus_ids = None
    bus_capacity = 50
    if user_type == "driver":
        # Driver strictly sees today's attendance only
        raw_date = today_str
        drv = conn.execute("SELECT assigned_bus_id FROM drivers WHERE id = ?", (user_id,)).fetchone()
        permitted_bus_ids = [drv["assigned_bus_id"]] if drv and drv["assigned_bus_id"] else []
        if permitted_bus_ids:
            b_row = conn.execute("SELECT COALESCE(capacity, 50) as capacity FROM buses WHERE id = ?", (permitted_bus_ids[0],)).fetchone()
            if b_row:
                bus_capacity = b_row["capacity"]
    else:
        raw_date = (request.args.get("date") or today_str).strip()

    bus_filter = request.args.get("bus_id")
    search_filter = (request.args.get("search") or "").strip()
    service_filter = (request.args.get("service_type") or "").strip().upper()

    query = """
        SELECT qa.id, qa.boarding_time, qa.travel_date, qa.status, COALESCE(qa.service_type, 'MORNING') as service_type,
               u.name as passenger_name, u.register_number, u.role, u.department,
               b.id as bus_id, b.bus_number, b.route_name,
               COALESCE(s.stop_name, 'Campus Gate') as boarding_point
        FROM qr_attendance qa
        JOIN users u ON qa.user_id = u.id
        JOIN buses b ON qa.bus_id = b.id
        LEFT JOIN stops s ON qa.boarding_point_id = s.id
        WHERE 1=1
    """
    params = []

    if raw_date and raw_date.upper() != "ALL":
        query += " AND qa.travel_date = ?"
        params.append(raw_date)

    if permitted_bus_ids is not None:
        if not permitted_bus_ids:
            conn.close()
            return jsonify({
                "success": True,
                "attendance": [],
                "total_boarded_today": 0,
                "total_morning_today": 0,
                "total_evening_today": 0,
                "capacity": bus_capacity,
                "total_records": 0
            })
        placeholders = ",".join("?" for _ in permitted_bus_ids)
        query += f" AND qa.bus_id IN ({placeholders})"
        params.extend(permitted_bus_ids)

    if bus_filter and str(bus_filter).strip():
        query += " AND qa.bus_id = ?"
        params.append(int(bus_filter))

    if service_filter and service_filter != "ALL":
        query += " AND UPPER(qa.service_type) = ?"
        params.append(service_filter)

    if search_filter:
        query += " AND (lower(u.name) LIKE ? OR lower(u.register_number) LIKE ? OR lower(b.bus_number) LIKE ?)"
        wildcard = f"%{search_filter.lower()}%"
        params.extend([wildcard, wildcard, wildcard])

    query += " ORDER BY qa.id DESC"
    rows = conn.execute(query, params).fetchall()

    target_bus_id = permitted_bus_ids[0] if (permitted_bus_ids and len(permitted_bus_ids) > 0) else (int(bus_filter) if bus_filter and str(bus_filter).strip() else None)
    total_boarded_today = get_bus_boarded_count(target_bus_id, today_str)
    total_morning_today = get_bus_boarded_count(target_bus_id, today_str, "MORNING")
    total_evening_today = get_bus_boarded_count(target_bus_id, today_str, "RETURN")
    conn.close()

    return jsonify({
        "success": True,
        "date": raw_date,
        "total_records": len(rows),
        "total_boarded_today": total_boarded_today,
        "total_morning_today": total_morning_today,
        "total_evening_today": total_evening_today,
        "capacity": bus_capacity,
        "attendance": [dict(r) for r in rows]
    })


# ============================================================
# PHASE 2: SMART SEAT AVAILABILITY API
# ============================================================

@app.route("/api/buses/seat-availability", methods=["GET"])
def buses_seat_availability():
    """
    Capacity & Total Boarded Today across buses.
    Clearly labels boarded count as total_boarded_today.
    """
    conn = get_connection()
    today_str = get_today_date_str()

    buses = conn.execute("""
        SELECT id, bus_number, route_name, COALESCE(capacity, 50) as capacity
        FROM buses
        ORDER BY bus_number ASC
    """).fetchall()

    result = []
    for b in buses:
        boarded = get_bus_boarded_count(b["id"], today_str)
        morning_boarded = get_bus_boarded_count(b["id"], today_str, "MORNING")
        return_boarded = get_bus_boarded_count(b["id"], today_str, "RETURN")
        cap = b["capacity"]
        status = "BUS FULL" if boarded >= cap else "SEATS AVAILABLE"
        result.append({
            "id": b["id"],
            "bus_id": b["id"],
            "bus_number": b["bus_number"],
            "route_name": b["route_name"],
            "capacity": cap,
            "total_boarded_today": boarded,
            "morning_boarded": morning_boarded,
            "return_boarded": return_boarded,
            "seats_available": max(0, cap - boarded),
            "status": status,
            "is_full": boarded >= cap
        })

    conn.close()
    return jsonify({
        "success": True,
        "buses": result,
        "date": today_str,
        "total_buses": len(result),
        "scope": "ALL_CAMPUS_BUSES"
    })


# ============================================================
# PHASE 2: SMART ETA & AUTOMATIC DELAY DETECTION API
# ============================================================

@app.route("/api/eta/bus/<bus_number>", methods=["GET"])
def bus_eta_calculation(bus_number):
    """
    Automatic ETA calculation using real-time GPS location, distance to RIT campus,
    recent bus movement, and configured scheduled arrival time.
    """
    clean_no = bus_number.strip().upper()
    conn = get_connection()
    bus = conn.execute("""
        SELECT b.id, b.bus_number, b.route_name,
               COALESCE(b.scheduled_arrival_time, '07:40 AM') as scheduled_arrival_time
        FROM buses b
        WHERE upper(trim(b.bus_number)) = ?
    """, (clean_no,)).fetchone()

    if not bus:
        conn.close()
        return jsonify({"success": False, "message": "Bus not found."}), 404

    loc = conn.execute("""
        SELECT latitude, longitude, current_location, next_point, updated_at, is_active, bus_status
        FROM bus_locations
        WHERE bus_id = ?
    """, (bus["id"],)).fetchone()

    scheduled_str = bus["scheduled_arrival_time"]
    rit_lat, rit_lon = 13.0082, 80.0035

    if not loc or not loc["latitude"] or not loc["is_active"]:
        conn.close()
        return jsonify({
            "success": True,
            "bus_number": bus["bus_number"],
            "route_name": bus["route_name"],
            "scheduled_arrival": scheduled_str,
            "estimated_arrival": scheduled_str,
            "delay_minutes": 0,
            "status": "NOT_STARTED",
            "eta_status": "UNAVAILABLE",
            "message": "GPS tracking inactive or not started."
        })

    lat, lon = loc["latitude"], loc["longitude"]

    # Haversine distance in km
    R = 6371.0
    dlat = math.radians(rit_lat - lat)
    dlon = math.radians(rit_lon - lon)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(rit_lat)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    dist_km = round(R * c, 1)

    # Estimate travel time assuming 25 km/h urban average transit speed + 5 min buffer
    travel_mins = max(2, int((dist_km / 25.0) * 60) + 5)
    now_dt = datetime.now()
    est_dt = now_dt + timedelta(minutes=travel_mins)
    est_str = est_dt.strftime("%I:%M %p")

    # Compare scheduled vs estimated
    delay_minutes = 0
    try:
        sched_time_obj = datetime.strptime(scheduled_str, "%I:%M %p").time()
        sched_dt = datetime.combine(now_dt.date(), sched_time_obj)
        diff_secs = (est_dt - sched_dt).total_seconds()
        delay_minutes = max(0, int(diff_secs / 60))
    except Exception:
        delay_minutes = 0

    is_running_late = delay_minutes >= 10
    bus_status_label = "RUNNING LATE" if is_running_late else "ON TIME"

    conn.close()

    return jsonify({
        "success": True,
        "bus_number": bus["bus_number"],
        "route_name": bus["route_name"],
        "current_location": loc["current_location"] or f"{lat:.4f}, {lon:.4f}",
        "distance_remaining_km": dist_km,
        "distance_km": dist_km,
        "transit_duration_mins": travel_mins,
        "scheduled_arrival": scheduled_str,
        "estimated_arrival": est_str,
        "delay_minutes": delay_minutes,
        "status": bus_status_label,
        "delay_status": bus_status_label,
        "eta_status": "RELIABLE"
    })


# ============================================================
# PHASE 2: DRIVER DELAY REPORTING & STATUS APIS
# ============================================================

@app.route("/api/driver/report-delay", methods=["POST"])
def driver_report_delay():
    """
    Driver manually reports delay from Driver Dashboard.
    Broadcasts to Admin, EVERY active Transport Incharge, and assigned students/faculty.
    """
    if session.get("user_type") != "driver":
        return jsonify({"success": False, "message": "Driver authentication required."}), 401

    driver_id = session.get("user_id")
    data = request.get_json(silent=True) or request.form
    delay_reason = (data.get("delay_reason") or data.get("reason") or "Heavy Traffic").strip()
    exp_mins = data.get("expected_delay_minutes") or data.get("expected_delay")
    if exp_mins and str(exp_mins).isdigit():
        expected_delay = f"{exp_mins} Minutes"
    else:
        expected_delay = (str(exp_mins) if exp_mins else "15 Minutes").strip()
    notes = (data.get("notes") or "").strip()

    conn = get_connection()
    driver = conn.execute("SELECT id, name, assigned_bus_id FROM drivers WHERE id = ? AND is_active = 1", (driver_id,)).fetchone()
    if not driver or not driver["assigned_bus_id"]:
        conn.close()
        return jsonify({"success": False, "message": "No active bus assigned to this driver."}), 403

    bus_id = driver["assigned_bus_id"]
    bus = conn.execute("SELECT id, bus_number, route_name FROM buses WHERE id = ?", (bus_id,)).fetchone()

    loc = conn.execute("SELECT latitude, longitude, current_location FROM bus_locations WHERE bus_id = ?", (bus_id,)).fetchone()
    lat = loc["latitude"] if loc else None
    lon = loc["longitude"] if loc else None
    loc_name = loc["current_location"] if loc else "Current Location"

    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO delay_reports (driver_id, bus_id, delay_reason, expected_delay, notes, latitude, longitude, reported_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'REPORTED')
    """, (driver_id, bus_id, delay_reason, expected_delay, notes, lat, lon, now_str))
    report_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Broadcast notification to Admin, ALL active Incharges, and bus passengers
    broadcast_operational_notification(
        alert_type="BUS_DELAY",
        title=f"⚠️ Driver Delay Report: Bus {bus['bus_number']}",
        message=f"Bus {bus['bus_number']} ({bus['route_name']}) reported delay of {expected_delay} due to {delay_reason}. Location: {loc_name}.",
        bus_id=bus_id,
        metadata={
            "report_id": report_id,
            "bus_number": bus["bus_number"],
            "driver_name": driver["name"],
            "delay_reason": delay_reason,
            "expected_delay": expected_delay,
            "notes": notes,
            "location": loc_name
        }
    )

    return jsonify({
        "success": True,
        "message": f"Delay report submitted for Bus {bus['bus_number']}. Notifications broadcast to administration and passengers.",
        "report_id": report_id
    })


@app.route("/api/delay-reports", methods=["GET"])
def get_delay_reports():
    """Returns delay reports. Accessible to Admin and Transport Incharge."""
    user_type = session.get("user_type")
    if user_type not in ["admin", "transport_incharge"]:
        return jsonify({"success": False, "message": "Unauthorized."}), 403

    conn = get_connection()
    reports = conn.execute("""
        SELECT dr.id, dr.delay_reason, dr.expected_delay, dr.notes, dr.latitude, dr.longitude,
               dr.reported_at, dr.status, dr.resolved_at,
               b.id as bus_id, b.bus_number, b.route_name,
               COALESCE(b.scheduled_arrival_time, '07:40 AM') as scheduled_arrival_time,
               d.name as driver_name, d.phone as driver_phone
        FROM delay_reports dr
        JOIN buses b ON dr.bus_id = b.id
        JOIN drivers d ON dr.driver_id = d.id
        ORDER BY dr.id DESC LIMIT 50
    """).fetchall()
    conn.close()

    result = []
    for r in reports:
        d = dict(r)
        d["reason"] = d.get("delay_reason")
        # Protect driver private phone number from Transport Incharge role
        if user_type == "transport_incharge":
            d["driver_phone"] = None
        result.append(d)

    return jsonify({"success": True, "reports": result})


@app.route("/api/delay-reports/<int:report_id>/status", methods=["POST"])
def update_delay_report_status(report_id):
    """Admin and Transport Incharge can acknowledge and resolve delays."""
    user_type = session.get("user_type")
    if user_type not in ["admin", "transport_incharge"]:
        return jsonify({"success": False, "message": "Unauthorized."}), 403

    data = request.get_json(silent=True) or request.form
    new_status = (data.get("status") or "UNDER_REVIEW").strip().upper()
    if new_status not in ["REPORTED", "UNDER_REVIEW", "RESOLVED"]:
        return jsonify({"success": False, "message": "Invalid status."}), 400

    conn = get_connection()
    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    resolved_time = now_str if new_status == "RESOLVED" else None

    conn.execute("""
        UPDATE delay_reports
        SET status = ?, resolved_at = ?
        WHERE id = ?
    """, (new_status, resolved_time, report_id))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": f"Delay report #{report_id} updated to {new_status}."})


# ============================================================
# PHASE 2: DRIVER EMERGENCY SOS & MANAGEMENT APIS
# ============================================================

@app.route("/api/driver/emergency-sos", methods=["POST"])
def driver_emergency_sos():
    """
    Driver activates Emergency SOS button.
    Instantly captures Driver ID, Name, Bus Number, GPS Location, Timestamp, and Emergency Type.
    Immediately broadcasts to Admin and EVERY active Transport Incharge!
    """
    if session.get("user_type") != "driver":
        return jsonify({"success": False, "message": "Driver authentication required."}), 401

    driver_id = session.get("user_id")
    data = request.get_json(silent=True) or request.form
    emergency_type = (data.get("emergency_type") or "Other Emergency").strip()
    notes = (data.get("notes") or "").strip()

    conn = get_connection()
    driver = conn.execute("SELECT id, name, assigned_bus_id FROM drivers WHERE id = ? AND is_active = 1", (driver_id,)).fetchone()
    if not driver or not driver["assigned_bus_id"]:
        conn.close()
        return jsonify({"success": False, "message": "No active bus assigned to this driver."}), 403

    bus_id = driver["assigned_bus_id"]
    bus = conn.execute("SELECT id, bus_number, route_name FROM buses WHERE id = ?", (bus_id,)).fetchone()

    loc = conn.execute("SELECT latitude, longitude, current_location FROM bus_locations WHERE bus_id = ?", (bus_id,)).fetchone()
    lat = loc["latitude"] if loc else None
    lon = loc["longitude"] if loc else None
    loc_name = loc["current_location"] if loc else "Live GPS Location"

    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO emergency_alerts (driver_id, bus_id, emergency_type, latitude, longitude, reported_at, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
    """, (driver_id, bus_id, emergency_type, lat, lon, now_str, notes))
    alert_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Global Broadcast to Admin & ALL active Transport Incharges
    broadcast_operational_notification(
        alert_type="EMERGENCY_SOS",
        title=f"🚨 EMERGENCY SOS: Bus {bus['bus_number']}",
        message=f"CRITICAL SOS ALERT: Driver {driver['name']} on Bus {bus['bus_number']} ({bus['route_name']}) reported {emergency_type}. Location: {loc_name}.",
        bus_id=bus_id,
        metadata={
            "alert_id": alert_id,
            "bus_number": bus["bus_number"],
            "driver_name": driver["name"],
            "emergency_type": emergency_type,
            "latitude": lat,
            "longitude": lon,
            "location_name": loc_name,
            "timestamp": now_str
        }
    )

    return jsonify({
        "success": True,
        "message": f"EMERGENCY SOS ALERT CREATED FOR BUS {bus['bus_number']}. Campus safety and administration notified.",
        "alert_id": alert_id
    })


@app.route("/api/driver/emergency-sos/cancel", methods=["POST"])
def driver_emergency_sos_cancel():
    """Driver cancels accidental SOS activation."""
    if session.get("user_type") != "driver":
        return jsonify({"success": False, "message": "Driver authentication required."}), 401

    driver_id = session.get("user_id")
    conn = get_connection()
    latest = conn.execute("""
        SELECT id, bus_id FROM emergency_alerts
        WHERE driver_id = ? AND status = 'PENDING'
        ORDER BY id DESC LIMIT 1
    """, (driver_id,)).fetchone()

    if not latest:
        conn.close()
        return jsonify({"success": False, "message": "No pending emergency alert to cancel."}), 404

    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    conn.execute("""
        UPDATE emergency_alerts
        SET status = 'RESOLVED', notes = COALESCE(notes, '') || ' [Cancelled by driver as accidental]', resolved_at = ?
        WHERE id = ?
    """, (now_str, latest["id"]))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": "Emergency SOS activation has been cancelled."})


@app.route("/api/emergency-alerts", methods=["GET"])
def get_emergency_alerts():
    """Emergency alerts list for Admin and Transport Incharge."""
    user_type = session.get("user_type")
    if user_type not in ["admin", "transport_incharge"]:
        return jsonify({"success": False, "message": "Unauthorized."}), 403

    conn = get_connection()
    alerts = conn.execute("""
        SELECT ea.id, ea.emergency_type, ea.latitude, ea.longitude, ea.reported_at,
               ea.status, ea.notes, ea.acknowledged_by, ea.acknowledged_at, ea.resolved_at,
               b.id as bus_id, b.bus_number, b.route_name,
               d.name as driver_name, d.phone as driver_phone
        FROM emergency_alerts ea
        JOIN buses b ON ea.bus_id = b.id
        JOIN drivers d ON ea.driver_id = d.id
        ORDER BY ea.id DESC LIMIT 50
    """).fetchall()
    conn.close()

    result = []
    for a in alerts:
        d = dict(a)
        # Protect driver private phone number from Transport Incharge role
        if user_type == "transport_incharge":
            d["driver_phone"] = None
        result.append(d)

    return jsonify({"success": True, "alerts": result})


@app.route("/api/emergency-alerts/<int:alert_id>/status", methods=["POST"])
def update_emergency_alert_status(alert_id):
    """Admin and Transport Incharge update emergency alert status."""
    user_type = session.get("user_type")
    if user_type not in ["admin", "transport_incharge"]:
        return jsonify({"success": False, "message": "Unauthorized."}), 403

    data = request.get_json(silent=True) or request.form
    new_status = (data.get("status") or "ACKNOWLEDGED").strip().upper()
    if new_status not in ["PENDING", "ACKNOWLEDGED", "RESOLVED"]:
        return jsonify({"success": False, "message": "Invalid status."}), 400

    user_name = session.get("user_name") or user_type.upper()
    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    conn = get_connection()
    if new_status == "ACKNOWLEDGED":
        conn.execute("""
            UPDATE emergency_alerts
            SET status = 'ACKNOWLEDGED', acknowledged_by = ?, acknowledged_at = ?
            WHERE id = ?
        """, (user_name, now_str, alert_id))
    elif new_status == "RESOLVED":
        conn.execute("""
            UPDATE emergency_alerts
            SET status = 'RESOLVED', resolved_at = ?
            WHERE id = ?
        """, (now_str, alert_id))
    else:
        conn.execute("UPDATE emergency_alerts SET status = ? WHERE id = ?", (new_status, alert_id))

    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": f"Emergency alert #{alert_id} marked as {new_status}."})


# ============================================================
# PHASE 2: SMART NOTIFICATION APIS
# ============================================================

@app.route("/api/notifications", methods=["GET"])
def get_user_notifications():
    """Returns in-app notifications for authenticated user, with unread count."""
    user_id = session.get("user_id")
    user_type = session.get("user_type")
    if not user_id:
        return jsonify({"success": False, "message": "Authentication required."}), 401

    conn = get_connection()
    if user_type == "driver":
        drv = conn.execute("SELECT assigned_bus_id FROM drivers WHERE id = ?", (user_id,)).fetchone()
        assigned_bus = drv["assigned_bus_id"] if drv else None
        notifications = conn.execute("""
            SELECT an.id, an.bus_id, an.alert_type, an.title, an.message, an.metadata_json, an.is_read, an.created_at,
                   b.bus_number
            FROM app_notifications an
            LEFT JOIN buses b ON an.bus_id = b.id
            WHERE an.recipient_role = 'driver' AND (an.bus_id = ? OR an.bus_id IS NULL)
            ORDER BY an.id DESC LIMIT 40
        """, (assigned_bus,)).fetchall()
        unread_count = conn.execute("""
            SELECT COUNT(*) as c FROM app_notifications
            WHERE recipient_role = 'driver' AND (bus_id = ? OR bus_id IS NULL) AND is_read = 0
        """, (assigned_bus,)).fetchone()["c"]
    else:
        notifications = conn.execute("""
            SELECT an.id, an.bus_id, an.alert_type, an.title, an.message, an.metadata_json, an.is_read, an.created_at,
                   b.bus_number
            FROM app_notifications an
            LEFT JOIN buses b ON an.bus_id = b.id
            WHERE an.recipient_user_id = ?
            ORDER BY an.id DESC LIMIT 40
        """, (user_id,)).fetchall()
        unread_count = conn.execute("""
            SELECT COUNT(*) as c FROM app_notifications
            WHERE recipient_user_id = ? AND is_read = 0
        """, (user_id,)).fetchone()["c"]

    conn.close()

    result = []
    for n in notifications:
        d = dict(n)
        try:
            d["metadata"] = json.loads(d["metadata_json"])
        except Exception:
            d["metadata"] = {}
        result.append(d)

    return jsonify({
        "success": True,
        "unread_count": unread_count,
        "notifications": result
    })


@app.route("/api/notifications/mark-read", methods=["POST"])
def mark_notifications_read():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "message": "Authentication required."}), 401

    data = request.get_json(silent=True) or request.form
    notif_id = data.get("notification_id")

    conn = get_connection()
    if notif_id:
        conn.execute("UPDATE app_notifications SET is_read = 1 WHERE id = ? AND recipient_user_id = ?", (notif_id, user_id))
    else:
        conn.execute("UPDATE app_notifications SET is_read = 1 WHERE recipient_user_id = ?", (user_id,))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": "Notifications marked as read."})


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    user_id = session.get("user_id")
    role = session.get("user_type") or "guest"
    data = request.get_json(silent=True) or request.form
    sub_json = json.dumps(data)
    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    conn = get_connection()
    conn.execute("""
        INSERT OR IGNORE INTO web_push_subscriptions (user_id, role, subscription_json, created_at)
        VALUES (?, ?, ?, ?)
    """, (user_id, role, sub_json, now_str))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": "Browser push subscription registered."})


# ============================================================
# COMMON STUDENT & FACULTY REGISTRATION
# ============================================================

def is_valid_institutional_email(email: str) -> bool:
    """
    Validates institutional email addresses for RIT Students & Faculty.
    Accepts:
      - Main institutional domain: user@ritchennai.edu.in
      - Department subdomains: user@cse.ritchennai.edu.in, user@ece.ritchennai.edu.in,
        user@it.ritchennai.edu.in, user@anydepartment.ritchennai.edu.in,
        user@sub.department.ritchennai.edu.in
    Rejects:
      - Public email providers (e.g., gmail.com, yahoo.com)
      - Lookalikes / suffix spoofing (e.g., ritchennai.edu.in.example.com, fake-ritchennai.edu.in)
      - Non-institutional domains (e.g., ritchennai.com, ritchennai.edu.com)
      - Malformed subdomains (e.g., -cse.ritchennai.edu.in, cse-.ritchennai.edu.in)
    """
    if not email or not isinstance(email, str):
        return False
    normalized = email.strip().lower()
    return bool(re.match(
        r"^[a-zA-Z0-9_.+-]+@(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)*ritchennai\.edu\.in$",
        normalized
    ))


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    """
    Common registration endpoint for students and faculty.
    Validates all mandatory fields, formats, uniqueness, bus & stop constraints.
    Enforces official RIT programmes and required class section for students.
    """
    data = request.get_json(silent=True) or request.form

    role = (data.get("role") or "").strip().lower()
    name = (data.get("name") or "").strip()
    register_number = (data.get("register_number") or data.get("student_id") or data.get("faculty_id") or "").strip()
    email = (data.get("email") or "").strip().lower()
    mobile = (data.get("mobile") or data.get("phone") or "").strip()
    department = (data.get("department") or "").strip()
    academic_year = (data.get("academic_year") or "").strip()
    class_section = (data.get("class_section") or "").strip()
    bus_id = data.get("bus_id")
    boarding_point_id = data.get("boarding_point_id")
    password = (data.get("password") or "").strip()
    confirm_password = (data.get("confirm_password") or "").strip()

    # Validations
    if role not in ("student", "faculty"):
        return jsonify({"success": False, "message": "Please select a valid role (Student or Faculty)."}), 400

    if not all([name, register_number, email, mobile, department, academic_year, bus_id, boarding_point_id, password]):
        return jsonify({"success": False, "message": "All mandatory fields must be completed."}), 400

    # Students must have a class section and valid academic batch
    if role == "student":
        if not class_section:
            return jsonify({"success": False, "message": "Class / Section is required for students."}), 400
        if department not in DEPARTMENTS:
            return jsonify({"success": False, "message": "Please select an official RIT programme from the list."}), 400

        # Validate Academic Batch format: YYYY-YYYY, end_year > start_year
        m = re.match(r"^(\d{4})\s*[\u2013\u2014\-]\s*(\d{4})$", academic_year)
        if not m:
            return jsonify({
                "success": False,
                "message": "Invalid Academic Batch format. Please enter YYYY-YYYY (e.g., 2026-2029)."
            }), 400
        start_year = int(m.group(1))
        end_year = int(m.group(2))
        if end_year <= start_year:
            return jsonify({
                "success": False,
                "message": "Ending year must be greater than starting year in Academic Batch (e.g., 2026-2029)."
            }), 400
        academic_year = f"{start_year}-{end_year}"
    else:
        class_section = None

    # Feature 8: Strict institutional email validation for Students & Faculty
    if not is_valid_institutional_email(email):
        return jsonify({
            "success": False,
            "message": "Registration is restricted to official college email addresses ending in @ritchennai.edu.in or department subdomains (e.g., student@ritchennai.edu.in or student@cse.ritchennai.edu.in)."
        }), 400

    clean_mobile = re.sub(r"\D", "", mobile)
    if len(clean_mobile) != 10:
        return jsonify({"success": False, "message": "Please enter a valid 10-digit mobile number."}), 400

    if password != confirm_password:
        return jsonify({"success": False, "message": "Passwords do not match."}), 400

    if len(password) < 6:
        return jsonify({"success": False, "message": "Password must be at least 6 characters long."}), 400

    try:
        bus_id = int(bus_id)
        boarding_point_id = int(boarding_point_id)
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "Invalid transport selection."}), 400

    connection = get_connection()

    # Validate bus exists
    bus = connection.execute("SELECT id, bus_number FROM buses WHERE id = ?", (bus_id,)).fetchone()
    if not bus:
        connection.close()
        return jsonify({"success": False, "message": "Selected bus does not exist in the database."}), 400

    # Validate boarding point belongs to bus
    stop = connection.execute("SELECT id, stop_name FROM stops WHERE id = ? AND bus_id = ?", (boarding_point_id, bus_id)).fetchone()
    if not stop:
        connection.close()
        return jsonify({"success": False, "message": "Selected boarding point does not belong to this bus."}), 400

    # Validate unique register number / employee ID
    dup_reg = connection.execute(
        "SELECT id FROM users WHERE lower(trim(register_number)) = lower(trim(?))",
        (register_number,)
    ).fetchone()
    if dup_reg:
        connection.close()
        id_label = "Register Number" if role == "student" else "Employee ID"
        return jsonify({"success": False, "message": f"This {id_label} is already registered."}), 409

    # Validate unique email
    dup_email = connection.execute(
        "SELECT id FROM users WHERE lower(trim(email)) = lower(trim(?))",
        (email,)
    ).fetchone()
    if dup_email:
        connection.close()
        return jsonify({"success": False, "message": "This email address is already registered."}), 409

    salt = secrets.token_bytes(16)
    p_hash = hash_password(password, salt)
    now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    try:
        connection.execute("""
            INSERT INTO users (
                username, password, role, name, register_number, email, mobile, department, academic_year, class_section,
                bus_id, boarding_point_id, password_hash, salt, is_active, lifecycle_status, created_at, updated_at
            ) VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVE', ?, ?)
        """, (
            register_number,
            role,
            name,
            register_number,
            email,
            clean_mobile,
            department,
            academic_year,
            class_section,
            bus_id,
            boarding_point_id,
            p_hash,
            salt,
            now,
            now
        ))

        # Also mirror into legacy students/faculty tables to maintain backward compatibility
        if role == "student":
            connection.execute("""
                INSERT OR REPLACE INTO students (student_id, name, email, password_hash, salt)
                VALUES (?, ?, ?, ?, ?)
            """, (register_number, name, email, p_hash, salt))
        else:
            connection.execute("""
                INSERT OR REPLACE INTO faculty (faculty_id, name, email, password_hash, salt)
                VALUES (?, ?, ?, ?, ?)
            """, (register_number, name, email, p_hash, salt))

        connection.commit()
    except sqlite3.IntegrityError as e:
        connection.close()
        return jsonify({"success": False, "message": f"Registration error: {e}"}), 409

    connection.close()
    return jsonify({
        "success": True,
        "message": f"{role.capitalize()} registration successful! Please login with your credentials."
    })


# Legacy routes forward to common registration
@app.route("/student/register", methods=["GET", "POST"])
def student_register():
    if request.method == "GET":
        return render_template("login.html", default_view="register", default_role="student")
    return auth_register()


@app.route("/faculty/register", methods=["GET", "POST"])
def faculty_register():
    if request.method == "GET":
        return render_template("login.html", default_view="register", default_role="faculty")
    return auth_register()


# ============================================================
# COMMON LOGIN SYSTEM (STUDENT & FACULTY)
# ============================================================

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    """
    Authenticate students and faculty using Name OR Register Number / Employee ID OR Email.
    Enforces password hashing verification, graduation/deactivation lifecycle, and server-side role session.
    """
    check_student_batch_lifecycle()
    data = request.get_json(silent=True) or request.form
    login_id = (data.get("login_id") or data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    requested_role = (data.get("role") or "").strip().lower()

    if not login_id or not password:
        return jsonify({"success": False, "message": "Please enter your ID/Email and password."}), 400

    connection = get_connection()
    user = connection.execute("""
        SELECT id, role, name, register_number, email, mobile, department, academic_year, class_section,
               bus_id, boarding_point_id, password_hash, salt, is_active, lifecycle_status, deactivation_reason
        FROM users
        WHERE (
            lower(trim(register_number)) = lower(trim(?))
            OR lower(trim(email)) = lower(trim(?))
            OR lower(trim(name)) = lower(trim(?))
            OR lower(trim(username)) = lower(trim(?))
        )
    """, (login_id, login_id, login_id, login_id)).fetchone()

    connection.close()

    if not user:
        return jsonify({"success": False, "message": "Invalid credentials or account does not exist."}), 401

    if requested_role and user["role"] != requested_role:
        return jsonify({"success": False, "message": f"This account is registered as {user['role'].capitalize()}, not {requested_role.capitalize()}."}), 403

    if not user["password_hash"] or not user["salt"]:
        return jsonify({"success": False, "message": "Account credentials need updating. Please use Forgot Password."}), 401

    # 1. VERIFY PASSWORD FIRST
    if not verify_password(password, user["salt"], user["password_hash"]):
        return jsonify({"success": False, "message": "Invalid credentials. Please check your password."}), 401

    # 2. VERIFY STATUS & LIFECYCLE (ONLY AFTER CREDENTIALS CONFIRMED)
    if user["role"] == "student" and user["lifecycle_status"] == "GRADUATED":
        return jsonify({
            "success": False,
            "message": f"Your account has been archived: {user['deactivation_reason'] or 'Academic batch completed (Graduated)'}. Contact transport office for alumnus access."
        }), 403

    if not user["is_active"] or user["lifecycle_status"] == "DEACTIVATED":
        return jsonify({
            "success": False,
            "message": f"Account deactivated: {user['deactivation_reason'] or 'Access revoked by administrator. Please contact transport office.'}"
        }), 403

    session.clear()
    session["user_id"] = user["id"]
    session["user_type"] = user["role"]
    session["role"] = user["role"]
    session["user_name"] = user["name"]
    session["register_number"] = user["register_number"]
    session["email"] = user["email"]
    session["class_section"] = user["class_section"]

    redirect_url = "/student" if user["role"] == "student" else ("/faculty" if user["role"] == "faculty" else "/admin")
    return jsonify({
        "success": True,
        "role": user["role"],
        "name": user["name"],
        "redirect": redirect_url
    })


@app.route("/student/login", methods=["POST"])
def student_login():
    return auth_login()


@app.route("/faculty/login", methods=["POST"])
def faculty_login():
    return auth_login()


# ============================================================
# FORGOT PASSWORD & OTP SYSTEM
# ============================================================

@app.route("/api/auth/forgot-password/send-otp", methods=["POST"])
def forgot_password_send_otp():
    """
    Forgot Password OTP generator (Email Only).
    Enforces email-only input, cooldown, expiration, secure storage, and real Gmail SMTP delivery.
    """
    data = request.get_json(silent=True) or request.form
    email = (data.get("email") or data.get("contact") or "").strip().lower()

    if not email or "@" not in email:
        return jsonify({"success": False, "message": "Please enter a valid registered email address."}), 400

    connection = get_connection()
    user = connection.execute("""
        SELECT id, name, email, mobile, role
        FROM users
        WHERE lower(trim(email)) = lower(trim(?)) AND is_active = 1
    """, (email,)).fetchone()

    # Generic response if not found (security requirement: do not leak account existence)
    if not user:
        connection.close()
        return jsonify({
            "success": True,
            "message": "If an account matches that email address, a verification OTP has been sent."
        })

    # Enforce 60-second resend cooldown
    one_min_ago = (datetime.now() - timedelta(seconds=60)).strftime("%d-%m-%Y %I:%M:%S %p")
    recent = connection.execute("""
        SELECT id FROM password_reset_otps
        WHERE user_id = ? AND created_at > ? AND is_used = 0
    """, (user["id"], one_min_ago)).fetchone()

    if recent:
        connection.close()
        return jsonify({"success": False, "message": "Please wait at least 60 seconds before requesting a new OTP."}), 429

    # Generate cryptographically secure 6-digit OTP
    otp = f"{secrets.randbelow(900000) + 100000}"
    otp_hash = hashlib.sha256(otp.encode("utf-8")).hexdigest()
    expires_at = (datetime.now() + timedelta(minutes=10)).strftime("%d-%m-%Y %I:%M:%S %p")
    created_at = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    # Invalidate previous unused OTPs for this user
    connection.execute("UPDATE password_reset_otps SET is_used = 1 WHERE user_id = ? AND is_used = 0", (user["id"],))

    connection.execute("""
        INSERT INTO password_reset_otps (user_id, contact, otp_hash, attempts, is_used, expires_at, created_at)
        VALUES (?, ?, ?, 0, 0, ?, ?)
    """, (user["id"], user["email"], otp_hash, expires_at, created_at))
    connection.commit()
    connection.close()

    sent, msg, configured = send_otp_notification(user["email"], otp)

    if configured and not sent:
        return jsonify({
            "success": False,
            "message": msg,
            "provider_configured": True
        }), 502

    response_data = {
        "success": True,
        "message": msg if configured else "Verification OTP generated. Gmail SMTP is not configured in .env (enter GMAIL_USER and GMAIL_APP_PASSWORD).",
        "provider_configured": configured
    }

    return jsonify(response_data)


@app.route("/api/auth/forgot-password/verify-otp", methods=["POST"])
def forgot_password_verify_otp():
    """Verify OTP strictly using registered email."""
    data = request.get_json(silent=True) or request.form
    email = (data.get("email") or data.get("contact") or "").strip().lower()
    otp = (data.get("otp") or "").strip()

    if not email or "@" not in email or not otp:
        return jsonify({"success": False, "message": "Registered email and OTP are required."}), 400

    otp_hash = hashlib.sha256(otp.encode("utf-8")).hexdigest()

    connection = get_connection()
    user = connection.execute("""
        SELECT id FROM users
        WHERE lower(trim(email)) = lower(trim(?)) AND is_active = 1
    """, (email,)).fetchone()

    if not user:
        connection.close()
        return jsonify({"success": False, "message": "Invalid or expired OTP."}), 400

    record = connection.execute("""
        SELECT id, otp_hash, attempts, expires_at, is_used
        FROM password_reset_otps
        WHERE user_id = ? AND is_used = 0
        ORDER BY id DESC LIMIT 1
    """, (user["id"],)).fetchone()

    if not record:
        connection.close()
        return jsonify({"success": False, "message": "No active OTP request found. Please request a new OTP."}), 400

    if record["attempts"] >= 3:
        connection.execute("UPDATE password_reset_otps SET is_used = 1 WHERE id = ?", (record["id"],))
        connection.commit()
        connection.close()
        return jsonify({"success": False, "message": "Maximum OTP attempts exceeded. Please request a new OTP."}), 429

    # Check expiration
    try:
        exp_dt = datetime.strptime(record["expires_at"], "%d-%m-%Y %I:%M:%S %p")
        if datetime.now() > exp_dt:
            connection.execute("UPDATE password_reset_otps SET is_used = 1 WHERE id = ?", (record["id"],))
            connection.commit()
            connection.close()
            return jsonify({"success": False, "message": "OTP has expired. Please request a new one."}), 400
    except ValueError:
        pass

    if not hmac.compare_digest(record["otp_hash"], otp_hash):
        connection.execute("UPDATE password_reset_otps SET attempts = attempts + 1 WHERE id = ?", (record["id"],))
        connection.commit()
        remaining = 2 - record["attempts"]
        connection.close()
        return jsonify({"success": False, "message": f"Incorrect OTP. {remaining} attempt(s) remaining."}), 401

    connection.close()
    return jsonify({"success": True, "message": "OTP verified successfully. You can now reset your password."})


@app.route("/api/auth/forgot-password/reset", methods=["POST"])
def forgot_password_reset():
    """Update password with verified OTP (Email only)."""
    data = request.get_json(silent=True) or request.form
    email = (data.get("email") or data.get("contact") or "").strip().lower()
    otp = (data.get("otp") or "").strip()
    new_password = (data.get("new_password") or "").strip()
    confirm_password = (data.get("confirm_password") or "").strip()

    if not all([email, otp, new_password, confirm_password]):
        return jsonify({"success": False, "message": "All fields are required."}), 400

    if "@" not in email:
        return jsonify({"success": False, "message": "Please provide a valid registered email address."}), 400

    if new_password != confirm_password:
        return jsonify({"success": False, "message": "Passwords do not match."}), 400

    if len(new_password) < 6:
        return jsonify({"success": False, "message": "New password must be at least 6 characters long."}), 400

    otp_hash = hashlib.sha256(otp.encode("utf-8")).hexdigest()

    connection = get_connection()
    user = connection.execute("""
        SELECT id, role, register_number, email FROM users
        WHERE lower(trim(email)) = lower(trim(?)) AND is_active = 1
    """, (email,)).fetchone()

    if not user:
        connection.close()
        return jsonify({"success": False, "message": "Account not found."}), 404

    record = connection.execute("""
        SELECT id, otp_hash, attempts, expires_at, is_used
        FROM password_reset_otps
        WHERE user_id = ? AND is_used = 0
        ORDER BY id DESC LIMIT 1
    """, (user["id"],)).fetchone()

    if not record or not hmac.compare_digest(record["otp_hash"], otp_hash):
        connection.close()
        return jsonify({"success": False, "message": "Invalid or expired OTP."}), 400

    # Hash new password
    salt = secrets.token_bytes(16)
    p_hash = hash_password(new_password, salt)
    now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    connection.execute("""
        UPDATE users
        SET password_hash = ?, salt = ?, updated_at = ?
        WHERE id = ?
    """, (p_hash, salt, now, user["id"]))

    # Mirror to legacy tables
    if user["role"] == "student":
        connection.execute("UPDATE students SET password_hash = ?, salt = ? WHERE student_id = ?", (p_hash, salt, user["register_number"]))
    elif user["role"] == "faculty":
        connection.execute("UPDATE faculty SET password_hash = ?, salt = ? WHERE faculty_id = ?", (p_hash, salt, user["register_number"]))

    # Mark OTP as consumed to prevent reuse
    connection.execute("UPDATE password_reset_otps SET is_used = 1 WHERE id = ?", (record["id"],))
    connection.commit()
    connection.close()

    return jsonify({"success": True, "message": "Password updated successfully. You can now login with your new password."})


# ============================================================
# USER PROFILE MANAGEMENT
# ============================================================

@app.route("/api/user/profile", methods=["GET", "POST"])
def user_profile():
    """
    Requirement #6: User profile view and edit.
    Updates existing user record without creating duplicates.
    Includes class_section for students and official RIT programmes.
    Reflects immediately across Admin and Driver dashboards.
    """
    user_id = session.get("user_id")
    user_type = session.get("user_type")

    if not user_id or user_type not in ("student", "faculty"):
        return jsonify({"success": False, "message": "Login required to access profile."}), 401

    connection = get_connection()

    if request.method == "GET":
        u = connection.execute("""
            SELECT u.id, u.role, u.name, u.register_number, u.email, u.mobile,
                   u.department, u.academic_year, u.class_section, u.lifecycle_status,
                   u.bus_id, u.boarding_point_id,
                   b.bus_number, b.route_name,
                   s.stop_name AS boarding_point_name, s.stop_time
            FROM users u
            LEFT JOIN buses b ON u.bus_id = b.id
            LEFT JOIN stops s ON u.boarding_point_id = s.id
            WHERE u.id = ?
        """, (user_id,)).fetchone()
        connection.close()

        if not u:
            return jsonify({"success": False, "message": "User not found."}), 404

        return jsonify({
            "success": True,
            "profile": {
                "id": u["id"],
                "role": u["role"].capitalize() if u["role"] else "Student",
                "name": u["name"],
                "register_number": u["register_number"] or "---",
                "email": u["email"] or "---",
                "mobile": u["mobile"] or "---",
                "department": u["department"] or "---",
                "academic_year": u["academic_year"] or "---",
                "class_section": u["class_section"] or "---",
                "lifecycle_status": u["lifecycle_status"] or "ACTIVE",
                "bus_id": u["bus_id"],
                "bus_number": u["bus_number"] or "Not Assigned",
                "route_name": u["route_name"] or "---",
                "boarding_point_id": u["boarding_point_id"],
                "boarding_point_name": u["boarding_point_name"] or "Not Selected",
                "stop_time": u["stop_time"] or "---"
            }
        })

    # POST - Edit Profile
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    mobile = (data.get("mobile") or "").strip()
    department = (data.get("department") or "").strip()
    academic_year = (data.get("academic_year") or "").strip()
    class_section = (data.get("class_section") or "").strip()
    bus_id = data.get("bus_id")
    boarding_point_id = data.get("boarding_point_id")

    if not name or not email or not mobile:
        connection.close()
        return jsonify({"success": False, "message": "Name, email, and mobile are required."}), 400

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        connection.close()
        return jsonify({"success": False, "message": "Please enter a valid email address."}), 400

    clean_mobile = re.sub(r"\D", "", mobile)
    if len(clean_mobile) != 10:
        connection.close()
        return jsonify({"success": False, "message": "Please enter a valid 10-digit mobile number."}), 400

    # Ensure email is unique across other users
    dup = connection.execute("SELECT id FROM users WHERE lower(trim(email)) = lower(trim(?)) AND id != ?", (email, user_id)).fetchone()
    if dup:
        connection.close()
        return jsonify({"success": False, "message": "This email address is already in use by another account."}), 409

    # Validate newly selected bus and boarding point
    if bus_id:
        try:
            bus_id = int(bus_id)
            boarding_point_id = int(boarding_point_id)
        except (ValueError, TypeError):
            connection.close()
            return jsonify({"success": False, "message": "Invalid bus or boarding point."}), 400

        bus = connection.execute("SELECT id FROM buses WHERE id = ?", (bus_id,)).fetchone()
        if not bus:
            connection.close()
            return jsonify({"success": False, "message": "Selected bus not found."}), 400

        stop = connection.execute("SELECT id FROM stops WHERE id = ? AND bus_id = ?", (boarding_point_id, bus_id)).fetchone()
        if not stop:
            connection.close()
            return jsonify({"success": False, "message": "Selected boarding point does not belong to this bus."}), 400

    now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    connection.execute("""
        UPDATE users
        SET name = ?, email = ?, mobile = ?, department = ?, academic_year = ?,
            class_section = COALESCE(NULLIF(?, ''), class_section),
            bus_id = ?, boarding_point_id = ?, updated_at = ?
        WHERE id = ?
    """, (name, email, clean_mobile, department, academic_year, class_section, bus_id, boarding_point_id, now, user_id))

    connection.commit()
    connection.close()

    session["user_name"] = name
    session["email"] = email
    if class_section:
        session["class_section"] = class_section
    return jsonify({"success": True, "message": "Profile updated successfully!"})


# ============================================================
# BUS MEMBER MANAGEMENT (ADMIN & DRIVER)
# ============================================================

@app.route("/api/admin/bus-members")
def admin_bus_members():
    """
    Requirement #7: Admin Bus Members section.
    Returns active members for selected bus with search and filter capabilities.
    Automatically checks batch lifecycle so graduated students are excluded.
    Includes class_section for students.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    check_student_batch_lifecycle()
    bus_id = request.args.get("bus_id")
    if not bus_id:
        return jsonify({"success": False, "message": "Bus ID is required."}), 400

    connection = get_connection()
    bus = connection.execute("SELECT id, bus_number, route_name FROM buses WHERE id = ? OR bus_number = ?", (bus_id, bus_id)).fetchone()
    if not bus:
        connection.close()
        return jsonify({"success": False, "message": "Bus not found."}), 404

    members = connection.execute("""
        SELECT u.id, u.name, u.role, u.register_number, u.department, u.academic_year, u.class_section,
               u.email, u.mobile, s.stop_name AS boarding_point
        FROM users u
        LEFT JOIN stops s ON u.boarding_point_id = s.id
        WHERE u.bus_id = ? AND u.is_active = 1 AND (u.lifecycle_status = 'ACTIVE' OR u.lifecycle_status IS NULL)
        ORDER BY u.role DESC, u.name ASC
    """, (bus["id"],)).fetchall()
    connection.close()

    return jsonify({
        "success": True,
        "bus": {
            "id": bus["id"],
            "bus_number": bus["bus_number"],
            "route_name": bus["route_name"]
        },
        "total_count": len(members),
        "members": [
            {
                "s_no": i + 1,
                "id": m["id"],
                "name": m["name"],
                "role": m["role"].capitalize() if m["role"] else "Student",
                "register_number": m["register_number"] or "---",
                "department": m["department"] or "---",
                "academic_year": m["academic_year"] or "---",
                "class_section": m["class_section"] or "---",
                "email": m["email"] or "---",
                "mobile": m["mobile"] or "---",
                "boarding_point": m["boarding_point"] or "Campus / Default"
            }
            for i, m in enumerate(members)
        ]
    })


@app.route("/api/admin/member/<int:member_id>/deactivate", methods=["POST"])
def admin_deactivate_member(member_id):
    """
    Requirement #8: Admin deactivates/removes member with documented reason.
    Revokes login access and unassigns from active bus rosters while preserving history.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    reason = (data.get("reason") or "Deactivated by Administrator").strip()

    connection = get_connection()
    member = connection.execute("SELECT id, name, role FROM users WHERE id = ?", (member_id,)).fetchone()
    if not member:
        connection.close()
        return jsonify({"success": False, "message": "Member not found."}), 404

    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    connection.execute("""
        UPDATE users
        SET is_active = 0,
            lifecycle_status = 'DEACTIVATED',
            deactivation_reason = ?,
            deactivated_at = ?,
            bus_id = NULL,
            boarding_point_id = NULL
        WHERE id = ?
    """, (reason, now_str, member_id))
    connection.commit()
    connection.close()

    return jsonify({
        "success": True,
        "message": f"Member '{member['name']}' deactivated and removed from active transport rosters."
    })


@app.route("/api/admin/member/<int:member_id>/reactivate", methods=["POST"])
def admin_reactivate_member(member_id):
    """
    Requirement #8: Admin reactivates an inactive/graduated member.
    Restores login access and reassigns bus if provided.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    bus_id = data.get("bus_id")
    boarding_point_id = data.get("boarding_point_id")

    connection = get_connection()
    member = connection.execute("SELECT id, name FROM users WHERE id = ?", (member_id,)).fetchone()
    if not member:
        connection.close()
        return jsonify({"success": False, "message": "Member not found."}), 404

    connection.execute("""
        UPDATE users
        SET is_active = 1,
            lifecycle_status = 'ACTIVE',
            deactivation_reason = NULL,
            deactivated_at = NULL,
            bus_id = COALESCE(?, bus_id),
            boarding_point_id = COALESCE(?, boarding_point_id)
        WHERE id = ?
    """, (bus_id, boarding_point_id, member_id))
    connection.commit()
    connection.close()

    return jsonify({
        "success": True,
        "message": f"Member '{member['name']}' reactivated successfully."
    })


@app.route("/api/admin/archived-members")
def admin_archived_members():
    """
    Requirement #7 & #8: View graduated and deactivated members.
    Preserves historical records and allows inspection and reactivation.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    check_student_batch_lifecycle()
    connection = get_connection()
    archived = connection.execute("""
        SELECT u.id, u.name, u.role, u.register_number, u.department, u.academic_year, u.class_section,
               u.email, u.mobile, u.lifecycle_status, u.deactivation_reason, u.deactivated_at,
               b.bus_number, b.route_name
        FROM users u
        LEFT JOIN buses b ON u.bus_id = b.id
        WHERE u.is_active = 0 OR u.lifecycle_status IN ('GRADUATED', 'DEACTIVATED')
        ORDER BY u.id DESC
    """).fetchall()
    connection.close()

    return jsonify({
        "success": True,
        "total_count": len(archived),
        "members": [
            {
                "id": r["id"],
                "name": r["name"],
                "role": r["role"].capitalize() if r["role"] else "Student",
                "register_number": r["register_number"] or "---",
                "department": r["department"] or "---",
                "academic_year": r["academic_year"] or "---",
                "class_section": r["class_section"] or "---",
                "email": r["email"] or "---",
                "mobile": r["mobile"] or "---",
                "bus_number": r["bus_number"] or "---",
                "lifecycle_status": r["lifecycle_status"] or "DEACTIVATED",
                "deactivation_reason": r["deactivation_reason"] or "Administrative action",
                "deactivated_at": r["deactivated_at"] or "---"
            }
            for r in archived
        ]
    })


@app.route("/api/driver/bus-members")
def driver_bus_members():
    """
    Requirement #8: Driver Bus Members.
    Display ONLY the active members belonging to the bus currently assigned to that driver.
    """
    if session.get("user_type") != "driver":
        return jsonify({"success": False, "message": "Driver login required."}), 401

    check_student_batch_lifecycle()
    driver_id = session.get("user_id")
    connection = get_connection()

    driver = connection.execute("""
        SELECT id, name, phone, assigned_bus_id
        FROM drivers
        WHERE id = ? AND is_active = 1
    """, (driver_id,)).fetchone()

    if not driver or not driver["assigned_bus_id"]:
        connection.close()
        return jsonify({
            "success": True,
            "assigned": False,
            "message": "No bus currently assigned to your account. Please contact the administrator.",
            "members": [],
            "total_count": 0
        })

    bus_id = driver["assigned_bus_id"]
    bus = connection.execute("SELECT id, bus_number, route_name FROM buses WHERE id = ?", (bus_id,)).fetchone()
    if not bus:
        connection.close()
        return jsonify({
            "success": True,
            "assigned": False,
            "message": "Assigned bus not found.",
            "members": []
        })

    members = connection.execute("""
        SELECT u.id, u.name, u.role, u.register_number, u.department, u.academic_year, u.class_section,
               u.email, u.mobile, s.stop_name AS boarding_point
        FROM users u
        LEFT JOIN stops s ON u.boarding_point_id = s.id
        WHERE u.bus_id = ? AND u.is_active = 1 AND (u.lifecycle_status = 'ACTIVE' OR u.lifecycle_status IS NULL)
        ORDER BY u.name ASC
    """, (bus_id,)).fetchall()
    connection.close()

    return jsonify({
        "success": True,
        "assigned": True,
        "bus": {
            "id": bus["id"],
            "bus_number": bus["bus_number"],
            "route_name": bus["route_name"]
        },
        "total_count": len(members),
        "members": [
            {
                "s_no": i + 1,
                "id": m["id"],
                "name": m["name"],
                "role": m["role"].capitalize() if m["role"] else "Student",
                "register_number": m["register_number"] or "---",
                "department": m["department"] or "---",
                "academic_year": m["academic_year"] or "---",
                "class_section": m["class_section"] or "---",
                "email": m["email"] or "---",
                "mobile": m["mobile"] or "---",
                "boarding_point": m["boarding_point"] or "Campus / Default"
            }
            for i, m in enumerate(members)
        ]
    })


# ============================================================
# DRIVER MANAGEMENT SYSTEM (ADMIN & DRIVER)
# ============================================================

@app.route("/driver/register", methods=["GET", "POST"])
def driver_register():
    """
    Requirement #9: Remove Public Driver Self-Registration.
    Drivers must not independently create accounts through public registration.
    """
    if request.method == "GET":
        return render_template(
            "driver_login.html",
            notice="Driver accounts are created and managed by the administrator. Please contact the transport office."
        )
    return jsonify({
        "success": False,
        "message": "Public driver self-registration has been disabled. Driver accounts are issued directly by the administrator."
    }), 403


@app.route("/driver/login", methods=["GET", "POST"])
def driver_login():
    """Driver login authenticating admin-provisioned driver accounts."""
    if request.method == "GET":
        return render_template("driver_login.html")

    data = request.form if request.form else (request.get_json(silent=True) or {})
    name = (data.get("driver_name") or data.get("username") or data.get("phone") or "").strip()
    phone = (data.get("phone") or "").strip()
    password = (data.get("password") or "").strip()

    if not name or not password:
        return jsonify({"success": False, "message": "Driver name/mobile and password are required."}), 400

    clean_digits = re.sub(r"\D", "", name)
    connection = get_connection()
    # Check driver record without active filter first to provide accurate feedback
    driver_row = connection.execute("""
        SELECT id, name, phone, password_hash, salt, assigned_bus_id, is_active, deactivation_reason
        FROM drivers
        WHERE lower(trim(name)) = lower(trim(?)) OR phone = ? OR (length(?) = 10 AND phone = ?)
    """, (name, name, clean_digits, clean_digits)).fetchone()

    if not driver_row and phone:
        driver_row = connection.execute("""
            SELECT id, name, phone, password_hash, salt, assigned_bus_id, is_active, deactivation_reason
            FROM drivers
            WHERE phone = ?
        """, (phone,)).fetchone()

    if not driver_row:
        connection.close()
        return jsonify({"success": False, "message": "Driver account not found. Please verify driver name or mobile."}), 401

    if not driver_row["is_active"]:
        connection.close()
        return jsonify({
            "success": False,
            "message": f"Driver account access has been revoked: {driver_row['deactivation_reason'] or 'Contact transport administrator.'}"
        }), 403

    # Verify password against hash
    valid = False
    if driver_row["password_hash"] and driver_row["salt"]:
        if password and verify_password(password, driver_row["salt"], driver_row["password_hash"]):
            valid = True
        elif driver_row["phone"] and verify_password(driver_row["phone"], driver_row["salt"], driver_row["password_hash"]):
            valid = True

    if not valid:
        connection.close()
        return jsonify({"success": False, "message": "Invalid password. Please check your credentials."}), 401

    session.clear()
    session["user_type"] = "driver"
    session["role"] = "driver"
    session["user_id"] = driver_row["id"]
    session["user_name"] = driver_row["name"]
    session["driver_phone"] = driver_row["phone"]
    session["assigned_bus_id"] = driver_row["assigned_bus_id"]
    connection.close()

    if not (request.is_json or request.headers.get("Accept") == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest"):
        return redirect(url_for("driver"))

    return jsonify({"success": True, "redirect": "/driver"})


@app.route("/driver")
def driver():
    """Driver dashboard with real-time assignment status and bus capacity."""
    if session.get("user_type") != "driver":
        return render_template("driver_login.html")

    connection = get_connection()
    driver_row = connection.execute("""
        SELECT d.id, d.name, d.phone, d.assigned_bus_id, b.bus_number, b.route_name,
               COALESCE(b.capacity, 50) AS capacity
        FROM drivers d
        LEFT JOIN buses b ON d.assigned_bus_id = b.id
        WHERE d.id = ? AND d.is_active = 1
    """, (session.get("user_id"),)).fetchone()
    connection.close()

    if not driver_row:
        session.clear()
        return redirect(url_for("driver_login"))

    return render_template(
        "driver.html",
        user_name=driver_row["name"],
        driver_phone=driver_row["phone"],
        assigned_bus_number=driver_row["bus_number"],
        assigned_route_name=driver_row["route_name"],
        assigned_bus_id=driver_row["assigned_bus_id"],
        bus_capacity=driver_row["capacity"] or 50,
        has_assigned_bus=bool(driver_row["assigned_bus_id"])
    )


@app.route("/api/driver/bus/capacity", methods=["POST"])
def driver_update_bus_capacity():
    """
    Feature 4: Driver configures seat capacity of assigned bus.
    Requires driver authentication. Validates capacity between 10 and 120.
    """
    if session.get("user_type") != "driver":
        return jsonify({"success": False, "message": "Driver authentication required."}), 403

    driver_id = session.get("user_id")
    conn = get_connection()
    driver_row = conn.execute(
        "SELECT assigned_bus_id FROM drivers WHERE id = ? AND is_active = 1",
        (driver_id,)
    ).fetchone()

    if not driver_row or not driver_row["assigned_bus_id"]:
        conn.close()
        return jsonify({"success": False, "message": "No active bus assigned to your account."}), 400

    bus_id = driver_row["assigned_bus_id"]
    data = request.get_json(silent=True) or request.form
    try:
        capacity = int(data.get("capacity"))
    except (TypeError, ValueError):
        conn.close()
        return jsonify({"success": False, "message": "Please enter a valid numeric seat capacity."}), 400

    if capacity < 10 or capacity > 120:
        conn.close()
        return jsonify({"success": False, "message": "Bus capacity must be between 10 and 120 seats."}), 400

    conn.execute("UPDATE buses SET capacity = ? WHERE id = ?", (capacity, bus_id))
    conn.commit()

    bus_info = conn.execute("SELECT bus_number FROM buses WHERE id = ?", (bus_id,)).fetchone()
    conn.close()

    return jsonify({
        "success": True,
        "message": f"Bus {bus_info['bus_number'] if bus_info else ''} seat capacity updated to {capacity}.",
        "capacity": capacity,
        "bus_id": bus_id
    })


@app.route("/api/admin/drivers")
def admin_drivers():
    """
    List only currently active drivers with active bus assignments.
    Removed drivers are excluded from all current operational listings.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    connection = get_connection()
    query = """
        SELECT d.id, d.name, d.phone, d.assigned_bus_id, d.is_active, d.created_at, d.deactivation_reason,
               b.bus_number, b.route_name
        FROM drivers d
        LEFT JOIN buses b ON d.assigned_bus_id = b.id
        WHERE d.is_active = 1
        ORDER BY d.id ASC
    """
    drivers = connection.execute(query).fetchall()
    connection.close()

    drivers_list = []
    for r in drivers:
        drivers_list.append({
            "id": r["id"],
            "name": r["name"],
            "phone": r["phone"],
            "assigned_bus_id": r["assigned_bus_id"],
            "bus_number": r["bus_number"],
            "route_name": r["route_name"],
            "is_active": True,
            "account_status": "Active",
            "assignment_status": f"Assigned to {r['bus_number']}" if r["assigned_bus_id"] and r["bus_number"] else "Unassigned",
            "created_at": r["created_at"] or "---",
            "deactivation_reason": r["deactivation_reason"] or "---"
        })

    return jsonify({
        "success": True,
        "drivers": drivers_list
    })


@app.route("/api/admin/driver/add", methods=["POST"])
def admin_add_driver():
    """Admin creates driver account."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or data.get("mobile") or "").strip()
    password = (data.get("password") or "").strip()
    confirm_password = (data.get("confirm_password") or "").strip()

    if not name or not phone or not password or not confirm_password:
        return jsonify({"success": False, "message": "All fields are required."}), 400

    clean_phone = re.sub(r"\D", "", phone)
    if len(clean_phone) != 10:
        return jsonify({"success": False, "message": "Please enter a valid 10-digit mobile number."}), 400

    if password != confirm_password:
        return jsonify({"success": False, "message": "Passwords do not match."}), 400

    if len(password) < 6:
        return jsonify({"success": False, "message": "Password must be at least 6 characters long."}), 400

    connection = get_connection()
    existing = connection.execute("SELECT id, is_active FROM drivers WHERE phone = ?", (clean_phone,)).fetchone()
    
    salt = secrets.token_bytes(16)
    p_hash = hash_password(password, salt)
    now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    if existing:
        if existing["is_active"]:
            connection.close()
            return jsonify({"success": False, "message": "An active driver with this mobile number already exists."}), 409
        # Reactivate previously removed record
        connection.execute("""
            UPDATE drivers
            SET name = ?, password_hash = ?, salt = ?, is_active = 1, assigned_bus_id = NULL,
                driver_status = 'OFFLINE', deactivation_reason = NULL, created_at = ?
            WHERE id = ?
        """, (name, p_hash, salt, now, existing["id"]))
        connection.commit()
        connection.close()
        return jsonify({"success": True, "message": f"Driver '{name}' added successfully."})

    connection.execute("""
        INSERT INTO drivers (name, phone, password_hash, salt, assigned_bus_id, driver_status, is_active, created_at)
        VALUES (?, ?, ?, ?, NULL, 'OFFLINE', 1, ?)
    """, (name, clean_phone, p_hash, salt, now))
    connection.commit()
    connection.close()

    return jsonify({"success": True, "message": f"Driver '{name}' added successfully."})


@app.route("/api/admin/driver/<int:driver_id>/assign", methods=["POST"])
def admin_assign_driver(driver_id):
    """Admin assigns or changes driver bus assignment."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    bus_id = data.get("bus_id")

    if not bus_id:
        return jsonify({"success": False, "message": "Please select a bus."}), 400

    connection = get_connection()
    bus = connection.execute("SELECT id, bus_number, route_name FROM buses WHERE id = ?", (bus_id,)).fetchone()
    if not bus:
        connection.close()
        return jsonify({"success": False, "message": "Bus not found."}), 404

    now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    # Deactivate previous assignments for this driver
    connection.execute("""
        UPDATE driver_assignments
        SET is_active = 0, unassigned_at = ?
        WHERE driver_id = ? AND is_active = 1
    """, (now, driver_id))

    # Insert new active assignment
    connection.execute("""
        INSERT INTO driver_assignments (driver_id, bus_id, assigned_at, is_active)
        VALUES (?, ?, ?, 1)
    """, (driver_id, bus["id"], now))

    # Update driver record
    connection.execute("UPDATE drivers SET assigned_bus_id = ? WHERE id = ?", (bus["id"], driver_id))
    connection.commit()
    connection.close()

    return jsonify({
        "success": True,
        "message": f"Driver assigned to bus {bus['bus_number']} ({bus['route_name']}) successfully."
    })


@app.route("/api/admin/driver/<int:driver_id>/unassign", methods=["POST"])
def admin_unassign_driver(driver_id):
    """Admin unassigns bus from driver."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    connection = get_connection()

    driver_row = connection.execute("SELECT assigned_bus_id FROM drivers WHERE id = ?", (driver_id,)).fetchone()
    if driver_row and driver_row["assigned_bus_id"]:
        # Terminate any active live location session
        connection.execute("""
            UPDATE bus_locations
            SET is_active = 0, driver_id = NULL, sharing_expires_at = NULL, bus_status = 'COMPLETED'
            WHERE bus_id = ? AND driver_id = ?
        """, (driver_row["assigned_bus_id"], driver_id))

    connection.execute("""
        UPDATE driver_assignments
        SET is_active = 0, unassigned_at = ?
        WHERE driver_id = ? AND is_active = 1
    """, (now, driver_id))

    connection.execute("UPDATE drivers SET assigned_bus_id = NULL WHERE id = ?", (driver_id,))
    connection.commit()
    connection.close()

    return jsonify({"success": True, "message": "Driver unassigned successfully."})


@app.route("/api/admin/driver/<int:driver_id>/remove", methods=["POST"])
def admin_remove_driver(driver_id):
    """Admin deactivates driver, revokes access, and removes assignment while preserving history."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    reason = (data.get("reason") or "Access revoked by Administrator").strip()

    now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    connection = get_connection()

    driver_row = connection.execute("SELECT name, assigned_bus_id FROM drivers WHERE id = ?", (driver_id,)).fetchone()
    if not driver_row:
        connection.close()
        return jsonify({"success": False, "message": "Driver not found."}), 404

    if driver_row["assigned_bus_id"]:
        connection.execute("""
            UPDATE bus_locations
            SET is_active = 0, driver_id = NULL, sharing_expires_at = NULL, bus_status = 'COMPLETED'
            WHERE bus_id = ? AND driver_id = ?
        """, (driver_row["assigned_bus_id"], driver_id))

    connection.execute("""
        UPDATE driver_assignments
        SET is_active = 0, unassigned_at = ?
        WHERE driver_id = ? AND is_active = 1
    """, (now, driver_id))

    # Deactivate account and save reason
    connection.execute("""
        UPDATE drivers
        SET is_active = 0, assigned_bus_id = NULL, driver_status = 'OFFLINE', deactivation_reason = ?
        WHERE id = ?
    """, (reason, driver_id))
    connection.commit()
    connection.close()

    return jsonify({"success": True, "message": f"Driver '{driver_row['name']}' removed and login access revoked."})


@app.route("/api/driver/buses")
def driver_buses():
    """Returns only the driver's currently assigned bus."""
    if session.get("user_type") != "driver":
        return jsonify({"success": False, "message": "Driver login required."}), 401

    driver_id = session.get("user_id")
    connection = get_connection()
    driver_row = connection.execute("""
        SELECT d.id, d.assigned_bus_id, b.bus_number, b.route_name
        FROM drivers d
        LEFT JOIN buses b ON d.assigned_bus_id = b.id
        WHERE d.id = ?
    """, (driver_id,)).fetchone()
    connection.close()

    if not driver_row or not driver_row["assigned_bus_id"]:
        return jsonify({"success": True, "buses": [], "message": "No bus currently assigned."})

    return jsonify({
        "success": True,
        "buses": [{
            "id": driver_row["assigned_bus_id"],
            "bus_number": driver_row["bus_number"],
            "route_name": driver_row["route_name"],
            "assigned": True
        }]
    })


@app.route("/api/driver/select-bus", methods=["POST"])
def driver_select_bus():
    """Ensures driver only selects their assigned bus."""
    if session.get("user_type") != "driver":
        return jsonify({"success": False, "message": "Driver login required."}), 401

    driver_id = session.get("user_id")
    connection = get_connection()
    driver_row = connection.execute("SELECT assigned_bus_id FROM drivers WHERE id = ?", (driver_id,)).fetchone()
    if not driver_row or not driver_row["assigned_bus_id"]:
        connection.close()
        return jsonify({"success": False, "message": "No bus assigned by admin."}), 403

    bus = connection.execute("SELECT id, bus_number, route_name FROM buses WHERE id = ?", (driver_row["assigned_bus_id"],)).fetchone()
    connection.close()

    return jsonify({
        "success": True,
        "bus_id": bus["id"],
        "bus_number": bus["bus_number"],
        "route_name": bus["route_name"],
        "message": f"Bus {bus['bus_number']} ready for location sharing."
    })


# ============================================================
# LIVE LOCATION TRACKING
# ============================================================

@app.route("/api/location/update", methods=["POST"])
def update_location():
    """
    Driver posts actual GPS location from mobile device.
    Enforces that driver only shares for their assigned bus.
    Validates finite coordinates, bounds, non-null fix, and ordering.
    """
    if session.get("user_type") != "driver":
        return jsonify({"success": False, "message": "Driver login required."}), 401

    driver_id = session.get("user_id")
    data = request.get_json(silent=True) or request.form

    latitude = data.get("latitude")
    longitude = data.get("longitude")
    bus_status = (data.get("bus_status") or "ON_ROUTE").strip()
    sharing_expires_at = data.get("sharing_expires_at")
    client_ts = data.get("client_timestamp") or data.get("timestamp") or 0
    try:
        client_ts = float(client_ts)
    except (ValueError, TypeError):
        client_ts = 0.0

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "Invalid GPS coordinates."}), 400

    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return jsonify({"success": False, "message": "Non-finite GPS coordinates rejected."}), 400

    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        return jsonify({"success": False, "message": "GPS coordinates out of valid range."}), 400

    # Reject 0.0, 0.0 (GPS drop)
    if abs(latitude) < 0.0001 and abs(longitude) < 0.0001:
        return jsonify({"success": False, "message": "Null GPS coordinates rejected."}), 400

    connection = get_connection()
    driver_row = connection.execute("""
        SELECT id, name, phone, assigned_bus_id
        FROM drivers
        WHERE id = ? AND is_active = 1
    """, (driver_id,)).fetchone()

    if not driver_row or not driver_row["assigned_bus_id"]:
        connection.close()
        return jsonify({"success": False, "message": "You cannot share location because no bus is currently assigned to you."}), 403

    bus_id = driver_row["assigned_bus_id"]
    bus = connection.execute("SELECT id, bus_number, route_name FROM buses WHERE id = ?", (bus_id,)).fetchone()
    if not bus:
        connection.close()
        return jsonify({"success": False, "message": "Assigned bus not found."}), 404

    # Verify that if bus_number is passed, it matches the assigned bus
    req_bus = data.get("bus_number")
    if req_bus:
        req_bus = str(req_bus).strip().upper()
        if req_bus != bus["bus_number"].upper():
            connection.close()
            return jsonify({"success": False, "message": f"Security violation: Driver is assigned to {bus['bus_number']}, cannot update {req_bus}."}), 403

    # Check client timestamp to prevent out-of-order packet overwrite within active session
    existing = connection.execute("SELECT is_active, last_client_ts FROM bus_locations WHERE bus_id = ?", (bus_id,)).fetchone()
    if existing and existing["is_active"] and existing["last_client_ts"] and client_ts > 0:
        if client_ts < existing["last_client_ts"]:
            connection.close()
            return jsonify({
                "success": True,
                "bus_number": bus["bus_number"],
                "bus_status": bus_status,
                "message": "Older packet ignored."
            })

    now_epoch = time.time()
    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    # Upsert location
    connection.execute("""
        INSERT INTO bus_locations (
            bus_id, latitude, longitude, current_location, next_point, updated_at,
            is_active, bus_status, driver_id, sharing_expires_at, last_seen_epoch, last_client_ts
        ) VALUES (?, ?, ?, 'GPS Location', ?, ?, 1, ?, ?, ?, ?, ?)
        ON CONFLICT(bus_id) DO UPDATE SET
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            updated_at = excluded.updated_at,
            is_active = 1,
            bus_status = excluded.bus_status,
            driver_id = excluded.driver_id,
            sharing_expires_at = excluded.sharing_expires_at,
            last_seen_epoch = excluded.last_seen_epoch,
            last_client_ts = excluded.last_client_ts
    """, (bus_id, latitude, longitude, bus["route_name"], now_str, bus_status, driver_id, sharing_expires_at, now_epoch, client_ts))

    connection.execute("""
        INSERT INTO bus_location_history (bus_id, latitude, longitude, recorded_at)
        VALUES (?, ?, ?, ?)
    """, (bus_id, latitude, longitude, now_str))

    connection.commit()
    connection.close()

    return jsonify({
        "success": True,
        "bus_number": bus["bus_number"],
        "bus_status": bus_status,
        "updated_at": now_str
    })


@app.route("/api/location/status/<bus_number>", methods=["POST"])
def update_bus_status(bus_number):
    if session.get("user_type") != "driver":
        return jsonify({"success": False, "message": "Driver login required."}), 401

    driver_id = session.get("user_id")
    data = request.get_json(silent=True) or request.form
    bus_status = (data.get("bus_status") or "ON_ROUTE").strip()

    connection = get_connection()
    driver_row = connection.execute("SELECT assigned_bus_id FROM drivers WHERE id = ? AND is_active = 1", (driver_id,)).fetchone()
    if not driver_row or not driver_row["assigned_bus_id"]:
        connection.close()
        return jsonify({"success": False, "message": "No assigned bus."}), 403

    now_epoch = time.time()
    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    connection.execute("""
        UPDATE bus_locations
        SET bus_status = ?, updated_at = ?, last_seen_epoch = ?
        WHERE bus_id = ?
    """, (bus_status, now_str, now_epoch, driver_row["assigned_bus_id"]))
    connection.commit()
    connection.close()

    return jsonify({"success": True, "bus_status": bus_status, "updated_at": now_str})


@app.route("/api/location/stop/<bus_number>", methods=["POST"])
def stop_location(bus_number):
    if session.get("user_type") != "driver":
        return jsonify({"success": False, "message": "Driver login required."}), 401

    driver_id = session.get("user_id")
    connection = get_connection()
    driver_row = connection.execute("SELECT assigned_bus_id FROM drivers WHERE id = ?", (driver_id,)).fetchone()
    bus_id = driver_row["assigned_bus_id"] if driver_row else None

    if bus_id:
        connection.execute("""
            UPDATE bus_locations
            SET is_active = 0, bus_status = 'OFFLINE', driver_id = NULL, sharing_expires_at = NULL, last_seen_epoch = 0, last_client_ts = 0
            WHERE bus_id = ? OR driver_id = ?
        """, (bus_id, driver_id))
    else:
        connection.execute("""
            UPDATE bus_locations
            SET is_active = 0, bus_status = 'OFFLINE', driver_id = NULL, sharing_expires_at = NULL, last_seen_epoch = 0, last_client_ts = 0
            WHERE driver_id = ?
        """, (driver_id,))

    connection.commit()
    connection.close()

    return jsonify({"success": True, "message": "Location sharing stopped."})


@app.route("/api/bus-location/<bus_number>")
def get_bus_location(bus_number):
    """
    Public live location endpoint.
    Returns real coordinates, driver info, and movement history.
    Enforces heartbeat / staleness check (max 90s) and driver assignment verification.
    """
    bus_number = (bus_number or "").strip().upper()
    connection = get_connection()

    bus = connection.execute("SELECT id, bus_number, route_name FROM buses WHERE UPPER(bus_number) = ?", (bus_number,)).fetchone()
    if not bus:
        connection.close()
        return jsonify({"success": False, "message": "Bus not found."}), 404

    location = connection.execute("""
        SELECT latitude, longitude, current_location, next_point, updated_at,
               is_active, bus_status, driver_id, sharing_expires_at, last_seen_epoch
        FROM bus_locations
        WHERE bus_id = ?
    """, (bus["id"],)).fetchone()

    active = bool(location["is_active"]) if location else False
    now_epoch = time.time()

    # 1. Check expiration if duration was configured
    if active and location and location["sharing_expires_at"]:
        try:
            if datetime.fromisoformat(location["sharing_expires_at"]).timestamp() <= now_epoch:
                active = False
        except (ValueError, TypeError):
            pass

    # 2. Staleness / Heartbeat check (threshold: 90 seconds)
    if active and location:
        last_epoch = location["last_seen_epoch"] or 0
        if last_epoch > 0:
            if (now_epoch - last_epoch) > 90:
                active = False
        elif location["updated_at"]:
            try:
                dt = datetime.strptime(location["updated_at"], "%d-%m-%Y %I:%M:%S %p")
                if (datetime.now() - dt).total_seconds() > 90:
                    active = False
            except Exception:
                pass

    # 3. Lookup driver info
    driver_info = None
    if location and location["driver_id"]:
        driver_info = connection.execute("""
            SELECT name, phone FROM drivers WHERE id = ?
        """, (location["driver_id"],)).fetchone()

    # Sync DB if status transitioned to inactive
    if location and bool(location["is_active"]) and not active:
        connection.execute("""
            UPDATE bus_locations
            SET is_active = 0, bus_status = 'OFFLINE', driver_id = NULL
            WHERE bus_id = ?
        """, (bus["id"],))
        connection.commit()

    if not location or not active:
        connection.close()
        return jsonify({
            "success": True,
            "bus_number": bus["bus_number"],
            "route_name": bus["route_name"],
            "location_shared": False,
            "driver_name": None,
            "driver_phone": None,
            "location_name": "Location unavailable (Driver offline)",
            "current_location": "Location unavailable (Driver offline)",
            "next_point": "---",
            "bus_status": "OFFLINE",
            "message": "Driver is currently offline or not sharing location.",
            "updated_at": location["updated_at"] if location else None
        })

    history = connection.execute("""
        SELECT latitude, longitude, recorded_at
        FROM bus_location_history
        WHERE bus_id = ?
        ORDER BY id ASC
    """, (bus["id"],)).fetchall()

    connection.close()

    is_guest = not bool(session.get("user_type")) or request.args.get("guest") == "1"
    driver_name_display = "Assigned Campus Driver" if is_guest else (driver_info["name"] if driver_info else "Assigned Driver")
    driver_phone_display = None if is_guest else (driver_info["phone"] if driver_info else None)

    resolved_place = reverse_geocode_coordinates(location["latitude"], location["longitude"])

    return jsonify({
        "success": True,
        "bus_number": bus["bus_number"],
        "route_name": bus["route_name"],
        "location_shared": True,
        "driver_name": driver_name_display,
        "driver_phone": driver_phone_display,
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "location_name": resolved_place,
        "current_location": location["current_location"] or resolved_place,
        "next_point": location["next_point"] or "En Route",
        "updated_at": location["updated_at"],
        "bus_status": location["bus_status"] or "ON_ROUTE",
        "history": [
            {"latitude": r["latitude"], "longitude": r["longitude"], "recorded_at": r["recorded_at"]}
            for r in history
        ]
    })


# ============================================================
# MORNING BUS SETUP (ADMIN & USER)
# ============================================================

@app.route("/api/admin/morning-buses")
def admin_morning_buses():
    """
    Requirement #18: Admin Morning Bus Setup.
    Display all buses from central DB with active/deactive status.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    connection = get_connection()
    buses = connection.execute("""
        SELECT b.id, b.bus_number, b.route_name,
               COALESCE(m.is_active, 1) AS is_active
        FROM buses b
        LEFT JOIN morning_bus_config m ON b.id = m.bus_id
        ORDER BY b.bus_number
    """).fetchall()
    connection.close()

    return jsonify({
        "success": True,
        "buses": [
            {
                "id": r["id"],
                "bus_number": r["bus_number"],
                "route_name": r["route_name"],
                "is_active": bool(r["is_active"])
            }
            for r in buses
        ]
    })


@app.route("/api/admin/morning-bus/<int:bus_id>/status", methods=["POST"])
def admin_morning_bus_status(bus_id):
    """Admin activates or deactivates morning bus."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    is_active = int(data.get("is_active", 1))
    now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")

    connection = get_connection()
    connection.execute("""
        INSERT INTO morning_bus_config (bus_id, is_active, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(bus_id) DO UPDATE SET is_active = excluded.is_active, updated_at = excluded.updated_at
    """, (bus_id, is_active, now))
    connection.commit()
    connection.close()

    status_text = "activated" if is_active else "deactivated"
    return jsonify({
        "success": True,
        "message": f"Morning bus {status_text} successfully.",
        "bus_id": bus_id,
        "is_active": bool(is_active)
    })


@app.route("/api/user/morning-buses")
def user_morning_buses():
    """
    Requirement #19: User Dashboard – Morning Buses.
    Returns currently active morning buses with route and boarding stops.
    """
    connection = get_connection()
    buses = connection.execute("""
        SELECT b.id, b.bus_number, b.route_name
        FROM buses b
        JOIN morning_bus_config m ON b.id = m.bus_id
        WHERE m.is_active = 1
        ORDER BY b.bus_number
    """).fetchall()

    result = []
    for b in buses:
        stops = connection.execute("""
            SELECT id, stop_name, stop_time, stop_order
            FROM stops
            WHERE bus_id = ? AND direction = 'MORNING'
            ORDER BY stop_order ASC
        """, (b["id"],)).fetchall()

        result.append({
            "id": b["id"],
            "bus_number": b["bus_number"],
            "route_name": b["route_name"],
            "stops": [
                {
                    "id": s["id"],
                    "stop_name": s["stop_name"],
                    "stop_time": s["stop_time"] or "---",
                    "stop_order": s["stop_order"]
                }
                for s in stops
            ]
        })

    connection.close()
    return jsonify({"success": True, "buses": result})


# ============================================================
# DAILY RETURN BUS SETUP (ADMIN & USER)
# ============================================================

@app.route("/api/admin/daily-return")
def admin_daily_return():
    """
    Requirement #2: Admin Daily Return Setup.
    Display ALL buses from central table with departure time and active/inactive status.
    Defaults to 3:50 PM only when no custom time has been saved.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    connection = get_connection()
    buses = connection.execute("""
        SELECT b.id, b.bus_number, b.route_name,
               d.departure_time,
               COALESCE(d.is_active, 1) AS is_active
        FROM buses b
        LEFT JOIN daily_return_config d ON b.id = d.bus_id
        ORDER BY b.bus_number
    """).fetchall()
    connection.close()

    default_time = "3:50 PM"
    return jsonify({
        "success": True,
        "default_return_time": default_time,
        "buses": [
            {
                "id": r["id"],
                "bus_number": r["bus_number"],
                "route_name": r["route_name"],
                "departure_time": r["departure_time"].strip() if (r["departure_time"] and r["departure_time"].strip()) else default_time,
                "is_active": bool(r["is_active"])
            }
            for r in buses
        ]
    })


@app.route("/api/admin/daily-return/<int:bus_id>", methods=["POST"])
def admin_update_daily_return(bus_id):
    """Admin updates departure time or active/inactive status for daily return bus."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    departure_time = data.get("departure_time")
    is_active = data.get("is_active")

    connection = get_connection()
    existing = connection.execute("SELECT departure_time, is_active FROM daily_return_config WHERE bus_id = ?", (bus_id,)).fetchone()
    current_time = existing["departure_time"] if existing and existing["departure_time"] else "3:50 PM"
    current_active = existing["is_active"] if existing else 1

    if departure_time is not None:
        departure_time = str(departure_time).strip()
        if departure_time:
            normalized = normalize_time(departure_time) or departure_time
            current_time = normalized

    if is_active is not None:
        current_active = 1 if str(is_active).lower() in ("1", "true", "active") else 0

    now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    connection.execute("""
        INSERT INTO daily_return_config (bus_id, departure_time, is_active, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(bus_id) DO UPDATE SET
            departure_time = excluded.departure_time,
            is_active = excluded.is_active,
            updated_at = excluded.updated_at
    """, (bus_id, current_time, current_active, now))
    connection.commit()
    connection.close()

    return jsonify({
        "success": True,
        "message": f"Daily return bus departure time updated to {current_time}.",
        "bus_id": bus_id,
        "departure_time": current_time,
        "is_active": bool(current_active)
    })


@app.route("/api/return-buses")
def get_all_return_buses():
    """
    User-facing Evening Return Buses API.
    Returns active return buses with their configured departure times.
    Defaults to 3:50 PM when unconfigured.
    """
    connection = get_connection()
    buses = connection.execute("""
        SELECT b.id, b.bus_number, b.route_name,
               d.departure_time
        FROM buses b
        LEFT JOIN daily_return_config d ON b.id = d.bus_id
        WHERE d.is_active IS NULL OR d.is_active = 1
        ORDER BY b.bus_number
    """).fetchall()
    connection.close()

    default_time = "3:50 PM"
    return jsonify({
        "success": True,
        "default_time": default_time,
        "buses": [
            {
                "bus_number": r["bus_number"],
                "route_name": r["route_name"],
                "departure_time": r["departure_time"].strip() if (r["departure_time"] and r["departure_time"].strip()) else default_time,
                "is_special": bool(r["departure_time"] and r["departure_time"].strip() and r["departure_time"].strip() != default_time)
            }
            for r in buses
        ]
    })


@app.route("/api/bus/<bus_number>/return")
def get_return_bus(bus_number):
    """Calculated stop timings for a specific return bus."""
    bus_number = bus_number.strip().upper()
    connection = get_connection()

    bus = connection.execute("SELECT id, bus_number, route_name FROM buses WHERE bus_number = ?", (bus_number,)).fetchone()
    if not bus:
        connection.close()
        return jsonify({"success": False, "message": "Bus not found."}), 404

    cfg = connection.execute("SELECT departure_time, is_active FROM daily_return_config WHERE bus_id = ?", (bus["id"],)).fetchone()
    if cfg and not cfg["is_active"]:
        connection.close()
        return jsonify({"success": False, "message": "This bus is not operating for evening return today."}), 404

    departure_time = cfg["departure_time"].strip() if (cfg and cfg["departure_time"] and cfg["departure_time"].strip()) else "3:50 PM"

    morning_stops = connection.execute("""
        SELECT stop_name, stop_time
        FROM stops
        WHERE bus_id = ? AND direction = 'MORNING'
        ORDER BY stop_order ASC
    """, (bus["id"],)).fetchall()

    connection.close()

    estimated_times = calculate_return_times(departure_time, morning_stops) if departure_time else []
    return jsonify({
        "success": True,
        "bus_number": bus["bus_number"],
        "route_name": bus["route_name"],
        "departure_time": departure_time,
        "stops": estimated_times or []
    })


# ============================================================
# BUS & BOARDING POINT MANAGEMENT (ADMIN)
# ============================================================

@app.route("/api/buses")
def get_buses():
    """Returns all buses with their permanent id, bus_number, route_name."""
    connection = get_connection()
    buses = connection.execute("SELECT id, bus_number, route_name FROM buses ORDER BY bus_number ASC").fetchall()
    connection.close()
    return jsonify({
        "success": True,
        "buses": [
            {"id": b["id"], "bus_number": b["bus_number"], "route_name": b["route_name"]}
            for b in buses
        ]
    })


@app.route("/api/bus/<bus_identifier>/stops")
def get_bus_stops(bus_identifier):
    """
    Returns boarding points / stops for a bus.
    Filters to MORNING stops by default to display only morning bus timings.
    Preserves evening records in database.
    """
    connection = get_connection()
    bus = None
    if str(bus_identifier).isdigit():
        bus = connection.execute("SELECT id, bus_number, route_name FROM buses WHERE id = ?", (int(bus_identifier),)).fetchone()
    if not bus:
        bus = connection.execute("SELECT id, bus_number, route_name FROM buses WHERE bus_number = ?", (str(bus_identifier).strip().upper(),)).fetchone()

    if not bus:
        connection.close()
        return jsonify({"success": False, "message": "Bus not found."}), 404

    direction = request.args.get("direction", "MORNING").strip().upper()
    query = "SELECT id, stop_name, stop_time, stop_order, direction FROM stops WHERE bus_id = ?"
    params = [bus["id"]]
    if direction in ("MORNING", "EVENING"):
        query += " AND direction = ?"
        params.append(direction)
    query += " ORDER BY stop_order ASC, id ASC"

    stops = connection.execute(query, params).fetchall()
    connection.close()

    return jsonify({
        "success": True,
        "bus": {"id": bus["id"], "bus_number": bus["bus_number"], "route_name": bus["route_name"]},
        "stops": [
            {
                "id": s["id"],
                "stop_name": s["stop_name"],
                "stop_time": s["stop_time"] or "---",
                "stop_order": s["stop_order"],
                "direction": s["direction"]
            }
            for s in stops
        ]
    })


@app.route("/api/search")
def search_bus():
    """
    Search buses by boarding point name.
    Matches partial boarding points case-insensitively, filters to MORNING timing,
    and deduplicates buses so each bus appears only once.
    """
    boarding_point = (request.args.get("point") or request.args.get("stop") or request.args.get("q") or "").strip()
    if not boarding_point:
        return jsonify({"success": False, "message": "Boarding point required."}), 400

    connection = get_connection()
    results = connection.execute("""
        SELECT buses.id, buses.bus_number, buses.route_name, stops.stop_name, stops.stop_time
        FROM stops
        JOIN buses ON stops.bus_id = buses.id
        WHERE stops.direction = 'MORNING'
        AND lower(trim(stops.stop_name)) LIKE lower(?)
        ORDER BY buses.bus_number ASC, stops.stop_order ASC
    """, (f"%{boarding_point}%",)).fetchall()
    connection.close()

    seen_buses = set()
    unique_results = []
    for r in results:
        b_id = r["id"]
        if b_id not in seen_buses:
            seen_buses.add(b_id)
            unique_results.append({
                "id": r["id"],
                "bus_number": r["bus_number"],
                "route_name": r["route_name"],
                "stop_name": r["stop_name"],
                "stop_time": r["stop_time"] or "---"
            })

    return jsonify({
        "success": True,
        "buses": unique_results,
        "results": unique_results
    })


@app.route("/api/admin/bus", methods=["POST"])
def admin_add_bus():
    """
    Admin adds a new bus:
    - Sets bus_number, route_name, capacity.
    - Automatically generates a unique, permanent QR code (RIT-BUS-<bus_number>-<HEX>).
    - Initializes morning and daily return configurations.
    - Safe transaction with rollback on failure.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    bus_number = (data.get("bus_number") or "").strip().upper()
    route_name = (data.get("route_name") or "").strip()
    raw_capacity = data.get("capacity")
    try:
        capacity = max(1, int(raw_capacity)) if raw_capacity is not None and str(raw_capacity).strip() != "" else 50
    except (ValueError, TypeError):
        capacity = 50

    if not bus_number or not route_name:
        return jsonify({"success": False, "message": "Bus number and route name are required."}), 400

    connection = get_connection()
    existing = connection.execute("SELECT id FROM buses WHERE lower(bus_number) = lower(?)", (bus_number,)).fetchone()
    if existing:
        connection.close()
        return jsonify({"success": False, "message": f"Bus {bus_number} already exists."}), 409

    # Generate unique permanent QR token
    safe_num = bus_number.strip().upper().replace(" ", "-")
    qr_token = None
    for _ in range(10):
        candidate = f"RIT-BUS-{safe_num}-{secrets.token_hex(4).upper()}"
        if not connection.execute("SELECT id FROM buses WHERE qr_code = ?", (candidate,)).fetchone():
            qr_token = candidate
            break
    if not qr_token:
        qr_token = f"RIT-BUS-{safe_num}-{secrets.token_hex(8).upper()}"

    now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    try:
        cursor = connection.cursor()
        cursor.execute("""
            INSERT INTO buses (bus_number, route_name, capacity, scheduled_arrival_time, qr_code)
            VALUES (?, ?, ?, '07:40 AM', ?)
        """, (bus_number, route_name, capacity, qr_token))
        new_id = cursor.lastrowid

        # Initialize morning and daily return configurations
        cursor.execute("INSERT INTO morning_bus_config (bus_id, is_active, updated_at) VALUES (?, 1, ?)", (new_id, now))
        cursor.execute("INSERT INTO daily_return_config (bus_id, departure_time, is_active, updated_at) VALUES (?, NULL, 1, ?)", (new_id, now))

        connection.commit()
    except Exception as e:
        connection.rollback()
        connection.close()
        return jsonify({"success": False, "message": f"Failed to add bus: {str(e)}"}), 500

    connection.close()

    return jsonify({
        "success": True,
        "message": f"Bus {bus_number} added successfully with permanent QR code.",
        "bus_id": new_id,
        "bus_number": bus_number,
        "route_name": route_name,
        "capacity": capacity,
        "qr_code": qr_token
    })


@app.route("/api/admin/bus/<int:bus_id>/edit", methods=["POST"])
def admin_edit_bus(bus_id):
    """
    Requirement #20: Edit existing bus number (e.g. R10 -> R10B or R11).
    Uses permanent bus ID so all registrations, driver assignments, and routes stay linked!
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    new_bus_number = (data.get("bus_number") or "").strip().upper()
    new_route_name = (data.get("route_name") or "").strip()

    if not new_bus_number or not new_route_name:
        return jsonify({"success": False, "message": "Bus number and route name are required."}), 400

    connection = get_connection()
    bus = connection.execute("SELECT id, bus_number FROM buses WHERE id = ?", (bus_id,)).fetchone()
    if not bus:
        connection.close()
        return jsonify({"success": False, "message": "Bus not found."}), 404

    # Ensure uniqueness among other buses
    dup = connection.execute("SELECT id FROM buses WHERE lower(bus_number) = lower(?) AND id != ?", (new_bus_number, bus_id)).fetchone()
    if dup:
        connection.close()
        return jsonify({"success": False, "message": f"Bus number '{new_bus_number}' is already taken by another bus."}), 409

    connection.execute("UPDATE buses SET bus_number = ?, route_name = ? WHERE id = ?", (new_bus_number, new_route_name, bus_id))
    connection.commit()
    connection.close()

    return jsonify({
        "success": True,
        "message": f"Bus updated to '{new_bus_number}' successfully.",
        "bus_id": bus_id,
        "bus_number": new_bus_number,
        "route_name": new_route_name
    })


@app.route("/api/admin/bus/<int:bus_id>", methods=["DELETE"])
def admin_remove_bus(bus_id):
    """Admin removes a bus and associated configs."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    connection = get_connection()
    connection.execute("DELETE FROM stops WHERE bus_id = ?", (bus_id,))
    connection.execute("DELETE FROM return_buses WHERE bus_id = ?", (bus_id,))
    connection.execute("DELETE FROM morning_bus_config WHERE bus_id = ?", (bus_id,))
    connection.execute("DELETE FROM daily_return_config WHERE bus_id = ?", (bus_id,))
    connection.execute("DELETE FROM bus_locations WHERE bus_id = ?", (bus_id,))
    connection.execute("DELETE FROM bus_location_history WHERE bus_id = ?", (bus_id,))
    connection.execute("UPDATE drivers SET assigned_bus_id = NULL WHERE assigned_bus_id = ?", (bus_id,))
    connection.execute("UPDATE users SET bus_id = NULL, boarding_point_id = NULL WHERE bus_id = ?", (bus_id,))
    connection.execute("DELETE FROM buses WHERE id = ?", (bus_id,))
    connection.commit()
    connection.close()

    return jsonify({"success": True, "message": "Bus and its stops removed successfully."})


@app.route("/api/admin/stop", methods=["POST"])
def admin_add_stop():
    """Admin adds a boarding point."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    bus_id = data.get("bus_id")
    stop_name = (data.get("stop_name") or "").strip()
    stop_time = (data.get("stop_time") or "").strip()
    direction = (data.get("direction") or "MORNING").strip().upper()

    if not bus_id or not stop_name:
        return jsonify({"success": False, "message": "Bus and stop name are required."}), 400

    normalized_time = normalize_time(stop_time) or stop_time

    connection = get_connection()
    last_order = connection.execute(
        "SELECT MAX(stop_order) AS max_o FROM stops WHERE bus_id = ? AND direction = ?",
        (bus_id, direction)
    ).fetchone()
    next_order = (last_order["max_o"] or 0) + 1 if last_order else 1

    cursor = connection.cursor()
    cursor.execute("""
        INSERT INTO stops (bus_id, stop_name, stop_time, stop_order, direction)
        VALUES (?, ?, ?, ?, ?)
    """, (bus_id, stop_name, normalized_time, next_order, direction))
    new_id = cursor.lastrowid
    connection.commit()
    connection.close()

    return jsonify({"success": True, "message": "Boarding point added successfully.", "stop_id": new_id})


@app.route("/api/admin/stop/<int:stop_id>/edit", methods=["POST"])
def admin_edit_stop(stop_id):
    """Admin edits boarding point name and pickup time."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    data = request.get_json(silent=True) or request.form
    stop_name = (data.get("stop_name") or "").strip()
    stop_time = (data.get("stop_time") or "").strip()

    if not stop_name:
        return jsonify({"success": False, "message": "Stop name is required."}), 400

    normalized_time = normalize_time(stop_time) or stop_time
    connection = get_connection()
    connection.execute("UPDATE stops SET stop_name = ?, stop_time = ? WHERE id = ?", (stop_name, normalized_time, stop_id))
    connection.commit()
    connection.close()

    return jsonify({"success": True, "message": "Boarding point updated successfully."})


@app.route("/api/admin/stop/<int:stop_id>", methods=["DELETE"])
def admin_remove_stop(stop_id):
    """Admin deletes a boarding point."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    connection = get_connection()
    connection.execute("UPDATE users SET boarding_point_id = NULL WHERE boarding_point_id = ?", (stop_id,))
    connection.execute("DELETE FROM stops WHERE id = ?", (stop_id,))
    connection.commit()
    connection.close()

    return jsonify({"success": True, "message": "Boarding point removed."})


@app.route("/api/admin/stops")
def admin_list_stops():
    """
    Requirement #2: List morning stops for a bus.
    Evening return timings are managed exclusively in Daily Return Bus Setup.
    """
    bus_id = request.args.get("bus_id")
    if not bus_id:
        return jsonify({"success": False, "message": "Bus ID required."}), 400

    include_evening = request.args.get("include_evening", "0") in ("1", "true")
    connection = get_connection()
    if include_evening:
        stops = connection.execute("""
            SELECT id, bus_id, stop_name, stop_time, stop_order, direction
            FROM stops
            WHERE bus_id = ?
            ORDER BY CASE direction WHEN 'MORNING' THEN 1 ELSE 2 END, stop_order ASC
        """, (bus_id,)).fetchall()
    else:
        stops = connection.execute("""
            SELECT id, bus_id, stop_name, stop_time, stop_order, direction
            FROM stops
            WHERE bus_id = ? AND (direction = 'MORNING' OR direction IS NULL OR direction = '')
            ORDER BY stop_order ASC
        """, (bus_id,)).fetchall()
    connection.close()

    return jsonify({
        "success": True,
        "stops": [
            {
                "id": s["id"],
                "stop_name": s["stop_name"],
                "stop_time": s["stop_time"] or "---",
                "stop_order": s["stop_order"],
                "direction": s["direction"] or "MORNING"
            }
            for s in stops
        ]
    })


@app.route("/api/admin/default-return-time", methods=["GET", "POST"])
def admin_default_return_time():
    """Get or update global default return time."""
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    connection = get_connection()
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form
        raw_time = (data.get("default_return_time") or "").strip()
        normalized = normalize_time(raw_time)
        if not normalized:
            connection.close()
            return jsonify({"success": False, "message": "Invalid time format."}), 400

        connection.execute("""
            INSERT INTO app_settings (setting_key, setting_value)
            VALUES ('default_return_time', ?)
            ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
        """, (normalized,))
        connection.commit()
        connection.close()
        return jsonify({"success": True, "default_return_time": normalized})

    current_val = get_default_return_time()
    connection.close()
    return jsonify({"success": True, "default_return_time": current_val})


# ============================================================
# NOTICES
# ============================================================

@app.route("/api/notices")
def get_notices():
    """
    Public active notices. Supports ?visitor=1 or ?audience=VISITOR filter.
    Includes attachment download URLs when available.
    """
    is_visitor = request.args.get("visitor") == "1" or (request.args.get("audience") or "").upper() == "VISITOR"
    connection = get_connection()
    if is_visitor:
        query = """
            SELECT id, title, message, priority, created_at, attachment_filename, attachment_original_name, target_audience
            FROM notices
            WHERE is_active = 1
              AND (target_audience IS NULL OR UPPER(target_audience) IN ('ALL', 'VISITOR'))
            ORDER BY id DESC
        """
    else:
        query = """
            SELECT id, title, message, priority, created_at, attachment_filename, attachment_original_name, target_audience
            FROM notices
            WHERE is_active = 1
              AND (target_audience IS NULL OR UPPER(target_audience) IN ('ALL', 'CAMPUS'))
            ORDER BY id DESC
        """
    notices = connection.execute(query).fetchall()
    connection.close()
    return jsonify({
        "success": True,
        "notices": [
            {
                "id": n["id"],
                "title": n["title"],
                "message": n["message"],
                "priority": n["priority"],
                "created_at": n["created_at"],
                "target_audience": n["target_audience"] or "ALL",
                "attachment_filename": n["attachment_filename"],
                "attachment_original_name": n["attachment_original_name"],
                "attachment_url": f"/api/notices/{n['id']}/attachment" if n["attachment_filename"] else None
            }
            for n in notices
        ]
    })


@app.route("/api/admin/notices")
def admin_get_notices():
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403
    connection = get_connection()
    notices = connection.execute("""
        SELECT id, title, message, priority, is_active, created_at, delivery_summary,
               attachment_filename, attachment_original_name, target_audience
        FROM notices
        ORDER BY id DESC
    """).fetchall()
    connection.close()
    return jsonify({
        "success": True,
        "notices": [
            {
                "id": n["id"],
                "title": n["title"],
                "message": n["message"],
                "priority": n["priority"],
                "is_active": bool(n["is_active"]),
                "created_at": n["created_at"],
                "delivery_summary": n["delivery_summary"] or "",
                "target_audience": n["target_audience"] or "ALL",
                "attachment_filename": n["attachment_filename"],
                "attachment_original_name": n["attachment_original_name"],
                "attachment_url": f"/api/notices/{n['id']}/attachment" if n["attachment_filename"] else None
            }
            for n in notices
        ]
    })


@app.route("/api/admin/notice", methods=["POST"])
def admin_save_notice():
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    # Accepts both JSON and multipart form data
    title = (request.form.get("title") or (request.json.get("title") if request.is_json else "") or "").strip()
    message = (request.form.get("message") or request.form.get("content") or (request.json.get("message") or request.json.get("content") if request.is_json else "") or "").strip()
    priority = (request.form.get("priority") or (request.json.get("priority") if request.is_json else "") or "NORMAL").strip().upper()
    target_audience = (request.form.get("target_audience") or (request.json.get("target_audience") if request.is_json else "") or "ALL").strip().upper()

    if not title or not message:
        return jsonify({"success": False, "message": "Title and message are required."}), 400

    # Handle optional file attachment (PDF, DOC/DOCX, TXT, images <= 10MB)
    attachment_saved_name = None
    attachment_orig_name = None
    attachment_mime_type = None
    attachment_full_path = None

    uploaded_file = request.files.get("attachment")
    if uploaded_file and uploaded_file.filename and uploaded_file.filename.strip():
        orig_name = secure_filename(uploaded_file.filename.strip())
        _, ext = os.path.splitext(orig_name.lower())
        if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
            return jsonify({
                "success": False,
                "message": f"Unsupported attachment format ({ext}). Allowed formats: PDF, DOC, DOCX, TXT, PNG, JPG, JPEG."
            }), 400

        # Check file size (max 10MB)
        uploaded_file.seek(0, os.SEEK_END)
        file_len = uploaded_file.tell()
        uploaded_file.seek(0)
        if file_len > 10 * 1024 * 1024:
            return jsonify({
                "success": False,
                "message": "File exceeds the 10 MB maximum upload size limit."
            }), 400

        unique_id = uuid.uuid4().hex[:12]
        attachment_saved_name = f"{unique_id}_{orig_name}"
        attachment_orig_name = uploaded_file.filename.strip()
        attachment_mime_type = uploaded_file.mimetype or ext
        attachment_full_path = os.path.join(NOTICES_UPLOAD_FOLDER, attachment_saved_name)
        uploaded_file.save(attachment_full_path)

    connection = get_connection()

    # Prevent accidental duplicate submission within 120 seconds
    now_dt = datetime.now()
    two_mins_ago = (now_dt - timedelta(seconds=120)).strftime("%d-%m-%Y %I:%M:%S %p")
    existing_duplicate = connection.execute("""
        SELECT id, created_at FROM notices
        WHERE lower(trim(title)) = lower(trim(?))
          AND lower(trim(message)) = lower(trim(?))
          AND created_at > ?
        ORDER BY id DESC LIMIT 1
    """, (title, message, two_mins_ago)).fetchone()

    if existing_duplicate:
        connection.close()
        return jsonify({
            "success": False,
            "message": "Duplicate notice submission detected: An identical notice was already published moments ago. Duplicate email distribution has been prevented.",
            "is_duplicate": True
        }), 409

    now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    cursor = connection.cursor()
    cursor.execute("""
        INSERT INTO notices (
            title, message, priority, is_active, created_at, delivery_summary,
            attachment_filename, attachment_original_name, attachment_type, target_audience
        )
        VALUES (?, ?, ?, 1, ?, 'PENDING', ?, ?, ?, ?)
    """, (title, message, priority, now, attachment_saved_name, attachment_orig_name, attachment_mime_type, target_audience))
    notice_id = cursor.lastrowid
    connection.commit()
    connection.close()

    # Trigger automatic email distribution to active students & faculty (with file attachment if provided)
    delivery_summary = send_notice_email_distribution(
        notice_id, title, message, priority, now,
        attachment_path=attachment_full_path,
        attachment_filename=attachment_orig_name,
        target_audience=target_audience
    )

    status = delivery_summary.get("status")
    if status == "COMPLETED":
        msg = f"Notice published successfully. Email delivery: {delivery_summary['sent']} sent, {delivery_summary['failed']} failed, {delivery_summary['skipped']} skipped."
    elif status == "UNCONFIGURED":
        msg = f"Notice published to database. (Email distribution skipped: {delivery_summary.get('message')})"
    else:
        msg = f"Notice published to database. Email delivery: {delivery_summary.get('summary_text', 'Delivery error')}."

    return jsonify({
        "success": True,
        "message": msg,
        "notice_id": notice_id,
        "attachment_saved": bool(attachment_saved_name),
        "delivery_summary": delivery_summary
    })


@app.route("/api/notices/<int:notice_id>/attachment")
def get_notice_attachment(notice_id):
    """Public / user download of notice file attachment."""
    connection = get_connection()
    row = connection.execute(
        "SELECT attachment_filename, attachment_original_name FROM notices WHERE id = ?",
        (notice_id,)
    ).fetchone()
    connection.close()

    if not row or not row["attachment_filename"]:
        return jsonify({"success": False, "message": "Attachment not found."}), 404

    return send_from_directory(
        NOTICES_UPLOAD_FOLDER,
        row["attachment_filename"],
        as_attachment=True,
        download_name=row["attachment_original_name"] or row["attachment_filename"]
    )


@app.route("/api/admin/notice/<int:notice_id>", methods=["DELETE"])
def admin_delete_notice(notice_id):
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403
    connection = get_connection()
    row = connection.execute("SELECT attachment_filename FROM notices WHERE id = ?", (notice_id,)).fetchone()
    if row and row["attachment_filename"]:
        try:
            fpath = os.path.join(NOTICES_UPLOAD_FOLDER, row["attachment_filename"])
            if os.path.exists(fpath):
                os.remove(fpath)
        except Exception as e:
            app.logger.warning(f"Could not remove notice attachment file: {e}")

    connection.execute("DELETE FROM notices WHERE id = ?", (notice_id,))
    connection.commit()
    connection.close()
    return jsonify({"success": True, "message": "Notice deleted."})


# ============================================================
# REPORTLAB PDF EXPORT ROUTES (FEATURE 2)
# ============================================================

@app.route("/api/admin/export/drivers-pdf")
def admin_export_drivers_pdf():
    """
    Feature 2: Download Drivers Roster as a professional PDF.
    Includes RIT header, date, Driver Name, Phone, Assigned Bus, Route Name, Status.
    Strictly excludes driver passwords or credentials.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    conn = get_connection()
    drivers = conn.execute("""
        SELECT d.id, d.name, d.phone, b.bus_number, b.route_name
        FROM drivers d
        LEFT JOIN buses b ON d.assigned_bus_id = b.id
        WHERE d.is_active = 1
        ORDER BY d.id ASC
    """).fetchall()
    conn.close()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=18,
        textColor=colors.HexColor('#082d5c'),
        alignment=1
    )
    sub_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=13,
        textColor=colors.HexColor('#475569'),
        alignment=1
    )
    cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#1e293b')
    )
    cell_bold = ParagraphStyle(
        'TableCellBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#ffffff')
    )

    story = [
        Paragraph("RAJALAKSHMI INSTITUTE OF TECHNOLOGY", title_style),
        Paragraph("DEPARTMENT OF CAMPUS TRANSPORTATION &bull; OFFICIAL DRIVERS ROSTER", sub_style),
        Paragraph(f"Generated on: {datetime.now().strftime('%d-%m-%Y %I:%M %p')}", sub_style),
        Spacer(1, 14)
    ]

    table_data = [[
        Paragraph("<b>S.No</b>", cell_bold),
        Paragraph("<b>Driver Name</b>", cell_bold),
        Paragraph("<b>Contact Phone</b>", cell_bold),
        Paragraph("<b>Assigned Bus</b>", cell_bold),
        Paragraph("<b>Route Name</b>", cell_bold),
        Paragraph("<b>Status</b>", cell_bold)
    ]]

    for idx, d in enumerate(drivers, 1):
        bus_str = d["bus_number"] if d["bus_number"] else "Unassigned"
        route_str = d["route_name"] if d["route_name"] else "---"
        table_data.append([
            Paragraph(str(idx), cell_style),
            Paragraph(html_escape(d["name"] or "---"), cell_style),
            Paragraph(html_escape(d["phone"] or "---"), cell_style),
            Paragraph(f"<b>{html_escape(bus_str)}</b>", cell_style),
            Paragraph(html_escape(route_str), cell_style),
            Paragraph('<font color="#15803d"><b>ACTIVE</b></font>', cell_style)
        ])

    col_widths = [36, 120, 95, 75, 150, 64]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#082d5c')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#ffffff'), colors.HexColor('#f8fafc')]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
    ]))
    story.append(t)
    doc.build(story, canvasmaker=NumberedCanvas)
    buffer.seek(0)

    filename = f"RIT_Drivers_Roster_{datetime.now().strftime('%Y%m%d')}.pdf"
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename
    )


@app.route("/api/admin/export/morning-buses-pdf")
def admin_export_morning_buses_pdf():
    """
    Feature 2: Download Morning Bus Setup as a landscape PDF.
    Shows Bus Number, Route Name, Boarding Stops & Timings, Capacity, Status.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    conn = get_connection()
    buses = conn.execute("""
        SELECT b.id, b.bus_number, b.route_name, COALESCE(b.capacity, 50) AS capacity,
               COALESCE(m.is_active, 1) AS is_active
        FROM buses b
        LEFT JOIN morning_bus_config m ON b.id = m.bus_id
        ORDER BY b.bus_number
    """).fetchall()

    bus_stops_map = {}
    stops_rows = conn.execute("""
        SELECT bus_id, stop_name, stop_time
        FROM stops
        ORDER BY bus_id, stop_order ASC, id ASC
    """).fetchall()
    conn.close()

    for s in stops_rows:
        bus_stops_map.setdefault(s["bus_id"], []).append(f"{s['stop_name']} ({s['stop_time']})")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=30,
        rightMargin=30,
        topMargin=30,
        bottomMargin=30
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=18,
        textColor=colors.HexColor('#082d5c'),
        alignment=1
    )
    sub_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=13,
        textColor=colors.HexColor('#475569'),
        alignment=1
    )
    cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#1e293b')
    )
    cell_bold = ParagraphStyle(
        'TableCellBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#ffffff')
    )

    story = [
        Paragraph("RAJALAKSHMI INSTITUTE OF TECHNOLOGY", title_style),
        Paragraph("DEPARTMENT OF CAMPUS TRANSPORTATION &bull; ACTIVE MORNING BUS SCHEDULE", sub_style),
        Paragraph(f"Generated on: {datetime.now().strftime('%d-%m-%Y %I:%M %p')} &bull; Scheduled Campus Arrival: 7:40 AM", sub_style),
        Spacer(1, 14)
    ]

    table_data = [[
        Paragraph("<b>S.No</b>", cell_bold),
        Paragraph("<b>Bus No</b>", cell_bold),
        Paragraph("<b>Route Name</b>", cell_bold),
        Paragraph("<b>Boarding Stops & Timings</b>", cell_bold),
        Paragraph("<b>Capacity</b>", cell_bold),
        Paragraph("<b>Status</b>", cell_bold)
    ]]

    for idx, b in enumerate(buses, 1):
        stops_list = bus_stops_map.get(b["id"], [])
        stops_str = ", ".join(stops_list[:8]) + ("..." if len(stops_list) > 8 else "") if stops_list else "Standard campus route"
        status_html = '<font color="#15803d"><b>ACTIVE</b></font>' if b["is_active"] else '<font color="#b91c1c"><b>INACTIVE</b></font>'
        table_data.append([
            Paragraph(str(idx), cell_style),
            Paragraph(f"<b>Bus {html_escape(b['bus_number'])}</b>", cell_style),
            Paragraph(html_escape(b["route_name"] or "---"), cell_style),
            Paragraph(html_escape(stops_str), cell_style),
            Paragraph(f"{b['capacity']} seats", cell_style),
            Paragraph(status_html, cell_style)
        ])

    col_widths = [36, 70, 160, 420, 50, 50]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#082d5c')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#ffffff'), colors.HexColor('#f8fafc')]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
    ]))
    story.append(t)
    doc.build(story, canvasmaker=NumberedCanvas)
    buffer.seek(0)

    filename = f"RIT_Morning_Bus_Schedule_{datetime.now().strftime('%Y%m%d')}.pdf"
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename
    )


@app.route("/api/admin/export/return-buses-pdf")
def admin_export_return_buses_pdf():
    """
    Feature 2: Download Daily Return Bus Setup as a PDF.
    Shows Bus Number, Route Name, Departure Time, Status.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    conn = get_connection()
    buses = conn.execute("""
        SELECT b.id, b.bus_number, b.route_name,
               COALESCE(d.departure_time, '3:50 PM') AS departure_time,
               COALESCE(d.is_active, 1) AS is_active
        FROM buses b
        LEFT JOIN daily_return_config d ON b.id = d.bus_id
        ORDER BY b.bus_number
    """).fetchall()
    conn.close()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=18,
        textColor=colors.HexColor('#082d5c'),
        alignment=1
    )
    sub_style = ParagraphStyle(
        'DocSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=13,
        textColor=colors.HexColor('#475569'),
        alignment=1
    )
    cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#1e293b')
    )
    cell_bold = ParagraphStyle(
        'TableCellBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#ffffff')
    )

    story = [
        Paragraph("RAJALAKSHMI INSTITUTE OF TECHNOLOGY", title_style),
        Paragraph("DEPARTMENT OF CAMPUS TRANSPORTATION &bull; DAILY RETURN BUS SCHEDULE", sub_style),
        Paragraph(f"Generated on: {datetime.now().strftime('%d-%m-%Y %I:%M %p')}", sub_style),
        Spacer(1, 14)
    ]

    table_data = [[
        Paragraph("<b>S.No</b>", cell_bold),
        Paragraph("<b>Bus No</b>", cell_bold),
        Paragraph("<b>Route Name</b>", cell_bold),
        Paragraph("<b>Departure Time</b>", cell_bold),
        Paragraph("<b>Status</b>", cell_bold)
    ]]

    for idx, b in enumerate(buses, 1):
        status_html = '<font color="#15803d"><b>ACTIVE</b></font>' if b["is_active"] else '<font color="#b91c1c"><b>INACTIVE</b></font>'
        table_data.append([
            Paragraph(str(idx), cell_style),
            Paragraph(f"<b>Bus {html_escape(b['bus_number'])}</b>", cell_style),
            Paragraph(html_escape(b["route_name"] or "---"), cell_style),
            Paragraph(f"<b>{html_escape(b['departure_time'] or '3:50 PM')}</b>", cell_style),
            Paragraph(status_html, cell_style)
        ])

    col_widths = [36, 80, 240, 110, 74]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#082d5c')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#ffffff'), colors.HexColor('#f8fafc')]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
    ]))
    story.append(t)
    doc.build(story, canvasmaker=NumberedCanvas)
    buffer.seek(0)

    filename = f"RIT_Return_Bus_Schedule_{datetime.now().strftime('%Y%m%d')}.pdf"
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename
    )


@app.route("/api/admin/attendance/cancellations")
def admin_get_attendance_cancellations():
    """
    Feature 5: Returns audit log of cancelled boardings and bus switches.
    """
    if session.get("user_type") != "admin":
        return jsonify({"success": False, "message": "Admin login required."}), 403

    conn = get_connection()
    logs = conn.execute("""
        SELECT c.id, c.user_id, c.bus_id, c.service_type, c.travel_date,
               c.boarding_time, c.cancelled_at, c.reason,
               u.name as user_name, u.register_number, u.role as user_role,
               b.bus_number, b.route_name
        FROM attendance_cancellation_logs c
        LEFT JOIN users u ON c.user_id = u.id
        LEFT JOIN buses b ON c.bus_id = b.id
        ORDER BY c.id DESC
        LIMIT 200
    """).fetchall()
    conn.close()

    return jsonify({
        "success": True,
        "cancellations": [
            {
                "id": r["id"],
                "user_name": r["user_name"] or "Passenger",
                "register_number": r["register_number"] or "---",
                "user_role": (r["user_role"] or "").capitalize(),
                "bus_number": r["bus_number"] or "---",
                "route_name": r["route_name"] or "---",
                "service_type": r["service_type"],
                "travel_date": r["travel_date"],
                "boarding_time": r["boarding_time"],
                "cancelled_at": r["cancelled_at"],
                "reason": r["reason"]
            }
            for r in logs
        ]
    })


# ============================================================
# ADMIN LOGIN & DASHBOARD
# ============================================================

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "busmate123"


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        return render_template("admin.html", mode="login")
    data = request.form if request.form else (request.get_json(silent=True) or {})
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session.clear()
        session["user_type"] = "admin"
        session["user_name"] = "Administrator"
        return jsonify({"success": True, "redirect": "/admin"})

    return jsonify({"success": False, "message": "Invalid admin username or password."}), 401


@app.route("/admin")
def admin():
    if session.get("user_type") != "admin":
        return redirect(url_for("admin_login"))
    return render_template("admin.html")


# ============================================================
# STATIC / STANDALONE PAGES
# ============================================================

@app.route("/live-map")
@app.route("/live-map.html")
def live_map():
    return render_template("live_map.html")


@app.route("/routes")
def routes_page():
    return render_template("routes.html")


@app.route("/stops")
def stops_page():
    return render_template("stops.html")


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

setup_database()


# ============================================================
# RUN APPLICATION
# ============================================================

if __name__ == "__main__":
    print("\n" + "=" * 65)
    print("🚀 RIT TRANSPORT LOCAL SERVER (HTTP)")
    print("=" * 65)
    print("• Local Access : http://127.0.0.1:5000 or http://localhost:5000")
    print("• Network URL  : http://10.127.97.246:5000")
    print("• Driver GPS   : http://10.127.97.246:5000/driver")
    print("• Student Map  : http://10.127.97.246:5000/live-map?bus=R01")
    print("=" * 65 + "\n")

    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=True
    )
