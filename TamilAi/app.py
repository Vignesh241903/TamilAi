import os

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

DB_CONFIG = dict(
    host="localhost",
    dbname="tamil",
    user="postgres",
    password="tamil123",
)


def get_db_connection():
    """Open a new connection to Postgres using DB_CONFIG."""
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    """Create the users table if it doesn't already exist."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id SERIAL PRIMARY KEY,
            username VARCHAR(80) UNIQUE NOT NULL,
            email VARCHAR(120) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )
    conn.commit()
    cur.close()
    conn.close()


@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("chat"))
    return render_template("index.html", active_tab="login")


@app.route("/register", methods=["POST"])
def register():
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()

    if not username or not email or not password:
        return render_template(
            "index.html",
            active_tab="signup",
            signup_error="அனைத்து விவரங்களையும் நிரப்பவும்.",
        )

    if len(password) < 8:
        return render_template(
            "index.html",
            active_tab="signup",
            signup_error="கடவுச்சொல் குறைந்தது 8 எழுத்துகள் இருக்க வேண்டும்.",
        )

    password_hash = generate_password_hash(password) 

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT user_id FROM users WHERE username = %s OR email = %s",
            (username, email),
        )
        if cur.fetchone():
            return render_template(
                "index.html",
                active_tab="signup",
                signup_error="இந்த பயனர் பெயர் அல்லது மின்னஞ்சல் ஏற்கனவே பதிவு செய்யப்பட்டுள்ளது.",
            )

        cur.execute(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES (%s, %s, %s)
            RETURNING user_id
            """,
            (username, email, password_hash),
        )
        user_id = cur.fetchone()[0]
        conn.commit()
    finally:
        cur.close()
        conn.close()

    session["user_id"] = user_id
    session["username"] = username
    return redirect(url_for("chat"))


@app.route("/login", methods=["POST"])
def login():
    identifier = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    if not identifier or not password:
        return render_template(
            "index.html",
            active_tab="login",
            login_error="பயனர் பெயரும் கடவுச்சொல்லும் தேவை.",
        )

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT user_id, username, password_hash FROM users "
            "WHERE username = %s OR email = %s",
            (identifier, identifier),
        )
        user = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not user or not check_password_hash(user["password_hash"], password):
        return render_template(
            "index.html",
            active_tab="login",
            login_error="கணக்கு இல்லை அல்லது தவறான கடவுச்சொல். பதிவு செய்யவும்.",
        )

    session["user_id"] = user["user_id"]
    session["username"] = user["username"]
    return redirect(url_for("chat"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/chat")
def chat():
    if not session.get("user_id"):
        return redirect(url_for("index"))
    return render_template("chat.html", username=session.get("username"))


@app.route("/api/send", methods=["POST"])
def api_send():
    """Placeholder endpoint - wire up your chatbot / AI logic here."""
    if not session.get("user_id"):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"error": "empty message"}), 400

    reply = "இந்த அம்சம் இன்னும் உருவாக்கப்படவில்லை."
    return jsonify({"reply": reply})


@app.route("/api/submissions")
def api_submissions():
    """Placeholder - returns empty history until submissions table logic is wired up."""
    if not session.get("user_id"):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify([])


@app.route("/chart-analysis")
def chart_analysis():
    if not session.get("user_id"):
        return redirect(url_for("index"))
    return "Coming soon"


if __name__ == "__main__":
    init_db()
    app.run(debug=True)