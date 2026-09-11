import difflib
import os
import re
import requests

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

#ollma

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "tamil-llama"


def load_system_prompt(modelfile_name="Modelfile"):
    """Extract the text inside SYSTEM \"\"\" ... \"\"\" from the Ollama Modelfile
    that sits next to this app.py."""
    modelfile_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        modelfile_name,
    )

    with open(modelfile_path, "r", encoding="utf-8") as f:
        content = f.read()

    match = re.search(r'SYSTEM\s*"""(.*?)"""', content, re.DOTALL)

    if not match:
        raise RuntimeError(
            f"Could not find a SYSTEM \"\"\" ... \"\"\" block in {modelfile_path}. "
            "app.py reads the system prompt from the Modelfile at startup -- "
            "make sure Modelfile is in the same folder as app.py and still "
            "has a SYSTEM block."
        )

    return match.group(1).strip()


SYSTEM_PROMPT = load_system_prompt()

OLLAMA_OPTIONS = {
    "num_ctx": 2048,
    "num_predict": 512,   
}

OLLAMA_KEEP_ALIVE = "30m"
MAX_OLLAMA_ATTEMPTS = 2


DB_CONFIG = dict(
    host="localhost",
    dbname="tamil",
    user="postgres",
    password="tamil123",
)


def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
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

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            submission_id SERIAL PRIMARY KEY,
            user_id INT REFERENCES users(user_id),
            input_text TEXT,
            clean_text TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS corrections (
            correction_id SERIAL PRIMARY KEY,
            submission_id INT REFERENCES submissions(submission_id),
            error_type VARCHAR(30),
            corrected_text TEXT,
            explanation TEXT
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            score_id SERIAL PRIMARY KEY,
            submission_id INT REFERENCES submissions(submission_id),
            spelling_score NUMERIC(5, 2),
            grammar_score NUMERIC(5, 2),
            sentence_score NUMERIC(5, 2),
            overall_score NUMERIC(5, 2)
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
            "index.html", active_tab="signup",
            signup_error="அனைத்து விவரங்களையும் நிரப்பவும்.",
        )

    if len(password) < 8:
        return render_template(
            "index.html", active_tab="signup",
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
                "index.html", active_tab="signup",
                signup_error="இந்த பயனர் பெயர் அல்லது மின்னஞ்சல் ஏற்கனவே பதிவு செய்யப்பட்டுள்ளது.",
            )

        cur.execute(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES (%s, %s, %s) RETURNING user_id
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
            "index.html", active_tab="login",
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
            "index.html", active_tab="login",
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




CORRECTED_LABEL_PATTERNS = [
    r"corrected\s+tamil\s+sentence\s*:\s*(.*)",
    r"திருத்தப்பட்ட\s+(?:தமிழ்\s+)?வாக்கியம்\s*:\s*(.*)",
    r"corrected\s+text\s*:\s*(.*)",
]

TEMPLATE_ECHO_MARKERS = ["XX/100", "[complete corrected", "Error 1:\nWrong: ...", "Wrong: ..."]


def extract_corrected_sentence(reply_text, original_text):
    """Pull just the corrected sentence out of the model's reply. Handles
    the 'Corrected Tamil Sentence: ...' style the model actually uses, and
    falls back sensibly if it replies in some other shape."""
    if not reply_text:
        return original_text, False

    for pattern in TEMPLATE_ECHO_MARKERS:
        if pattern in reply_text:
            # The model echoed our instructions instead of answering --
            # treat this attempt as unusable.
            return original_text, False

    for pattern in CORRECTED_LABEL_PATTERNS:
        match = re.search(pattern, reply_text, re.IGNORECASE | re.DOTALL)
        if match:
            candidate = match.group(1).strip().splitlines()[0].strip()
            if candidate:
                return candidate, True

    cleaned = reply_text.strip()
    if cleaned and len(cleaned) < 400 and "\n\n" not in cleaned:
        return cleaned.splitlines()[0].strip(), True

    return original_text, False


def char_similarity(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def diff_errors(original_text, corrected_text):
    """Word-level diff between the original and corrected sentence/paragraph.
    Returns (spelling_errors, grammar_errors) lists of
    {wrong, correct, explanation} dicts.

    Classification heuristic: for each changed word/phrase, compare the
    character-level similarity of the 'wrong' vs 'correct' form. A small
    edit (most characters shared, e.g. a single letter fixed) is treated as
    a spelling error; a larger change (different word/suffix/tense) is
    treated as a grammar error. This is a simple, explainable heuristic --
    not true grammatical parsing -- but it is deterministic and always
    produces a real, defensible answer.
    """
    orig_words = original_text.split()
    corr_words = corrected_text.split()

    matcher = difflib.SequenceMatcher(None, orig_words, corr_words)

    spelling_errors = []
    grammar_errors = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        wrong_chunk = " ".join(orig_words[i1:i2]) or "(விடுபட்டுள்ளது)"
        correct_chunk = " ".join(corr_words[j1:j2]) or "(நீக்கப்பட்டுள்ளது)"

        similarity = char_similarity(wrong_chunk, correct_chunk)

        if tag == "replace" and similarity >= 0.6:
            spelling_errors.append({
                "wrong": wrong_chunk,
                "correct": correct_chunk,
                "explanation": f"'{wrong_chunk}' என்பது எழுத்துப் பிழையுடன் உள்ளது; சரியான வடிவம் '{correct_chunk}'.",
            })
        else:
            label = "மாற்றப்பட வேண்டும்" if tag == "replace" else (
                "நீக்கப்பட வேண்டும்" if tag == "delete" else "சேர்க்கப்பட வேண்டும்"
            )
            grammar_errors.append({
                "wrong": wrong_chunk,
                "correct": correct_chunk,
                "explanation": f"'{wrong_chunk}' {label}; சரியான வடிவம் '{correct_chunk}' (இலக்கணம்/வாக்கிய அமைப்பு).",
            })

    return spelling_errors, grammar_errors


def compute_result(original_text, model_reply):
    corrected_text, parsed_ok = extract_corrected_sentence(model_reply, original_text)

    total_words = max(len(original_text.split()), 1)
    spelling_errors, grammar_errors = diff_errors(original_text, corrected_text)

    spelling_count = len(spelling_errors)
    grammar_count = len(grammar_errors)

    spelling_score = round(max(0, (total_words - spelling_count) / total_words * 100))
    grammar_score = round(max(0, (total_words - grammar_count) / total_words * 100))
    sentence_score = round(max(0, (total_words - spelling_count - grammar_count) / total_words * 100))
    overall_score = round(0.3 * spelling_score + 0.4 * grammar_score + 0.3 * sentence_score)

    if spelling_count == 0 and grammar_count == 0:
        overall_explanation = "வாக்கியத்தில்/பத்தியில் பிழைகள் எதுவும் இல்லை."
    else:
        overall_explanation = (
            f"மொத்தம் {spelling_count} எழுத்துப் பிழை(கள்) மற்றும் "
            f"{grammar_count} இலக்கணப் பிழை(கள்) கண்டறியப்பட்டு திருத்தப்பட்டுள்ளன."
        )

    return {
        "parsed_ok": parsed_ok,
        "corrected_text": corrected_text,
        "spelling_errors": spelling_errors,
        "spelling_score": spelling_score,
        "grammar_errors": grammar_errors,
        "grammar_score": grammar_score,
        "sentence_score": sentence_score,
        "overall_score": overall_score,
        "overall_explanation": overall_explanation,
    }


def save_submission(user_id, input_text, parsed):
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO submissions (user_id, input_text, clean_text)
            VALUES (%s, %s, %s) RETURNING submission_id
            """,
            (user_id, input_text, parsed["corrected_text"]),
        )
        submission_id = cur.fetchone()[0]

        for err in parsed["spelling_errors"]:
            cur.execute(
                """
                INSERT INTO corrections (submission_id, error_type, corrected_text, explanation)
                VALUES (%s, %s, %s, %s)
                """,
                (submission_id, "spelling", f"{err['wrong']} → {err['correct']}", err["explanation"]),
            )

        for err in parsed["grammar_errors"]:
            cur.execute(
                """
                INSERT INTO corrections (submission_id, error_type, corrected_text, explanation)
                VALUES (%s, %s, %s, %s)
                """,
                (submission_id, "grammar", f"{err['wrong']} → {err['correct']}", err["explanation"]),
            )

        cur.execute(
            """
            INSERT INTO scores (submission_id, spelling_score, grammar_score, sentence_score, overall_score)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (submission_id, parsed["spelling_score"], parsed["grammar_score"],
             parsed["sentence_score"], parsed["overall_score"]),
        )

        conn.commit()
        print(f"[DB] Saved submission_id={submission_id} for user_id={user_id} "
              f"({len(parsed['spelling_errors'])} spelling, {len(parsed['grammar_errors'])} grammar)")
        return submission_id

    finally:
        cur.close()
        conn.close()


# =========================
# SEND MESSAGE TO OLLAMA
# =========================

def call_ollama_once(user_message):
    """Ask the model for exactly the one thing it's proven to do reliably:
    a corrected version of the sentence, in its own natural reply style.

    NEW: we no longer pass an explicit "system" field here. `ollama create`
    already bakes the Modelfile's SYSTEM block into the model itself, so
    every /api/generate call already includes it automatically. Sending it
    AGAIN as an explicit override on top of the baked-in one is what was
    producing empty responses -- `ollama run` (which never sends an
    override) worked fine with the exact same model and input."""
    prompt = (
        f"{user_message}\n\n"
        "Reply with only the corrected Tamil sentence, in the form:\n"
        "Corrected Tamil Sentence: <the corrected sentence>"
    )

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": OLLAMA_OPTIONS,
            "keep_alive": OLLAMA_KEEP_ALIVE,
        },
        timeout=240,
    )

    print("Ollama status:", response.status_code)
    print("Ollama raw response:", response.text[:1000])

    response.raise_for_status()
    result = response.json()
    return result.get("response", "").strip(), result


