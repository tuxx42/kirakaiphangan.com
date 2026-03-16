#!/usr/bin/env python3
"""Kira Kai booking system — Flask + PostgreSQL (prod) / SQLite (local dev)."""

import os
import sqlite3
import secrets
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, g

app = Flask(__name__, static_folder=".", static_url_path="")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "kiraKAI2026")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith("postgres")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

# Email config — set these env vars or emails won't send (fails silently)
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")  # e.g. https://kirakai.com

log = logging.getLogger(__name__)


# ── Database ────────────────────────────────────────────────────────────────

class SqliteDictRow(sqlite3.Row):
    """sqlite3.Row wrapper that supports .get() like a dict."""
    def get(self, key, default=None):
        try:
            return self[key]
        except (IndexError, KeyError):
            return default


def get_db():
    if "db" not in g:
        if USE_POSTGRES:
            g.db = psycopg2.connect(DATABASE_URL)
            g.db.autocommit = False
        else:
            g.db = sqlite3.connect(os.environ.get("DB_PATH", "bookings.db"))
            g.db.row_factory = SqliteDictRow
            g.db.execute("PRAGMA journal_mode=WAL")
            g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


def _adapt_query(query):
    """Convert %s placeholders to ? for SQLite."""
    if not USE_POSTGRES:
        query = query.replace("%s", "?")
        query = query.replace("CURRENT_DATE::text", "date('now')")
    return query


def db_execute(query, params=None):
    """Execute a query returning dict-like rows."""
    db = get_db()
    query = _adapt_query(query)
    if USE_POSTGRES:
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, params)
        return cur
    else:
        return db.execute(query, params or ())


def db_fetchone(query, params=None):
    cur = db_execute(query, params)
    return cur.fetchone()


def db_fetchall(query, params=None):
    cur = db_execute(query, params)
    return cur.fetchall()


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                date TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'Sunday Roast',
                description TEXT,
                menu_description TEXT,
                price_thb INTEGER NOT NULL DEFAULT 0,
                max_covers INTEGER NOT NULL DEFAULT 30,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                event_id INTEGER NOT NULL REFERENCES events(id),
                ref_code TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                guests INTEGER NOT NULL DEFAULT 1,
                dietary_notes TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.close()
    else:
        db = sqlite3.connect(os.environ.get("DB_PATH", "bookings.db"))
        db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'Sunday Roast',
                description TEXT,
                menu_description TEXT,
                price_thb INTEGER NOT NULL DEFAULT 0,
                max_covers INTEGER NOT NULL DEFAULT 30,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES events(id),
                ref_code TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                guests INTEGER NOT NULL DEFAULT 1,
                dietary_notes TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        db.close()


# ── Email ───────────────────────────────────────────────────────────────────

def send_email(to_email, subject, html_body):
    """Send an email. Fails silently if SMTP not configured."""
    if not SMTP_EMAIL or not SMTP_PASSWORD or not to_email:
        log.info("Email skipped: SMTP not configured or no recipient")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"Kira Kai <{SMTP_EMAIL}>"
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, to_email, msg.as_string())
        log.info("Email sent to %s: %s", to_email, subject)
        return True
    except Exception as e:
        log.error("Failed to send email to %s: %s", to_email, e)
        return False


