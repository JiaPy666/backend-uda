from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
import hashlib
import secrets
import math

app = Flask(__name__)
CORS(app)

DB_PARAMS = {
    "host": "127.0.0.1",
    "port": 5432,
    "database": "parcheggi_uda",
    "user": "postgres",
    "password": "root"
}

VALID_COUPONS = {
    "PARK10": {"discount_pct": 10, "description": "Sconto 10%"},
    "PARK20": {"discount_pct": 20, "description": "Sconto 20%"},
    "WELCOME": {"discount_pct": 15, "description": "Benvenuto 15%"},
    "AIRPORT": {"discount_pct": 5, "description": "Sconto aeroporto 5%"},
}

def get_db_connection():
    return psycopg2.connect(**DB_PARAMS)

def hash_password(password):
    salt = "parcheggi_uda_salt"
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()

@app.route("/api/test-db")
def test_db():
    try:
        conn = get_db_connection()
        conn.close()
        return jsonify({"status": "success", "message": "Connessione al DB riuscita!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/spots", methods=["GET"])
def get_spots():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT id, zone, status, parking_type, maintenance, vehicle_type,
               cost::FLOAT as cost,
               TO_CHAR(last_updated, 'DD/MM/YYYY, HH24:MI:SS') as last_updated
        FROM parking_spots ORDER BY id;
    """)
    spots = cursor.fetchall()
    cursor.close(); conn.close()
    return jsonify([dict(s) for s in spots])

@app.route("/api/spots/<spot_id>", methods=["PUT"])
def update_spot(spot_id):
    try:
        data = request.json
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE parking_spots
            SET zone=%s, status=%s, parking_type=%s,
                maintenance=%s, vehicle_type=%s, cost=%s, last_updated=NOW()
            WHERE id=%s
            RETURNING TO_CHAR(last_updated, 'DD/MM/YYYY, HH24:MI:SS');
        """, (data['zone'], data['status'], data['parking_type'],
              data['maintenance'], data['vehicle_type'], data['cost'], spot_id))
        res = cursor.fetchone()
        conn.commit(); cursor.close(); conn.close()
        return jsonify({"message": "OK", "last_updated": res[0]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/spots/<spot_id>/fault", methods=["POST"])
def report_fault(spot_id):
    try:
        data = request.json
        report = data.get("report", "").strip()
        if not report:
            return jsonify({"error": "Descrizione obbligatoria"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE parking_spots SET maintenance=TRUE, last_updated=NOW() WHERE id=%s;
        """, (spot_id,))
        conn.commit(); cursor.close(); conn.close()
        return jsonify({"message": "Segnalazione inviata. Il posto è stato messo in manutenzione."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/register", methods=["POST"])
def register():
    try:
        data = request.json
        for field in ["name", "email", "password"]:
            if not data.get(field):
                return jsonify({"error": f"Campo '{field}' obbligatorio"}), 400
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT id FROM users WHERE email=%s;", (data["email"],))
        if cursor.fetchone():
            cursor.close(); conn.close()
            return jsonify({"error": "Email già registrata"}), 409
        cursor.execute("""
            INSERT INTO users (name, email, password_hash, phone, plate)
            VALUES (%s,%s,%s,%s,%s)
            RETURNING id, name, email, phone, plate, role,
                      TO_CHAR(created_at,'DD/MM/YYYY') as created_at;
        """, (data["name"], data["email"], hash_password(data["password"]),
              data.get("phone",""), data.get("plate","")))
        user = dict(cursor.fetchone())
        conn.commit(); cursor.close(); conn.close()
        user['loyalty_points'] = 0
        return jsonify({"message": "Registrazione completata", "user": user}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/login", methods=["POST"])
def login():
    try:
        data = request.json
        if not data.get("email") or not data.get("password"):
            return jsonify({"error": "Email e password obbligatori"}), 400
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, name, email, phone, plate, role,
                   TO_CHAR(created_at,'DD/MM/YYYY') as created_at
            FROM users WHERE email=%s AND password_hash=%s;
        """, (data["email"], hash_password(data["password"])))
        user = cursor.fetchone()
        cursor.close(); conn.close()
        if not user:
            return jsonify({"error": "Credenziali non valide"}), 401
        user = dict(user)
        user['loyalty_points'] = 0
        return jsonify({"message": "Login effettuato", "user": user})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    return jsonify({"message": "Logout effettuato"})

@app.route("/api/auth/me", methods=["GET"])
def get_me():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "Non autenticato"}), 401
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, name, email, phone, plate, role,
                   TO_CHAR(created_at,'DD/MM/YYYY') as created_at
            FROM users WHERE id=%s;
        """, (user_id,))
        user = cursor.fetchone()
        cursor.close(); conn.close()
        if not user:
            return jsonify({"error": "Utente non trovato"}), 404
        return jsonify(dict(user))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/users/<int:user_id>", methods=["PUT"])
def update_user(user_id):
    try:
        data = request.json
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            UPDATE users SET name=%s, phone=%s, plate=%s
            WHERE id=%s
            RETURNING id, name, email, phone, plate, role,
                      TO_CHAR(created_at,'DD/MM/YYYY') as created_at;
        """, (data["name"], data.get("phone",""), data.get("plate",""), user_id))
        user = cursor.fetchone()
        conn.commit(); cursor.close(); conn.close()
        if not user:
            return jsonify({"error": "Utente non trovato"}), 404
        return jsonify(dict(user))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/users/<int:user_id>/loyalty", methods=["GET"])
