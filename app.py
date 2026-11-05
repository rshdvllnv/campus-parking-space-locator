"""Campus Parking Space Locator - Flask working prototype (CPE 304L Final Exam).

Database: SQLite by default (local use, file parking.db).
          PostgreSQL when the DATABASE_URL environment variable is set (e.g. Neon on Vercel).

Security highlights:
  * Passwords are stored as Werkzeug hashes (never in plain text).
  * Session cookies are HttpOnly/SameSite (Secure on Vercel).
  * Every state-changing form carries a per-session CSRF token.
  * Driver/Admin roles are enforced server-side, and reservation ownership is checked.
"""
import csv
import hmac
import io
import os
import re
import secrets
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
# Vercel's filesystem is read-only (except /tmp), so fall back to /tmp when no database URL is set.
if os.environ.get("PARKING_DB"):
    DB_PATH = os.environ["PARKING_DB"]
elif os.environ.get("VERCEL") and not USE_PG:
    DB_PATH = "/tmp/parking.db"
else:
    DB_PATH = os.path.join(BASE, "parking.db")

HOLD_MIN = 15
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PLATE_RE = re.compile(r"^[A-Z0-9][A-Z0-9 -]{1,14}$")
VEHICLE_TYPES = ("Car", "Motorcycle", "Van", "SUV")
MAX_CAPACITY = 200

