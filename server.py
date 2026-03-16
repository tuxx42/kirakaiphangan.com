#!/usr/bin/env python3
"""Kira Kai booking system — Flask + PostgreSQL (prod) / SQLite (local dev)."""

import os
import sqlite3
import secrets
import smtplib
import logging
import calendar
from datetime import datetime, timedelta, date
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


# ── Seed data ───────────────────────────────────────────────────────────────

SEED_EVENT_TYPES = [
    {
        "slug": "breakfast-club",
        "title": "The Breakfast Club",
        "description": "Our signature Saturday brunch. Three small plates, optional bottomless drinks (90 min), and the best Bloody Marys on the island.",
        "time_display": "11:00 – 16:00",
        "price_thb": 0,
        "max_covers": 30,
        "is_bookable": True,
        "recurrence_rule": "weekly:6",
        "status": "coming_soon",
        "sort_order": 1,
    },
    {
        "slug": "sunday-roast",
        "title": "Sunday Roast",
        "description": "A proper roast by the sea. All the trimmings, wine by the glass, and a relaxed afternoon vibe.",
        "time_display": "12:00 – 18:00",
        "price_thb": 650,
        "max_covers": 30,
        "is_bookable": True,
        "recurrence_rule": "weekly:0",
        "status": "active",
        "sort_order": 2,
    },
    {
        "slug": "aperitivo-nights",
        "title": "Aperitivo Nights",
        "description": "Buy a drink, enjoy complimentary canapes and small bites. Negronis, spritzes, and the golden hour.",
        "time_display": "17:00 – 19:00",
        "price_thb": 0,
        "max_covers": 30,
        "is_bookable": False,
        "recurrence_rule": "weekly:5",
        "status": "coming_soon",
        "sort_order": 3,
    },
    {
        "slug": "bbq-night",
        "title": "BBQ Night",
        "description": "Fresh local seafood and meats on the outdoor grill. Cooked to order, eaten by the beach.",
        "time_display": "From 18:00",
        "price_thb": 0,
        "max_covers": 30,
        "is_bookable": False,
        "recurrence_rule": "monthly:first:6",
        "status": "coming_soon",
        "sort_order": 4,
    },
    {
        "slug": "coffee-rave",
        "title": "Coffee Rave",
        "description": "Morning DJs, specialty coffee, brunch cocktails. A different kind of wake-up call.",
        "time_display": "Morning sessions",
        "price_thb": 0,
        "max_covers": 30,
        "is_bookable": False,
        "recurrence_rule": None,
        "status": "coming_soon",
        "sort_order": 5,
    },
    {
        "slug": "happiness-hours",
        "title": "Happiness Hours",
        "description": "Twice daily — midday refreshments and sunset specials. The best times to try our signatures.",
        "time_display": "12:00–14:00 & 16:00–19:00",
        "price_thb": 0,
        "max_covers": 30,
        "is_bookable": False,
        "recurrence_rule": "daily",
        "status": "coming_soon",
        "sort_order": 6,
    },
]


