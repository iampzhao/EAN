import os
import uuid
import random
import string
from datetime import datetime, timedelta
from flask import Flask, request, render_template, redirect, session, flash
from dotenv import load_dotenv

import mysql.connector
from mysql.connector import errorcode

from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import HtmlFormatter
import smtplib
from email.mime.text import MIMEText
from cryptography.fernet import Fernet
import base64
import hashlib

load_dotenv()

# Get key from .env and convert it to 32-byte base64
raw_key = os.getenv("ENCRYPTION_KEY")
key = base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode()).digest())
fernet = Fernet(key)

def hash_email(email: str) -> str:
    return hashlib.sha256(email.lower().encode()).hexdigest()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# DB connection string
conn_str = mysql.connector.connect(
    host=os.getenv("DB_SERVER"),
    port=int(os.getenv("DB_PORT")),
    database=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD")
)

cursor = conn_str.cursor()

# Create tables if not exist
cursor.execute("""
CREATE TABLE IF NOT EXISTS pastes (
    id INT AUTO_INCREMENT PRIMARY KEY,
    paste_id VARCHAR(16) UNIQUE NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME,
    views_left INT NULL DEFAULT NULL,
    language VARCHAR(50) DEFAULT 'text'
);
""")

conn_str.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS paste_viewers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    paste_id VARCHAR(16),
    email VARCHAR(255),
    email_hash VARCHAR(64),
    access_code VARCHAR(10),
    verified BOOLEAN DEFAULT FALSE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (paste_id) REFERENCES pastes(paste_id) ON DELETE CASCADE
);
""")
conn_str.commit()

def generate_code(length=6):
    return ''.join(random.choices(string.digits, k=length))

def send_email(to_email, paste_url, code):
    smtp_host = os.getenv("EMAIL_HOST")
    smtp_port = int(os.getenv("EMAIL_PORT"))
    smtp_user = os.getenv("EMAIL_USER")
    smtp_pass = os.getenv("EMAIL_PASSWORD")

    subject = "Access Code for Your Paste"
    body = f"""
Hello,

You have been granted access to view a paste at:

{paste_url}

Use the following access code to view it:

{code}

