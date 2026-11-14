import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "busmate.db"


def run_migration():
    print(f"Connecting to database: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")

    # 1. USERS TABLE UPGRADE
    user_cols = {row["name"] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    
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
            print(f"Adding column '{col_name}' to users table...")
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")

    # Set default class_section for existing students if null
    cursor.execute("""
        UPDATE users
        SET class_section = 'A'
        WHERE role = 'student' AND (class_section IS NULL OR class_section = '')
    """)

    # Set default lifecycle_status for existing users
    cursor.execute("""
        UPDATE users
        SET lifecycle_status = 'ACTIVE'
        WHERE lifecycle_status IS NULL OR lifecycle_status = ''
    """)

    # Set admin's register_number to 'admin' if not already set
    admin_row = cursor.execute("SELECT id, register_number FROM users WHERE role = 'admin' OR username = 'admin'").fetchone()
    if admin_row:
        cursor.execute("""
            UPDATE users
            SET register_number = COALESCE(register_number, 'admin'),
                role = 'admin',
                updated_at = ?
            WHERE id = ?
        """, (datetime.now().strftime("%d-%m-%Y %I:%M:%S %p"), admin_row["id"]))

    # 2. MIGRATE STUDENTS INTO USERS
    students = cursor.execute("SELECT * FROM students").fetchall()
    default_bus = cursor.execute("SELECT id FROM buses ORDER BY id ASC LIMIT 1").fetchone()
    default_bus_id = default_bus["id"] if default_bus else 1
    default_stop = cursor.execute("SELECT id FROM stops WHERE bus_id = ? ORDER BY id ASC LIMIT 1", (default_bus_id,)).fetchone()
    default_stop_id = default_stop["id"] if default_stop else None

    for s in students:
        s_id = s["student_id"]
        existing = cursor.execute("SELECT id FROM users WHERE register_number = ? OR email = ? OR username = ?", (s_id, s["email"], s_id)).fetchone()
        if not existing:
            print(f"Migrating student {s['name']} ({s_id}) into users table...")
            cursor.execute("""
                INSERT INTO users (
                    username, password, role, name, register_number, email, mobile, department, academic_year, class_section,
                    bus_id, boarding_point_id, password_hash, salt, is_active, lifecycle_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVE', ?, ?)
            """, (
                s_id,
                "",
                "student",
                s["name"],
                s_id,
                s["email"],
                "",
                "B.E. Computer Science & Engineering",
                "2022–2026",
                "A",
                default_bus_id,
                default_stop_id,
                s["password_hash"],
                s["salt"],
                datetime.now().strftime("%d-%m-%Y %I:%M:%S %p"),
                datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
            ))
        else:
            cursor.execute("""
                UPDATE users
                SET password_hash = COALESCE(password_hash, ?),
                    salt = COALESCE(salt, ?),
                    register_number = COALESCE(register_number, ?),
                    role = 'student',
                    bus_id = COALESCE(bus_id, ?),
                    boarding_point_id = COALESCE(boarding_point_id, ?),
                    class_section = COALESCE(class_section, 'A')
                WHERE id = ?
            """, (s["password_hash"], s["salt"], s_id, default_bus_id, default_stop_id, existing["id"]))

    # 3. MIGRATE FACULTY INTO USERS
    faculty_members = cursor.execute("SELECT * FROM faculty").fetchall()
    for f in faculty_members:
        f_id = f["faculty_id"]
        existing = cursor.execute("SELECT id FROM users WHERE register_number = ? OR email = ? OR username = ?", (f_id, f["email"], f_id)).fetchone()
        if not existing:
            print(f"Migrating faculty {f['name']} ({f_id}) into users table...")
            cursor.execute("""
                INSERT INTO users (
                    username, password, role, name, register_number, email, mobile, department, academic_year, class_section,
                    bus_id, boarding_point_id, password_hash, salt, is_active, lifecycle_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 1, 'ACTIVE', ?, ?)
            """, (
                f_id,
                "",
                "faculty",
                f["name"],
                f_id,
                f["email"],
                "",
                "B.E. Computer Science & Engineering",
                "Associate Professor",
                default_bus_id,
                default_stop_id,
                f["password_hash"],
                f["salt"],
                datetime.now().strftime("%d-%m-%Y %I:%M:%S %p"),
                datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
            ))
        else:
            cursor.execute("""
                UPDATE users
                SET password_hash = COALESCE(password_hash, ?),
                    salt = COALESCE(salt, ?),
                    register_number = COALESCE(register_number, ?),
                    role = 'faculty',
                    bus_id = COALESCE(bus_id, ?),
                    boarding_point_id = COALESCE(boarding_point_id, ?)
                WHERE id = ?
            """, (f["password_hash"], f["salt"], f_id, default_bus_id, default_stop_id, existing["id"]))

    # 4. DRIVERS TABLE UPGRADE
    driver_cols = {row["name"] for row in cursor.execute("PRAGMA table_info(drivers)").fetchall()}
    if "is_active" not in driver_cols:
        print("Adding 'is_active' column to drivers table...")
        cursor.execute("ALTER TABLE drivers ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "deactivation_reason" not in driver_cols:
        print("Adding 'deactivation_reason' column to drivers table...")
        cursor.execute("ALTER TABLE drivers ADD COLUMN deactivation_reason TEXT")
    if "created_at" not in driver_cols:
        print("Adding 'created_at' column to drivers table...")
        cursor.execute("ALTER TABLE drivers ADD COLUMN created_at TEXT")
        cursor.execute("UPDATE drivers SET created_at = ?", (datetime.now().strftime("%d-%m-%Y %I:%M:%S %p"),))

    # 5. DRIVER ASSIGNMENTS TABLE
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

    # Populate active assignments from drivers table
    existing_assignments = cursor.execute("SELECT COUNT(*) FROM driver_assignments").fetchone()[0]
    if existing_assignments == 0:
        drivers = cursor.execute("SELECT id, assigned_bus_id FROM drivers WHERE assigned_bus_id IS NOT NULL").fetchall()
        for d in drivers:
            print(f"Creating driver assignment for driver {d['id']} -> bus {d['assigned_bus_id']}...")
            cursor.execute("""
                INSERT INTO driver_assignments (driver_id, bus_id, assigned_at, is_active)
                VALUES (?, ?, ?, 1)
            """, (d["id"], d["assigned_bus_id"], datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")))

    # 6. MORNING BUS CONFIGURATION TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS morning_bus_config (
            bus_id INTEGER PRIMARY KEY,
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    all_buses = cursor.execute("SELECT id FROM buses").fetchall()
    now_str = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    for b in all_buses:
        cursor.execute("""
            INSERT OR IGNORE INTO morning_bus_config (bus_id, is_active, updated_at)
            VALUES (?, 1, ?)
        """, (b["id"], now_str))

    # 7. DAILY RETURN BUS CONFIGURATION TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_return_config (
            bus_id INTEGER PRIMARY KEY,
            departure_time TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT,
            FOREIGN KEY (bus_id) REFERENCES buses(id) ON DELETE CASCADE
        )
    """)

    for b in all_buses:
        cursor.execute("""
            INSERT OR IGNORE INTO daily_return_config (bus_id, departure_time, is_active, updated_at)
            VALUES (?, NULL, 1, ?)
        """, (b["id"], now_str))

    # 8. PASSWORD RESET OTPS TABLE
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

    # 9. PERFORMANCE INDEXES
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_reg_number ON users(register_number)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_bus_id ON users(bus_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_lifecycle ON users(lifecycle_status, is_active)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_drivers_phone ON drivers(phone)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_stops_bus_id ON stops(bus_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_driver_assignments_active ON driver_assignments(driver_id, is_active)")

    conn.commit()
    conn.close()
    print("Database migration completed successfully!")


if __name__ == "__main__":
    run_migration()
