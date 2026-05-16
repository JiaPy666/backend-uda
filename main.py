from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
import hashlib, secrets, math

app = Flask(__name__)
CORS(app, resources={r"/api/*": {
    "origins": "*",
    "methods": ["GET","POST","PUT","DELETE","OPTIONS"],
    "allow_headers": ["Content-Type","Authorization"]
}})

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    return response

DB_PARAMS = {
    "host": "127.0.0.1", "port": 5432,
    "database": "parcheggi_uda",
    "user": "postgres", "password": "root"
}

def db():
    return psycopg2.connect(**DB_PARAMS)

def hash_pw(p):
    return hashlib.sha256(f"parcheggi_uda_salt{p}".encode()).hexdigest()

# ── Auto-migration: aggiunge colonne/tabelle mancanti senza perdere dati ───────
def run_migrations():
    stmts = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS loyalty_points INTEGER DEFAULT 0",
        "ALTER TABLE parking_spots ADD COLUMN IF NOT EXISTS floor_level INTEGER DEFAULT 1",
        "ALTER TABLE parking_spots ADD COLUMN IF NOT EXISTS fault_report TEXT DEFAULT ''",
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS free_hour_used BOOLEAN DEFAULT FALSE",
        """UPDATE parking_spots SET floor_level = CASE zone
            WHEN 'A' THEN 1 WHEN 'B' THEN 2 WHEN 'C' THEN 3 WHEN 'D' THEN 4 ELSE 1
           END WHERE floor_level = 1""",
        """CREATE TABLE IF NOT EXISTS loyalty_rewards (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            points_spent INTEGER NOT NULL DEFAULT 10000,
            status VARCHAR(20) NOT NULL DEFAULT 'available'
                CHECK (status IN ('available','used','expired')),
            booking_id INTEGER REFERENCES bookings(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            used_at TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_rewards_user ON loyalty_rewards (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_rewards_status ON loyalty_rewards (status)",
        """CREATE TABLE IF NOT EXISTS fault_reports (
            id SERIAL PRIMARY KEY,
            spot_id VARCHAR(20) NOT NULL REFERENCES parking_spots(id),
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            report_type VARCHAR(50) NOT NULL DEFAULT 'Altro',
            description TEXT DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'aperta'
                CHECK (status IN ('aperta','in lavorazione','risolta')),
            created_at TIMESTAMP DEFAULT NOW(),
            resolved_at TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_fault_spot ON fault_reports (spot_id)",
        "CREATE INDEX IF NOT EXISTS idx_fault_status ON fault_reports (status)",
        """CREATE TABLE IF NOT EXISTS maintenance_schedule (
            id SERIAL PRIMARY KEY,
            zone VARCHAR(1) NOT NULL,
            scheduled_date DATE NOT NULL,
            operator VARCHAR(100) NOT NULL,
            intervention_type VARCHAR(100) NOT NULL DEFAULT 'Pulizia ordinaria',
            priority VARCHAR(20) NOT NULL DEFAULT 'normale'
                CHECK (priority IN ('bassa','normale','alta')),
            notes TEXT DEFAULT '',
            status VARCHAR(30) NOT NULL DEFAULT 'programmato'
                CHECK (status IN ('programmato','in corso','completato','annullato')),
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_maint_zone ON maintenance_schedule (zone)",
        "CREATE INDEX IF NOT EXISTS idx_maint_date ON maintenance_schedule (scheduled_date)",
    ]
    try:
        conn = psycopg2.connect(**DB_PARAMS)
        cur = conn.cursor()
        for s in stmts:
            try:
                cur.execute(s)
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"  migration skipped: {str(e)[:60]}")
        cur.close(); conn.close()
        print("✅ DB migrations OK")
    except Exception as e:
        print(f"⚠️  Migration warning: {e}")

run_migrations()

# ── POSTI ──────────────────────────────────────────────────────────────────────
@app.route("/api/spots", methods=["GET"])
def get_spots():
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, zone, status, parking_type, maintenance, vehicle_type,
                   cost::FLOAT, floor_level,
                   COALESCE(fault_report,'') AS fault_report,
                   TO_CHAR(last_updated,'DD/MM/YYYY, HH24:MI:SS') AS last_updated
            FROM parking_spots ORDER BY id
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/spots/<spot_id>", methods=["PUT"])
def update_spot(spot_id):
    try:
        data = request.json
        conn = db(); cur = conn.cursor()
        cur.execute("""
            UPDATE parking_spots
            SET zone=%s, status=%s, parking_type=%s, maintenance=%s,
                vehicle_type=%s, cost=%s, last_updated=NOW()
            WHERE id=%s
            RETURNING TO_CHAR(last_updated,'DD/MM/YYYY, HH24:MI:SS')
        """, (data['zone'], data['status'], data['parking_type'],
              data['maintenance'], data['vehicle_type'], data['cost'], spot_id))
        res = cur.fetchone(); conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "OK", "last_updated": res[0]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── SEGNALAZIONI GUASTO ────────────────────────────────────────────────────────
@app.route("/api/spots/<spot_id>/fault", methods=["POST","OPTIONS"])
def report_fault(spot_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = request.json or {}
        report_type = data.get("report_type", "Altro").strip()
        description = data.get("description", "").strip()
        user_id     = data.get("user_id")
        if not report_type:
            return jsonify({"error": "Tipo segnalazione obbligatorio"}), 400

        conn = db(); cur = conn.cursor()

        # Verifica che il posto esista
        cur.execute("SELECT status FROM parking_spots WHERE id=%s", (spot_id,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"error": f"Posto {spot_id} non trovato"}), 404

        spot_status = row[0]
        fault_note  = f"{report_type}: {description}" if description else report_type
        fault_id    = None

        # Salva in fault_reports
        try:
            cur.execute("""
                INSERT INTO fault_reports (spot_id, user_id, report_type, description, status)
                VALUES (%s, %s, %s, %s, 'aperta') RETURNING id
            """, (spot_id, user_id or None, report_type, description))
            fault_id = cur.fetchone()[0]
        except Exception as e:
            conn.rollback()
            print(f"fault_reports insert skipped: {e}")

        # Aggiorna il posto — controlla il constraint
        if spot_status == 'free':
            # Posto libero: metti in manutenzione
            try:
                cur.execute("""
                    UPDATE parking_spots
                    SET maintenance=TRUE, fault_report=%s, last_updated=NOW()
                    WHERE id=%s
                """, (fault_note, spot_id))
            except Exception:
                conn.rollback()
                cur.execute("UPDATE parking_spots SET maintenance=TRUE, last_updated=NOW() WHERE id=%s", (spot_id,))
            msg = "Segnalazione salvata — posto messo in manutenzione"
        else:
            # Posto occupato: salva solo la nota (non può essere maintenance+occupied)
            try:
                cur.execute("UPDATE parking_spots SET fault_report=%s, last_updated=NOW() WHERE id=%s",
                            (fault_note, spot_id))
            except Exception:
                conn.rollback()
            msg = "Segnalazione registrata — il posto sarà messo in manutenzione al termine della sosta"

        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": msg, "id": fault_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/faults", methods=["GET"])
def get_faults():
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT fr.id, fr.spot_id, fr.report_type, fr.description, fr.status,
                   fr.created_at, fr.resolved_at,
                   COALESCE(ps.zone::TEXT, '?') AS zone,
                   COALESCE(u.name, 'Anonimo')  AS user_name
            FROM fault_reports fr
            LEFT JOIN parking_spots ps ON fr.spot_id = ps.id
            LEFT JOIN users u          ON fr.user_id  = u.id
            ORDER BY fr.created_at DESC
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        result = []
        for r in rows:
            row = dict(r)
            row['created_at']  = row['created_at'].strftime('%d/%m/%Y %H:%M')  if row['created_at']  else ''
            row['resolved_at'] = row['resolved_at'].strftime('%d/%m/%Y %H:%M') if row['resolved_at'] else ''
            result.append(row)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/faults/<int:fault_id>", methods=["PUT"])
def update_fault(fault_id):
    try:
        data       = request.json
        new_status = data.get("status")
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        if new_status == 'risolta':
            cur.execute("""
                UPDATE fault_reports SET status=%s, resolved_at=NOW()
                WHERE id=%s RETURNING spot_id
            """, (new_status, fault_id))
            row = cur.fetchone()
            if row:
                spot_id = row['spot_id']
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM fault_reports
                    WHERE spot_id=%s AND status != 'risolta'
                """, (spot_id,))
                cnt = cur.fetchone()['cnt']
                if cnt == 0:
                    cur.execute("""
                        UPDATE parking_spots
                        SET maintenance=FALSE, fault_report='', last_updated=NOW()
                        WHERE id=%s
                    """, (spot_id,))
        else:
            cur.execute("UPDATE fault_reports SET status=%s WHERE id=%s", (new_status, fault_id))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "Aggiornato"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── AUTH ───────────────────────────────────────────────────────────────────────
