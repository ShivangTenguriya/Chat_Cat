from flask import Flask, request, render_template
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras

load_dotenv()  

app = Flask(__name__)
app.secret_key=os.getenv("secret_key")
socketio = SocketIO(app, cors_allowed_origins="*")

app.config['SESSION_COOKIE_SECURE'] = True     
app.config['SESSION_COOKIE_HTTPONLY'] = True  

DB_PARAMS = {
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT", 5432)
}

def get_db_connection():
    conn = psycopg2.connect(**DB_PARAMS)
    return conn


connected_users = {}  
user_sockets = {}     

from flask import session

@app.route('/test_session')
def test_session():
    session['test'] = 'ok'
    return f"Session value: {session.get('test')}"


@socketio.on("login")
def handle_login(data):
    mobile = data['mobile']
    name = data['name']
    age = data['age']
    gender = data['gender']

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    
    cur.execute("SELECT * FROM users WHERE mobile = %s AND active = TRUE", (mobile,))
    active_user = cur.fetchone()
    if active_user:
        emit("login_error", {"message": "You are already connected from another device."}, to=request.sid)
        cur.close()
        conn.close()
        return

    
    cur.execute("SELECT * FROM users WHERE mobile = %s", (mobile,))
    user = cur.fetchone()
    if user:
        user_id = user['id']
        cur.execute("UPDATE users SET active = TRUE WHERE id = %s", (user_id,))
    else:
        cur.execute(
            "INSERT INTO users (nickname, age, gender, mobile, active) VALUES (%s,%s,%s,%s,TRUE) RETURNING id",
            (name, age, gender, mobile)
        )
        user_id = cur.fetchone()['id']

    conn.commit()

    
    connected_users[request.sid] = user_id
    user_sockets[user_id] = request.sid

    
    cur.execute("""
        SELECT * FROM users
        WHERE active = TRUE AND id != %s AND current_partner IS NULL
        LIMIT 1
    """, (user_id,))
    partner = cur.fetchone()

    if partner:
        partner_id = partner['id']
        partner_name = partner['nickname']

        cur.execute("UPDATE users SET current_partner=%s WHERE id=%s", (partner_id, user_id))
        cur.execute("UPDATE users SET current_partner=%s WHERE id=%s", (user_id, partner_id))
        conn.commit()

        room = f"room_{user_id}_{partner_id}"

        join_room(room, sid=request.sid)
        join_room(room, sid=user_sockets[partner_id])

        cur.execute("SELECT nickname FROM users WHERE id = %s", (user_id,))
        my_name = cur.fetchone()["nickname"]

        emit(
            "chat_started",
            {
                "room": room,
                "partner_name": partner_name
            },
            to=request.sid
        )

        emit(
            "chat_started",
            {
                "room": room,
                "partner_name": my_name
            },
            to=user_sockets[partner_id]
        )

    else:
        emit("waiting", {"message": "Waiting for another user..."}, to=request.sid)

    cur.close()
    conn.close()


@socketio.on("sendMessage")
def handle_message(data):
    room = data.get("room")
    text = data.get("text")
    replyText = data.get("replyText")
    timestamp = data.get("timestamp")
    emit("receiveMessage", {
        "text": text,
        "replyText": replyText,
        "timestamp": timestamp,
        "reactions": {}
    }, room=room, skip_sid=request.sid)


@socketio.on("message_seen")
def handle_seen(data):
    room = data.get("room")
    text = data.get("text")
    emit("message_status_update", {"text": text, "status": "seen"}, room=room, skip_sid=request.sid)


@socketio.on("typing")
def handle_typing(data):
    room = data.get("room")
    emit("typing", room=room, skip_sid=request.sid)



@socketio.on("skip_partner")
def handle_skip(data):
    room = data.get("room")
    socket_id = request.sid
    user_id = connected_users.get(socket_id)
    if not user_id:
        return

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT current_partner FROM users WHERE id=%s", (user_id,))
    row = cur.fetchone()
    partner_id = row['current_partner'] if row else None

    cur.execute("UPDATE users SET current_partner=NULL WHERE id=%s", (user_id,))
    if partner_id:
        cur.execute("UPDATE users SET current_partner=NULL WHERE id=%s", (partner_id,))
        if partner_id in user_sockets:
            emit("partner_disconnected", {"message": "Your partner skipped you."}, to=user_sockets[partner_id])

    conn.commit()
    cur.close()
    conn.close()


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



@app.route("/")
def home():
    return render_template("main.html")

if __name__ == "__main__":
    socketio.run(app,port=5000, debug=True)
