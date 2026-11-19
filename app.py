"""
Annam backend: Food Waste Redistribution & NGO Coordination Platform.

Modules (matching the front-end page):
  1. Accounts & roles   -> /api/auth/*, role-checked routes (JWT, PyJWT)
  2. Donation listing   -> POST/GET /api/donations, safety-window expiry
  3. Geo matching       -> Haversine, NGOs within 10 km, nearest first
  4. Reservation lock   -> atomic claim (first NGO wins, no race conditions)
  5. Dispatch & proof   -> pickup + PIN-verified delivery

Run:  python app.py        (serves API and the web page on http://localhost:5000)
"""
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from functools import wraps

import jwt
from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ANNAM_DB", os.path.join(BASE, "annam.db"))
SECRET = os.environ.get("ANNAM_JWT_SECRET")
if not SECRET:
    SECRET = secrets.token_hex(32)
    print("WARNING: ANNAM_JWT_SECRET not set. Using a random key; logins reset on restart.")
DEFAULT_PIN = os.environ.get("ANNAM_DEFAULT_PIN", "NGO_CONFIRM_2026")
TOKEN_HOURS = 8
MAX_RADIUS_KM = 10.0
ROLES = ("Donor", "NGO", "Volunteer")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__, static_folder=None)
app.config["JSON_SORT_KEYS"] = False