def init_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cur = conn.cursor()

        # Create event_types table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS event_types (
                id SERIAL PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                time_display TEXT,
                price_thb INTEGER DEFAULT 0,
                max_covers INTEGER DEFAULT 30,
                is_bookable BOOLEAN DEFAULT FALSE,
                recurrence_rule TEXT,
                status TEXT DEFAULT 'coming_soon',
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        # Create event_instances table (replaces old events)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS event_instances (
                id SERIAL PRIMARY KEY,
                event_type_id INTEGER NOT NULL REFERENCES event_types(id),
                date TEXT NOT NULL,
                title TEXT,
                description TEXT,
                menu_description TEXT,
                price_thb INTEGER,
                max_covers INTEGER,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(event_type_id, date)
            );
        """)

        # Create bookings table referencing event_instances
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                event_id INTEGER NOT NULL REFERENCES event_instances(id),
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

        # Migrate: if old events table exists, move data to event_instances
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables WHERE table_name = 'events'
            );
        """)
        old_events_exist = cur.fetchone()[0]

        if old_events_exist:
            # Make sure sunday-roast type exists first (seed below handles it)
            _seed_event_types_pg(cur)

            # Get sunday-roast type id
            cur.execute("SELECT id FROM event_types WHERE slug = 'sunday-roast'")
            row = cur.fetchone()
            if row:
                sr_id = row[0]
                # Move old events to event_instances
                cur.execute("""
                    INSERT INTO event_instances (event_type_id, date, title, description, menu_description, price_thb, max_covers, status, created_at)
                    SELECT %s, date, title, description, menu_description, price_thb, max_covers, status, created_at
                    FROM events
                    ON CONFLICT (event_type_id, date) DO NOTHING
                """, (sr_id,))

                # Update bookings FK: map old event_id -> new event_instances.id
                cur.execute("""
                    UPDATE bookings SET event_id = ei.id
                    FROM event_instances ei, events e
                    WHERE bookings.event_id = e.id
                    AND ei.event_type_id = %s AND ei.date = e.date
                    AND EXISTS (SELECT 1 FROM events WHERE id = bookings.event_id)
                """, (sr_id,))

            # Rename old table out of the way
            cur.execute("ALTER TABLE events RENAME TO events_old_backup")
            log.info("Migrated old events table to event_instances")
        else:
            _seed_event_types_pg(cur)

        conn.close()
    else:
        db = sqlite3.connect(os.environ.get("DB_PATH", "bookings.db"))

        # Create event_types table
        db.execute("""
            CREATE TABLE IF NOT EXISTS event_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                time_display TEXT,
                price_thb INTEGER DEFAULT 0,
                max_covers INTEGER DEFAULT 30,
                is_bookable INTEGER DEFAULT 0,
                recurrence_rule TEXT,
                status TEXT DEFAULT 'coming_soon',
                sort_order INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Create event_instances table
        db.execute("""
            CREATE TABLE IF NOT EXISTS event_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type_id INTEGER NOT NULL REFERENCES event_types(id),
                date TEXT NOT NULL,
                title TEXT,
                description TEXT,
                menu_description TEXT,
                price_thb INTEGER,
                max_covers INTEGER,
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(event_type_id, date)
            )
        """)

        # Create bookings table
        db.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES event_instances(id),
                ref_code TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                guests INTEGER NOT NULL DEFAULT 1,
                dietary_notes TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Migrate old events table if it exists
        try:
            db.execute("SELECT 1 FROM events LIMIT 1")
            old_events_exist = True
        except sqlite3.OperationalError:
            old_events_exist = False

        # Seed event types
        for et in SEED_EVENT_TYPES:
            db.execute(
                """INSERT OR IGNORE INTO event_types
                   (slug, title, description, time_display, price_thb, max_covers, is_bookable, recurrence_rule, status, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (et["slug"], et["title"], et["description"], et["time_display"],
                 et["price_thb"], et["max_covers"], 1 if et["is_bookable"] else 0,
                 et["recurrence_rule"], et["status"], et["sort_order"])
            )

        if old_events_exist:
            sr_row = db.execute("SELECT id FROM event_types WHERE slug = 'sunday-roast'").fetchone()
            if sr_row:
                sr_id = sr_row[0]
                db.execute("""
                    INSERT OR IGNORE INTO event_instances (event_type_id, date, title, description, menu_description, price_thb, max_covers, status, created_at)
                    SELECT ?, date, title, description, menu_description, price_thb, max_covers, status, created_at
                    FROM events
                """, (sr_id,))

                # Update bookings FK
                for old_ev in db.execute("SELECT id, date FROM events").fetchall():
                    new_ei = db.execute(
                        "SELECT id FROM event_instances WHERE event_type_id = ? AND date = ?",
                        (sr_id, old_ev[0] if isinstance(old_ev, tuple) else old_ev["date"])
                    ).fetchone()
                    if new_ei:
                        db.execute(
                            "UPDATE bookings SET event_id = ? WHERE event_id = ?",
                            (new_ei[0] if isinstance(new_ei, tuple) else new_ei["id"],
                             old_ev[0] if isinstance(old_ev, tuple) else old_ev["id"])
                        )

            db.execute("ALTER TABLE events RENAME TO events_old_backup")
            log.info("Migrated old events table to event_instances")

        db.commit()
        db.close()


