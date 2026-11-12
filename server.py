import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
MEDIA_DIR = Path(os.environ.get("CINELANKA_MEDIA_DIR", ROOT / "uploads"))
DATA_DIR = Path(os.environ.get("CINELANKA_DATA_DIR", ROOT / "data"))
RECEIPTS_DIR = DATA_DIR / "receipts"
DATABASE = DATA_DIR / "cinelanka.sqlite3"
ADMIN_PASSWORD = os.environ.get("CINELANKA_ADMIN_PASSWORD", "")
HOST = os.environ.get("CINELANKA_HOST", "127.0.0.1")
PORT = int(os.environ.get("CINELANKA_PORT", "8000"))
COOKIE_SECURE = os.environ.get("CINELANKA_COOKIE_SECURE") == "1"
SESSION_COOKIE = "cinelanka_admin"
VISITOR_COOKIE = "cinelanka_visitor"
SESSION_TTL = 12 * 60 * 60
MAX_JSON_SIZE = 64 * 1024
MAX_VIDEO_SIZE = 20 * 1024 * 1024 * 1024
MAX_COVER_SIZE = 12 * 1024 * 1024
MAX_RECEIPT_SIZE = 12 * 1024 * 1024
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpg", ".mpeg", ".ts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
RECEIPT_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"}
VIDEO_TYPES = {
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".ts": "video/mp2t",
}

if len(ADMIN_PASSWORD) < 14:
    raise SystemExit("CINELANKA_ADMIN_PASSWORD must be set to a password with at least 14 characters.")
if not 1 <= PORT <= 65535:
    raise SystemExit("CINELANKA_PORT must be between 1 and 65535.")

MEDIA_DIR.mkdir(exist_ok=True)
RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

SESSIONS = {}
LOGIN_ATTEMPTS = {}