Thanks!
"""

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = smtp_user
    msg['To'] = to_email

    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"Sent access code to {to_email}")
    except Exception as e:
        print(f"Error sending email to {to_email}: {e}")

@app.route("/", methods=["GET", "POST"])
def home():
    conn_str = mysql.connector.connect(
        host=os.getenv("DB_SERVER"),
        port=int(os.getenv("DB_PORT")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

    cursor = conn_str.cursor()

    if request.method == "POST":
        text = request.form["paste"]
        language = request.form.get("language", "text")

        views_input = request.form.get("views_left", "").strip()
        views_left = int(views_input) if views_input.isdigit() else None

        expiry_hours_str = request.form.get("expiry_hours", "").strip()
        try:
            expiry_hours = int(expiry_hours_str)
            expiry_hours = max(1, min(expiry_hours, 720))
        except ValueError:
            expiry_hours = 1

        expires_at = datetime.utcnow() + timedelta(hours=expiry_hours)
        paste_id = uuid.uuid4().hex[:8]

        encrypted_content = fernet.encrypt(text.encode()).decode()

        cursor.execute("""
            INSERT INTO pastes (paste_id, content, expires_at, views_left, language)
            VALUES (?, ?, ?, ?, ?)
        """, (paste_id, encrypted_content, expires_at, views_left, language))
        conn_str.commit()

        emails_raw = request.form.get("emails", "").strip()
        emails = [e.strip() for e in emails_raw.split(",") if e.strip()]
        for email in emails:
            email_hash = hash_email(email)
            encrypted_email = fernet.encrypt(email.encode()).decode()
            cursor.execute("""
                INSERT INTO paste_viewers (paste_id, email, email_hash)
                VALUES (?, ?, ?)
            """, (paste_id, encrypted_email, email_hash))
            conn_str.commit()

        cursor.close()
        paste_url = request.host_url.rstrip("/") + f"/p/{paste_id}"
        return render_template("home.html", paste_created=True, paste_url=paste_url)

    cursor.close()
    return render_template("home.html", paste=None)

@app.route("/p/<paste_id>", methods=["GET", "POST"])
def view_paste(paste_id):
    conn_str = mysql.connector.connect(
        host=os.getenv("DB_SERVER"),
        port=int(os.getenv("DB_PORT")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

    cursor = conn_str.cursor()

    cursor.execute("SELECT id, content, expires_at, views_left, language FROM pastes WHERE paste_id = ?", paste_id)
    paste = cursor.fetchone()
    if not paste:
        return "Paste not found", 404

    paste_id_db, content, expires_at, views_left, language = paste

    if expires_at and datetime.utcnow() > expires_at:
        cursor.execute("DELETE FROM pastes WHERE id = ?", paste_id_db)
        conn_str.commit()
        return "This paste has expired or been deleted.", 410

    if views_left is not None and views_left <= 0:
        cursor.execute("DELETE FROM pastes WHERE id = ?", paste_id_db)
        conn_str.commit()
        return "This paste has expired or been deleted.", 410

    cursor.execute("SELECT id, email, email_hash, verified FROM paste_viewers WHERE paste_id = ?", paste_id)
    viewers = cursor.fetchall()
    verified_emails = session.get(f"verified_{paste_id}", [])

    if viewers:
        if request.method == "POST" and "request_code" in request.form:
            submitted_email = request.form.get("email", "").strip().lower()
            email_hash_val = hash_email(submitted_email)

            viewer = next((v for v in viewers if v.email_hash == email_hash_val), None)
            if not viewer:
                return render_template("auth.html", paste_id=paste_id, error="Email not authorized.")

            viewer_id = viewer.id
            decrypted_email = fernet.decrypt(viewer.email.encode()).decode()
            code = generate_code()

            cursor.execute("""
                UPDATE paste_viewers SET access_code = ?, verified = FALSE WHERE id = ?
            """, (code, viewer_id))
            conn_str.commit()

            paste_url = request.host_url.rstrip("/") + f"/p/{paste_id}"
            send_email(decrypted_email, paste_url, code)

            return render_template("auth.html", paste_id=paste_id, email=decrypted_email, message="Access code sent.")

        elif request.method == "POST" and "verify_code" in request.form:
            email = request.form.get("email", "").strip().lower()
            code = request.form.get("code", "").strip()
            email_hash_val = hash_email(email)

            cursor.execute("SELECT id, access_code FROM paste_viewers WHERE paste_id = ? AND email_hash = ?",
                           (paste_id, email_hash_val))
            row = cursor.fetchone()

            if not row or row.access_code != code:
                return render_template("auth.html", paste_id=paste_id, email=email, error="Invalid code.")

            cursor.execute("UPDATE paste_viewers SET verified = TRUE WHERE id = ?", row.id)
            conn_str.commit()
            verified_emails.append(email)
            session[f"verified_{paste_id}"] = verified_emails

        if not verified_emails:
            return render_template("auth.html", paste_id=paste_id)

    if views_left is not None:
        cursor.execute("UPDATE pastes SET views_left = views_left - 1 WHERE id = ?", paste_id_db)
        conn_str.commit()

    try:
        lexer = get_lexer_by_name(language)
    except Exception:
        lexer = get_lexer_by_name("text")

    formatter = HtmlFormatter(linenos=True, cssclass="codehilite")
    decrypted_content = fernet.decrypt(content.encode()).decode()
    highlighted = highlight(decrypted_content, lexer, formatter)
    style = formatter.get_style_defs('.codehilite')

    conn_str.close()

    return render_template("home.html", paste={"highlighted": highlighted}, css=style)

if __name__ == "__main__":
    app.run(debug=True)