def _seed_event_types_pg(cur):
    """Insert seed event types into Postgres (skip existing slugs)."""
    for et in SEED_EVENT_TYPES:
        cur.execute(
            """INSERT INTO event_types
               (slug, title, description, time_display, price_thb, max_covers, is_bookable, recurrence_rule, status, sort_order)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (slug) DO NOTHING""",
            (et["slug"], et["title"], et["description"], et["time_display"],
             et["price_thb"], et["max_covers"], et["is_bookable"],
             et["recurrence_rule"], et["status"], et["sort_order"])
        )


# ── Recurrence generation ──────────────────────────────────────────────────

def _parse_recurrence_dates(rule, start_date, weeks=8):
    """Given a recurrence rule, return a list of dates from start_date for N weeks."""
    end_date = start_date + timedelta(weeks=weeks)
    dates = []

    if rule == "daily":
        d = start_date
        while d <= end_date:
            dates.append(d)
            d += timedelta(days=1)

    elif rule and rule.startswith("weekly:"):
        dow = int(rule.split(":")[1])  # 0=Sun, 6=Sat
        # Convert to Python weekday (0=Mon, 6=Sun)
        py_dow = (dow - 1) % 7  # Sun(0)->6, Mon(1)->0, Sat(6)->5
        d = start_date
        while d <= end_date:
            if d.weekday() == py_dow:
                dates.append(d)
            d += timedelta(days=1)

    elif rule and rule.startswith("monthly:"):
        parts = rule.split(":")
        # e.g. monthly:first:6 or monthly:last:5
        which = parts[1]  # first, last, second, third, fourth
        dow = int(parts[2])  # 0=Sun, 6=Sat
        py_dow = (dow - 1) % 7

        month = start_date.month
        year = start_date.year
        for _ in range(3):  # check 3 months ahead
            cal = calendar.monthcalendar(year, month)
            if which == "first":
                for week in cal:
                    if week[py_dow] != 0:
                        d = date(year, month, week[py_dow])
                        if start_date <= d <= end_date:
                            dates.append(d)
                        break
            elif which == "last":
                for week in reversed(cal):
                    if week[py_dow] != 0:
                        d = date(year, month, week[py_dow])
                        if start_date <= d <= end_date:
                            dates.append(d)
                        break
            month += 1
            if month > 12:
                month = 1
                year += 1

    return dates