@app.route("/api/send", methods=["POST"])
def api_send():
    if not session.get("user_id"):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"error": "empty message"}), 400

    print("\n========== AI REQUEST ==========")
    print("User message:", user_message)

    try:
        reply = ""
        result = {}
        parsed = None

        for attempt in range(1, MAX_OLLAMA_ATTEMPTS + 1):
            reply, result = call_ollama_once(user_message)
            print(f"Attempt {attempt} reply:", reply)

            parsed = compute_result(user_message, reply)
            if parsed["parsed_ok"]:
                break

        print("================================\n")

        if parsed is None:
            return jsonify({"error": "Ollama returned an empty response.", "ollama_response": result}), 500

        submission_id = save_submission(session["user_id"], user_message, parsed)

        return jsonify({
            "reply": reply,
            "parsed_ok": parsed["parsed_ok"],
            "submission_id": submission_id,
            "corrected_text": parsed["corrected_text"],
            "spelling_errors": parsed["spelling_errors"],
            "grammar_errors": parsed["grammar_errors"],
            "overall_explanation": parsed["overall_explanation"],
            "scores": {
                "spelling_score": parsed["spelling_score"],
                "grammar_score": parsed["grammar_score"],
                "sentence_score": parsed["sentence_score"],
                "overall_score": parsed["overall_score"],
            },
        })

    except requests.exceptions.ConnectionError as e:
        print("OLLAMA CONNECTION ERROR:", e)
        return jsonify({"error": "Ollama is not running or cannot be reached."}), 500

    except requests.exceptions.Timeout as e:
        print("OLLAMA TIMEOUT:", e)
        return jsonify({"error": "Tamil AI took too long to respond."}), 500

    except requests.exceptions.HTTPError as e:
        print("OLLAMA HTTP ERROR:", e)
        return jsonify({"error": f"Ollama error: {e}"}), 500

    except Exception as e:
        print("UNEXPECTED ERROR:", repr(e))
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