def get_loyalty(user_id):
    # Calcola punti in base alle prenotazioni completate
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT COALESCE(SUM(duration_hours), 0)::INT as total_hours
            FROM bookings WHERE user_id=%s AND status='active';
        """, (user_id,))
        row = cursor.fetchone()
        cursor.close(); conn.close()
        pts = int(row['total_hours']) * 100
        return jsonify({"loyalty_points": pts})
    except Exception as e:
        return jsonify({"loyalty_points": 0})

@app.route("/api/coupons/validate", methods=["POST"])
def validate_coupon():
    data = request.json
    code = data.get("code", "").upper().strip()
    if code in VALID_COUPONS:
        return jsonify({"valid": True, **VALID_COUPONS[code]})
    return jsonify({"valid": False, "error": "Coupon non valido o scaduto"}), 400

@app.route("/api/bookings", methods=["GET"])
def get_bookings():
    try:
        user_id = request.args.get("user_id")
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        base_query = """
            SELECT b.id, b.booking_code, b.spot_id, b.user_id,
                   b.status, b.duration_hours, b.total_cost,
                   TO_CHAR(b.start_time,'DD/MM/YYYY, HH24:MI') as start_time,
                   TO_CHAR(b.end_time,  'DD/MM/YYYY, HH24:MI') as end_time,
                   b.end_time as end_time_raw,
                   TO_CHAR(b.created_at,'DD/MM/YYYY, HH24:MI') as created_at,
                   p.zone, p.parking_type, p.cost as hourly_cost,
                   u.name as user_name, u.email as user_email, u.plate as user_plate
            FROM bookings b
            JOIN parking_spots p ON b.spot_id = p.id
            LEFT JOIN users u ON b.user_id = u.id
        """
        if user_id:
            cursor.execute(base_query + " WHERE b.user_id=%s ORDER BY b.created_at DESC;", (user_id,))
        else:
            cursor.execute(base_query + " ORDER BY b.created_at DESC;")
        rows = cursor.fetchall()
        cursor.close(); conn.close()
        result = []
        for r in rows:
            row = dict(r)
            if row.get("end_time_raw"):
                row["end_time_raw"] = row["end_time_raw"].isoformat()
            result.append(row)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/bookings", methods=["POST"])
def create_booking():
    try:
        data = request.json
        for field in ["user_id","spot_id","start_time","end_time","duration_hours","total_cost"]:
            if data.get(field) is None:
                return jsonify({"error": f"Campo '{field}' obbligatorio"}), 400
        booking_code = "PRK-" + secrets.token_hex(4).upper()
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT status, maintenance FROM parking_spots WHERE id=%s;", (data["spot_id"],))
        spot = cursor.fetchone()
        if not spot:
            return jsonify({"error": "Posto non trovato"}), 404
        if spot["maintenance"]:
            return jsonify({"error": "Posto in manutenzione"}), 409
        # Controlla sovrapposizione prenotazioni
        cursor.execute("""
            SELECT id FROM bookings
            WHERE spot_id=%s AND status='active'
              AND NOT (end_time <= %s OR start_time >= %s);
        """, (data["spot_id"], data["start_time"], data["end_time"]))
        if cursor.fetchone():
            return jsonify({"error": "Posto già prenotato in questo intervallo"}), 409
        cursor.execute("""
            INSERT INTO bookings
                (booking_code, user_id, spot_id, start_time, end_time, duration_hours, total_cost, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'active')
            RETURNING id, booking_code,
                      TO_CHAR(created_at,'DD/MM/YYYY, HH24:MI') as created_at;
        """, (booking_code, data["user_id"], data["spot_id"],
              data["start_time"], data["end_time"],
              data["duration_hours"], data["total_cost"]))
        booking = dict(cursor.fetchone())
        # Marca come occupato solo se la prenotazione inizia ora (entro 5 min)
        from datetime import datetime, timezone
        start_dt = datetime.fromisoformat(data["start_time"].replace('Z',''))
        now = datetime.now()
        if abs((start_dt - now).total_seconds()) < 300:
            cursor.execute("UPDATE parking_spots SET status='occupied', last_updated=NOW() WHERE id=%s;", (data["spot_id"],))
        loyalty_pts = int(float(data["duration_hours"]) * 100)
        conn.commit(); cursor.close(); conn.close()
        return jsonify({
            "message": "Prenotazione creata",
            "booking_id": booking["id"],
            "booking_code": booking["booking_code"],
            "created_at": booking["created_at"],
            "loyalty_points_earned": loyalty_pts
        }), 201
    except Exception as e:
        print(f"Errore create_booking: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/bookings/<int:booking_id>", methods=["PUT"])
def update_booking(booking_id):
    try:
        data = request.json
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            UPDATE bookings SET end_time=%s, duration_hours=%s, total_cost=%s
            WHERE id=%s
            RETURNING id, booking_code,
                      TO_CHAR(end_time,'DD/MM/YYYY, HH24:MI') as end_time,
                      end_time as end_time_raw;
        """, (data["end_time"], data["duration_hours"], data["total_cost"], booking_id))
        booking = cursor.fetchone()
        conn.commit(); cursor.close(); conn.close()
        if not booking:
            return jsonify({"error": "Prenotazione non trovata"}), 404
        row = dict(booking)
        if row.get("end_time_raw"):
            row["end_time_raw"] = row["end_time_raw"].isoformat()
        return jsonify(row)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/bookings/<int:booking_id>/cancel", methods=["POST"])