@app.route("/api/auth/register", methods=["POST"])
def register():
    try:
        data = request.json
        for f in ["name","email","password"]:
            if not data.get(f):
                return jsonify({"error": f"Campo '{f}' obbligatorio"}), 400
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id FROM users WHERE email=%s", (data["email"],))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"error": "Email già registrata"}), 409
        cur.execute("""
            INSERT INTO users (name, email, password_hash, phone, plate, loyalty_points)
            VALUES (%s,%s,%s,%s,%s,0)
            RETURNING id, name, email, phone, plate, role, loyalty_points,
                      TO_CHAR(created_at,'DD/MM/YYYY') AS created_at
        """, (data["name"], data["email"], hash_pw(data["password"]),
              data.get("phone",""), data.get("plate","")))
        user = dict(cur.fetchone()); conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "Registrazione completata", "user": user}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/login", methods=["POST"])
def login():
    try:
        data = request.json
        if not data.get("email") or not data.get("password"):
            return jsonify({"error": "Email e password obbligatori"}), 400
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute("""
                SELECT id, name, email, phone, plate, role, loyalty_points,
                       TO_CHAR(created_at,'DD/MM/YYYY') AS created_at
                FROM users WHERE email=%s AND password_hash=%s
            """, (data["email"], hash_pw(data["password"])))
        except Exception:
            conn.rollback()
            cur.execute("""
                SELECT id, name, email, phone, plate, role,
                       TO_CHAR(created_at,'DD/MM/YYYY') AS created_at
                FROM users WHERE email=%s AND password_hash=%s
            """, (data["email"], hash_pw(data["password"])))
        user = cur.fetchone(); cur.close(); conn.close()
        if not user:
            return jsonify({"error": "Credenziali non valide"}), 401
        result = dict(user)
        result.setdefault("loyalty_points", 0)
        return jsonify({"message": "Login effettuato", "user": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    return jsonify({"message": "Logout effettuato"})

@app.route("/api/users/<int:user_id>", methods=["PUT"])
def update_user(user_id):
    try:
        data = request.json
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            UPDATE users SET name=%s, phone=%s, plate=%s WHERE id=%s
            RETURNING id, name, email, phone, plate, role, loyalty_points,
                      TO_CHAR(created_at,'DD/MM/YYYY') AS created_at
        """, (data["name"], data.get("phone",""), data.get("plate",""), user_id))
        user = cur.fetchone(); conn.commit(); cur.close(); conn.close()
        if not user:
            return jsonify({"error": "Utente non trovato"}), 404
        return jsonify(dict(user))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── LOYALTY ────────────────────────────────────────────────────────────────────
@app.route("/api/users/<int:user_id>/loyalty", methods=["GET"])
def get_loyalty(user_id):
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT loyalty_points FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS cnt FROM loyalty_rewards WHERE user_id=%s AND status='available'", (user_id,))
        rewards = cur.fetchone()
        cur.close(); conn.close()
        return jsonify({
            "loyalty_points": row['loyalty_points'] if row else 0,
            "free_hours_available": int(rewards['cnt']) if rewards else 0
        })
    except Exception as e:
        return jsonify({"loyalty_points": 0, "free_hours_available": 0})

@app.route("/api/users/<int:user_id>/loyalty/redeem", methods=["POST"])
def redeem_loyalty(user_id):
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT loyalty_points FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        if not row or row['loyalty_points'] < 10000:
            cur.close(); conn.close()
            return jsonify({"error": "Punti insufficienti (servono 10.000)"}), 400
        cur.execute("UPDATE users SET loyalty_points = loyalty_points - 10000 WHERE id=%s RETURNING loyalty_points", (user_id,))
        new_pts = cur.fetchone()['loyalty_points']
        cur.execute("INSERT INTO loyalty_rewards (user_id, points_spent, status) VALUES (%s,10000,'available') RETURNING id", (user_id,))
        reward_id = cur.fetchone()['id']
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "Premio riscattato!", "reward_id": reward_id, "loyalty_points_remaining": new_pts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/users/<int:user_id>/loyalty/rewards", methods=["GET"])
def get_rewards(user_id):
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT lr.id, lr.points_spent, lr.status, lr.created_at, lr.used_at,
                   b.booking_code, b.spot_id, b.start_time
            FROM loyalty_rewards lr
            LEFT JOIN bookings b ON lr.booking_id = b.id
            WHERE lr.user_id = %s ORDER BY lr.created_at DESC
        """, (user_id,))
        rows = cur.fetchall(); cur.close(); conn.close()
        result = []
        for r in rows:
            row = dict(r)
            row['created_at'] = row['created_at'].strftime('%d/%m/%Y %H:%M') if row['created_at'] else ''
            row['used_at']    = row['used_at'].strftime('%d/%m/%Y %H:%M')    if row['used_at']    else ''
            if row.get('start_time'):
                row['start_time'] = row['start_time'].strftime('%d/%m/%Y %H:%M')
            result.append(row)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── PRENOTAZIONI ───────────────────────────────────────────────────────────────
@app.route("/api/bookings", methods=["GET"])
def get_bookings():
    try:
        user_id = request.args.get("user_id")
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        q = """
            SELECT b.id, b.booking_code, b.spot_id, b.user_id, b.status,
                   b.duration_hours::FLOAT, b.total_cost::FLOAT, b.free_hour_used,
                   TO_CHAR(b.start_time,'DD/MM/YYYY, HH24:MI') AS start_time,
                   TO_CHAR(b.end_time,  'DD/MM/YYYY, HH24:MI') AS end_time,
                   b.end_time AS end_time_raw,
                   TO_CHAR(b.created_at,'DD/MM/YYYY, HH24:MI') AS created_at,
                   p.zone, p.parking_type, p.cost::FLOAT AS hourly_cost, p.floor_level,
                   u.name AS user_name, u.email AS user_email, u.plate AS user_plate
            FROM bookings b
            JOIN parking_spots p ON b.spot_id = p.id
            LEFT JOIN users u    ON b.user_id  = u.id
        """
        if user_id:
            cur.execute(q + " WHERE b.user_id=%s ORDER BY b.created_at DESC", (user_id,))
        else:
            cur.execute(q + " ORDER BY b.created_at DESC")
        rows = cur.fetchall(); cur.close(); conn.close()
        result = []
        for r in rows:
            row = dict(r)
            if row.get('end_time_raw'):
                row['end_time_raw'] = row['end_time_raw'].isoformat()
            result.append(row)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/bookings", methods=["POST"])
def create_booking():
    try:
        data = request.json
        for f in ["user_id","spot_id","start_time","end_time","duration_hours","total_cost"]:
            if data.get(f) is None:
                return jsonify({"error": f"Campo '{f}' obbligatorio"}), 400

        use_free = data.get("use_free_hour", False)
        reward_id = data.get("reward_id")

        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT status, maintenance FROM parking_spots WHERE id=%s", (data["spot_id"],))
        spot = cur.fetchone()
        if not spot:
            return jsonify({"error": "Posto non trovato"}), 404
        if spot["maintenance"]:
            return jsonify({"error": "Posto in manutenzione"}), 409

        cur.execute("""
            SELECT id FROM bookings WHERE spot_id=%s AND status='active'
            AND NOT (end_time <= %s OR start_time >= %s)
        """, (data["spot_id"], data["start_time"], data["end_time"]))
        if cur.fetchone():
            return jsonify({"error": "Posto già prenotato in questo intervallo"}), 409

        if use_free and reward_id:
            cur.execute("SELECT id FROM loyalty_rewards WHERE id=%s AND user_id=%s AND status='available'",
                        (reward_id, data["user_id"]))
            if not cur.fetchone():
                cur.close(); conn.close()
                return jsonify({"error": "Premio non valido o già usato"}), 400

        code = "PRK-" + secrets.token_hex(4).upper()
        cur.execute("""
            INSERT INTO bookings
                (booking_code, user_id, spot_id, start_time, end_time,
                 duration_hours, total_cost, free_hour_used, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active')
            RETURNING id, booking_code, TO_CHAR(created_at,'DD/MM/YYYY, HH24:MI') AS created_at
        """, (code, data["user_id"], data["spot_id"],
              data["start_time"], data["end_time"],
              data["duration_hours"], data["total_cost"], use_free))
        booking = dict(cur.fetchone())

        cur.execute("UPDATE parking_spots SET status='occupied', last_updated=NOW() WHERE id=%s", (data["spot_id"],))
        pts = int(float(data["duration_hours"]) * 100)
        cur.execute("UPDATE users SET loyalty_points = loyalty_points + %s WHERE id=%s", (pts, data["user_id"]))

        if use_free and reward_id:
            cur.execute("UPDATE loyalty_rewards SET status='used', booking_id=%s, used_at=NOW() WHERE id=%s",
                        (booking["id"], reward_id))

        conn.commit(); cur.close(); conn.close()
        return jsonify({
            "message": "Prenotazione creata",
            "booking_id": booking["id"],
            "booking_code": booking["booking_code"],
            "created_at": booking["created_at"],
            "loyalty_points_earned": pts
        }), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/bookings/<int:booking_id>", methods=["PUT"])
def update_booking(booking_id):
    try:
        data = request.json
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            UPDATE bookings SET end_time=%s, duration_hours=%s, total_cost=%s
            WHERE id=%s
            RETURNING id, booking_code,
                      TO_CHAR(end_time,'DD/MM/YYYY, HH24:MI') AS end_time,
                      end_time AS end_time_raw
        """, (data["end_time"], data["duration_hours"], data["total_cost"], booking_id))
        b = cur.fetchone(); conn.commit(); cur.close(); conn.close()
        if not b:
            return jsonify({"error": "Non trovata"}), 404
        row = dict(b)
        row['end_time_raw'] = row['end_time_raw'].isoformat()
        return jsonify(row)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/bookings/<int:booking_id>/cancel", methods=["POST"])
def cancel_booking(booking_id):
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT spot_id FROM bookings WHERE id=%s AND status='active'", (booking_id,))
        b = cur.fetchone()
        if not b:
            return jsonify({"error": "Non trovata o già cancellata"}), 404
        cur.execute("UPDATE bookings SET status='cancelled' WHERE id=%s", (booking_id,))
        cur.execute("UPDATE parking_spots SET status='free', last_updated=NOW() WHERE id=%s", (b["spot_id"],))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "Cancellata"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── MANUTENZIONE ───────────────────────────────────────────────────────────────
@app.route("/api/maintenance/schedule", methods=["GET"])
def get_maintenance():
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, zone, intervention_type AS type, priority, notes, status, operator,
                   TO_CHAR(scheduled_date,'DD/MM/YYYY') AS date,
                   scheduled_date::TEXT AS date_iso,
                   TO_CHAR(created_at,'DD/MM/YYYY HH24:MI') AS created_at,
                   TO_CHAR(updated_at,'DD/MM/YYYY HH24:MI') AS updated_at
            FROM maintenance_schedule ORDER BY scheduled_date DESC
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/maintenance/schedule", methods=["POST"])
def add_maintenance():
    try:
        data = request.json
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            INSERT INTO maintenance_schedule
                (zone, scheduled_date, operator, intervention_type, priority, notes, status)
            VALUES (%s,%s,%s,%s,%s,%s,'programmato')
            RETURNING id, TO_CHAR(scheduled_date,'DD/MM/YYYY') AS date
        """, (data['zone'], data['date_iso'], data['operator'],
              data.get('type','Pulizia ordinaria'),
              data.get('priority','normale'),
              data.get('notes','')))
        row = dict(cur.fetchone()); conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "Turno aggiunto", "id": row['id'], "date": row['date']}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/maintenance/schedule/<int:sid>", methods=["PUT"])