SEED_NGOS = [
    ("N1", "Anbu Illam", 13.0827, 80.2707),
    ("N2", "Asha Community Kitchen", 13.0674, 80.2376),
    ("N3", "Thulir Trust", 12.9941, 80.2185),
    ("N4", "Kaval Food Bank", 13.1143, 80.2329),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS ngos(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  pin_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
  pwd_hash TEXT NOT NULL, role TEXT NOT NULL, ngo_id TEXT REFERENCES ngos(id),
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS donations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, donor TEXT NOT NULL, food_type TEXT NOT NULL,
  qty INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'POSTED'
    CHECK(status IN ('POSTED','ACCEPTED','IN_TRANSIT','DELIVERED','EXPIRED')),
  ngo_id TEXT REFERENCES ngos(id), volunteer TEXT,
  accepted_at INTEGER, picked_at INTEGER, delivered_at INTEGER);
CREATE INDEX IF NOT EXISTS ix_don_status ON donations(status, expires_at);
"""


# ---------------------------------------------------------------- database
def now_ms():
    return int(time.time() * 1000)


def db():
    if "db" not in g:
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        g.db = c
    return g.db


@app.teardown_appcontext
def close_db(_):
    c = g.pop("db", None)
    if c is not None:
        c.close()


def init_db(seed=True):
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    if c.execute("SELECT COUNT(*) FROM ngos").fetchone()[0] == 0:
        ph = generate_password_hash(DEFAULT_PIN)
        c.executemany("INSERT INTO ngos VALUES(?,?,?,?,?)", [(*n, ph) for n in SEED_NGOS])
    if seed and c.execute("SELECT COUNT(*) FROM donations").fetchone()[0] == 0:
        t = now_ms()
        H = 3600e3
        rows = [
            ("Hotel Saravana", "Veg biryani", 40, 13.0418, 80.2341, 3, "POSTED", None),
            ("Wedding hall, Adyar", "Chapati and dal", 65, 13.0012, 80.2565, 5, "POSTED", None),
            ("Campus canteen", "Bread and fruit", 25, 13.0900, 80.2500, 1.5, "POSTED", None),
            ("Bakery, T. Nagar", "Packed buns", 30, 13.0418, 80.2341, 6, "ACCEPTED", "N2"),
        ]
        for donor, ft, q, la, lo, hrs, st, ngo in rows:
            c.execute(
                "INSERT INTO donations(donor,food_type,qty,lat,lon,created_at,expires_at,status,ngo_id,accepted_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (donor, ft, q, la, lo, t, int(t + hrs * H), st, ngo, t if ngo else None))
    c.commit()
    c.close()


# ---------------------------------------------------------------- helpers
def err(msg, code=400):
    return jsonify(error=msg), code


@app.errorhandler(404)
def _404(_):
    return err("Not found", 404)


@app.errorhandler(405)
def _405(_):
    return err("Method not allowed", 405)


@app.errorhandler(500)
def _500(_):
    return err("Server error", 500)


def haversine(a, b, c, d):
    """Great-circle distance in km (Module 3)."""
    r = math.pi / 180
    dl, dn = (c - a) * r, (d - b) * r
    x = math.sin(dl / 2) ** 2 + math.cos(a * r) * math.cos(c * r) * math.sin(dn / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))


def all_ngos():
    return db().execute("SELECT id,name,lat,lon FROM ngos ORDER BY id").fetchall()


def nearby(lat, lon, ngos=None):
    out = []
    for n in ngos or all_ngos():
        km = haversine(lat, lon, n["lat"], n["lon"])
        if km <= MAX_RADIUS_KM:
            out.append({"id": n["id"], "name": n["name"], "km": round(km, 2)})
    return sorted(out, key=lambda x: x["km"])


def expire_stale():
    """Module 2: anything unclaimed past its safety window leaves the pipeline."""
    db().execute("UPDATE donations SET status='EXPIRED' WHERE status='POSTED' AND expires_at<=?", (now_ms(),))
    db().commit()


def num(v, lo, hi, name, integer=False):
    if isinstance(v, bool) or v is None:
        raise ValueError(f"{name} is required")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(x) or not (lo <= x <= hi):
        raise ValueError(f"{name} must be between {lo} and {hi}")
    if integer:
        if x != int(x):
            raise ValueError(f"{name} must be a whole number")
        return int(x)
    return x


def mask(donor):
    if "@" not in donor:
        return donor
    u, d = donor.split("@", 1)
    return u[:1] + "***@" + d


def serialize(r, user, ngos):
    d = {
        "id": r["id"], "type": r["food_type"], "qty": r["qty"], "lat": r["lat"], "lon": r["lon"],
        "exp": r["expires_at"], "status": r["status"], "ngo": r["ngo_id"],
        "donor": r["donor"] if user else mask(r["donor"]),
        "vol_me": bool(user and r["volunteer"] == user["email"]),
        "near": nearby(r["lat"], r["lon"], ngos) if r["status"] == "POSTED" else [],
    }
    return d


def issue_token(email, role, ngo):
    t = int(time.time())
    return jwt.encode({"sub": email, "role": role, "ngo": ngo, "iat": t, "exp": t + TOKEN_HOURS * 3600},
                      SECRET, algorithm="HS256")


def current_user(required=False, roles=()):
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return ("Authentication required", 401) if required else None
    try:
        p = jwt.decode(h[7:], SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return ("Session expired. Log in again.", 401)
    except jwt.InvalidTokenError:
        return ("Invalid token", 401)
    u = db().execute("SELECT email,role,ngo_id FROM users WHERE email=?", (p["sub"],)).fetchone()
    if not u:
        return ("Account not found", 401)
    user = {"email": u["email"], "role": u["role"], "ngo": u["ngo_id"]}
    if roles and user["role"] not in roles:
        return (f"Only {' / '.join(roles)} accounts can do this", 403)
    return user


def auth(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = current_user(required=True, roles=roles)
            if isinstance(u, tuple):
                return err(*u)
            g.user = u
            return f(*a, **k)
        return w
    return deco


# simple in-memory throttle for password / PIN guessing
_fails, _lock = {}, threading.Lock()


def throttled(key, limit=8, window=900):
    with _lock:
        t = time.time()
        _fails[key] = [x for x in _fails.get(key, []) if t - x < window]
        return len(_fails[key]) >= limit


def record_fail(key):
    with _lock:
        _fails.setdefault(key, []).append(time.time())


def clear_fails(key):
    with _lock:
        _fails.pop(key, None)


# ------------------------------------------------- Module 1: accounts & roles
def auth_response(email, role, ngo, code=200):
    return jsonify(token=issue_token(email, role, ngo),
                   user={"email": email, "role": role, "ngo": ngo}, expires_in=TOKEN_HOURS * 3600), code


@app.post("/api/auth/register")
def register():
    b = request.get_json(silent=True) or {}
    email = str(b.get("email", "")).strip().lower()
    pwd, role, ngo = str(b.get("password", "")), b.get("role"), b.get("ngo")
    if not EMAIL_RE.match(email) or len(email) > 254:
        return err("Enter a valid email address.")
    if len(pwd) < 6 or len(pwd) > 128:
        return err("Password must be 6 to 128 characters.")
    if role not in ROLES:
        return err("Role must be Donor, NGO or Volunteer.")
    if role == "NGO":
        if not db().execute("SELECT 1 FROM ngos WHERE id=?", (ngo,)).fetchone():
            return err("Choose a valid organisation.")
    else:
        ngo = None
    try:
        db().execute("INSERT INTO users(email,pwd_hash,role,ngo_id,created_at) VALUES(?,?,?,?,?)",
                     (email, generate_password_hash(pwd), role, ngo, now_ms()))
        db().commit()
    except sqlite3.IntegrityError:
        return err("That email is already registered. Switch to Log in.", 409)
    return auth_response(email, role, ngo, 201)


@app.post("/api/auth/login")
def login():
    b = request.get_json(silent=True) or {}
    email, pwd = str(b.get("email", "")).strip().lower(), str(b.get("password", ""))
    key = f"login:{request.remote_addr}:{email}"
    if throttled(key):
        return err("Too many attempts. Try again in a few minutes.", 429)
    u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u or not check_password_hash(u["pwd_hash"], pwd):
        record_fail(key)
        return err("Email or password is incorrect.", 401)
    clear_fails(key)
    return auth_response(u["email"], u["role"], u["ngo_id"])


@app.get("/api/me")
@auth()
def me():
    return jsonify(user=g.user)


@app.get("/api/ngos")
def ngos():
    return jsonify(ngos=[dict(n) for n in all_ngos()])


@app.put("/api/ngo/pin")
@auth("NGO")
def set_pin():
    pin = str((request.get_json(silent=True) or {}).get("pin", "")).strip()
    if len(pin) < 6 or len(pin) > 64:
        return err("PIN must be 6 to 64 characters.")
    db().execute("UPDATE ngos SET pin_hash=? WHERE id=?", (generate_password_hash(pin), g.user["ngo"]))
    db().commit()
    return jsonify(ok=True)


# ------------------------------------------- Module 2 + 3: listing & matching
@app.post("/api/donations")
@auth("Donor")
def create_donation():
    b = request.get_json(silent=True) or {}
    try:
        ftype = str(b.get("type", "")).strip()
        if not (1 <= len(ftype) <= 100):
            raise ValueError("Food type is required (max 100 characters).")
        qty = num(b.get("qty"), 1, 100000, "Quantity", integer=True)
        hrs = num(b.get("shelf_life_hours"), 0.1, 72, "Shelf life (hours)")
        lat = num(b.get("lat"), -90, 90, "Latitude")
        lon = num(b.get("lon"), -180, 180, "Longitude")
    except ValueError as e:
        return err(str(e))
    t = now_ms()
    cur = db().execute(
        "INSERT INTO donations(donor,food_type,qty,lat,lon,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
        (g.user["email"], ftype, qty, lat, lon, t, int(t + hrs * 3600e3)))
    db().commit()
    r = db().execute("SELECT * FROM donations WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(donation=serialize(r, g.user, all_ngos())), 201


@app.get("/api/donations")
def list_donations():
    user = current_user()
    if isinstance(user, tuple):
        return err(*user)
    expire_stale()
    c, ng = db(), all_ngos()
    if user is None:
        rows = c.execute("SELECT * FROM donations ORDER BY id DESC LIMIT 200").fetchall()
    elif user["role"] == "Donor":
        rows = c.execute("SELECT * FROM donations WHERE donor=? ORDER BY id DESC LIMIT 200", (user["email"],)).fetchall()
    elif user["role"] == "NGO":
        me_ngo = next(n for n in ng if n["id"] == user["ngo"])
        rows = [r for r in c.execute(
            "SELECT * FROM donations WHERE ngo_id=? OR status='POSTED' ORDER BY id DESC LIMIT 500", (user["ngo"],))
            if r["ngo_id"] == user["ngo"] or haversine(r["lat"], r["lon"], me_ngo["lat"], me_ngo["lon"]) <= MAX_RADIUS_KM]
    else:
        rows = c.execute("SELECT * FROM donations WHERE status='ACCEPTED' OR volunteer=? ORDER BY id DESC LIMIT 200",
                         (user["email"],)).fetchall()
    return jsonify(donations=[serialize(r, user, ng) for r in rows], server_time=now_ms(), max_radius_km=MAX_RADIUS_KM)


@app.get("/api/stats")
def stats():
    expire_stale()
    counts = {s: 0 for s in ("POSTED", "ACCEPTED", "IN_TRANSIT", "DELIVERED", "EXPIRED")}
    for r in db().execute("SELECT status, COUNT(*) n FROM donations GROUP BY status"):
        counts[r["status"]] = r["n"]
    return jsonify(counts=counts)


# --------------------------------------- Module 4: reservation lock (atomic)
def _why_failed(did, default):
    r = db().execute("SELECT status FROM donations WHERE id=?", (did,)).fetchone()
    if not r:
        return err("Donation not found.", 404)
    return err(default(r["status"]), 409)


@app.post("/api/donations/<int:did>/claim")
@auth("NGO")
def claim(did):
    expire_stale()
    t = now_ms()
    # One UPDATE decides the winner: only a still-POSTED, unexpired row can change.
    cur = db().execute(
        "UPDATE donations SET status='ACCEPTED', ngo_id=?, accepted_at=? "
        "WHERE id=? AND status='POSTED' AND expires_at>?", (g.user["ngo"], t, did, t))
    db().commit()
    if cur.rowcount == 0:
        return _why_failed(did, lambda s: "That donation has expired." if s == "EXPIRED"
                           else "Already claimed by another NGO.")
    r = db().execute("SELECT * FROM donations WHERE id=?", (did,)).fetchone()
    return jsonify(donation=serialize(r, g.user, all_ngos()))


# ------------------------------------------------- Module 5: dispatch & proof
@app.post("/api/donations/<int:did>/pickup")
@auth("Volunteer")
def pickup(did):
    cur = db().execute(
        "UPDATE donations SET status='IN_TRANSIT', volunteer=?, picked_at=? WHERE id=? AND status='ACCEPTED'",
        (g.user["email"], now_ms(), did))
    db().commit()
    if cur.rowcount == 0:
        return _why_failed(did, lambda s: "This pickup is no longer available.")
    r = db().execute("SELECT * FROM donations WHERE id=?", (did,)).fetchone()
    return jsonify(donation=serialize(r, g.user, all_ngos()))


@app.post("/api/donations/<int:did>/deliver")
@auth("Volunteer")
def deliver(did):
    pin = str((request.get_json(silent=True) or {}).get("pin", "")).strip()
    r = db().execute("SELECT d.status, d.volunteer, n.pin_hash FROM donations d "
                     "JOIN ngos n ON n.id=d.ngo_id WHERE d.id=?", (did,)).fetchone()
    if not r:
        return err("Donation not found.", 404)
    if r["status"] != "IN_TRANSIT" or r["volunteer"] != g.user["email"]:
        return err("Only your own donations in transit can be closed.", 409)
    key = f"pin:{did}"
    if throttled(key, limit=5, window=300):
        return err("Too many wrong PINs. Try again in a few minutes.", 429)
    if not check_password_hash(r["pin_hash"], pin):
        record_fail(key)
        return err("PIN does not match. Ask the NGO for the correct PIN.", 403)
    clear_fails(key)
    cur = db().execute("UPDATE donations SET status='DELIVERED', delivered_at=? "
                       "WHERE id=? AND status='IN_TRANSIT' AND volunteer=?", (now_ms(), did, g.user["email"]))
    db().commit()
    if cur.rowcount == 0:
        return err("Only your own donations in transit can be closed.", 409)
    row = db().execute("SELECT * FROM donations WHERE id=?", (did,)).fetchone()
    return jsonify(donation=serialize(row, g.user, all_ngos()))


# ---------------------------------------------------------------- web page
@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE, "static"), "index.html")


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    origin = os.environ.get("ANNAM_CORS_ORIGIN")
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
    return resp


init_db(seed=os.environ.get("ANNAM_SEED", "1") != "0")

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)), threaded=True)