def generate_instances(weeks=8):
    """Generate event instances for the next N weeks for all active+bookable types."""
    today = date.today()
    db = get_db()

    # Get active, bookable event types
    types = db_fetchall(
        "SELECT * FROM event_types WHERE status = 'active' AND is_bookable = %s",
        (True if USE_POSTGRES else 1,)
    )

    created = 0
    for et in types:
        rule = et["recurrence_rule"]
        if not rule:
            continue

        dates = _parse_recurrence_dates(rule, today, weeks)
        for d in dates:
            date_str = d.isoformat()
            # Insert only if not exists (UNIQUE constraint)
            try:
                db_execute(
                    "INSERT INTO event_instances (event_type_id, date) VALUES (%s, %s)",
                    (et["id"], date_str)
                )
                created += 1
            except Exception:
                # Already exists — skip
                if USE_POSTGRES:
                    db.rollback()
                continue

    db.commit()
    return created


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
    d = datetime.strptime(event["date"], "%Y-%m-%d")
    date_str = d.strftime("%A %d %B %Y")
    time_display = event.get("time_display") or "12:00 – 18:00"
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
            <p style="color:#c69b6d;font-size:14px;margin:0 0 4px;">{date_str} &middot; {time_display}</p>
            <p style="color:#f7f3ee;font-size:14px;margin:0;">
                <strong>{booking["name"]}</strong> &middot; {booking["guests"]} guest{"s" if booking["guests"] > 1 else ""}
            </p>
            <p style="color:#9a8b82;font-size:13px;margin:8px 0 0;">Ref: {booking["ref_code"]}</p>
        </div>

        {payment_section}

        {f'<div style="background:#3d322c;border:1px solid rgba(198,155,109,0.2);border-radius:4px;padding:24px;margin-bottom:24px;"><h3 style="font-family:Georgia,serif;color:#c69b6d;font-size:15px;margin:0 0 12px;">On the Menu</h3><p style="color:rgba(247,243,238,0.8);font-size:13px;line-height:1.8;white-space:pre-line;margin:0;">{event["menu_description"]}</p></div>' if event.get("menu_description") else ""}

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
    d = datetime.strptime(event["date"], "%Y-%m-%d")
    date_str = d.strftime("%A %d %B %Y")
    time_display = event.get("time_display") or "12:00 – 18:00"
    total = event["price_thb"] * booking["guests"]

    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;background:#2a2320;color:#f7f3ee;padding:40px;">
        <h1 style="font-family:Georgia,serif;color:#c69b6d;font-size:24px;margin-bottom:4px;">Booking Confirmed</h1>
        <p style="color:#9a8b82;font-size:14px;margin-bottom:24px;">Kira Kai &middot; Koh Phangan</p>

        <div style="background:#3d322c;border:1px solid rgba(198,155,109,0.2);border-radius:4px;padding:24px;margin-bottom:24px;">
            <h2 style="font-family:Georgia,serif;color:#f7f3ee;font-size:18px;margin:0 0 16px;">{event["title"]}</h2>
            <p style="color:#c69b6d;font-size:14px;margin:0 0 4px;">{date_str} &middot; {time_display}</p>
            <p style="color:#f7f3ee;font-size:14px;margin:0;">
                <strong>{booking["name"]}</strong> &middot; {booking["guests"]} guest{"s" if booking["guests"] > 1 else ""}
            </p>
            <p style="color:#9a8b82;font-size:13px;margin:8px 0 0;">Ref: {booking["ref_code"]}</p>
        </div>

        {f'<div style="background:#3d322c;border:1px solid rgba(198,155,109,0.2);border-radius:4px;padding:24px;margin-bottom:24px;"><h3 style="font-family:Georgia,serif;color:#c69b6d;font-size:15px;margin:0 0 12px;">On the Menu</h3><p style="color:rgba(247,243,238,0.8);font-size:13px;line-height:1.8;white-space:pre-line;margin:0;">{event["menu_description"]}</p></div>' if event.get("menu_description") else ""}

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


# ── Helper: build event response from instance + type ──────────────────────

def _instance_response(inst, et=None):
    """Build a public-facing event dict from an instance row, inheriting from type."""
    title = inst.get("title") or (et["title"] if et else "Event")
    description = inst.get("description") or (et["description"] if et else "")
    price = inst["price_thb"] if inst["price_thb"] is not None else (et["price_thb"] if et else 0)
    max_covers = inst["max_covers"] if inst["max_covers"] is not None else (et["max_covers"] if et else 30)
    time_display = et["time_display"] if et else "12:00 – 18:00"

    booked = db_fetchone(
        "SELECT COALESCE(SUM(guests), 0) as total FROM bookings WHERE event_id = %s AND status != 'cancelled'",
        (inst["id"],)
    )["total"]

    return {
        "id": inst["id"],
        "date": inst["date"],
        "title": title,
        "description": description,
        "menu_description": inst.get("menu_description") or "",
        "price_thb": price,
        "max_covers": max_covers,
        "booked_covers": booked,
        "spots_left": max_covers - booked,
        "status": inst["status"],
        "time_display": time_display,
        "event_type_id": inst["event_type_id"],
    }


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

