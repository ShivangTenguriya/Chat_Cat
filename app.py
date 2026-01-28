import os
from flask import Flask, render_template, request, jsonify
from flask_login import LoginManager, UserMixin, current_user, login_user
from flask_socketio import SocketIO, emit
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("secret_key")

# Session cookie settings
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = os.getenv("FLASK_ENV") == "production"

# ------------------- SocketIO -------------------
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=True)

# ------------------- Flask-Login -------------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# ------------------- User class -------------------
class User(UserMixin):
    def __init__(self, id):
        self.id = id

# ------------------- Database connection -------------------
def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

# ------------------- User loader -------------------
@login_manager.user_loader
def load_user(user_id):
    try:
        user_id = int(user_id) 
    except ValueError:
        return None
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return User(row['id'])
    return None

# ------------------- In-memory socket mapping -------------------
connected_users = {}  
user_sockets = {}     

# ------------------- Helper: find partner -------------------
def find_partner(user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, nickname FROM users
        WHERE active=TRUE AND current_partner IS NULL AND id!=%s LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    partner_id = row['id'] if row else None

    if partner_id:
        cur.execute("UPDATE users SET current_partner=%s WHERE id=%s", (partner_id, user_id))
        cur.execute("UPDATE users SET current_partner=%s WHERE id=%s", (user_id, partner_id))
        conn.commit()
    cur.close()
    conn.close()
    return partner_id, row['nickname'] if row else None

# ------------------- Routes -------------------
@app.route("/")
def home():
    return render_template("main.html")


@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route("/login", methods=["POST"])
def login():
    data = request.json
    nickname = data.get("nickname")
    age = int(data.get("age"))
    gender = data.get("gender")
    mobile = data.get("mobile")

    if age < 18 or not nickname or not gender or not mobile or not mobile[0] in "6789":
        return jsonify({"error": "Invalid data"}), 400

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM users WHERE mobile=%s", (mobile,))
    user = cur.fetchone()
    if user:
        user_id = user['id']
        cur.execute("UPDATE users SET active=TRUE, nickname=%s, gender=%s, age=%s, current_partner=NULL WHERE id=%s",
                    (nickname, gender, age, user_id))
    else:
        cur.execute("INSERT INTO users (nickname, active, current_partner, mobile, gender, age) VALUES (%s, TRUE, NULL, %s, %s, %s) RETURNING id",
                    (nickname, mobile, gender, age))
        user_id = cur.fetchone()['id']

    conn.commit()
    cur.close()
    conn.close()

    user_obj = User(user_id)
    login_user(user_obj)

    return jsonify({"success": True})

# ------------------- SocketIO events -------------------
@socketio.on("login")
def handle_socket_login():
    if not current_user.is_authenticated:
        emit("login_error", {"message": "Invalid session"}, to=request.sid)
        return

    connected_users[request.sid] = current_user.id
    user_sockets[current_user.id] = request.sid

    partner_id, partner_name = find_partner(current_user.id)
    if partner_id:
        emit("chat_started", {"partner_name": partner_name}, to=request.sid)
        emit("chat_started", {"partner_name": current_user.id}, to=user_sockets[partner_id])
    else:
        emit("waiting", {"message": "Waiting for a partner..."}, to=request.sid)

# ------------------- Send Message -------------------
@socketio.on("sendMessage")
def handle_message(data):
    if not current_user.is_authenticated:
        emit("login_error", {"message": "Invalid session"}, to=request.sid)
        return

    text = data.get("text")
    replyText = data.get("replyText")
    timestamp = data.get("timestamp")

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT current_partner FROM users WHERE id=%s", (current_user.id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    partner_id = row['current_partner'] if row else None
    if partner_id and partner_id in user_sockets:
        emit("receiveMessage", {"text": text, "replyText": replyText, "timestamp": timestamp}, to=user_sockets[partner_id])

# ------------------- Skip Partner -------------------
@socketio.on("skip_partner")
def handle_skip():
    if not current_user.is_authenticated:
        emit("login_error", {"message": "Invalid session"}, to=request.sid)
        return

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    user_id = current_user.id

    cur.execute("SELECT current_partner FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    partner_id = row['current_partner'] if row else None

    # Clear partners
    cur.execute("UPDATE users SET current_partner=NULL WHERE id=%s", (user_id,))
    if partner_id:
        cur.execute("UPDATE users SET current_partner=NULL WHERE id=%s", (partner_id,))
        if partner_id in user_sockets:
            emit("partner_disconnected", {"message": "Your partner skipped you."}, to=user_sockets[partner_id])
    conn.commit()
    cur.close()
    conn.close()

# ------------------- Disconnect -------------------
@socketio.on("disconnect")
def handle_disconnect():
    socket_id = request.sid
    user_id = connected_users.get(socket_id)
    if not user_id:
        return

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT current_partner FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    partner_id = row['current_partner'] if row else None

    cur.execute("UPDATE users SET active=FALSE, current_partner=NULL WHERE id=%s", (user_id,))
    if partner_id:
        cur.execute("UPDATE users SET current_partner=NULL WHERE id=%s", (partner_id,))
        if partner_id in user_sockets:
            emit("partner_disconnected", {"message": "Your partner disconnected."}, to=user_sockets[partner_id])
    conn.commit()
    cur.close()
    conn.close()

    connected_users.pop(socket_id, None)
    user_sockets.pop(user_id, None)

if __name__ == "__main__":
    socketio.run(app)