@app.route("/api/submissions")
def api_submissions():
    if not session.get("user_id"):
        return jsonify({"error": "unauthorized"}), 401

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        cur.execute(
            """
            SELECT submission_id, input_text, clean_text, created_at
            FROM submissions WHERE user_id = %s ORDER BY created_at DESC
            """,
            (session["user_id"],),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    return jsonify([dict(row) for row in rows])


@app.route("/api/debug/counts")
def api_debug_counts():
    if not session.get("user_id"):
        return jsonify({"error": "unauthorized"}), 401

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT current_database(), inet_server_addr(), inet_server_port();")
        db_name, host, port = cur.fetchone()

        cur.execute("SELECT COUNT(*) FROM submissions;")
        total_submissions = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM submissions WHERE user_id = %s;", (session["user_id"],))
        my_submissions = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM corrections;")
        total_corrections = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM scores;")
        total_scores = cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()

    return jsonify({
        "connected_to_database": db_name,
        "connected_to_host": str(host) if host else "localhost (unix socket)",
        "connected_to_port": port,
        "total_submissions": total_submissions,
        "my_submissions": my_submissions,
        "total_corrections": total_corrections,
        "total_scores": total_scores,
    })


@app.route("/chart-analysis")
def chart_analysis():
    if not session.get("user_id"):
        return redirect(url_for("index"))
    return "Coming soon"


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