def send_booking_received_email(booking, event):
    """Email sent immediately when a customer submits a booking."""
    from datetime import datetime
    d = datetime.strptime(event["date"], "%Y-%m-%d")
    date_str = d.strftime("%A %d %B %Y")
    total = event["price_thb"] * booking["guests"]

    payment_section = ""
    if total > 0:
        payment_section = f"""
        <div style="background:#3d322c;border:1px solid rgba(198,155,109,0.2);border-radius:4px;padding:24px;margin-bottom:24px;">
            <h3 style="font-family:Georgia,serif;color:#c69b6d;font-size:15px;margin:0 0 12px;">Payment Details</h3>
            <p style="color:#f7f3ee;font-size:16px;font-weight:600;margin:0 0 16px;">{total:,} THB</p>
            <p style="color:rgba(247,243,238,0.8);font-size:13px;line-height:1.8;margin:0;">
                Please include your reference code <strong>{booking["ref_code"]}</strong> with your payment.
            </p>

            <div style="margin-top:20px;padding-top:16px;border-top:1px solid rgba(198,155,109,0.15);">
                <p style="color:#c69b6d;font-size:13px;font-weight:600;margin:0 0 4px;">Revolut</p>
                <p style="color:rgba(247,243,238,0.7);font-size:13px;margin:0;">Send to: <strong>joelthomas83</strong> &middot; Ref: <strong>{booking["ref_code"]}</strong></p>
            </div>

            <div style="margin-top:16px;padding-top:16px;border-top:1px solid rgba(198,155,109,0.15);">
                <p style="color:#c69b6d;font-size:13px;font-weight:600;margin:0 0 4px;">Wise</p>
                <p style="color:rgba(247,243,238,0.7);font-size:13px;margin:0;">Send to: <strong>joelt134</strong> &middot; Ref: <strong>{booking["ref_code"]}</strong></p>
            </div>

            <div style="margin-top:16px;padding-top:16px;border-top:1px solid rgba(198,155,109,0.15);">
                <p style="color:#c69b6d;font-size:13px;font-weight:600;margin:0 0 4px;">Thai Bank Transfer (PromptPay)</p>
                <p style="color:rgba(247,243,238,0.7);font-size:13px;margin:0;">Coming soon</p>
            </div>
        </div>
        """

    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#2a2320;color:#f7f3ee;padding:40px;">
        <h1 style="font-family:Georgia,serif;color:#c69b6d;font-size:24px;margin-bottom:4px;">Booking Received</h1>
        <p style="color:#9a8b82;font-size:14px;margin-bottom:24px;">Kira Kai &middot; Koh Phangan</p>

        <div style="background:#3d322c;border:1px solid rgba(198,155,109,0.2);border-radius:4px;padding:24px;margin-bottom:24px;">
            <h2 style="font-family:Georgia,serif;color:#f7f3ee;font-size:18px;margin:0 0 16px;">{event["title"]}</h2>
            <p style="color:#c69b6d;font-size:14px;margin:0 0 4px;">{date_str} &middot; 12:00 – 18:00</p>
            <p style="color:#f7f3ee;font-size:14px;margin:0;">
                <strong>{booking["name"]}</strong> &middot; {booking["guests"]} guest{"s" if booking["guests"] > 1 else ""}
            </p>
            <p style="color:#9a8b82;font-size:13px;margin:8px 0 0;">Ref: {booking["ref_code"]}</p>
        </div>

        {payment_section}

        {f'<div style="background:#3d322c;border:1px solid rgba(198,155,109,0.2);border-radius:4px;padding:24px;margin-bottom:24px;"><h3 style="font-family:Georgia,serif;color:#c69b6d;font-size:15px;margin:0 0 12px;">On the Menu</h3><p style="color:rgba(247,243,238,0.8);font-size:13px;line-height:1.8;white-space:pre-line;margin:0;">{event["menu_description"]}</p></div>' if event["menu_description"] else ""}

        <p style="color:rgba(247,243,238,0.7);font-size:13px;line-height:1.6;">
            {"Your booking is pending until payment is received. We'll confirm once we've matched your payment." if total > 0 else "You're all set! No payment required for this event."}
            If you have any questions, just reply to this email or message us directly.
        </p>

        <p style="color:#9a8b82;font-size:12px;margin-top:32px;border-top:1px solid rgba(198,155,109,0.1);padding-top:16px;">
            Kira Kai &middot; 17 Moo 1, Ban Tai, Koh Phangan
        </p>
    </div>
    """
    send_email(booking["email"], f"Booking Received — {event['title']} {date_str}", html)


def send_booking_confirmed_email(booking, event):
    from datetime import datetime
    d = datetime.strptime(event["date"], "%Y-%m-%d")
    date_str = d.strftime("%A %d %B %Y")
    total = event["price_thb"] * booking["guests"]

    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#2a2320;color:#f7f3ee;padding:40px;">
        <h1 style="font-family:Georgia,serif;color:#c69b6d;font-size:24px;margin-bottom:4px;">Booking Confirmed</h1>
        <p style="color:#9a8b82;font-size:14px;margin-bottom:24px;">Kira Kai &middot; Koh Phangan</p>

        <div style="background:#3d322c;border:1px solid rgba(198,155,109,0.2);border-radius:4px;padding:24px;margin-bottom:24px;">
            <h2 style="font-family:Georgia,serif;color:#f7f3ee;font-size:18px;margin:0 0 16px;">{event["title"]}</h2>
            <p style="color:#c69b6d;font-size:14px;margin:0 0 4px;">{date_str} &middot; 12:00 – 18:00</p>
            <p style="color:#f7f3ee;font-size:14px;margin:0;">
                <strong>{booking["name"]}</strong> &middot; {booking["guests"]} guest{"s" if booking["guests"] > 1 else ""}
            </p>
            <p style="color:#9a8b82;font-size:13px;margin:8px 0 0;">Ref: {booking["ref_code"]}</p>
        </div>

        {f'<div style="background:#3d322c;border:1px solid rgba(198,155,109,0.2);border-radius:4px;padding:24px;margin-bottom:24px;"><h3 style="font-family:Georgia,serif;color:#c69b6d;font-size:15px;margin:0 0 12px;">On the Menu</h3><p style="color:rgba(247,243,238,0.8);font-size:13px;line-height:1.8;white-space:pre-line;margin:0;">{event["menu_description"]}</p></div>' if event["menu_description"] else ""}

        <p style="color:rgba(247,243,238,0.7);font-size:13px;line-height:1.6;">
            We look forward to seeing you! If you need to make changes, reply to this email or message us directly.
        </p>

        <p style="color:#9a8b82;font-size:12px;margin-top:32px;border-top:1px solid rgba(198,155,109,0.1);padding-top:16px;">
            Kira Kai &middot; 17 Moo 1, Ban Tai, Koh Phangan
        </p>
    </div>
    """
    send_email(booking["email"], f"Booking Confirmed — {event['title']} {date_str}", html)


