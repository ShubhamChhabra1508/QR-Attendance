import os
import io
import csv
import base64
import secrets
from functools import wraps
from datetime import datetime, timezone
from contextlib import contextmanager
import io
from flask import send_file
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
import psycopg2
import psycopg2.extras
from psycopg2 import errors
import qrcode
from flask import (
    Flask, render_template, request, redirect, url_for, flash, jsonify, Response, abort
)
from flask_login import (
    LoginManager, UserMixin, login_user, login_required, logout_user, current_user
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# Load environment variables if .env exists
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Example: "
        "postgresql://user:pass@ep-xxxx-pooler.region.aws.neon.tech/dbname?sslmode=require"
    )

app = Flask(
    __name__,
    static_folder=STATIC_DIR,
    template_folder=TEMPLATES_DIR,
    static_url_path="/static"
)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "qr_india")
app.config["WTF_CSRF_TIME_LIMIT"] = 7200
csrf = CSRFProtect(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"

QR_TIMEOUT = 10
QR_GRACE_PERIOD = 3


def make_dynamic_token():
    return secrets.token_urlsafe(32)


def generate_qr_base64(data_payload):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(data_payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class PgConnection(psycopg2.extras.RealDictConnection):
    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur


@contextmanager
def db():
    con = psycopg2.connect(DATABASE_URL, connection_factory=PgConnection, connect_timeout=10)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with db() as con:
        with con.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(827364)")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin','teacher','student'))
            );

            CREATE TABLE IF NOT EXISTS teachers (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                department TEXT NOT NULL DEFAULT 'Computer Science',
                email TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS students (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                roll_no TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                created_at BIGINT NOT NULL,
                expires_at BIGINT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS dynamic_tokens (
                id SERIAL PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                token TEXT UNIQUE NOT NULL,
                created_at BIGINT NOT NULL,
                expires_at BIGINT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_dynamic_tokens_lookup ON dynamic_tokens(token, expires_at);

            CREATE TABLE IF NOT EXISTS attendance (
                id SERIAL PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                marked_at BIGINT NOT NULL,
                UNIQUE (session_id, student_id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                author_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'teacher',
                message TEXT NOT NULL,
                created_at BIGINT NOT NULL
            );
            """)

            # Ensure default admin account exists
            cur.execute("SELECT id FROM users WHERE username = %s", ("admin",))
            admin_row = cur.fetchone()
            if not admin_row:
                cur.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
                    ("admin", generate_password_hash("disha@123"), "admin")
                )
                admin_id = cur.fetchone()["id"]
                cur.execute(
                    "INSERT INTO teachers (user_id, name, department, email) VALUES (%s, %s, %s, %s)",
                    (admin_id, "System Administrator", "Administration", "admin@institution.edu")
                )
            else:
                cur.execute("SELECT id FROM teachers WHERE user_id = %s", (admin_row["id"],))
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO teachers (user_id, name, department, email) VALUES (%s, %s, %s, %s)",
                        (admin_row["id"], "System Administrator", "Administration", "admin@institution.edu")
                    )


init_db()


class User(UserMixin):
    def __init__(self, row, profile=None):
        self.id = str(row["id"])
        self.username = row["username"]
        self.role = row["role"]
        self.display_name = profile["name"] if profile and "name" in profile else row["username"]


@login_manager.user_loader
def load_user(user_id):
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE id = %s", (uid,)).fetchone()
        if not row:
            return None
        profile = None
        if row["role"] in ("admin", "teacher"):
            profile = con.execute("SELECT name FROM teachers WHERE user_id = %s", (uid,)).fetchone()
        elif row["role"] == "student":
            profile = con.execute("SELECT name FROM students WHERE user_id = %s", (uid,)).fetchone()
        return User(row, profile)


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def create_account(username, password, role, name=None, roll_no=None, department=None, email=None):
    if not username.strip() or len(password) < 8:
        raise ValueError("Username is required; password must be at least 8 characters.")
    if role == "student" and (not name or not roll_no):
        raise ValueError("Student name and roll number are required.")
    try:
        with db() as con:
            cur = con.execute(
                "INSERT INTO users(username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
                (username.strip(), generate_password_hash(password), role),
            )
            user_id = cur.fetchone()["id"]
            if role == "student":
                con.execute(
                    "INSERT INTO students(user_id, roll_no, name) VALUES (%s, %s, %s)",
                    (user_id, roll_no.strip(), name.strip()),
                )
            elif role in ("teacher", "admin"):
                t_name = name.strip() if name else username.strip()
                con.execute(
                    "INSERT INTO teachers(user_id, name, department, email) VALUES (%s, %s, %s, %s)",
                    (
                        user_id,
                        t_name,
                        department.strip() if department else "Computer Science",
                        email.strip() if email else "",
                    ),
                )
    except errors.UniqueViolation:
        raise ValueError("Username or roll number already exists.")


def generate_session_token(con, session_id):
    now = int(datetime.now(timezone.utc).timestamp())
    token = make_dynamic_token()
    expires_at = now + QR_TIMEOUT
    con.execute("""
        UPDATE sessions
        SET token = %s, created_at = %s, expires_at = %s, active = 1
        WHERE id = %s
    """, (token, now, expires_at, session_id))
    con.execute("""
        INSERT INTO dynamic_tokens(session_id, token, created_at, expires_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (token) DO NOTHING
    """, (session_id, token, now, expires_at + QR_GRACE_PERIOD))
    con.execute("DELETE FROM dynamic_tokens WHERE expires_at < %s", (now - 3600,))
    return token, expires_at


@app.template_filter("datetimeformat")
def datetimeformat(value):
    if not value:
        return ""
    try:
        dt = datetime.fromtimestamp(int(value), timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(value)


@app.template_filter("timeago")
def timeago_filter(value):
    if not value:
        return ""
    try:
        now = int(datetime.now(timezone.utc).timestamp())
        diff = max(0, now - int(value))
        if diff < 60:
            return "just now"
        elif diff < 3600:
            return f"{diff // 60}m ago"
        elif diff < 86400:
            return f"{diff // 3600}h ago"
        else:
            return f"{diff // 86400}d ago"
    except Exception:
        return ""


@app.context_processor
def inject_clock():
    return {"namespace_now": int(datetime.now(timezone.utc).timestamp())}


@app.cli.command("init-demo")
def init_demo():
    init_db()
    demo_accounts = [
        dict(username="admin", password="disha@123", role="admin", name="Administrator", department="Management"),
        dict(username="teacher1", password="Teacher123!", role="teacher", name="Prof. Alan Turing", department="Computer Science", email="turing@college.edu"),
        dict(username="student1", password="Student123!", role="student", name="Demo Student", roll_no="BT001"),
    ]
    for acc in demo_accounts:
        with db() as con:
            exists = con.execute("SELECT id FROM users WHERE username = %s", (acc["username"],)).fetchone()
            if not exists:
                create_account(**acc)
    print("Demo accounts ready. Change their passwords before real use.")


@app.route("/")
def index():
    if current_user.is_authenticated:
        endpoint = "admin_dashboard" if current_user.role in ("admin", "teacher") else "student_dashboard"
        return redirect(url_for(endpoint))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = None
        profile = None
        with db() as con:
            row = con.execute("SELECT * FROM users WHERE username = %s", (username,)).fetchone()
            if row and check_password_hash(row["password_hash"], password):
                if row["role"] in ("admin", "teacher"):
                    profile = con.execute("SELECT name FROM teachers WHERE user_id = %s", (row["id"],)).fetchone()
                elif row["role"] == "student":
                    profile = con.execute("SELECT name FROM students WHERE user_id = %s", (row["id"],)).fetchone()
                login_user(User(row, profile))
                return redirect(url_for("admin_dashboard" if row["role"] in ("admin", "teacher") else "student_dashboard"))
        flash("Invalid username or password.")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/admin")
@role_required("admin", "teacher")
def admin_dashboard():
    with db() as con:
        students_count = con.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
        teachers_count = con.execute("SELECT COUNT(*) AS n FROM teachers").fetchone()["n"]
        sessions_count = con.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        active_sessions_count = con.execute("SELECT COUNT(*) AS n FROM sessions WHERE active = 1").fetchone()["n"]
        attendance_count = con.execute("SELECT COUNT(*) AS n FROM attendance").fetchone()["n"]

        recent = con.execute("""
            SELECT s.id, s.title, s.created_at, s.expires_at, s.active, COUNT(a.id) AS total
            FROM sessions s
            LEFT JOIN attendance a ON a.session_id = s.id
            GROUP BY s.id
            ORDER BY s.id DESC
            LIMIT 25
        """).fetchall()

        trend_rows = con.execute("""
            SELECT day, cnt FROM (
                SELECT to_char(to_timestamp(marked_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day, COUNT(*) AS cnt
                FROM attendance
                GROUP BY 1
                ORDER BY 1 DESC
                LIMIT 14
            ) t ORDER BY day ASC
        """).fetchall()

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if trend_rows:
            chart_trend_labels = [r["day"] for r in trend_rows]
            chart_trend_data = [r["cnt"] for r in trend_rows]
        else:
            chart_trend_labels = [today_str]
            chart_trend_data = [0]

        session_chart_rows = list(reversed(recent[:8]))
        if session_chart_rows:
            chart_session_labels = [s["title"][:22] for s in session_chart_rows]
            chart_session_data = [s["total"] for s in session_chart_rows]
        else:
            chart_session_labels = ["No lectures yet"]
            chart_session_data = [0]

        closed_count = max(0, sessions_count - active_sessions_count)

        messages = con.execute("""
            SELECT m.*, u.username
            FROM messages m
            JOIN users u ON u.id = m.user_id
            ORDER BY m.id DESC
            LIMIT 30
        """).fetchall()

    return render_template(
        "admin.html",
        students_count=students_count,
        teachers_count=teachers_count,
        sessions_count=sessions_count,
        active_sessions_count=active_sessions_count,
        attendance_count=attendance_count,
        recent=recent,
        chart_trend_labels=chart_trend_labels,
        chart_trend_data=chart_trend_data,
        chart_session_labels=chart_session_labels,
        chart_session_data=chart_session_data,
        active_count=active_sessions_count,
        closed_count=closed_count,
        messages=messages
    )


@app.route("/admin/students", methods=["GET", "POST"])
@role_required("admin", "teacher")
def manage_students():
    if request.method == "POST":
        try:
            create_account(
                username=request.form.get("username", ""),
                password=request.form.get("password", ""),
                role="student",
                name=request.form.get("name", ""),
                roll_no=request.form.get("roll_no", "")
            )
            flash("Student account created successfully.")
        except ValueError as exc:
            flash(f"Could not create student: {exc}")
        except psycopg2.IntegrityError:
            flash("Could not create student: duplicate or invalid data.")
        return redirect(url_for("manage_students"))

    with db() as con:
        students = con.execute("""
            SELECT s.id, s.roll_no, s.name, u.username, u.id AS user_id, COUNT(a.id) AS attendance_count
            FROM students s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN attendance a ON a.student_id = s.id
            GROUP BY s.id, u.id
            ORDER BY s.roll_no ASC
        """).fetchall()
    return render_template("students.html", students=students)


@app.route("/admin/students/<int:student_id>/edit", methods=["GET", "POST"])
@role_required("admin", "teacher")
def edit_student(student_id):
    with db() as con:
        st = con.execute("""
            SELECT s.*, u.username, u.id AS user_id
            FROM students s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = %s
        """, (student_id,)).fetchone()
        if not st:
            abort(404)

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            roll_no = request.form.get("roll_no", "").strip()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            if not name or not roll_no or not username:
                flash("Name, Roll Number, and Username are required.")
                return redirect(url_for("edit_student", student_id=student_id))

            if password and len(password) < 8:
                flash("Password must be at least 8 characters.")
                return redirect(url_for("edit_student", student_id=student_id))

            try:
                con.execute("UPDATE students SET name = %s, roll_no = %s WHERE id = %s", (name, roll_no, student_id))
                if password:
                    con.execute(
                        "UPDATE users SET username = %s, password_hash = %s WHERE id = %s",
                        (username, generate_password_hash(password), st["user_id"])
                    )
                else:
                    con.execute("UPDATE users SET username = %s WHERE id = %s", (username, st["user_id"]))
            except psycopg2.IntegrityError:
                con.rollback()
                flash("Update failed: duplicate roll number or username.")
                return redirect(url_for("edit_student", student_id=student_id))

            flash(f"Student '{name}' updated successfully.")
            return redirect(url_for("manage_students"))

    return render_template("edit_student.html", student=st)


@app.route("/admin/students/<int:student_id>/delete", methods=["POST"])
@role_required("admin", "teacher")
def delete_student(student_id):
    with db() as con:
        st = con.execute("SELECT * FROM students WHERE id = %s", (student_id,)).fetchone()
        if not st:
            flash("Student not found.")
            return redirect(url_for("manage_students"))
        con.execute("DELETE FROM attendance WHERE student_id = %s", (student_id,))
        con.execute("DELETE FROM students WHERE id = %s", (student_id,))
        con.execute("DELETE FROM users WHERE id = %s", (st["user_id"],))
        flash(f"Student '{st['name']}' ({st['roll_no']}) deleted successfully.")
    return redirect(url_for("manage_students"))


@app.route("/admin/teachers", methods=["GET", "POST"])
@role_required("admin", "teacher")
def manage_teachers():
    if request.method == "POST":
        try:
            create_account(
                username=request.form.get("username", ""),
                password=request.form.get("password", ""),
                role="teacher",
                name=request.form.get("name", ""),
                department=request.form.get("department", "Computer Science"),
                email=request.form.get("email", "")
            )
            flash("Teacher account created successfully.")
        except ValueError as exc:
            flash(f"Could not create teacher: {exc}")
        except psycopg2.IntegrityError:
            flash("Could not create teacher: duplicate or invalid data.")
        return redirect(url_for("manage_teachers"))

    with db() as con:
        teachers = con.execute("""
            SELECT t.id, t.name, t.department, t.email, u.username, u.role, u.id AS user_id
            FROM teachers t
            JOIN users u ON u.id = t.user_id
            ORDER BY t.name ASC
        """).fetchall()
    return render_template("teachers.html", teachers=teachers)


@app.route("/admin/teachers/<int:teacher_id>/edit", methods=["GET", "POST"])
@role_required("admin", "teacher")
def edit_teacher(teacher_id):
    with db() as con:
        t = con.execute("""
            SELECT t.*, u.username, u.id AS user_id
            FROM teachers t
            JOIN users u ON u.id = t.user_id
            WHERE t.id = %s
        """, (teacher_id,)).fetchone()
        if not t:
            abort(404)

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            department = request.form.get("department", "").strip()
            email = request.form.get("email", "").strip()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            if not name or not username:
                flash("Name and Username are required.")
                return redirect(url_for("edit_teacher", teacher_id=teacher_id))

            if password and len(password) < 8:
                flash("Password must be at least 8 characters.")
                return redirect(url_for("edit_teacher", teacher_id=teacher_id))

            try:
                con.execute(
                    "UPDATE teachers SET name = %s, department = %s, email = %s WHERE id = %s",
                    (name, department, email, teacher_id)
                )
                if password:
                    con.execute(
                        "UPDATE users SET username = %s, password_hash = %s WHERE id = %s",
                        (username, generate_password_hash(password), t["user_id"])
                    )
                else:
                    con.execute("UPDATE users SET username = %s WHERE id = %s", (username, t["user_id"]))
            except psycopg2.IntegrityError:
                con.rollback()
                flash("Update failed: duplicate username.")
                return redirect(url_for("edit_teacher", teacher_id=teacher_id))

            flash(f"Teacher '{name}' updated successfully.")
            return redirect(url_for("manage_teachers"))

    return render_template("edit_teacher.html", teacher=t)


@app.route("/admin/teachers/<int:teacher_id>/delete", methods=["POST"])
@role_required("admin", "teacher")
def delete_teacher(teacher_id):
    with db() as con:
        t = con.execute("SELECT * FROM teachers WHERE id = %s", (teacher_id,)).fetchone()
        if not t:
            flash("Teacher not found.")
            return redirect(url_for("manage_teachers"))
        if t["user_id"] == int(current_user.id):
            flash("You cannot delete your own account while logged in.")
            return redirect(url_for("manage_teachers"))
        con.execute("DELETE FROM teachers WHERE id = %s", (teacher_id,))
        con.execute("DELETE FROM users WHERE id = %s", (t["user_id"],))
        flash(f"Teacher '{t['name']}' deleted successfully.")
    return redirect(url_for("manage_teachers"))


@app.route("/admin/session/new", methods=["POST"])
@role_required("admin", "teacher")
def new_session():
    title = request.form.get("title", "").strip()
    if not title:
        flash("Enter a session or lecture title.")
        return redirect(url_for("admin_dashboard"))

    now = int(datetime.now(timezone.utc).timestamp())
    token = make_dynamic_token()
    expires_at = now + QR_TIMEOUT

    with db() as con:
        cur = con.execute("""
            INSERT INTO sessions(title, token, created_at, expires_at, active)
            VALUES (%s, %s, %s, %s, 1)
            RETURNING id
        """, (title, token, now, expires_at))
        session_id = cur.fetchone()["id"]
        con.execute("""
            INSERT INTO dynamic_tokens(session_id, token, created_at, expires_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (token) DO NOTHING
        """, (session_id, token, now, expires_at + QR_GRACE_PERIOD))

    return redirect(url_for("show_session", session_id=session_id))


@app.route("/admin/session/<int:session_id>")
@role_required("admin", "teacher")
def show_session(session_id):
    now = int(datetime.now(timezone.utc).timestamp())
    with db() as con:
        s = con.execute("SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone()
        if not s:
            abort(404)

        if s["active"] == 1 and s["expires_at"] <= now:
            generate_session_token(con, session_id)
            s = con.execute("SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone()

        count = con.execute("SELECT COUNT(*) AS total FROM attendance WHERE session_id = %s", (session_id,)).fetchone()["total"]
        qr_data = generate_qr_base64(s["token"]) if s["active"] == 1 else ""
        remaining = max(0, s["expires_at"] - now) if s["active"] == 1 else 0

    return render_template(
        "session.html",
        s=s,
        qr_data=qr_data,
        timeout=QR_TIMEOUT,
        remaining=remaining,
        attendance_count=count
    )


@app.route("/admin/session/<int:session_id>/edit", methods=["GET", "POST"])
@role_required("admin", "teacher")
def edit_session(session_id):
    with db() as con:
        s = con.execute("SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone()
        if not s:
            abort(404)

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            active = 1 if request.form.get("active") in ("1", "on", "true") else 0
            if not title:
                flash("Lecture title cannot be empty.")
                return redirect(url_for("edit_session", session_id=session_id))
            con.execute("UPDATE sessions SET title = %s, active = %s WHERE id = %s", (title, active, session_id))
            flash(f"Lecture '{title}' updated successfully.")
            return redirect(url_for("admin_dashboard"))

    return render_template("edit_session.html", session=s)


@app.route("/admin/session/<int:session_id>/delete", methods=["POST"])
@role_required("admin", "teacher")
def delete_session(session_id):
    with db() as con:
        s = con.execute("SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone()
        if not s:
            flash("Lecture session not found.")
            return redirect(url_for("admin_dashboard"))
        con.execute("DELETE FROM dynamic_tokens WHERE session_id = %s", (session_id,))
        con.execute("DELETE FROM attendance WHERE session_id = %s", (session_id,))
        con.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
        flash(f"Lecture '{s['title']}' deleted successfully.")
    return redirect(url_for("admin_dashboard"))


@app.route("/api/session/<int:session_id>/qr")
@role_required("admin", "teacher")
def session_qr_feed(session_id):
    now = int(datetime.now(timezone.utc).timestamp())
    with db() as con:
        s = con.execute("SELECT * FROM sessions WHERE id = %s", (session_id,)).fetchone()
        if not s:
            return jsonify(ok=False, message="Session not found."), 404
        if not s["active"]:
            return jsonify(ok=False, active=False, message="Session is closed.")

        if s["expires_at"] <= now:
            token, expires_at = generate_session_token(con, session_id)
        else:
            token = s["token"]
            expires_at = s["expires_at"]

        count = con.execute("SELECT COUNT(*) AS total FROM attendance WHERE session_id = %s", (session_id,)).fetchone()["total"]
        qr_b64 = generate_qr_base64(token)

    return jsonify(
        ok=True,
        active=True,
        qr_data=qr_b64,
        expires_at=expires_at,
        remaining=max(0, expires_at - now),
        timeout=QR_TIMEOUT,
        attendance_count=count
    )


@app.route("/admin/session/<int:session_id>/close", methods=["POST"])
@role_required("admin", "teacher")
def close_session(session_id):
    with db() as con:
        con.execute("UPDATE sessions SET active = 0 WHERE id = %s", (session_id,))
        con.execute("DELETE FROM dynamic_tokens WHERE session_id = %s", (session_id,))
        flash("Attendance session closed.")
    return redirect(url_for("admin_dashboard"))


@app.route("/api/chat/post", methods=["POST"])
@login_required
def post_chat_message():
    message = request.form.get("message", "").strip()
    if not message:
        flash("Message cannot be empty.")
        return redirect(request.referrer or url_for("admin_dashboard"))
    now = int(datetime.now(timezone.utc).timestamp())
    author = current_user.display_name or current_user.username
    with db() as con:
        con.execute("""
            INSERT INTO messages(user_id, author_name, role, message, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (int(current_user.id), author, current_user.role, message, now))
    flash("Announcement posted.")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/api/chat/<int:message_id>/delete", methods=["POST"])
@login_required
def delete_chat_message(message_id):
    with db() as con:
        msg = con.execute("SELECT * FROM messages WHERE id = %s", (message_id,)).fetchone()
        if not msg:
            flash("Message not found.")
            return redirect(request.referrer or url_for("admin_dashboard"))
        if current_user.role not in ("admin", "teacher") and msg["user_id"] != int(current_user.id):
            abort(403)
        con.execute("DELETE FROM messages WHERE id = %s", (message_id,))
        flash("Message deleted.")
    return redirect(request.referrer or url_for("admin_dashboard"))


@app.route("/student")
@role_required("student")
def student_dashboard():
    with db() as con:
        student = con.execute("SELECT * FROM students WHERE user_id = %s", (int(current_user.id),)).fetchone()
        if not student:
            abort(404)
        records = con.execute("""
            SELECT s.title, a.marked_at
            FROM attendance a
            JOIN sessions s ON s.id = a.session_id
            WHERE a.student_id = %s
            ORDER BY a.marked_at DESC
            LIMIT 20
        """, (student["id"],)).fetchall()
        messages = con.execute("""
            SELECT m.*, u.username
            FROM messages m
            JOIN users u ON u.id = m.user_id
            ORDER BY m.id DESC
            LIMIT 20
        """).fetchall()

    return render_template("student.html", student=student, records=records, messages=messages)


@app.route("/api/attendance", methods=["POST"])
@role_required("student")
def mark_attendance():
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", "")).strip()
    now = int(datetime.now(timezone.utc).timestamp())
    if not token:
        return jsonify(ok=False, message="No QR code data received."), 400

    with db() as con:
        student = con.execute("SELECT id FROM students WHERE user_id = %s", (int(current_user.id),)).fetchone()
        if not student:
            return jsonify(ok=False, message="Student profile not found."), 403

        s = con.execute("""
            SELECT s.id, s.title
            FROM sessions s
            WHERE s.active = 1
              AND (
                (s.token = %s AND s.expires_at + %s >= %s)
                OR EXISTS (
                    SELECT 1 FROM dynamic_tokens dt
                    WHERE dt.session_id = s.id AND dt.token = %s AND dt.expires_at >= %s
                )
              )
        """, (token, QR_GRACE_PERIOD, now, token, now)).fetchone()

        if not s:
            return jsonify(ok=False, message="QR code expired or invalid. Please scan the current code on screen."), 400

        cur = con.execute("""
            INSERT INTO attendance(session_id, student_id, marked_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (session_id, student_id) DO NOTHING
        """, (s["id"], student["id"], now))

        if cur.rowcount == 0:
            return jsonify(ok=True, message=f"Attendance already recorded for '{s['title']}'.")

    return jsonify(ok=True, message=f"Attendance marked successfully for '{s['title']}'!")


@app.route("/admin/report")
@role_required("admin", "teacher")
def report():
    with db() as con:
        rows = con.execute("""
            SELECT s.title, st.roll_no, st.name, a.marked_at
            FROM attendance a
            JOIN sessions s ON s.id = a.session_id
            JOIN students st ON st.id = a.student_id
            ORDER BY a.marked_at DESC
        """).fetchall()
    return render_template("report.html", records=rows)


@app.route('/admin/report/export-excel')
def export_excel():
    # 1. Fetch records (use your existing attendance query / DB logic here)
    # Example: records = Attendance.query.all()
    # Or raw SQL: records = db.session.execute(...) / cursor.fetchall()
    
    # 2. Create the Excel workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Attendance Report"

    # Define headers matching your current table
    headers = ["Session", "Roll Number", "Student", "Recorded Time (UTC)"]
    ws.append(headers)

    # Style header row (Dark Navy + Bold White text)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # 3. Populate attendance rows
    for r in records:
        ws.append([
            getattr(r, 'session_name', '') or r[0],
            getattr(r, 'roll_number', '') or r[1],
            getattr(r, 'student_name', '') or r[2],
            str(getattr(r, 'timestamp', '') or r[3])
        ])

    # 4. Auto-fit column widths
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = col[0].column_letter
        ws.column_dimensions[col_letter].width = max(max_len + 4, 15)

    # 5. Stream the .xlsx file to the client
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="Attendance_Report.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=5000)