def update_maintenance(sid):
    try:
        data = request.json
        conn = db(); cur = conn.cursor()
        cur.execute("UPDATE maintenance_schedule SET status=%s, updated_at=NOW() WHERE id=%s",
                    (data['status'], sid))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "Aggiornato"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/maintenance/schedule/<int:sid>", methods=["DELETE"])
def delete_maintenance(sid):
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("DELETE FROM maintenance_schedule WHERE id=%s", (sid,))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": "Eliminato"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── STATISTICHE VISITE ─────────────────────────────────────────────────────────
@app.route("/api/stats/visits", methods=["GET"])
def get_visit_stats():
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT EXTRACT(HOUR FROM start_time)::INT AS hour, COUNT(*) AS count
            FROM bookings WHERE status IN ('active','completed')
            GROUP BY hour ORDER BY hour
        """)
        hourly = cur.fetchall()
        cur.execute("""
            SELECT EXTRACT(ISODOW FROM start_time)::INT AS dow, COUNT(*) AS count
            FROM bookings WHERE status IN ('active','completed')
            GROUP BY dow ORDER BY dow
        """)
        weekly = cur.fetchall(); cur.close(); conn.close()
        h_map = {r['hour']: int(r['count']) for r in hourly}
        w_map = {r['dow']:  int(r['count']) for r in weekly}
        days  = ['Lun','Mar','Mer','Gio','Ven','Sab','Dom']
        return jsonify({
            "hourly":  [{"hour": h, "count": h_map.get(h, 0)} for h in range(24)],
            "weekly":  [{"day": days[d-1], "dow": d, "count": w_map.get(d, 0)} for d in range(1,8)]
        })
    except Exception:
        return jsonify({
            "hourly": [{"hour": h, "count": int(max(0, 8*abs(math.sin(h/3.8)) + (12 if 8<=h<=10 else 8 if 14<=h<=16 else 3)))} for h in range(24)],
            "weekly": [{"day": d, "dow": i+1, "count": v} for i,(d,v) in enumerate(zip(['Lun','Mar','Mer','Gio','Ven','Sab','Dom'],[12,18,25,20,30,45,38]))]
        })

if __name__ == "__main__":
    app.run(debug=True, port=5000, host="0.0.0.0")