def send_booking_cancelled_email(booking, event):
    from datetime import datetime
    d = datetime.strptime(event["date"], "%Y-%m-%d")
    date_str = d.strftime("%A %d %B %Y")

    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#2a2320;color:#f7f3ee;padding:40px;">
        <h1 style="font-family:Georgia,serif;color:#c0392b;font-size:24px;margin-bottom:4px;">Booking Cancelled</h1>
        <p style="color:#9a8b82;font-size:14px;margin-bottom:24px;">Kira Kai &middot; Koh Phangan</p>

        <div style="background:#3d322c;border:1px solid rgba(198,155,109,0.2);border-radius:4px;padding:24px;margin-bottom:24px;">
            <h2 style="font-family:Georgia,serif;color:#f7f3ee;font-size:18px;margin:0 0 16px;">{event["title"]}</h2>
            <p style="color:#9a8b82;font-size:14px;margin:0 0 4px;">{date_str}</p>
            <p style="color:#f7f3ee;font-size:14px;margin:0;">
                <strong>{booking["name"]}</strong> &middot; {booking["guests"]} guest{"s" if booking["guests"] > 1 else ""}
            </p>
            <p style="color:#9a8b82;font-size:13px;margin:8px 0 0;">Ref: {booking["ref_code"]}</p>
        </div>

        <p style="color:rgba(247,243,238,0.7);font-size:13px;line-height:1.6;">
            Your booking has been cancelled. If you believe this is a mistake or would like to rebook,
            please reply to this email or visit us at the bar.
        </p>

        <p style="color:#9a8b82;font-size:12px;margin-top:32px;border-top:1px solid rgba(198,155,109,0.1);padding-top:16px;">
            Kira Kai &middot; 17 Moo 1, Ban Tai, Koh Phangan
        </p>
    </div>
    """
    send_email(booking["email"], f"Booking Cancelled — {event['title']} {date_str}", html)


# ── Auth ────────────────────────────────────────────────────────────────────

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {ADMIN_PASSWORD}":
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Static pages ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/booking")
def booking_page():
    return send_from_directory(".", "booking.html")


@app.route("/admin")
def admin_page():
    return send_from_directory(".", "admin.html")


# ── Public API ──────────────────────────────────────────────────────────────

@app.route("/api/events", methods=["GET"])
def list_events():
    """Return open events (public)."""
    rows = db_fetchall(
        "SELECT * FROM events WHERE status = 'open' AND date >= CURRENT_DATE::text ORDER BY date"
    )
    events = []
    for r in rows:
        booked = db_fetchone(
            "SELECT COALESCE(SUM(guests), 0) as total FROM bookings WHERE event_id = %s AND status != 'cancelled'",
            (r["id"],)
        )["total"]
        events.append({
            "id": r["id"], "date": r["date"], "title": r["title"],
            "description": r["description"], "menu_description": r["menu_description"],
            "price_thb": r["price_thb"], "max_covers": r["max_covers"],
            "booked_covers": booked, "spots_left": r["max_covers"] - booked,
            "status": r["status"],
        })
    return jsonify(events)


@app.route("/api/events/<int:event_id>", methods=["GET"])
def get_event(event_id):
    r = db_fetchone("SELECT * FROM events WHERE id = %s", (event_id,))
    if not r:
        return jsonify({"error": "event not found"}), 404
    booked = db_fetchone(
        "SELECT COALESCE(SUM(guests), 0) as total FROM bookings WHERE event_id = %s AND status != 'cancelled'",
        (r["id"],)
    )["total"]
    return jsonify({
        "id": r["id"], "date": r["date"], "title": r["title"],
        "description": r["description"], "menu_description": r["menu_description"],
        "price_thb": r["price_thb"], "max_covers": r["max_covers"],
        "booked_covers": booked, "spots_left": r["max_covers"] - booked,
        "status": r["status"],
    })


@app.route("/api/bookings", methods=["POST"])
def create_booking():
    """Customer creates a booking request."""
    data = request.json
    if not data:
        return jsonify({"error": "no data"}), 400

    required = ["event_id", "name", "guests"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"missing {field}"}), 400

    guests = int(data["guests"])
    if guests < 1 or guests > 20:
        return jsonify({"error": "guests must be 1-20"}), 400

    db = get_db()
    event = db_fetchone("SELECT * FROM events WHERE id = %s AND status = 'open'", (data["event_id"],))
    if not event:
        return jsonify({"error": "event not available"}), 404

    booked = db_fetchone(
        "SELECT COALESCE(SUM(guests), 0) as total FROM bookings WHERE event_id = %s AND status != 'cancelled'",
        (event["id"],)
    )["total"]

    if booked + guests > event["max_covers"]:
        return jsonify({"error": "not enough spots available"}), 400

    ref_code = "KK-" + secrets.token_hex(3).upper()

    db_execute(
        "INSERT INTO bookings (event_id, ref_code, name, email, phone, guests, dietary_notes) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (data["event_id"], ref_code, data["name"], data.get("email", ""),
         data.get("phone", ""), guests, data.get("dietary_notes", ""))
    )
    db.commit()

    total_price = event["price_thb"] * guests

    # Send booking received email to customer
    if data.get("email"):
        booking_data = {"name": data["name"], "email": data["email"],
                        "guests": guests, "ref_code": ref_code}
        send_booking_received_email(booking_data, event)

    return jsonify({
        "ref_code": ref_code,
        "total_price": total_price,
        "message": "Booking request received! Please complete payment to confirm.",
    }), 201


# ── Admin API ───────────────────────────────────────────────────────────────

@app.route("/api/admin/events", methods=["GET"])
@require_admin
def admin_list_events():
    rows = db_fetchall("SELECT * FROM events ORDER BY date DESC")
    events = []
    for r in rows:
        booked = db_fetchone(
            "SELECT COALESCE(SUM(guests), 0) as total FROM bookings WHERE event_id = %s AND status != 'cancelled'",
            (r["id"],)
        )["total"]
        events.append({
            "id": r["id"], "date": r["date"], "title": r["title"],
            "description": r["description"], "menu_description": r["menu_description"],
            "price_thb": r["price_thb"], "max_covers": r["max_covers"],
            "booked_covers": booked, "status": r["status"],
            "created_at": str(r["created_at"]) if r["created_at"] else None,
        })
    return jsonify(events)


@app.route("/api/admin/events", methods=["POST"])
@require_admin
def admin_create_event():
    data = request.json
    if not data or not data.get("date"):
        return jsonify({"error": "date is required"}), 400
    db = get_db()
    db_execute(
        "INSERT INTO events (date, title, description, menu_description, price_thb, max_covers) VALUES (%s, %s, %s, %s, %s, %s)",
        (data["date"], data.get("title", "Sunday Roast"), data.get("description", ""),
         data.get("menu_description", ""), int(data.get("price_thb", 0)), int(data.get("max_covers", 30)))
    )
    db.commit()
    return jsonify({"message": "event created"}), 201


@app.route("/api/admin/events/<int:event_id>", methods=["PUT"])
@require_admin
def admin_update_event(event_id):
    data = request.json
    db = get_db()
    event = db_fetchone("SELECT * FROM events WHERE id = %s", (event_id,))
    if not event:
        return jsonify({"error": "not found"}), 404
    db_execute(
        "UPDATE events SET date=%s, title=%s, description=%s, menu_description=%s, price_thb=%s, max_covers=%s, status=%s WHERE id=%s",
        (data.get("date", event["date"]), data.get("title", event["title"]),
         data.get("description", event["description"]),
         data.get("menu_description", event["menu_description"]),
         int(data.get("price_thb", event["price_thb"])),
         int(data.get("max_covers", event["max_covers"])),
         data.get("status", event["status"]), event_id)
    )
    db.commit()
    return jsonify({"message": "updated"})


@app.route("/api/admin/events/<int:event_id>", methods=["DELETE"])
@require_admin
def admin_delete_event(event_id):
    db = get_db()
    db_execute("DELETE FROM bookings WHERE event_id = %s", (event_id,))
    db_execute("DELETE FROM events WHERE id = %s", (event_id,))
    db.commit()
    return jsonify({"message": "deleted"})


@app.route("/api/admin/bookings/<int:event_id>", methods=["GET"])
@require_admin
def admin_list_bookings(event_id):
    rows = db_fetchall(
        "SELECT * FROM bookings WHERE event_id = %s ORDER BY created_at", (event_id,)
    )
    result = []
    for r in rows:
        row = dict(r)
        if row.get("created_at"):
            row["created_at"] = str(row["created_at"])
        result.append(row)
    return jsonify(result)


@app.route("/api/admin/bookings/<int:booking_id>/status", methods=["PUT"])
@require_admin
def admin_update_booking_status(booking_id):
    data = request.json
    new_status = data.get("status")
    if new_status not in ("pending", "confirmed", "cancelled"):
        return jsonify({"error": "invalid status"}), 400
    db = get_db()
    booking = db_fetchone("SELECT * FROM bookings WHERE id = %s", (booking_id,))
    if not booking:
        return jsonify({"error": "booking not found"}), 404
    old_status = booking["status"]
    db_execute("UPDATE bookings SET status = %s WHERE id = %s", (new_status, booking_id))
    db.commit()

    # Send email on status change
    if new_status != old_status and booking["email"]:
        booking = db_fetchone("SELECT * FROM bookings WHERE id = %s", (booking_id,))
        event = db_fetchone("SELECT * FROM events WHERE id = %s", (booking["event_id"],))
        if event:
            if new_status == "confirmed":
                send_booking_confirmed_email(booking, event)
            elif new_status == "cancelled":
                send_booking_cancelled_email(booking, event)

    return jsonify({"message": "updated"})


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