app = Flask(__name__, static_folder="public", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "cpe304l-group12-dev-key-change-me")
app.config.update(
    SESSION_COOKIE_NAME="parking_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)
if not os.environ.get("SECRET_KEY"):
    app.logger.warning("SECRET_KEY is not set; using the development key. Set SECRET_KEY in production.")

FMT = "%Y-%m-%d %H:%M:%S"

if USE_PG:
    import psycopg
    DB_ERRORS = (psycopg.IntegrityError,)
else:
    DB_ERRORS = (sqlite3.IntegrityError,)


def now():
    """Campus local time (Asia/Manila) as a naive datetime; serverless hosts run in UTC."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(os.environ.get("APP_TZ", "Asia/Manila"))).replace(tzinfo=None)
    except Exception:
        return datetime.now()


class Row(dict):
    """Row usable as row['col'], row.col (Jinja) and row[0]."""
    def __getitem__(self, k):
        return list(self.values())[k] if isinstance(k, int) else dict.__getitem__(self, k)


def _row_factory(cursor):
    if cursor.description is None:
        return None

    names = [
        c.name if hasattr(c, "name") else c[0]
        for c in cursor.description
    ]

    def make(values):
        return Row(
            (n, v.strftime(FMT) if isinstance(v, datetime) else v)
            for n, v in zip(names, values)
        )

    return make


class Conn:
    """Thin wrapper so the same SQL ('?' placeholders) runs on SQLite and PostgreSQL."""
    def __init__(self):
        if USE_PG:
            import psycopg
            self.raw = psycopg.connect(DATABASE_URL, row_factory=_row_factory, prepare_threshold=None)
        else:
            self.raw = sqlite3.connect(DB_PATH)
            self.raw.row_factory = lambda cur, row: Row(zip([c[0] for c in cur.description], row))
            self.raw.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql, params=()):
        if USE_PG:
            return self.raw.execute(sql.replace("?", "%s"), params)
        return self.raw.execute(sql, params)

    def insert(self, sql, params, id_col):
        """INSERT and return the new primary key on both back-ends."""
        if USE_PG:
            return self.execute(sql + " RETURNING " + id_col, params).fetchone()[id_col]
        return self.execute(sql, params).lastrowid

    def begin_write(self):
        if not USE_PG:                       # SQLite: take the write lock up front
            self.raw.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def db():
    if "db" not in g:
        g.db = Conn()
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


_ready = False


def seed_demo(con):
    """Create the original demo drivers, vehicles, zones and one hold."""
    driver_ids = []
    for n, r in [("Racid Glenn Banico", "Student"), ("Mark Justin Ragragio", "Student"),
                 ("Rasheed Villanueva", "Student"), ("Engr. Maria Lorena Villena", "Faculty")]:
        driver_ids.append(con.insert("INSERT INTO driver(name,role) VALUES (?,?)", (n, r), "driver_id"))
    for v in [("NBC 1234", "Car", 0), ("ABC 5678", "Motorcycle", 1),
              ("XYZ 9012", "Car", 2), ("FAC 2026", "Car", 3)]:
        con.execute("INSERT INTO vehicle(plate_number,vehicle_type,driver_id) VALUES (?,?,?)",
                    (v[0], v[1], driver_ids[v[2]]))
    for zname, cap, letter, occ in [("Zone A", 6, "A", [3, 6]), ("Zone B", 6, "B", [1, 2, 3, 4, 5, 6])]:
        zid = con.insert("INSERT INTO parking_zone(zone_name,total_capacity) VALUES (?,?)", (zname, cap), "zone_id")
        for n in range(1, cap + 1):
            con.execute("INSERT INTO parking_slot(zone_id,is_occupied,slot_label) VALUES (?,?,?)",
                        (zid, 1 if n in occ else 0, f"{letter}-{n:02d}"))
    # A-04 is held by another driver (Reserved)
    sid = con.execute("SELECT slot_id FROM parking_slot WHERE slot_label='A-04'").fetchone()["slot_id"]
    con.execute("INSERT INTO reservation(slot_id,plate_number,reservation_time,status) VALUES (?,?,?,'Active')",
                (sid, "FAC 2026", now().strftime(FMT)))


DEMO_ACCOUNTS = [
    ("admin", "admin@campus.edu", "Admin@123", "Admin", None),
    ("rasheed", "rasheed@campus.edu", "Driver@123", "Driver", "Rasheed Villanueva"),
    ("racid", "racid@campus.edu", "Driver@123", "Driver", "Racid Glenn Banico"),
    ("mark", "mark@campus.edu", "Driver@123", "Driver", "Mark Justin Ragragio"),
    ("villena", "villena@campus.edu", "Driver@123", "Driver", "Engr. Maria Lorena Villena"),
]


def seed_users(con):
    """Create demo accounts once, linking each driver to one login."""
    if con.execute("SELECT COUNT(*) AS n FROM app_user").fetchone()["n"]:
        return
    for username, email, password, role, driver_name in DEMO_ACCOUNTS:
        driver_id = None
        if driver_name:
            row = con.execute("SELECT driver_id FROM driver WHERE name=?", (driver_name,)).fetchone()
            if row:
                driver_id = row["driver_id"]
            else:
                driver_id = con.insert("INSERT INTO driver(name,role) VALUES (?,'Student')",
                                       (driver_name,), "driver_id")
        con.execute(
            "INSERT INTO app_user(username,email,password_hash,role,driver_id,is_active) VALUES (?,?,?,?,?,1)",
            (username, email, generate_password_hash(password), role, driver_id),
        )


def init_db():
    """Create tables and seed demo data on first use (idempotent)."""
    con = Conn()
    try:
        schema = open(os.path.join(BASE, "schema_pg.sql" if USE_PG else "schema.sql")).read()
        if USE_PG:
            statements = [
                statement.strip()
                for statement in schema.split(";")
                if statement.strip()
            ]
            for statement in statements:
                con.execute(statement)
        else:
            con.raw.executescript(schema)
        con.commit()
        if con.execute("SELECT COUNT(*) AS n FROM parking_zone").fetchone()["n"] == 0:
            seed_demo(con)
            con.commit()
        seed_users(con)
        con.commit()
    except DB_ERRORS:
        con.rollback()
        app.logger.exception("Database initialization failed")
        raise
    finally:
        con.close()


@app.before_request
def ensure_ready():
    global _ready
    if not _ready:
        init_db()
        _ready = True


# ---------------- Session, CSRF and access control ----------------
def current_user():
    return g.get("user")


@app.before_request
def load_user():
    g.user = None
    uid = session.get("user_id")
    if not uid:
        return
    try:
        row = db().execute(
            """SELECT u.user_id, u.username, u.email, u.password_hash, u.role, u.driver_id,
                      u.is_active, u.created_at, d.name AS driver_name
               FROM app_user u LEFT JOIN driver d ON d.driver_id=u.driver_id
               WHERE u.user_id=?""", (uid,)).fetchone()
    except Exception:
        app.logger.exception("Could not load the signed-in user")
        return
    if row and row["is_active"]:
        g.user = row
    else:
        session.clear()


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("_csrf")
        if not expected or not token or not hmac.compare_digest(token, expected):
            abort(400, description="Your security token expired. Reload the page and try again.")


def requested_path():
    path = request.full_path
    return path[:-1] if path.endswith("?") else path


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("dashboard")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("login", next=requested_path()))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            flash("Please sign in with an administrator account.", "warning")
            return redirect(url_for("login", next=requested_path()))
        if user["role"] != "Admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def owns_reservation(res):
    user = current_user()
    if not user or not res:
        return False
    return user["role"] == "Admin" or (user["driver_id"] and res["driver_id"] == user["driver_id"])


_DUMMY_HASH = None


def dummy_hash():
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = generate_password_hash(secrets.token_hex(16))
    return _DUMMY_HASH


def password_problem(password):
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must include at least one letter and one number."
    return None


@app.context_processor
def helpers():
    return dict(HOLD_MIN=HOLD_MIN, now=now, current_user=current_user(),
                csrf_token=csrf_token, nav_active_res=my_active_reservation(),
                MAX_CAPACITY=MAX_CAPACITY)


# ---------------- Shared queries ----------------
def expire_old():
    """Release holds older than 15 minutes (FR-003: the hold is never extended)."""
    cutoff = (now() - timedelta(minutes=HOLD_MIN)).strftime(FMT)
    db().execute("UPDATE reservation SET status='Expired' WHERE status='Active' AND reservation_time <= ?", (cutoff,))
    db().commit()


def slot_state_sql():
    return """CASE WHEN s.is_occupied=1 THEN 'Occupied'
                    WHEN EXISTS (SELECT 1 FROM reservation r WHERE r.slot_id=s.slot_id AND r.status='Active')
                         THEN 'Reserved' ELSE 'Available' END"""


def zones():
    return db().execute(f"""
      SELECT z.zone_id, z.zone_name, z.total_capacity,
             SUM(CASE WHEN {slot_state_sql()}='Available' THEN 1 ELSE 0 END) AS available,
             SUM(CASE WHEN {slot_state_sql()}='Occupied'  THEN 1 ELSE 0 END) AS occupied,
             SUM(CASE WHEN {slot_state_sql()}='Reserved'  THEN 1 ELSE 0 END) AS reserved,
             COUNT(s.slot_id) AS slots
      FROM parking_zone z LEFT JOIN parking_slot s ON s.zone_id=z.zone_id
      GROUP BY z.zone_id ORDER BY z.zone_name""").fetchall()


def slots_of(zone_id):
    return db().execute(f"""SELECT s.slot_id, s.slot_label, {slot_state_sql()} AS state
                            FROM parking_slot s WHERE s.zone_id=? ORDER BY s.slot_label""", (zone_id,)).fetchall()


def get_res(res_id):
    return db().execute("""SELECT r.*, s.slot_label, z.zone_name, v.vehicle_type, v.driver_id,
                                  d.name AS driver_name
        FROM reservation r JOIN parking_slot s ON s.slot_id=r.slot_id JOIN parking_zone z ON z.zone_id=s.zone_id
        JOIN vehicle v ON v.plate_number=r.plate_number JOIN driver d ON d.driver_id=v.driver_id
        WHERE r.res_id=?""", (res_id,)).fetchone()


def remaining(res):
    start = datetime.strptime(res["reservation_time"], FMT)
    return max(0, int((start + timedelta(minutes=HOLD_MIN) - now()).total_seconds()))


def my_active_reservation():
    """The signed-in driver's current hold (used by the header and dashboard notice)."""
    if "my_active" in g:
        return g.my_active
    user = current_user()
    res = None
    if user and user["driver_id"]:
        try:
            res = db().execute("""SELECT r.res_id, r.reservation_time, r.status, s.slot_label, z.zone_name
                FROM reservation r JOIN parking_slot s ON s.slot_id=r.slot_id
                JOIN parking_zone z ON z.zone_id=s.zone_id
                JOIN vehicle v ON v.plate_number=r.plate_number
                WHERE v.driver_id=? AND r.status='Active'
                ORDER BY r.res_id DESC LIMIT 1""", (user["driver_id"],)).fetchone()
            if res:
                res["secs"] = remaining(res)
        except Exception:
            res = None
    g.my_active = res
    return res


# ---------------- Authentication ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))

    next_url = request.values.get("next", "")
    username = request.values.get("username", "")
    if request.method == "POST":
        if session.get("login_lock_until", 0) > time.time():
            flash("Too many failed attempts. Please wait 5 minutes and try again.", "error")
            return render_template("login.html", next=next_url, username=""), 429

        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = db().execute(
            "SELECT * FROM app_user WHERE lower(username)=? OR lower(email)=?", (username, username)).fetchone()

        if user and user["is_active"] and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["user_id"]
            session["_csrf"] = secrets.token_urlsafe(32)
            session.permanent = True
            flash(f"Welcome back, {user['username']}.", "success")
            return redirect(safe_next(next_url))

        if not user:
            check_password_hash(dummy_hash(), password)   # reduce username-enumeration timing hints
        failures = session.get("login_fail_count", 0) + 1
        if failures >= 5:
            session["login_fail_count"] = 0
            session["login_lock_until"] = time.time() + 300
            flash("Too many failed attempts. Please wait 5 minutes and try again.", "error")
        else:
            session["login_fail_count"] = failures
            flash("Invalid username/email or password.", "error")
    return render_template("login.html", next=next_url, username=username)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("dashboard"))

    form = dict(full_name="", username="", email="", driver_role="Student")
    if request.method == "POST":
        form = dict(
            full_name=request.form.get("full_name", "").strip(),
            username=request.form.get("username", "").strip().lower(),
            email=request.form.get("email", "").strip().lower(),
            driver_role=request.form.get("driver_role", "Student"),
        )
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        problem = None

        if not 2 <= len(form["full_name"]) <= 100:
            problem = "Enter your full name (2-100 characters)."
        elif not USERNAME_RE.fullmatch(form["username"]):
            problem = "Username must be 3-30 characters: letters, numbers, dot, dash or underscore."
        elif not EMAIL_RE.fullmatch(form["email"]):
            problem = "Enter a valid email address."
        elif form["driver_role"] not in ("Student", "Faculty"):
            problem = "Choose Student or Faculty."
        elif password_problem(password):
            problem = password_problem(password)
        elif password != confirm:
            problem = "The passwords do not match."
        elif db().execute("SELECT 1 FROM app_user WHERE username=? OR email=?", (form["username"], form["email"])).fetchone():
            problem = "That username or email is already registered."

        if problem:
            flash(problem, "error")
        else:
            try:
                db().begin_write()
                driver_id = db().insert("INSERT INTO driver(name,role) VALUES (?,?)",
                                        (form["full_name"], form["driver_role"]), "driver_id")
                db().insert(
                    "INSERT INTO app_user(username,email,password_hash,role,driver_id,is_active) VALUES (?,?,?,?,?,1)",
                    (form["username"], form["email"], generate_password_hash(password), "Driver", driver_id),
                    "user_id")
                db().commit()
                flash("Account created. Sign in with your new username.", "success")
                return redirect(url_for("login", username=form["username"]))
            except DB_ERRORS:
                db().rollback()
                flash("That username or email is already registered.", "error")
    return render_template("register.html", form=form)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("dashboard"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    user = current_user()
    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "profile":
            email = request.form.get("email", "").strip().lower()
            name = request.form.get("full_name", "").strip()
            if not EMAIL_RE.fullmatch(email):
                flash("Enter a valid email address.", "error")
            elif user["driver_id"] and not 2 <= len(name) <= 100:
                flash("Enter your full name (2-100 characters).", "error")
            elif db().execute("SELECT 1 FROM app_user WHERE email=? AND user_id<>?",
                              (email, user["user_id"])).fetchone():
                flash("That email address is already used by another account.", "error")
            else:
                db().execute("UPDATE app_user SET email=? WHERE user_id=?", (email, user["user_id"]))
                if user["driver_id"]:
                    db().execute("UPDATE driver SET name=? WHERE driver_id=?", (name, user["driver_id"]))
                db().commit()
                flash("Profile updated.", "success")
            return redirect(url_for("account"))

        if action == "password":
            current = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            problem = password_problem(new) if password_problem(new) else (
                None if new == confirm else "The new passwords do not match.")
            if not check_password_hash(user["password_hash"], current):
                flash("Your current password is incorrect.", "error")
            elif problem:
                flash(problem, "error")
            else:
                db().execute("UPDATE app_user SET password_hash=? WHERE user_id=?",
                             (generate_password_hash(new), user["user_id"]))
                db().commit()
                session["_csrf"] = secrets.token_urlsafe(32)
                flash("Password changed.", "success")
            return redirect(url_for("account"))

        if action == "vehicle_add":
            plate = " ".join(request.form.get("plate_number", "").upper().split())
            vehicle_type = request.form.get("vehicle_type", "")
            if not user["driver_id"]:
                flash("Administrator accounts do not have a driver profile.", "error")
            elif not PLATE_RE.fullmatch(plate):
                flash("Enter a valid plate number (2-15 letters/numbers, spaces or dashes).", "error")
            elif vehicle_type not in VEHICLE_TYPES:
                flash("Choose a valid vehicle type.", "error")
            elif db().execute("SELECT 1 FROM vehicle WHERE plate_number=?", (plate,)).fetchone():
                flash("That plate number is already registered.", "error")
            else:
                db().execute("INSERT INTO vehicle(plate_number,vehicle_type,driver_id) VALUES (?,?,?)",
                             (plate, vehicle_type, user["driver_id"]))
                db().commit()
                flash(f"Vehicle {plate} added.", "success")
            return redirect(url_for("account"))

        if action == "vehicle_remove":
            plate = request.form.get("plate_number", "")
            own = db().execute("SELECT 1 FROM vehicle WHERE plate_number=? AND driver_id=?",
                               (plate, user["driver_id"])).fetchone() if user["driver_id"] else None
            if not own:
                flash("That vehicle is not registered to your account.", "error")
            elif db().execute("SELECT 1 FROM reservation WHERE plate_number=? LIMIT 1", (plate,)).fetchone():
                flash("A vehicle with reservation history cannot be removed.", "error")
            else:
                db().execute("DELETE FROM vehicle WHERE plate_number=? AND driver_id=?",
                             (plate, user["driver_id"]))
                db().commit()
                flash(f"Vehicle {plate} removed.", "success")
            return redirect(url_for("account"))

        flash("Unknown account action.", "error")
        return redirect(url_for("account"))

    vehicles = []
    if user["driver_id"]:
        vehicles = db().execute("SELECT plate_number, vehicle_type FROM vehicle WHERE driver_id=? ORDER BY plate_number",
                                (user["driver_id"],)).fetchall()
    return render_template("account.html", user=user, vehicles=vehicles, vehicle_types=VEHICLE_TYPES)


# ---------------- Driver screens ----------------
@app.route("/")
def dashboard():
    expire_old()
    zs = zones()
    total = sum(z["total_capacity"] for z in zs)
    avail = sum(z["available"] or 0 for z in zs)
    return render_template("dashboard.html", zones=zs, total=total, avail=avail,
                           my_active=my_active_reservation())


@app.route("/zone/<int:zone_id>")
def zone_slots(zone_id):
    expire_old()
    z = next((z for z in zones() if z["zone_id"] == zone_id), None)
    if not z:
        abort(404)
    sl = slots_of(zone_id)
    sel = request.args.get("slot", type=int)
    if sel not in [s["slot_id"] for s in sl if s["state"] == "Available"]:
        sel = None                      # occupied / reserved slots cannot be selected (FR-002)
    label = next((s["slot_label"] for s in sl if s["slot_id"] == sel), None)
    return render_template("slots.html", zone=z, slots=sl, sel=sel, sel_label=label)


@app.route("/review")
@login_required
def review():
    expire_old()
    user = current_user()
    slot_id = request.args.get("slot", type=int)
    row = db().execute(f"""SELECT s.slot_id, s.slot_label, z.zone_id, z.zone_name, {slot_state_sql()} AS state
        FROM parking_slot s JOIN parking_zone z ON z.zone_id=s.zone_id WHERE s.slot_id=?""", (slot_id,)).fetchone()
    if not row or row["state"] != "Available":
        flash("That slot is no longer available. Please choose another slot.", "error")
        return redirect(url_for("dashboard"))

    if user["role"] == "Admin":
        vehicles = db().execute("""SELECT v.plate_number, v.vehicle_type, d.name FROM vehicle v
                                   JOIN driver d ON d.driver_id=v.driver_id ORDER BY d.name""").fetchall()
    elif user["driver_id"]:
        vehicles = db().execute("""SELECT v.plate_number, v.vehicle_type, d.name FROM vehicle v
                                   JOIN driver d ON d.driver_id=v.driver_id
                                   WHERE v.driver_id=? ORDER BY v.plate_number""", (user["driver_id"],)).fetchall()
    else:
        vehicles = []

    if not vehicles:
        flash("Add a vehicle to your account before reserving a slot.", "warning")
        return redirect(url_for("account"))

    start = now()
    return render_template("review.html", slot=row, vehicles=vehicles, start=start,
                           end=start + timedelta(minutes=HOLD_MIN))


@app.route("/reserve", methods=["POST"])
@login_required
def reserve():
    expire_old()
    user = current_user()
    slot_id = request.form.get("slot_id", type=int)
    plate = request.form.get("plate_number", "")
    zrow = db().execute("SELECT zone_id, slot_label FROM parking_slot WHERE slot_id=?", (slot_id,)).fetchone()
    back = url_for("zone_slots", zone_id=zrow["zone_id"]) if zrow else url_for("dashboard")

    if user["role"] == "Admin":
        vehicle = db().execute("SELECT 1 FROM vehicle WHERE plate_number=?", (plate,)).fetchone()
    else:
        vehicle = db().execute("SELECT 1 FROM vehicle WHERE plate_number=? AND driver_id=?",
                               (plate, user["driver_id"])).fetchone() if user["driver_id"] else None
    if not vehicle:
        flash("Choose one of your registered vehicles.", "error")
        return redirect(back)

    try:
        db().begin_write()
        st = db().execute(f"SELECT {slot_state_sql()} AS st FROM parking_slot s WHERE s.slot_id=?",
                          (slot_id,)).fetchone()
        if not st or st["st"] != "Available":
            raise ValueError("taken")
        if db().execute("SELECT 1 FROM reservation WHERE plate_number=? AND status='Active'", (plate,)).fetchone():
            db().rollback()
            flash("This vehicle already has an active reservation.", "error")
            return redirect(back)
        new_id = db().insert("INSERT INTO reservation(slot_id,plate_number,reservation_time,status) VALUES (?,?,?,'Active')",
                             (slot_id, plate, now().strftime(FMT)), "res_id")
        db().commit()
        return redirect(url_for("active", res_id=new_id))
    except (ValueError,) + DB_ERRORS:
        db().rollback()
        label = zrow["slot_label"] if zrow else "That slot"
        flash(f"Slot no longer available. {label} was just taken. Choose another available slot below.", "error")
        return redirect(back)


@app.route("/reservation/<int:res_id>")
@login_required
def active(res_id):
    expire_old()
    r = get_res(res_id)
    if not r:
        abort(404)
    if not owns_reservation(r):
        abort(403)
    if r["status"] == "Completed":
        return redirect(url_for("arrived", res_id=res_id))
    return render_template("active.html", r=r, secs=remaining(r))


@app.route("/reservation/<int:res_id>/arrive", methods=["POST"])
@login_required
def arrive(res_id):
    expire_old()
    r = get_res(res_id)
    if not r:
        abort(404)
    if not owns_reservation(r):
        abort(403)
    if r["status"] != "Active":
        flash("Your reservation is no longer active. Please reserve a slot again.", "error")
        return redirect(url_for("dashboard"))
    db().execute("UPDATE reservation SET status='Completed' WHERE res_id=?", (res_id,))
    db().execute("UPDATE parking_slot SET is_occupied=1 WHERE slot_id=?", (r["slot_id"],))  # FR-004
    db().commit()
    return redirect(url_for("arrived", res_id=res_id))


@app.route("/reservation/<int:res_id>/cancel", methods=["POST"])
@login_required
def cancel(res_id):
    r = get_res(res_id)
    if not r:
        abort(404)
    if not owns_reservation(r):
        abort(403)
    if r["status"] == "Active":
        db().execute("UPDATE reservation SET status='Cancelled' WHERE res_id=?", (res_id,))
        db().commit()
        flash("Reservation cancelled. The slot is available again.", "success")
    else:
        flash("That reservation has already ended.", "warning")
    return redirect(url_for("my_reservations") if current_user()["role"] != "Admin" else url_for("dashboard"))


@app.route("/reservation/<int:res_id>/arrived")
@login_required
def arrived(res_id):
    expire_old()
    r = get_res(res_id)
    if not r:
        abort(404)
    if not owns_reservation(r):
        abort(403)
    zs = zones()
    return render_template("arrived.html", r=r, total=sum(z["total_capacity"] for z in zs),
                           avail=sum(z["available"] or 0 for z in zs))


@app.route("/reservations")
@login_required
def my_reservations():
    expire_old()
    user = current_user()
    if not user["driver_id"]:
        flash("Administrator accounts do not have reservation history. Use a driver account.", "warning")
        return redirect(url_for("dashboard"))
    rows = db().execute("""SELECT r.res_id, r.slot_id, r.plate_number, r.reservation_time, r.status,
                                  s.slot_label, z.zone_name
        FROM reservation r JOIN parking_slot s ON s.slot_id=r.slot_id
        JOIN parking_zone z ON z.zone_id=s.zone_id
        JOIN vehicle v ON v.plate_number=r.plate_number
        WHERE v.driver_id=? ORDER BY r.res_id DESC LIMIT 30""", (user["driver_id"],)).fetchall()
    for r in rows:
        r["secs"] = remaining(r) if r["status"] == "Active" else None
    return render_template("my_reservations.html", reservations=rows)


# ---------------- Administrator ----------------
@app.route("/admin")
@admin_required
def admin():
    expire_old()
    log = db().execute("""SELECT r.res_id, s.slot_label, z.zone_name, r.plate_number, d.name AS driver_name,
                                 r.reservation_time, r.status
        FROM reservation r JOIN parking_slot s ON s.slot_id=r.slot_id JOIN parking_zone z ON z.zone_id=s.zone_id
        JOIN vehicle v ON v.plate_number=r.plate_number JOIN driver d ON d.driver_id=v.driver_id
        ORDER BY r.res_id DESC LIMIT 15""").fetchall()
    zs = zones()
    all_slots = db().execute(f"""SELECT s.slot_id, s.slot_label, z.zone_name, s.is_occupied, {slot_state_sql()} AS state
        FROM parking_slot s JOIN parking_zone z ON z.zone_id=s.zone_id ORDER BY z.zone_name, s.slot_label""").fetchall()
    stats = dict(total=sum(z["total_capacity"] for z in zs), avail=sum(z["available"] or 0 for z in zs),
                 occ=sum(z["occupied"] or 0 for z in zs), res=sum(z["reserved"] or 0 for z in zs),
                 drivers=db().execute("SELECT COUNT(*) FROM driver").fetchone()[0],
                 vehicles=db().execute("SELECT COUNT(*) FROM vehicle").fetchone()[0])
    return render_template("admin.html", zones=zs, slots=all_slots, log=log, stats=stats)


@app.route("/admin/zone/add", methods=["POST"])
@admin_required
def zone_add():
    name = request.form.get("zone_name", "").strip()
    cap = request.form.get("total_capacity", type=int)
    if not 1 <= len(name) <= 50:
        flash("Enter a zone name (1-50 characters).", "error")
        return redirect(url_for("admin"))
    if not cap or not 1 <= cap <= MAX_CAPACITY:
        flash(f"Capacity must be between 1 and {MAX_CAPACITY}.", "error")
        return redirect(url_for("admin"))
    try:
        db().begin_write()
        zid = db().insert("INSERT INTO parking_zone(zone_name,total_capacity) VALUES (?,?)", (name, cap), "zone_id")
        letter = name.split()[-1][:1].upper()
        for n in range(1, cap + 1):
            db().execute("INSERT INTO parking_slot(zone_id,is_occupied,slot_label) VALUES (?,0,?)",
                         (zid, f"{letter}-{n:02d}"))
        db().commit()
        flash(f"{name} added with {cap} slots.", "success")
    except DB_ERRORS:
        db().rollback()
        flash("A zone with that name already exists.", "error")
    return redirect(url_for("admin"))


@app.route("/admin/zone/<int:zone_id>/capacity", methods=["POST"])
@admin_required
def zone_capacity(zone_id):
    cap = request.form.get("total_capacity", type=int)
    if not cap or not 1 <= cap <= MAX_CAPACITY:
        flash(f"Capacity must be between 1 and {MAX_CAPACITY}.", "error")
        return redirect(url_for("admin"))
    z = db().execute("SELECT * FROM parking_zone WHERE zone_id=?", (zone_id,)).fetchone()
    if not z:
        abort(404)
    try:
        db().begin_write()
        cur = db().execute("SELECT COUNT(*) FROM parking_slot WHERE zone_id=?", (zone_id,)).fetchone()[0]
        letter = z["zone_name"].split()[-1][:1].upper()
        if cap > cur:
            for n in range(cur + 1, cap + 1):
                db().execute("INSERT INTO parking_slot(zone_id,is_occupied,slot_label) VALUES (?,0,?)",
                             (zone_id, f"{letter}-{n:02d}"))
        elif cap < cur:
            removable = db().execute(f"""SELECT s.slot_id FROM parking_slot s WHERE s.zone_id=? AND {slot_state_sql()}='Available'
                                         AND NOT EXISTS (SELECT 1 FROM reservation h WHERE h.slot_id=s.slot_id)
                                         ORDER BY s.slot_label DESC""", (zone_id,)).fetchall()
            if len(removable) < cur - cap:
                db().rollback()
                flash("Cannot reduce capacity: occupied or reserved slots would be removed.", "error")
                return redirect(url_for("admin"))
            for r in removable[: cur - cap]:
                db().execute("DELETE FROM parking_slot WHERE slot_id=?", (r["slot_id"],))
        db().execute("UPDATE parking_zone SET total_capacity=? WHERE zone_id=?", (cap, zone_id))
        db().commit()
        flash(f"{z['zone_name']} capacity updated to {cap}.", "success")
    except DB_ERRORS:
        db().rollback()
        flash("Could not update the capacity because related records exist.", "error")
    return redirect(url_for("admin"))


@app.route("/admin/slot/<int:slot_id>/toggle", methods=["POST"])
@admin_required
def slot_toggle(slot_id):
    """Simulates a vehicle entering / leaving a slot (FR-007 sensor event)."""
    s = db().execute(f"SELECT is_occupied, {slot_state_sql()} AS state FROM parking_slot s WHERE slot_id=?",
                     (slot_id,)).fetchone()
    if not s:
        abort(404)
    if s["state"] == "Reserved":
        flash("Reserved slots cannot be changed until the hold ends.", "error")
    else:
        db().execute("UPDATE parking_slot SET is_occupied=? WHERE slot_id=?",
                     (0 if s["is_occupied"] else 1, slot_id))
        db().commit()
    return redirect(url_for("admin"))


@app.route("/admin/users")
@admin_required
def admin_users():
    users = db().execute("""SELECT u.user_id, u.username, u.email, u.role, u.is_active, u.created_at, u.driver_id,
                                   d.name AS driver_name,
                                   (SELECT COUNT(*) FROM vehicle v WHERE v.driver_id=u.driver_id) AS vehicles
        FROM app_user u LEFT JOIN driver d ON d.driver_id=u.driver_id
        ORDER BY u.role, u.username""").fetchall()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_user_role(user_id):
    role = request.form.get("role", "")
    if role not in ("Driver", "Admin"):
        abort(400, description="Invalid role.")
    target = db().execute("SELECT user_id, username, role, driver_id FROM app_user WHERE user_id=?",
                          (user_id,)).fetchone()
    if not target:
        abort(404)
    if target["user_id"] == current_user()["user_id"]:
        flash("You cannot change your own role.", "error")
    elif role == "Driver" and not target["driver_id"]:
        flash("This administrator has no driver profile and cannot be demoted to Driver.", "error")
    else:
        db().execute("UPDATE app_user SET role=? WHERE user_id=?", (role, user_id))
        db().commit()
        flash(f"{target['username']} is now {role}.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/active", methods=["POST"])
@admin_required
def admin_user_active(user_id):
    target = db().execute("SELECT user_id, username, role, driver_id, is_active FROM app_user WHERE user_id=?",
                          (user_id,)).fetchone()
    if not target:
        abort(404)
    if target["user_id"] == current_user()["user_id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("admin_users"))

    new_state = 0 if target["is_active"] else 1
    try:
        db().begin_write()
        db().execute("UPDATE app_user SET is_active=? WHERE user_id=?", (new_state, user_id))
        if not new_state and target["driver_id"]:
            db().execute("""UPDATE reservation SET status='Cancelled'
                            WHERE status='Active' AND plate_number IN
                            (SELECT plate_number FROM vehicle WHERE driver_id=?)""", (target["driver_id"],))
        db().commit()
        flash(f"{target['username']} {'activated' if new_state else 'deactivated'}."
              + (" Active holds were released." if not new_state else ""), "success")
    except DB_ERRORS:
        db().rollback()
        flash("Could not update the account.", "error")
    return redirect(url_for("admin_users"))


def report_rows():
    return db().execute("""SELECT r.res_id, r.plate_number, r.reservation_time, r.status,
                                  s.slot_label, z.zone_name, v.vehicle_type, d.name AS driver_name
        FROM reservation r JOIN parking_slot s ON s.slot_id=r.slot_id
        JOIN parking_zone z ON z.zone_id=s.zone_id
        JOIN vehicle v ON v.plate_number=r.plate_number
        JOIN driver d ON d.driver_id=v.driver_id
        ORDER BY r.res_id DESC""").fetchall()


@app.route("/admin/reports")
@admin_required
def admin_reports():
    expire_old()
    rows = report_rows()
    counts = Counter(r["status"] for r in rows)
    total = len(rows)
    zones_rows = zones()
    hours = Counter()
    for r in rows:
        try:
            hours[datetime.strptime(r["reservation_time"], FMT).hour] += 1
        except (ValueError, TypeError):
            pass
    peak = sorted(hours.items(), key=lambda item: (-item[1], item[0]))[:6]
    peak_max = max((count for _, count in peak), default=1)
    return render_template("admin_reports.html", rows=rows[:15], counts=counts, total=total,
                           zones=zones_rows, peak=peak, peak_max=peak_max,
                           generated=now().strftime(FMT))


@app.route("/admin/reports.csv")
@admin_required
def admin_reports_csv():
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Reference", "Slot", "Zone", "Plate", "Vehicle", "Driver", "Reserved at", "Status"])
    for r in report_rows():
        writer.writerow([f"PK-{r['res_id']:06d}", r["slot_label"], r["zone_name"], r["plate_number"],
                         r["vehicle_type"], r["driver_name"], r["reservation_time"], r["status"]])
    filename = f"parking-report-{now().strftime('%Y%m%d-%H%M')}.csv"
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------- API, headers and errors ----------------
@app.route("/api/availability")
def api_availability():
    expire_old()
    zs = zones()
    return jsonify(updated=now().strftime("%H:%M:%S"),
                   campus=dict(available=sum(z["available"] or 0 for z in zs),
                               total=sum(z["total_capacity"] for z in zs)),
                   zones=[dict(zone=z["zone_name"], available=z["available"], total=z["total_capacity"])
                          for z in zs])


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'self'")
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


def error_page(code, title, message):
    return render_template("error.html", code=code, title=title, message=message), code


@app.errorhandler(400)
def bad_request(e):
    return error_page(400, "Bad request", getattr(e, "description", "The request could not be processed."))


@app.errorhandler(403)
def forbidden(_):
    return error_page(403, "Access denied", "Your account does not have permission to view this page.")


@app.errorhandler(404)
def not_found(_):
    return error_page(404, "Page not found", "The page or record you requested does not exist.")


@app.errorhandler(405)
def method_not_allowed(_):
    return error_page(405, "Method not allowed", "That action is not available for this page.")


@app.errorhandler(413)
def too_large(_):
    return error_page(413, "Request too large", "The submitted data is larger than this application accepts.")


@app.errorhandler(500)
def internal_error(e):
    app.logger.exception("Unhandled application error: %s", e)
    return error_page(500, "Something went wrong",
                      "An unexpected error occurred. Please try again, or contact the administrator if it continues.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