@app.route("/api/event-types", methods=["GET"])
def list_event_types():
    """Homepage: return all active/coming_soon event types with next bookable instance."""
    rows = db_fetchall(
        "SELECT * FROM event_types WHERE status IN ('active', 'coming_soon') ORDER BY sort_order, title"
    )
    result = []
    for et in rows:
        item = {
            "id": et["id"],
            "slug": et["slug"],
            "title": et["title"],
            "description": et["description"],
            "time_display": et["time_display"],
            "price_thb": et["price_thb"],
            "max_covers": et["max_covers"],
            "is_bookable": bool(et["is_bookable"]),
            "recurrence_rule": et["recurrence_rule"],
            "status": et["status"],
            "sort_order": et["sort_order"],
            "next_instance": None,
        }
        # Get next open instance for bookable types
        if et["is_bookable"] if USE_POSTGRES else bool(et["is_bookable"]):
            ni = db_fetchone(
                "SELECT id, date FROM event_instances WHERE event_type_id = %s AND status = 'open' AND date >= CURRENT_DATE::text ORDER BY date LIMIT 1",
                (et["id"],)
            )
            if ni:
                item["next_instance"] = {"id": ni["id"], "date": ni["date"]}
        result.append(item)
    return jsonify(result)


@app.route("/api/events", methods=["GET"])
def list_events():
    """Booking page: return open instances with type defaults applied."""
    rows = db_fetchall("""
        SELECT ei.*, et.title as type_title, et.description as type_description,
               et.price_thb as type_price, et.max_covers as type_max_covers,
               et.time_display as time_display
        FROM event_instances ei
        JOIN event_types et ON ei.event_type_id = et.id
        WHERE ei.status = 'open' AND ei.date >= CURRENT_DATE::text
        ORDER BY ei.date
    """)
    events = []
    for r in rows:
        title = r.get("title") or r["type_title"]
        description = r.get("description") or r["type_description"]
        price = r["price_thb"] if r["price_thb"] is not None else r["type_price"]
        max_covers = r["max_covers"] if r["max_covers"] is not None else r["type_max_covers"]
        time_display = r.get("time_display") or "12:00 – 18:00"

        booked = db_fetchone(
            "SELECT COALESCE(SUM(guests), 0) as total FROM bookings WHERE event_id = %s AND status != 'cancelled'",
            (r["id"],)
        )["total"]

        events.append({
            "id": r["id"], "date": r["date"], "title": title,
            "description": description, "menu_description": r.get("menu_description") or "",
            "price_thb": price, "max_covers": max_covers,
            "booked_covers": booked, "spots_left": max_covers - booked,
            "status": r["status"], "time_display": time_display,
            "event_type_id": r["event_type_id"],
        })
    return jsonify(events)


@app.route("/api/events/<int:event_id>", methods=["GET"])
def get_event(event_id):
    r = db_fetchone("SELECT * FROM event_instances WHERE id = %s", (event_id,))
    if not r:
        return jsonify({"error": "event not found"}), 404
    et = db_fetchone("SELECT * FROM event_types WHERE id = %s", (r["event_type_id"],))
    return jsonify(_instance_response(r, et))


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
    inst = db_fetchone("SELECT * FROM event_instances WHERE id = %s AND status = 'open'", (data["event_id"],))
    if not inst:
        return jsonify({"error": "event not available"}), 404

    et = db_fetchone("SELECT * FROM event_types WHERE id = %s", (inst["event_type_id"],))
    event_data = _instance_response(inst, et)

    booked = event_data["booked_covers"]
    max_covers = event_data["max_covers"]

    if booked + guests > max_covers:
        return jsonify({"error": "not enough spots available"}), 400

    ref_code = "KK-" + secrets.token_hex(3).upper()

    db_execute(
        "INSERT INTO bookings (event_id, ref_code, name, email, phone, guests, dietary_notes) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (data["event_id"], ref_code, data["name"], data.get("email", ""),
         data.get("phone", ""), guests, data.get("dietary_notes", ""))
    )
    db.commit()

    total_price = event_data["price_thb"] * guests

    # Send booking received email to customer
    if data.get("email"):
        booking_data = {"name": data["name"], "email": data["email"],
                        "guests": guests, "ref_code": ref_code}
        send_booking_received_email(booking_data, event_data)

    return jsonify({
        "ref_code": ref_code,
        "total_price": total_price,
        "message": "Booking request received! Please complete payment to confirm.",
    }), 201


