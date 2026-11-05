"""Campus Parking Space Locator - Flask working prototype (CPE 304L Final Exam).

Database: SQLite by default (local use, file parking.db).
          PostgreSQL when the DATABASE_URL environment variable is set (e.g. Neon on Vercel).
"""
import os, sqlite3
from datetime import datetime, timedelta
from flask import Flask, g, render_template, request, redirect, url_for, jsonify, flash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("PARKING_DB", os.path.join(BASE, "parking.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
HOLD_MIN = 15
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cpe304l-group12")
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
            for n, r in [("Racid Glenn Banico", "Student"), ("Mark Justin Ragragio", "Student"),
                         ("Rasheed Villanueva", "Student"), ("Engr. Maria Lorena Villena", "Faculty")]:
                con.execute("INSERT INTO driver(name,role) VALUES (?,?)", (n, r))
            for v in [("NBC 1234", "Car", 1), ("ABC 5678", "Motorcycle", 2),
                      ("XYZ 9012", "Car", 3), ("FAC 2026", "Car", 4)]:
                con.execute("INSERT INTO vehicle(plate_number,vehicle_type,driver_id) VALUES (?,?,?)", v)
            for zname, cap, letter, occ in [("Zone A", 6, "A", [3, 6]), ("Zone B", 6, "B", [1, 2, 3, 4, 5, 6])]:
                zid = con.insert("INSERT INTO parking_zone(zone_name,total_capacity) VALUES (?,?)", (zname, cap), "zone_id")
                for n in range(1, cap + 1):
                    con.execute("INSERT INTO parking_slot(zone_id,is_occupied,slot_label) VALUES (?,?,?)",
                                (zid, 1 if n in occ else 0, f"{letter}-{n:02d}"))
            # A-04 is held by another driver (Reserved)
            sid = con.execute("SELECT slot_id FROM parking_slot WHERE slot_label='A-04'").fetchone()["slot_id"]
            con.execute("INSERT INTO reservation(slot_id,plate_number,reservation_time,status) VALUES (?,?,?,'Active')",
                        (sid, "FAC 2026", now().strftime(FMT)))
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
    return db().execute("""SELECT r.*, s.slot_label, z.zone_name, v.vehicle_type, d.name AS driver_name
        FROM reservation r JOIN parking_slot s ON s.slot_id=r.slot_id JOIN parking_zone z ON z.zone_id=s.zone_id
        JOIN vehicle v ON v.plate_number=r.plate_number JOIN driver d ON d.driver_id=v.driver_id
        WHERE r.res_id=?""", (res_id,)).fetchone()


def remaining(res):
    start = datetime.strptime(res["reservation_time"], FMT)
    return max(0, int((start + timedelta(minutes=HOLD_MIN) - now()).total_seconds()))


@app.context_processor
def helpers():
    return dict(HOLD_MIN=HOLD_MIN, now=now)


# ---------------- Driver screens ----------------
@app.route("/")
def dashboard():
    expire_old()
    zs = zones()
    total = sum(z["total_capacity"] for z in zs)
    avail = sum(z["available"] or 0 for z in zs)
    return render_template("dashboard.html", zones=zs, total=total, avail=avail)


@app.route("/zone/<int:zone_id>")
def zone_slots(zone_id):
    expire_old()
    z = next((z for z in zones() if z["zone_id"] == zone_id), None)
    if not z:
        return redirect(url_for("dashboard"))
    sl = slots_of(zone_id)
    sel = request.args.get("slot", type=int)
    if sel not in [s["slot_id"] for s in sl if s["state"] == "Available"]:
        sel = None                      # occupied / reserved slots cannot be selected (FR-002)
    label = next((s["slot_label"] for s in sl if s["slot_id"] == sel), None)
    return render_template("slots.html", zone=z, slots=sl, sel=sel, sel_label=label)


@app.route("/review")
def review():
    expire_old()
    slot_id = request.args.get("slot", type=int)
    row = db().execute(f"""SELECT s.slot_id, s.slot_label, z.zone_id, z.zone_name, {slot_state_sql()} AS state
        FROM parking_slot s JOIN parking_zone z ON z.zone_id=s.zone_id WHERE s.slot_id=?""", (slot_id,)).fetchone()
    if not row or row["state"] != "Available":
        flash("That slot is no longer available. Please choose another slot.", "error")
        return redirect(url_for("dashboard"))
    vehicles = db().execute("""SELECT v.plate_number, v.vehicle_type, d.name FROM vehicle v
                               JOIN driver d ON d.driver_id=v.driver_id ORDER BY d.name""").fetchall()
    start = now()
    return render_template("review.html", slot=row, vehicles=vehicles, start=start,
                           end=start + timedelta(minutes=HOLD_MIN))


@app.route("/reserve", methods=["POST"])
def reserve():
    expire_old()
    slot_id = request.form.get("slot_id", type=int)
    plate = request.form.get("plate_number", "")
    zrow = db().execute("SELECT zone_id, slot_label FROM parking_slot WHERE slot_id=?", (slot_id,)).fetchone()
    back = url_for("zone_slots", zone_id=zrow["zone_id"]) if zrow else url_for("dashboard")
    try:
        db().begin_write()
        st = db().execute(f"SELECT {slot_state_sql()} AS st FROM parking_slot s WHERE s.slot_id=?", (slot_id,)).fetchone()
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
def active(res_id):
    expire_old()
    r = get_res(res_id)
    if not r:
        return redirect(url_for("dashboard"))
    if r["status"] == "Completed":
        return redirect(url_for("arrived", res_id=res_id))
    return render_template("active.html", r=r, secs=remaining(r))


@app.route("/reservation/<int:res_id>/arrive", methods=["POST"])
def arrive(res_id):
    expire_old()
    r = get_res(res_id)
    if not r or r["status"] != "Active":
        flash("Your reservation is no longer active. Please reserve a slot again.", "error")
        return redirect(url_for("dashboard"))
    db().execute("UPDATE reservation SET status='Completed' WHERE res_id=?", (res_id,))
    db().execute("UPDATE parking_slot SET is_occupied=1 WHERE slot_id=?", (r["slot_id"],))  # FR-004
    db().commit()
    return redirect(url_for("arrived", res_id=res_id))


@app.route("/reservation/<int:res_id>/cancel", methods=["POST"])
def cancel(res_id):
    db().execute("UPDATE reservation SET status='Cancelled' WHERE res_id=? AND status='Active'", (res_id,))
    db().commit()
    flash("Reservation cancelled. The slot is available again.", "success")
    return redirect(url_for("dashboard"))


@app.route("/reservation/<int:res_id>/arrived")
def arrived(res_id):
    expire_old()
    r = get_res(res_id)
    if not r:
        return redirect(url_for("dashboard"))
    zs = zones()
    return render_template("arrived.html", r=r, total=sum(z["total_capacity"] for z in zs),
                           avail=sum(z["available"] or 0 for z in zs))


# ---------------- Administrator ----------------
@app.route("/admin")
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
def zone_add():
    name = request.form.get("zone_name", "").strip()
    cap = request.form.get("total_capacity", type=int)
    if not name or not cap or cap < 1:
        flash("Enter a zone name and a capacity of at least 1.", "error")
        return redirect(url_for("admin"))
    try:
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
def zone_capacity(zone_id):
    cap = request.form.get("total_capacity", type=int)
    if not cap or cap < 1:
        flash("Capacity must be at least 1.", "error")
        return redirect(url_for("admin"))
    z = db().execute("SELECT * FROM parking_zone WHERE zone_id=?", (zone_id,)).fetchone()
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
            flash("Cannot reduce capacity: occupied or reserved slots would be removed.", "error")
            return redirect(url_for("admin"))
        for r in removable[: cur - cap]:
            db().execute("DELETE FROM parking_slot WHERE slot_id=?", (r["slot_id"],))
    db().execute("UPDATE parking_zone SET total_capacity=? WHERE zone_id=?", (cap, zone_id))
    db().commit()
    flash(f"{z['zone_name']} capacity updated to {cap}.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/slot/<int:slot_id>/toggle", methods=["POST"])
def slot_toggle(slot_id):
    """Simulates a vehicle entering / leaving a slot (FR-007 sensor event)."""
    s = db().execute(f"SELECT is_occupied, {slot_state_sql()} AS state FROM parking_slot s WHERE slot_id=?", (slot_id,)).fetchone()
    if s and s["state"] == "Reserved":
        flash("Reserved slots cannot be changed until the hold ends.", "error")
    elif s:
        db().execute("UPDATE parking_slot SET is_occupied=? WHERE slot_id=?", (0 if s["is_occupied"] else 1, slot_id))
        db().commit()
    return redirect(url_for("admin"))


@app.route("/api/availability")
def api_availability():
    expire_old()
    zs = zones()
    return jsonify(updated=now().strftime("%H:%M:%S"),
                   campus=dict(available=sum(z["available"] or 0 for z in zs), total=sum(z["total_capacity"] for z in zs)),
                   zones=[dict(zone=z["zone_name"], available=z["available"], total=z["total_capacity"]) for z in zs])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