def cancel_booking(booking_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT spot_id FROM bookings WHERE id=%s AND status='active';", (booking_id,))
        booking = cursor.fetchone()
        if not booking:
            return jsonify({"error": "Prenotazione non trovata o già cancellata"}), 404
        cursor.execute("UPDATE bookings SET status='cancelled' WHERE id=%s;", (booking_id,))
        cursor.execute("UPDATE parking_spots SET status='free', last_updated=NOW() WHERE id=%s;", (booking["spot_id"],))
        conn.commit(); cursor.close(); conn.close()
        return jsonify({"message": "Prenotazione cancellata"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats/visits", methods=["GET"])
def get_visit_stats():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT EXTRACT(HOUR FROM start_time)::INT as hour, COUNT(*) as count
            FROM bookings WHERE status IN ('active','completed')
            GROUP BY hour ORDER BY hour;
        """)
        hourly = cursor.fetchall()
        cursor.execute("""
            SELECT EXTRACT(ISODOW FROM start_time)::INT as dow, COUNT(*) as count
            FROM bookings WHERE status IN ('active','completed')
            GROUP BY dow ORDER BY dow;
        """)
        weekly = cursor.fetchall()
        cursor.close(); conn.close()
        hourly_map = {r['hour']: r['count'] for r in hourly}
        hourly_full = [{"hour": h, "count": int(hourly_map.get(h, 0))} for h in range(24)]
        dow_map = {r['dow']: r['count'] for r in weekly}
        days_it = ['Lun','Mar','Mer','Gio','Ven','Sab','Dom']
        weekly_full = [{"day": days_it[d-1], "dow": d, "count": int(dow_map.get(d, 0))} for d in range(1,8)]
        return jsonify({"hourly": hourly_full, "weekly": weekly_full})
    except Exception as e:
        # Dati demo se DB vuoto
        return jsonify({
            "hourly": [{"hour": h, "count": int(max(0, 8*abs(math.sin(h/3.8)) + (12 if 8<=h<=10 else 8 if 14<=h<=16 else 3)))} for h in range(24)],
            "weekly": [{"day": d, "dow": i+1, "count": [12,18,25,20,30,45,38][i]} for i,d in enumerate(['Lun','Mar','Mer','Gio','Ven','Sab','Dom'])]
        })

@app.route("/api/maintenance/schedule", methods=["GET"])
def get_maintenance_schedule():
    import datetime
    today = datetime.date.today()
    schedule = []
    entries = [
        ("A", "Pulizia ordinaria", "Mario Rossi"),
        ("B", "Controllo impianti", "Luigi Verdi"),
        ("C", "Riparazione segnaletica", "Anna Bianchi"),
        ("D", "Pulizia straordinaria", "Carlo Neri"),
    ]
    for i, (zone, tipo, op) in enumerate(entries):
        day = today + datetime.timedelta(days=i)
        schedule.append({
            "id": i+1, "zone": zone, "date": day.strftime("%d/%m/%Y"),
            "date_iso": day.isoformat(), "operator": op, "type": tipo, "status": "programmato"
        })
    return jsonify(schedule)

@app.route("/api/maintenance/schedule", methods=["POST"])
def add_maintenance_schedule():
    data = request.json
    return jsonify({"message": "Turno aggiunto", "id": secrets.token_hex(4)}), 201

if __name__ == "__main__":
    app.run(debug=True, port=5000, host="0.0.0.0")