# ── Admin API: Event Types ─────────────────────────────────────────────────

@app.route("/api/admin/event-types", methods=["GET"])
@require_admin
def admin_list_event_types():
    rows = db_fetchall("SELECT * FROM event_types ORDER BY sort_order, title")
    result = []
    for r in rows:
        result.append({
            "id": r["id"], "slug": r["slug"], "title": r["title"],
            "description": r["description"], "time_display": r["time_display"],
            "price_thb": r["price_thb"], "max_covers": r["max_covers"],
            "is_bookable": bool(r["is_bookable"]),
            "recurrence_rule": r["recurrence_rule"],
            "status": r["status"], "sort_order": r["sort_order"],
            "created_at": str(r["created_at"]) if r.get("created_at") else None,
        })
    return jsonify(result)


@app.route("/api/admin/event-types", methods=["POST"])
@require_admin
def admin_create_event_type():
    data = request.json
    if not data or not data.get("slug") or not data.get("title"):
        return jsonify({"error": "slug and title required"}), 400
    db = get_db()
    db_execute(
        """INSERT INTO event_types (slug, title, description, time_display, price_thb, max_covers, is_bookable, recurrence_rule, status, sort_order)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (data["slug"], data["title"], data.get("description", ""),
         data.get("time_display", ""), int(data.get("price_thb", 0)),
         int(data.get("max_covers", 30)),
         data.get("is_bookable", False) if USE_POSTGRES else (1 if data.get("is_bookable") else 0),
         data.get("recurrence_rule"), data.get("status", "coming_soon"),
         int(data.get("sort_order", 0)))
    )
    db.commit()
    return jsonify({"message": "event type created"}), 201


@app.route("/api/admin/event-types/<int:type_id>", methods=["PUT"])
@require_admin
def admin_update_event_type(type_id):
    data = request.json
    db = get_db()
    et = db_fetchone("SELECT * FROM event_types WHERE id = %s", (type_id,))
    if not et:
        return jsonify({"error": "not found"}), 404
    db_execute(
        """UPDATE event_types SET slug=%s, title=%s, description=%s, time_display=%s,
           price_thb=%s, max_covers=%s, is_bookable=%s, recurrence_rule=%s, status=%s, sort_order=%s
           WHERE id=%s""",
        (data.get("slug", et["slug"]), data.get("title", et["title"]),
         data.get("description", et["description"]),
         data.get("time_display", et["time_display"]),
         int(data.get("price_thb", et["price_thb"])),
         int(data.get("max_covers", et["max_covers"])),
         data.get("is_bookable", et["is_bookable"]) if USE_POSTGRES else (1 if data.get("is_bookable", bool(et["is_bookable"])) else 0),
         data.get("recurrence_rule", et["recurrence_rule"]),
         data.get("status", et["status"]),
         int(data.get("sort_order", et["sort_order"])),
         type_id)
    )
    db.commit()
    return jsonify({"message": "updated"})


@app.route("/api/admin/event-types/<int:type_id>", methods=["DELETE"])
@require_admin
def admin_delete_event_type(type_id):
    db = get_db()
    # Delete bookings for all instances of this type
    db_execute(
        "DELETE FROM bookings WHERE event_id IN (SELECT id FROM event_instances WHERE event_type_id = %s)",
        (type_id,)
    )
    db_execute("DELETE FROM event_instances WHERE event_type_id = %s", (type_id,))
    db_execute("DELETE FROM event_types WHERE id = %s", (type_id,))
    db.commit()
    return jsonify({"message": "deleted"})


# ── Admin API: Event Instances ─────────────────────────────────────────────

@app.route("/api/admin/event-instances", methods=["GET"])
@require_admin
def admin_list_instances():
    type_id = request.args.get("type_id")
    if type_id:
        rows = db_fetchall(
            "SELECT * FROM event_instances WHERE event_type_id = %s ORDER BY date DESC",
            (int(type_id),)
        )
    else:
        rows = db_fetchall("SELECT * FROM event_instances ORDER BY date DESC")
    result = []
    for r in rows:
        booked = db_fetchone(
            "SELECT COALESCE(SUM(guests), 0) as total FROM bookings WHERE event_id = %s AND status != 'cancelled'",
            (r["id"],)
        )["total"]
        result.append({
            "id": r["id"], "event_type_id": r["event_type_id"],
            "date": r["date"], "title": r["title"],
            "description": r["description"],
            "menu_description": r.get("menu_description"),
            "price_thb": r["price_thb"], "max_covers": r["max_covers"],
            "booked_covers": booked, "status": r["status"],
            "created_at": str(r["created_at"]) if r.get("created_at") else None,
        })
    return jsonify(result)


@app.route("/api/admin/event-instances", methods=["POST"])
@require_admin
def admin_create_instance():
    data = request.json
    if not data or not data.get("event_type_id") or not data.get("date"):
        return jsonify({"error": "event_type_id and date required"}), 400
    db = get_db()
    db_execute(
        """INSERT INTO event_instances (event_type_id, date, title, description, menu_description, price_thb, max_covers, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (int(data["event_type_id"]), data["date"], data.get("title"),
         data.get("description"), data.get("menu_description"),
         int(data["price_thb"]) if data.get("price_thb") is not None else None,
         int(data["max_covers"]) if data.get("max_covers") is not None else None,
         data.get("status", "open"))
    )
    db.commit()
    return jsonify({"message": "instance created"}), 201


@app.route("/api/admin/event-instances/<int:instance_id>", methods=["PUT"])
@require_admin
def admin_update_instance(instance_id):
    data = request.json
    db = get_db()
    inst = db_fetchone("SELECT * FROM event_instances WHERE id = %s", (instance_id,))
    if not inst:
        return jsonify({"error": "not found"}), 404
    db_execute(
        """UPDATE event_instances SET date=%s, title=%s, description=%s, menu_description=%s,
           price_thb=%s, max_covers=%s, status=%s WHERE id=%s""",
        (data.get("date", inst["date"]),
         data.get("title", inst.get("title")),
         data.get("description", inst.get("description")),
         data.get("menu_description", inst.get("menu_description")),
         int(data["price_thb"]) if data.get("price_thb") is not None else inst["price_thb"],
         int(data["max_covers"]) if data.get("max_covers") is not None else inst["max_covers"],
         data.get("status", inst["status"]),
         instance_id)
    )
    db.commit()
    return jsonify({"message": "updated"})


@app.route("/api/admin/event-instances/<int:instance_id>", methods=["DELETE"])
@require_admin
def admin_delete_instance(instance_id):
    db = get_db()
    db_execute("DELETE FROM bookings WHERE event_id = %s", (instance_id,))
    db_execute("DELETE FROM event_instances WHERE id = %s", (instance_id,))
    db.commit()
    return jsonify({"message": "deleted"})


# ── Admin API: Generate Instances ──────────────────────────────────────────

@app.route("/api/admin/generate-instances", methods=["POST"])
@require_admin
def admin_generate_instances():
    weeks = int(request.json.get("weeks", 8)) if request.json else 8
    created = generate_instances(weeks)
    return jsonify({"message": f"Generated {created} new instances", "created": created})


# ── Admin API: Legacy event endpoints (redirect to instances) ──────────────

@app.route("/api/admin/events", methods=["GET"])
@require_admin
def admin_list_events():
    """Legacy: list all instances with type info joined."""
    rows = db_fetchall("""
        SELECT ei.*, et.title as type_title, et.description as type_description,
               et.price_thb as type_price, et.max_covers as type_max_covers,
               et.time_display as time_display
        FROM event_instances ei
        JOIN event_types et ON ei.event_type_id = et.id
        ORDER BY ei.date DESC
    """)
    events = []
    for r in rows:
        booked = db_fetchone(
            "SELECT COALESCE(SUM(guests), 0) as total FROM bookings WHERE event_id = %s AND status != 'cancelled'",
            (r["id"],)
        )["total"]
        events.append({
            "id": r["id"], "date": r["date"],
            "title": r.get("title") or r["type_title"],
            "description": r.get("description") or r["type_description"],
            "menu_description": r.get("menu_description") or "",
            "price_thb": r["price_thb"] if r["price_thb"] is not None else r["type_price"],
            "max_covers": r["max_covers"] if r["max_covers"] is not None else r["type_max_covers"],
            "booked_covers": booked, "status": r["status"],
            "event_type_id": r["event_type_id"],
            "created_at": str(r["created_at"]) if r.get("created_at") else None,
        })
    return jsonify(events)


@app.route("/api/admin/events", methods=["POST"])
@require_admin
def admin_create_event():
    """Legacy: create an instance. Requires event_type_id or defaults to sunday-roast."""
    data = request.json
    if not data or not data.get("date"):
        return jsonify({"error": "date is required"}), 400
    db = get_db()
    type_id = data.get("event_type_id")
    if not type_id:
        sr = db_fetchone("SELECT id FROM event_types WHERE slug = 'sunday-roast'")
        type_id = sr["id"] if sr else 1
    db_execute(
        """INSERT INTO event_instances (event_type_id, date, title, description, menu_description, price_thb, max_covers)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (type_id, data["date"], data.get("title"), data.get("description", ""),
         data.get("menu_description", ""),
         int(data.get("price_thb")) if data.get("price_thb") is not None else None,
         int(data.get("max_covers")) if data.get("max_covers") is not None else None)
    )
    db.commit()
    return jsonify({"message": "event created"}), 201


@app.route("/api/admin/events/<int:event_id>", methods=["PUT"])
@require_admin
def admin_update_event(event_id):
    data = request.json
    db = get_db()
    inst = db_fetchone("SELECT * FROM event_instances WHERE id = %s", (event_id,))
    if not inst:
        return jsonify({"error": "not found"}), 404
    db_execute(
        """UPDATE event_instances SET date=%s, title=%s, description=%s, menu_description=%s,
           price_thb=%s, max_covers=%s, status=%s WHERE id=%s""",
        (data.get("date", inst["date"]),
         data.get("title", inst.get("title")),
         data.get("description", inst.get("description")),
         data.get("menu_description", inst.get("menu_description")),
         int(data["price_thb"]) if data.get("price_thb") is not None else inst["price_thb"],
         int(data["max_covers"]) if data.get("max_covers") is not None else inst["max_covers"],
         data.get("status", inst["status"]),
         event_id)
    )
    db.commit()
    return jsonify({"message": "updated"})


@app.route("/api/admin/events/<int:event_id>", methods=["DELETE"])
@require_admin
def admin_delete_event(event_id):
    db = get_db()
    db_execute("DELETE FROM bookings WHERE event_id = %s", (event_id,))
    db_execute("DELETE FROM event_instances WHERE id = %s", (event_id,))
    db.commit()
    return jsonify({"message": "deleted"})


# ── Admin API: Bookings ───────────────────────────────────────────────────

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
        inst = db_fetchone("SELECT * FROM event_instances WHERE id = %s", (booking["event_id"],))
        if inst:
            et = db_fetchone("SELECT * FROM event_types WHERE id = %s", (inst["event_type_id"],))
            event_data = _instance_response(inst, et)
            if new_status == "confirmed":
                send_booking_confirmed_email(booking, event_data)
            elif new_status == "cancelled":
                send_booking_cancelled_email(booking, event_data)

    return jsonify({"message": "updated"})


if __name__ == "__main__":
    init_db()
    # Generate instances for active+bookable event types on startup
    with app.app_context():
        try:
            generate_instances()
        except Exception as e:
            log.warning("Instance generation on startup failed: %s", e)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
