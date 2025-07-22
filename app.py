import os
import uuid
import random
import string
from datetime import datetime, timedelta
from flask import Flask, request, render_template, redirect, session, flash
from dotenv import load_dotenv
import pyodbc
from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import HtmlFormatter
import smtplib
from email.mime.text import MIMEText

load_dotenv()

app = Flask(__name__)

# DB connection string
conn_str = (
    f"DRIVER={os.getenv('DB_DRIVER')};"
    f"SERVER={os.getenv('DB_SERVER')}:{os.getenv('DB_PORT')};"
    f"DATABASE={os.getenv('DB_NAME')};"
    f"UID={os.getenv('DB_USER')};"
    f"PWD={os.getenv('DB_PASSWORD')};"
)
conn = pyodbc.connect(conn_str, autocommit=True)
cursor = conn.cursor()

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

cursor.execute("""
CREATE TABLE IF NOT EXISTS paste_viewers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    paste_id VARCHAR(16),
    email VARCHAR(255),
    access_code VARCHAR(10),
    verified BOOLEAN DEFAULT FALSE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (paste_id) REFERENCES pastes(paste_id) ON DELETE CASCADE
);
""")

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

    conn = pyodbc.connect(conn_str, autocommit=True)
    cursor = conn.cursor()

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

        cursor.execute("""
            INSERT INTO pastes (paste_id, content, expires_at, views_left, language)
            VALUES (?, ?, ?, ?, ?)
        """, (paste_id, text, expires_at, views_left, language))

        # Just save emails to DB, no emailing here
        emails_raw = request.form.get("emails", "").strip()
        emails = [e.strip() for e in emails_raw.split(",") if e.strip()]
        for email in emails:
            cursor.execute("""
                INSERT INTO paste_viewers (paste_id, email)
                VALUES (?, ?)
            """, (paste_id, email))

        conn.commit()

        paste_url = request.host_url.rstrip("/") + f"/p/{paste_id}"
        return render_template("home.html", paste_created=True, paste_url=paste_url)
    
    conn.close()

    return render_template("home.html", paste=None)


@app.route("/p/<paste_id>", methods=["GET", "POST"])
def view_paste(paste_id):

    conn = pyodbc.connect(conn_str, autocommit=True)
    cursor = conn.cursor()

    # Fetch paste details as before
    cursor.execute("SELECT id, content, expires_at, views_left, language FROM pastes WHERE paste_id = ?", paste_id)
    paste = cursor.fetchone()
    if not paste:
        return "Paste not found", 404

    paste_id_db, content, expires_at, views_left, language = paste

    if expires_at and datetime.utcnow() > expires_at:
        cursor.execute("DELETE FROM pastes WHERE id = ?", paste_id_db)
        return "This paste has expired or been deleted.", 410

    if views_left is not None and views_left <= 0:
        cursor.execute("DELETE FROM pastes WHERE id = ?", paste_id_db)
        return "This paste has expired or been deleted.", 410

    # Check restricted viewers
    cursor.execute("SELECT email FROM paste_viewers WHERE paste_id = ?", paste_id)
    allowed_emails = [row[0] for row in cursor.fetchall()]
    verified_emails = session.get(f"verified_{paste_id}", [])

    if allowed_emails:
        # Step 1: User submits email to receive code (request new code)
        if request.method == "POST" and "request_code" in request.form:
            email = request.form.get("email", "").strip()
            if email not in allowed_emails:
                return render_template("auth.html", paste_id=paste_id, error="Email not authorized.")

            # Generate new code and save
            code = generate_code()
            cursor.execute("""
                UPDATE paste_viewers SET access_code = ?, verified = FALSE WHERE paste_id = ? AND email = ?
            """, (code, paste_id, email))
            conn.commit()

            paste_url = request.host_url.rstrip("/") + f"/p/{paste_id}"
            send_email(email, paste_url, code)

            return render_template("auth.html", paste_id=paste_id, email=email, message="Access code sent to your email. Please enter it below.")

        # Step 2: User submits code to verify
        elif request.method == "POST" and "verify_code" in request.form:
            email = request.form.get("email", "").strip()
            code = request.form.get("code", "").strip()

            cursor.execute("""
                SELECT id, access_code FROM paste_viewers WHERE paste_id = ? AND email = ?
            """, (paste_id, email))
            row = cursor.fetchone()

            if not row or row[1] != code:
                return render_template("auth.html", paste_id=paste_id, email=email, error="Invalid code. Please try again.")

            # Mark verified in DB & session
            cursor.execute("UPDATE paste_viewers SET verified = TRUE WHERE id = ?", row[0])
            conn.commit()

            verified_emails.append(email)
            session[f"verified_{paste_id}"] = verified_emails

        # If not verified, show email entry form (step 1)
        if not verified_emails:
            return render_template("auth.html", paste_id=paste_id)

    # Decrement views only if limited
    if views_left is not None:
        cursor.execute("UPDATE pastes SET views_left = views_left - 1 WHERE id = ?", paste_id_db)

    # Show highlighted paste
    try:
        lexer = get_lexer_by_name(language)
    except Exception:
        lexer = get_lexer_by_name("text")

    formatter = HtmlFormatter(linenos=True, cssclass="codehilite")
    highlighted = highlight(content, lexer, formatter)
    style = formatter.get_style_defs('.codehilite')

    conn.close()

    return render_template("home.html", paste={"highlighted": highlighted}, css=style)

if __name__ == "__main__":
    app.run(debug=True)