@contextmanager
def connect_db():
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_db():
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS titles (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('Movie', 'TV Series')),
                genre TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                year INTEGER NOT NULL,
                access TEXT NOT NULL CHECK(access IN ('free', 'paid')),
                price INTEGER NOT NULL DEFAULT 0,
                video_name TEXT NOT NULL,
                video_path TEXT,
                video_size INTEGER,
                video_type TEXT,
                cover_name TEXT NOT NULL,
                cover_path TEXT,
                created_at TEXT NOT NULL,
                ready INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                title_id TEXT NOT NULL REFERENCES titles(id),
                visitor_id TEXT NOT NULL,
                receipt_path TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_receipt', 'pending', 'approved')),
                UNIQUE(title_id, visitor_id)
            );
            CREATE INDEX IF NOT EXISTS payments_status_created ON payments(status, created_at);
            """
        )


initialize_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_filename(value, allowed_extensions):
    name = unquote(value or "")
    name = name.replace("\\", "/").split("/")[-1]
    extension = Path(name).suffix.lower()
    if extension not in allowed_extensions:
        return None
    stem = Path(name).stem
    stem = re.sub(r"[^A-Za-z0-9._ -]", "", stem).strip(" .")
    if not stem:
        stem = "upload"
    return f"{stem[:100]}{extension}", extension


class Handler(BaseHTTPRequestHandler):
    server_version = "CineLanka/1"
    sys_version = ""

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        super().end_headers()

    def send_json(self, status, value, headers=None):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if headers:
            for name, value in headers:
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_empty(self, status, headers=None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        if headers:
            for name, value in headers:
                self.send_header(name, value)
        self.end_headers()

    def error(self, status, message):
        self.send_json(status, {"error": message})

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request size.")
        if length < 1 or length > MAX_JSON_SIZE:
            self.close_connection = True
            raise ValueError("Invalid request size.")
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Invalid JSON request.")
        if not isinstance(value, dict):
            raise ValueError("Invalid JSON request.")
        return value

    def cookies(self):
        parsed = SimpleCookie()
        try:
            parsed.load(self.headers.get("Cookie", ""))
        except CookieError:
            return {}
        return {key: morsel.value for key, morsel in parsed.items()}

    def visitor_id(self):
        return self.cookies().get(VISITOR_COOKIE)

    def is_admin(self):
        token = self.cookies().get(SESSION_COOKIE)
        session = SESSIONS.get(token)
        if not session:
            return False
        if session <= time.time():
            SESSIONS.pop(token, None)
            return False
        return True

    def require_admin(self):
        if not self.is_admin():
            self.error(401, "Admin login required.")
            return False
        return True

    def require_same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            self.error(403, "Same-origin request required.")
            return False
        parsed = urlsplit(origin)
        expected_host = self.headers.get("Host", "").lower()
        expected_scheme = "https" if COOKIE_SECURE else "http"
        if parsed.netloc.lower() != expected_host or parsed.scheme != expected_scheme:
            self.error(403, "Cross-origin request rejected.")
            return False
        return True

    def cookies_for_session(self, token=None, visitor=None):
        values = []
        if token is not None:
            suffix = "; Secure" if COOKIE_SECURE else ""
            values.append(
                f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}{suffix}"
            )
        if visitor is not None:
            suffix = "; Secure" if COOKIE_SECURE else ""
            values.append(
                f"{VISITOR_COOKIE}={visitor}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000{suffix}"
            )
        return [("Set-Cookie", value) for value in values]

    def clear_session_cookie(self):
        suffix = "; Secure" if COOKIE_SECURE else ""
        return [("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{suffix}")]

    def static_file(self, relative_path):
        path = ROOT / relative_path
        if not path.is_file():
            self.error(404, "Not found.")
            return
        body_size = path.stat().st_size
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(body_size))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, length=64 * 1024)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path in ("/", "/index.html"):
            self.static_file("index.html")
            return
        if path in ("/cinelanka-logo.jpg", "/assets/cinelanka-logo.jpg"):
            self.static_file("cinelanka-logo.jpg")
            return
        if path == "/healthz":
            self.send_json(200, {"ok": True})
            return
        if path == "/api/session":
            visitor = self.visitor_id()
            headers = [] if visitor else self.cookies_for_session(visitor=secrets.token_urlsafe(24))
            self.send_json(200, {"isAdmin": self.is_admin()}, headers)
            return
        if path == "/api/titles":
            self.get_titles()
            return
        if path == "/api/admin/payments":
            if not self.require_admin():
                return
            self.get_payment_queue()
            return
        match = re.fullmatch(r"/api/admin/payments/([0-9a-f-]+)/receipt", path)
        if match:
            if not self.require_admin():
                return
            self.get_receipt(match.group(1))
            return
        match = re.fullmatch(r"/media/([0-9a-f-]+)/(video|cover)", path)
        if match:
            self.get_media(match.group(1), match.group(2))
            return
        self.error(404, "Not found.")

    def do_POST(self):
        if not self.require_same_origin():
            return
        path = unquote(urlsplit(self.path).path)
        if path == "/api/session":
            self.login()
            return
        if path == "/api/titles":
            if not self.require_admin():
                return
            self.create_title()
            return
        if path == "/api/payment-requests":
            self.create_payment_request()
            return
        match = re.fullmatch(r"/api/admin/payments/([0-9a-f-]+)/approve", path)
        if match:
            if not self.require_admin():
                return
            self.approve_payment(match.group(1))
            return
        self.error(404, "Not found.")

    def do_PUT(self):
        if not self.require_same_origin():
            return
        path = unquote(urlsplit(self.path).path)
        if not path.startswith("/api/payment-requests/") and not self.require_admin():
            return
        match = re.fullmatch(r"/api/titles/([0-9a-f-]+)/(video|cover)", path)
        if match:
            if not self.require_admin():
                return
            self.upload_title_file(match.group(1), match.group(2))
            return
        match = re.fullmatch(r"/api/payment-requests/([0-9a-f-]+)/receipt", path)
        if match:
            self.upload_receipt(match.group(1))
            return
        self.error(404, "Not found.")

    def do_DELETE(self):
        if not self.require_same_origin():
            return
        if urlsplit(self.path).path != "/api/session":
            self.error(404, "Not found.")
            return
        token = self.cookies().get(SESSION_COOKIE)
        if token:
            SESSIONS.pop(token, None)
        self.send_empty(204, self.clear_session_cookie())

    def login(self):
        address = self.client_address[0]
        now = time.time()
        if len(LOGIN_ATTEMPTS) > 10000:
            LOGIN_ATTEMPTS.clear()
        attempts = [when for when in LOGIN_ATTEMPTS.get(address, []) if now - when < 900]
        LOGIN_ATTEMPTS[address] = attempts
        if len(attempts) >= 8:
            self.error(429, "Too many login attempts. Try again later.")
            return
        try:
            value = self.read_json()
        except ValueError as error:
            self.error(400, str(error))
            return
        password = value.get("password")
        valid = isinstance(password, str) and hmac.compare_digest(
            hashlib.sha256(password.encode("utf-8")).digest(),
            hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).digest(),
        )
        if not valid:
            attempts.append(now)
            LOGIN_ATTEMPTS[address] = attempts
            self.error(401, "Incorrect admin password.")
            return
        LOGIN_ATTEMPTS.pop(address, None)
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = time.time() + SESSION_TTL
        self.send_json(200, {"isAdmin": True}, self.cookies_for_session(token=token))

    def create_title(self):
        try:
            value = self.read_json()
        except ValueError as error:
            self.error(400, str(error))
            return
        title = value.get("title")
        media_type = value.get("type")
        genre = value.get("genre", "")
        description = value.get("description", "")
        access = value.get("access")
        try:
            price = int(value.get("price") or 0)
        except (TypeError, ValueError):
            self.error(400, "Invalid price.")
            return
        video = safe_filename(value.get("videoName"), VIDEO_EXTENSIONS)
        cover = safe_filename(value.get("coverName"), IMAGE_EXTENSIONS)
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            self.error(400, "Enter a title up to 100 characters.")
            return
        if media_type not in ("Movie", "TV Series"):
            self.error(400, "Select a valid title type.")
            return
        if not isinstance(genre, str) or len(genre) > 80 or not isinstance(description, str) or len(description) > 500:
            self.error(400, "Genre or description is too long.")
            return
        if access not in ("free", "paid") or (access == "paid" and price < 1) or (access == "free" and price != 0):
            self.error(400, "Enter a valid access price.")
            return
        if not video or not cover or value.get("rights") is not True:
            self.error(400, "Choose supported video and poster files and confirm you have distribution rights.")
            return
        identifier = str(uuid.uuid4())
        with connect_db() as db:
            db.execute(
                """INSERT INTO titles
                (id,title,type,genre,description,year,access,price,video_name,cover_name,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    title.strip(),
                    media_type,
                    genre.strip(),
                    description.strip(),
                    datetime.now().year,
                    access,
                    price,
                    video[0],
                    cover[0],
                    now_iso(),
                ),
            )
        self.send_json(201, {"id": identifier})

    def stream_request_to_file(self, destination, maximum):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            return "Invalid upload size.", 400
        if length < 1:
            return "The selected file is empty.", 400
        if length > maximum:
            self.close_connection = True
            return "The selected file exceeds the upload size limit.", 413
        temporary = destination.with_name(destination.name + ".part")
        remaining = length
        try:
            with temporary.open("wb") as target:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("Upload ended before all file data arrived.")
                    target.write(chunk)
                    remaining -= len(chunk)
            temporary.replace(destination)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            return str(error), 400
        return None, length

    def upload_title_file(self, identifier, kind):
        with connect_db() as db:
            row = db.execute("SELECT * FROM titles WHERE id=?", (identifier,)).fetchone()
        if not row:
            self.error(404, "Title not found.")
            return
        if kind == "video":
            safe = safe_filename(row["video_name"], VIDEO_EXTENSIONS)
            maximum = MAX_VIDEO_SIZE
            label = "video"
        else:
            safe = safe_filename(row["cover_name"], IMAGE_EXTENSIONS)
            maximum = MAX_COVER_SIZE
            label = "cover"
        if not safe:
            self.error(400, "Unsupported file type.")
            return
        path = MEDIA_DIR / f"{identifier}_{label}{safe[1]}"
        error, size = self.stream_request_to_file(path, maximum)
        if error:
            self.error(size if isinstance(size, int) else 400, error)
            return
        with connect_db() as db:
            if kind == "video":
                db.execute(
                    "UPDATE titles SET video_path=?,video_size=?,video_type=?,ready=CASE WHEN cover_path IS NOT NULL THEN 1 ELSE 0 END WHERE id=?",
                    (path.name, size, VIDEO_TYPES.get(safe[1], mimetypes.guess_type(safe[0])[0] or "application/octet-stream"), identifier),
                )
            else:
                db.execute(
                    "UPDATE titles SET cover_path=?,ready=CASE WHEN video_path IS NOT NULL THEN 1 ELSE 0 END WHERE id=?",
                    (path.name, identifier),
                )
            row = db.execute("SELECT ready FROM titles WHERE id=?", (identifier,)).fetchone()
        self.send_json(200, {"ready": bool(row["ready"])})

    def get_titles(self):
        visitor = self.visitor_id()
        with connect_db() as db:
            rows = db.execute(
                """SELECT t.*, EXISTS(
                    SELECT 1 FROM payments p
                    WHERE p.title_id=t.id AND p.visitor_id=? AND p.status='approved'
                ) AS unlocked
                FROM titles t WHERE t.ready=1 ORDER BY t.created_at DESC""",
                (visitor or "",),
            ).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "type": row["type"],
                    "genre": row["genre"],
                    "description": row["description"],
                    "year": row["year"],
                    "access": row["access"],
                    "price": row["price"],
                    "rating": "New",
                    "cover": bool(row["cover_path"]),
                    "unlocked": bool(row["unlocked"]),
                    "createdAt": row["created_at"],
                }
            )
        self.send_json(200, {"titles": result})

    def stream_file(self, path, content_type, filename, allow_range=False):
        try:
            size = path.stat().st_size
        except OSError:
            self.error(404, "File not found.")
            return
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range") if allow_range else None
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                self.send_empty(416, [("Content-Range", f"bytes */{size}")])
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
            else:
                suffix = int(match.group(2))
                start = max(0, size - suffix)
            if start >= size or end < start:
                self.send_empty(416, [("Content-Range", f"bytes */{size}")])
                return
            end = min(end, size - 1)
            status = 206
        length = max(0, end - start + 1)
        disposition = "inline" if content_type.startswith("image/") else "attachment"
        safe_ascii = re.sub(r"[^A-Za-z0-9._ -]", "_", filename) or "download"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", f'{disposition}; filename="{safe_ascii}"')
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def get_media(self, identifier, kind):
        with connect_db() as db:
            row = db.execute("SELECT * FROM titles WHERE id=? AND ready=1", (identifier,)).fetchone()
            if not row:
                self.error(404, "Title not found.")
                return
            if kind == "video" and row["access"] == "paid" and not self.is_admin():
                visitor = self.visitor_id()
                unlocked = db.execute(
                    "SELECT 1 FROM payments WHERE title_id=? AND visitor_id=? AND status='approved'",
                    (identifier, visitor or ""),
                ).fetchone()
                if not unlocked:
                    self.error(403, "Payment must be verified before download.")
                    return
            stored_path = row["video_path"] if kind == "video" else row["cover_path"]
            original_name = row["video_name"] if kind == "video" else row["cover_name"]
            content_type = row["video_type"] if kind == "video" else mimetypes.guess_type(original_name)[0]
        if not stored_path:
            self.error(404, "File not found.")
            return
        path = MEDIA_DIR / stored_path
        self.stream_file(path, content_type or "application/octet-stream", original_name, allow_range=kind == "video")

    def create_payment_request(self):
        visitor = self.visitor_id()
        if not visitor:
            self.error(400, "Reload the page and try again.")
            return
        try:
            value = self.read_json()
        except ValueError as error:
            self.error(400, str(error))
            return
        title_id = value.get("titleId")
        if not isinstance(title_id, str):
            self.error(400, "Invalid title.")
            return
        identifier = str(uuid.uuid4())
        try:
            with connect_db() as db:
                title = db.execute("SELECT id,access FROM titles WHERE id=? AND ready=1", (title_id,)).fetchone()
                if not title or title["access"] != "paid":
                    self.error(404, "Paid title not found.")
                    return
                db.execute(
                    "INSERT INTO payments (id,title_id,visitor_id,created_at,status) VALUES (?,?,?,?,?)",
                    (identifier, title_id, visitor, now_iso(), "awaiting_receipt"),
                )
        except sqlite3.IntegrityError:
            with connect_db() as db:
                existing = db.execute(
                    "SELECT id,status FROM payments WHERE title_id=? AND visitor_id=?",
                    (title_id, visitor),
                ).fetchone()
            if existing and existing["status"] == "approved":
                self.error(409, "Access has already been approved for this browser.")
                return
            if existing:
                identifier = existing["id"]
                with connect_db() as db:
                    db.execute("UPDATE payments SET status='awaiting_receipt' WHERE id=?", (identifier,))
            else:
                self.error(409, "A payment request already exists.")
                return
        self.send_json(201, {"id": identifier})

    def upload_receipt(self, identifier):
        visitor = self.visitor_id()
        with connect_db() as db:
            request = db.execute("SELECT * FROM payments WHERE id=?", (identifier,)).fetchone()
        if not request or not visitor or not hmac.compare_digest(request["visitor_id"], visitor):
            self.error(404, "Payment request not found.")
            return
        original_name = unquote(self.headers.get("X-File-Name", "receipt.jpg"))
        safe = safe_filename(original_name, RECEIPT_EXTENSIONS)
        if not safe:
            self.error(400, "Choose a JPG, PNG, WebP or PDF receipt.")
            return
        path = RECEIPTS_DIR / f"{identifier}{safe[1]}"
        error, size = self.stream_request_to_file(path, MAX_RECEIPT_SIZE)
        if error:
            self.error(size if isinstance(size, int) else 400, error)
            return
        with connect_db() as db:
            db.execute(
                "UPDATE payments SET receipt_path=?,status='pending',created_at=? WHERE id=? AND visitor_id=?",
                (path.name, now_iso(), identifier, visitor),
            )
        self.send_json(200, {"status": "pending"})

    def get_payment_queue(self):
        with connect_db() as db:
            rows = db.execute(
                """SELECT p.id,p.created_at,t.title,t.price FROM payments p
                JOIN titles t ON t.id=p.title_id WHERE p.status='pending' AND p.receipt_path IS NOT NULL
                ORDER BY p.created_at"""
            ).fetchall()
        self.send_json(
            200,
            {
                "payments": [
                    {"id": row["id"], "title": row["title"], "price": row["price"], "createdAt": row["created_at"]}
                    for row in rows
                ]
            },
        )

    def get_receipt(self, identifier):
        with connect_db() as db:
            row = db.execute(
                "SELECT receipt_path FROM payments WHERE id=? AND receipt_path IS NOT NULL",
                (identifier,),
            ).fetchone()
        if not row:
            self.error(404, "Receipt not found.")
            return
        path = RECEIPTS_DIR / row["receipt_path"]
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.stream_file(path, mime_type, path.name)

    def approve_payment(self, identifier):
        with connect_db() as db:
            result = db.execute(
                "UPDATE payments SET status='approved' WHERE id=? AND status='pending' AND receipt_path IS NOT NULL",
                (identifier,),
            )
            if result.rowcount != 1:
                self.error(404, "Pending payment receipt not found.")
                return
        self.send_json(200, {"approved": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32


if __name__ == "__main__":
    scheme = "https behind a trusted reverse proxy" if COOKIE_SECURE else "local HTTP"
    print(f"CineLanka VIP running at http://{HOST}:{PORT}/ ({scheme})")
    print("Press Ctrl+C to stop the server. Admin password is held only in this process.")
    try:
        Server((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nCineLanka VIP stopped.")
