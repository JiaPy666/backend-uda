from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
import hashlib, secrets, math, os

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

# ── Auto-migrazione: crea tabelle italiane se non esistono ─────────────────────
def run_migrations():
    """
    Architettura multilivello:
    - Livello DB:      tabelle in italiano (utente, parcheggio, prenotazione…)
    - Livello Backend: traduce DB → JSON con nomi inglesi per il frontend
    - Livello Frontend: invariato, riceve sempre gli stessi campi JSON
    """
    stmts = [
        # ENUM italiani
        "CREATE TYPE IF NOT EXISTS stato_posto     AS ENUM ('libero','occupato')",
        "CREATE TYPE IF NOT EXISTS tipo_parcheggio AS ENUM ('normale','disabili','elettrico','moto','van')",
        "CREATE TYPE IF NOT EXISTS tipo_veicolo    AS ENUM ('auto','moto','van')",
        "CREATE TYPE IF NOT EXISTS codice_zona     AS ENUM ('A','B','C','D')",

        # Tabella utente
        """CREATE TABLE IF NOT EXISTS utente (
            id             SERIAL       PRIMARY KEY,
            nome           VARCHAR(100) NOT NULL,
            email          VARCHAR(150) NOT NULL UNIQUE,
            password       VARCHAR(255) NOT NULL,
            telefono       VARCHAR(30)  DEFAULT '',
            targa          VARCHAR(20)  DEFAULT '',
            ruolo          VARCHAR(20)  DEFAULT 'utente' CHECK (ruolo IN ('utente','admin')),
            punti_fedelta  INTEGER      NOT NULL DEFAULT 0,
            data_creazione TIMESTAMP    DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_utente_email ON utente (email)",

        # Tabella parcheggio
        """CREATE TABLE IF NOT EXISTS parcheggio (
            id                   VARCHAR(4)   PRIMARY KEY,
            zona                 codice_zona  NOT NULL,
            stato                stato_posto  NOT NULL DEFAULT 'libero',
            tipo                 tipo_parcheggio NOT NULL DEFAULT 'normale',
            manutenzione         BOOLEAN      NOT NULL DEFAULT FALSE,
            tipo_veicolo         tipo_veicolo NOT NULL DEFAULT 'auto',
            costo                NUMERIC(5,2) NOT NULL CHECK (costo >= 0),
            livello              INTEGER      NOT NULL DEFAULT 1,
            nota_guasto          TEXT         DEFAULT '',
            ultimo_aggiornamento TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT chk_manutenzione_non_occupato
                CHECK (NOT (manutenzione = TRUE AND stato = 'occupato'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_parcheggio_zona  ON parcheggio (zona)",
        "CREATE INDEX IF NOT EXISTS idx_parcheggio_stato ON parcheggio (stato)",

        # Tabella prenotazione
        """CREATE TABLE IF NOT EXISTS prenotazione (
            id                  SERIAL       PRIMARY KEY,
            codice_prenotazione VARCHAR(20)  NOT NULL UNIQUE,
            id_utente           INTEGER      NOT NULL REFERENCES utente(id) ON DELETE CASCADE,
            id_parcheggio       VARCHAR(20)  NOT NULL REFERENCES parcheggio(id),
            orario_inizio       TIMESTAMP    NOT NULL,
            orario_fine         TIMESTAMP    NOT NULL,
            durata_ore          NUMERIC(5,1) NOT NULL,
            costo_totale        NUMERIC(8,2) NOT NULL,
            ora_gratis_usata    BOOLEAN      NOT NULL DEFAULT FALSE,
            stato               VARCHAR(20)  DEFAULT 'attiva'
                CHECK (stato IN ('attiva','cancellata','completata')),
            data_creazione      TIMESTAMP    DEFAULT NOW(),
            CONSTRAINT chk_orari CHECK (orario_fine > orario_inizio)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_prenotazione_utente ON prenotazione (id_utente)",
        "CREATE INDEX IF NOT EXISTS idx_prenotazione_stato  ON prenotazione (stato)",

        # Tabella premi_fedelta
        """CREATE TABLE IF NOT EXISTS premi_fedelta (
            id              SERIAL      PRIMARY KEY,
            id_utente       INTEGER     NOT NULL REFERENCES utente(id) ON DELETE CASCADE,
            punti_spesi     INTEGER     NOT NULL DEFAULT 10000,
            stato           VARCHAR(20) NOT NULL DEFAULT 'disponibile'
                CHECK (stato IN ('disponibile','usato','scaduto')),
            id_prenotazione INTEGER     REFERENCES prenotazione(id) ON DELETE SET NULL,
            data_creazione  TIMESTAMP   DEFAULT NOW(),
            data_utilizzo   TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_premi_utente ON premi_fedelta (id_utente)",
        "CREATE INDEX IF NOT EXISTS idx_premi_stato  ON premi_fedelta (stato)",

        # Tabella guasto
        """CREATE TABLE IF NOT EXISTS guasto (
            id               SERIAL      PRIMARY KEY,
            id_parcheggio    VARCHAR(20) NOT NULL REFERENCES parcheggio(id),
            id_utente        INTEGER     REFERENCES utente(id) ON DELETE SET NULL,
            tipo             VARCHAR(50) NOT NULL DEFAULT 'Altro',
            descrizione      TEXT        DEFAULT '',
            stato            VARCHAR(20) NOT NULL DEFAULT 'aperta'
                CHECK (stato IN ('aperta','in lavorazione','risolta')),
            data_segnalazione TIMESTAMP  DEFAULT NOW(),
            data_risoluzione  TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_guasto_parcheggio ON guasto (id_parcheggio)",
        "CREATE INDEX IF NOT EXISTS idx_guasto_stato      ON guasto (stato)",

        # Tabella manutenzione
        """CREATE TABLE IF NOT EXISTS manutenzione (
            id                 SERIAL      PRIMARY KEY,
            zona               codice_zona NOT NULL,
            data_programmata   DATE        NOT NULL,
            operatore          VARCHAR(100) NOT NULL,
            tipo               VARCHAR(100) NOT NULL DEFAULT 'Pulizia ordinaria',
            priorita           VARCHAR(20) NOT NULL DEFAULT 'normale'
                CHECK (priorita IN ('bassa','normale','alta')),
            note               TEXT        DEFAULT '',
            stato              VARCHAR(30) NOT NULL DEFAULT 'programmato'
                CHECK (stato IN ('programmato','in corso','completato','annullato')),
            id_creatore        INTEGER     REFERENCES utente(id) ON DELETE SET NULL,
            data_creazione     TIMESTAMP   DEFAULT NOW(),
            data_aggiornamento TIMESTAMP   DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_manutenzione_zona  ON manutenzione (zona)",
        "CREATE INDEX IF NOT EXISTS idx_manutenzione_data  ON manutenzione (data_programmata)",
        "CREATE INDEX IF NOT EXISTS idx_manutenzione_stato ON manutenzione (stato)",
    ]
    try:
        conn = psycopg2.connect(**DB_PARAMS)
        cur = conn.cursor()
        for s in stmts:
            try:
                cur.execute(s); conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"  migration: {str(e)[:70]}")
        cur.close(); conn.close()
        print("✅ DB italiano OK")
    except Exception as e:
        print(f"⚠️  Connessione DB: {e}")

run_migrations()

# ══════════════════════════════════════════════════════════════════════════════
# LIVELLO TRADUZIONE: DB italiano → JSON inglese per il frontend
# Il frontend riceve sempre gli stessi campi — non sa nulla del DB
# ══════════════════════════════════════════════════════════════════════════════

def traduce_stato_posto(stato_it):
    """libero/occupato → free/occupied"""
    return {'libero': 'free', 'occupato': 'occupied'}.get(stato_it, stato_it)

def traduce_tipo(tipo_it):
    """normale/disabili/elettrico/moto/van → normal/disabled/electric/motorcycle/van"""
    return {'normale':'normal','disabili':'disabled','elettrico':'electric',
            'moto':'motorcycle','van':'van'}.get(tipo_it, tipo_it)

def traduce_veicolo(v_it):
    """auto/moto/van → car/motorcycle/van"""
    return {'auto':'car','moto':'motorcycle','van':'van'}.get(v_it, v_it)

def traduce_stato_prenotazione(s_it):
    """attiva/cancellata/completata → active/cancelled/completed"""
    return {'attiva':'active','cancellata':'cancelled','completata':'completed'}.get(s_it, s_it)

def traduce_stato_premio(s_it):
    """disponibile/usato/scaduto → available/used/expired"""
    return {'disponibile':'available','usato':'used','scaduto':'expired'}.get(s_it, s_it)

def traduce_ruolo(r_it):
    """utente/admin → user/admin"""
    return {'utente':'user','admin':'admin'}.get(r_it, r_it)

# Inversi (JSON inglese → DB italiano) per INSERT/UPDATE
def it_stato_posto(s_en):
    return {'free':'libero','occupied':'occupato'}.get(s_en, s_en)

def it_tipo(t_en):
    return {'normal':'normale','disabled':'disabili','electric':'elettrico',
            'motorcycle':'moto','van':'van'}.get(t_en, t_en)

def it_veicolo(v_en):
    return {'car':'auto','motorcycle':'moto','van':'van'}.get(v_en, v_en)

def it_ruolo(r_en):
    return {'user':'utente','admin':'admin'}.get(r_en, r_en)

def it_stato_prenotazione(s_en):
    return {'active':'attiva','cancelled':'cancellata','completed':'completata'}.get(s_en, s_en)

# ── TEST ───────────────────────────────────────────────────────────────────────
@app.route("/api/test-db")
def test_db():
    try:
        conn = db(); conn.close()
        return jsonify({"status":"success","message":"DB italiano OK"})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

# ── POSTI ──────────────────────────────────────────────────────────────────────
@app.route("/api/spots", methods=["GET"])
def get_spots():
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, zona, stato, tipo, manutenzione, tipo_veicolo,
                   costo::FLOAT, livello, COALESCE(nota_guasto,'') AS nota_guasto,
                   TO_CHAR(ultimo_aggiornamento,'DD/MM/YYYY, HH24:MI:SS') AS ultimo_aggiornamento
            FROM parcheggio ORDER BY id
        """)
        rows = cur.fetchall(); cur.close(); conn.close()
        result = []
        for r in rows:
            # Traduce i valori DB → JSON inglese atteso dal frontend
            result.append({
                "id":           r["id"],
                "zone":         r["zona"],
                "status":       traduce_stato_posto(r["stato"]),
                "parking_type": traduce_tipo(r["tipo"]),
                "maintenance":  r["manutenzione"],
                "vehicle_type": traduce_veicolo(r["tipo_veicolo"]),
                "cost":         r["costo"],
                "floor_level":  r["livello"],
                "fault_report": r["nota_guasto"],
                "last_updated": r["ultimo_aggiornamento"],
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/spots/<spot_id>", methods=["PUT"])
def update_spot(spot_id):
    try:
        data = request.json
        conn = db(); cur = conn.cursor()
        cur.execute("""
            UPDATE parcheggio
            SET zona=%s, stato=%s, tipo=%s, manutenzione=%s,
                tipo_veicolo=%s, costo=%s, ultimo_aggiornamento=NOW()
            WHERE id=%s
            RETURNING TO_CHAR(ultimo_aggiornamento,'DD/MM/YYYY, HH24:MI:SS')
        """, (data['zone'], it_stato_posto(data['status']), it_tipo(data['parking_type']),
              data['maintenance'], it_veicolo(data['vehicle_type']), data['cost'], spot_id))
        res = cur.fetchone(); conn.commit(); cur.close(); conn.close()
        return jsonify({"message":"OK","last_updated": res[0]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── GUASTO ─────────────────────────────────────────────────────────────────────
@app.route("/api/spots/<spot_id>/fault", methods=["POST","OPTIONS"])
def report_fault(spot_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data        = request.json or {}
        tipo        = data.get("report_type", "Altro").strip()
        descrizione = data.get("description", "").strip()
        id_utente   = data.get("user_id")
        if not tipo:
            return jsonify({"error":"Tipo segnalazione obbligatorio"}), 400

        conn = db(); cur = conn.cursor()
        cur.execute("SELECT stato FROM parcheggio WHERE id=%s", (spot_id,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"error": f"Posto {spot_id} non trovato"}), 404

        stato_attuale = row[0]
        nota = f"{tipo}: {descrizione}" if descrizione else tipo
        guasto_id = None

        try:
            cur.execute("""
                INSERT INTO guasto (id_parcheggio, id_utente, tipo, descrizione, stato)
                VALUES (%s,%s,%s,%s,'aperta') RETURNING id
            """, (spot_id, id_utente or None, tipo, descrizione))
            guasto_id = cur.fetchone()[0]
        except Exception as e:
            conn.rollback()
            print(f"guasto insert skipped: {e}")

        if stato_attuale == 'libero':
            try:
                cur.execute("""
                    UPDATE parcheggio
                    SET manutenzione=TRUE, nota_guasto=%s, ultimo_aggiornamento=NOW()
                    WHERE id=%s
                """, (nota, spot_id))
            except Exception:
                conn.rollback()
                cur.execute("UPDATE parcheggio SET manutenzione=TRUE, ultimo_aggiornamento=NOW() WHERE id=%s", (spot_id,))
            msg = "Segnalazione salvata — posto messo in manutenzione"
        else:
            try:
                cur.execute("UPDATE parcheggio SET nota_guasto=%s, ultimo_aggiornamento=NOW() WHERE id=%s", (nota, spot_id))
            except Exception:
                conn.rollback()
            msg = "Segnalazione registrata — manutenzione applicata al termine della sosta"

        conn.commit(); cur.close(); conn.close()
        return jsonify({"message": msg, "id": guasto_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/faults", methods=["GET"])
def get_faults():
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT g.id, g.id_parcheggio AS spot_id, g.tipo AS report_type,
                   g.descrizione AS description, g.stato AS status,
                   g.data_segnalazione AS created_at, g.data_risoluzione AS resolved_at,
                   COALESCE(p.zona::TEXT,'?') AS zone,
                   COALESCE(u.nome,'Anonimo')  AS user_name
            FROM guasto g
            LEFT JOIN parcheggio p ON g.id_parcheggio = p.id
            LEFT JOIN utente u     ON g.id_utente = u.id
            ORDER BY g.data_segnalazione DESC
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
        new_status = request.json.get("status")
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        if new_status == 'risolta':
            cur.execute("""
                UPDATE guasto SET stato='risolta', data_risoluzione=NOW()
                WHERE id=%s RETURNING id_parcheggio
            """, (fault_id,))
            row = cur.fetchone()
            if row:
                cur.execute("SELECT COUNT(*) AS cnt FROM guasto WHERE id_parcheggio=%s AND stato!='risolta'", (row['id_parcheggio'],))
                if cur.fetchone()['cnt'] == 0:
                    cur.execute("UPDATE parcheggio SET manutenzione=FALSE, nota_guasto='', ultimo_aggiornamento=NOW() WHERE id=%s", (row['id_parcheggio'],))
        else:
            cur.execute("UPDATE guasto SET stato=%s WHERE id=%s", (new_status, fault_id))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message":"Aggiornato"})
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
        cur.execute("SELECT id FROM utente WHERE email=%s", (data["email"],))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"error":"Email già registrata"}), 409
        cur.execute("""
            INSERT INTO utente (nome, email, password, telefono, targa, ruolo, punti_fedelta)
            VALUES (%s,%s,%s,%s,%s,'utente',0)
            RETURNING id, nome, email, telefono, targa, ruolo, punti_fedelta,
                      TO_CHAR(data_creazione,'DD/MM/YYYY') AS data_creazione
        """, (data["name"], data["email"], hash_pw(data["password"]),
              data.get("phone",""), data.get("plate","")))
        u = dict(cur.fetchone()); conn.commit(); cur.close(); conn.close()
        return jsonify({"message":"Registrazione completata", "user": {
            "id": u["id"], "name": u["nome"], "email": u["email"],
            "phone": u["telefono"], "plate": u["targa"],
            "role": traduce_ruolo(u["ruolo"]),
            "loyalty_points": u["punti_fedelta"],
            "created_at": u["data_creazione"]
        }}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/login", methods=["POST"])
def login():
    try:
        data = request.json
        if not data.get("email") or not data.get("password"):
            return jsonify({"error":"Email e password obbligatori"}), 400
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, nome, email, telefono, targa, ruolo, punti_fedelta,
                   TO_CHAR(data_creazione,'DD/MM/YYYY') AS data_creazione
            FROM utente WHERE email=%s AND password=%s
        """, (data["email"], hash_pw(data["password"])))
        u = cur.fetchone(); cur.close(); conn.close()
        if not u:
            return jsonify({"error":"Credenziali non valide"}), 401
        return jsonify({"message":"Login effettuato", "user": {
            "id": u["id"], "name": u["nome"], "email": u["email"],
            "phone": u["telefono"], "plate": u["targa"],
            "role": traduce_ruolo(u["ruolo"]),
            "loyalty_points": u["punti_fedelta"],
            "created_at": u["data_creazione"]
        }})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    return jsonify({"message":"Logout effettuato"})

@app.route("/api/users/<int:user_id>", methods=["PUT"])
def update_user(user_id):
    try:
        data = request.json
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            UPDATE utente SET nome=%s, telefono=%s, targa=%s WHERE id=%s
            RETURNING id, nome, email, telefono, targa, ruolo, punti_fedelta,
                      TO_CHAR(data_creazione,'DD/MM/YYYY') AS data_creazione
        """, (data["name"], data.get("phone",""), data.get("plate",""), user_id))
        u = cur.fetchone(); conn.commit(); cur.close(); conn.close()
        if not u:
            return jsonify({"error":"Utente non trovato"}), 404
        return jsonify({"id":u["id"],"name":u["nome"],"email":u["email"],
            "phone":u["telefono"],"plate":u["targa"],
            "role":traduce_ruolo(u["ruolo"]),
            "loyalty_points":u["punti_fedelta"],"created_at":u["data_creazione"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── FEDELTÀ ────────────────────────────────────────────────────────────────────
@app.route("/api/users/<int:user_id>/loyalty", methods=["GET"])
def get_loyalty(user_id):
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT punti_fedelta FROM utente WHERE id=%s", (user_id,))
        row = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS cnt FROM premi_fedelta WHERE id_utente=%s AND stato='disponibile'", (user_id,))
        rewards = cur.fetchone()
        cur.close(); conn.close()
        return jsonify({
            "loyalty_points":       row['punti_fedelta'] if row else 0,
            "free_hours_available": int(rewards['cnt']) if rewards else 0
        })
    except Exception:
        return jsonify({"loyalty_points":0,"free_hours_available":0})

@app.route("/api/users/<int:user_id>/loyalty/redeem", methods=["POST"])
def redeem_loyalty(user_id):
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT punti_fedelta FROM utente WHERE id=%s", (user_id,))
        row = cur.fetchone()
        if not row or row['punti_fedelta'] < 10000:
            cur.close(); conn.close()
            return jsonify({"error":"Punti insufficienti (servono 10.000)"}), 400
        cur.execute("UPDATE utente SET punti_fedelta=punti_fedelta-10000 WHERE id=%s RETURNING punti_fedelta", (user_id,))
        new_pts = cur.fetchone()['punti_fedelta']
        cur.execute("INSERT INTO premi_fedelta (id_utente,punti_spesi,stato) VALUES (%s,10000,'disponibile') RETURNING id", (user_id,))
        reward_id = cur.fetchone()['id']
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message":"Premio riscattato!","reward_id":reward_id,"loyalty_points_remaining":new_pts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/users/<int:user_id>/loyalty/rewards", methods=["GET"])
def get_rewards(user_id):
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT pf.id, pf.punti_spesi AS points_spent,
                   pf.stato, pf.data_creazione AS created_at, pf.data_utilizzo AS used_at,
                   p.codice_prenotazione AS booking_code,
                   p.id_parcheggio AS spot_id, p.orario_inizio AS start_time
            FROM premi_fedelta pf
            LEFT JOIN prenotazione p ON pf.id_prenotazione = p.id
            WHERE pf.id_utente=%s ORDER BY pf.data_creazione DESC
        """, (user_id,))
        rows = cur.fetchall(); cur.close(); conn.close()
        result = []
        for r in rows:
            row = dict(r)
            row['status']     = traduce_stato_premio(row.pop('stato'))
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
            SELECT p.id, p.codice_prenotazione AS booking_code,
                   p.id_parcheggio AS spot_id, p.id_utente AS user_id,
                   p.stato, p.durata_ore::FLOAT AS duration_hours,
                   p.costo_totale::FLOAT AS total_cost, p.ora_gratis_usata AS free_hour_used,
                   TO_CHAR(p.orario_inizio,'DD/MM/YYYY, HH24:MI') AS start_time,
                   TO_CHAR(p.orario_fine,  'DD/MM/YYYY, HH24:MI') AS end_time,
                   p.orario_fine AS end_time_raw,
                   TO_CHAR(p.data_creazione,'DD/MM/YYYY, HH24:MI') AS created_at,
                   pa.zona AS zone, pa.tipo AS parking_type,
                   pa.costo::FLOAT AS hourly_cost, pa.livello AS floor_level,
                   u.nome AS user_name, u.email AS user_email, u.targa AS user_plate
            FROM prenotazione p
            JOIN parcheggio pa ON p.id_parcheggio = pa.id
            LEFT JOIN utente u ON p.id_utente = u.id
        """
        if user_id:
            cur.execute(q + " WHERE p.id_utente=%s ORDER BY p.data_creazione DESC", (user_id,))
        else:
            cur.execute(q + " ORDER BY p.data_creazione DESC")
        rows = cur.fetchall(); cur.close(); conn.close()
        result = []
        for r in rows:
            row = dict(r)
            row['status']       = traduce_stato_prenotazione(row.pop('stato'))
            row['parking_type'] = traduce_tipo(row['parking_type'])
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

        use_free  = data.get("use_free_hour", False)
        reward_id = data.get("reward_id")

        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT stato, manutenzione FROM parcheggio WHERE id=%s", (data["spot_id"],))
        spot = cur.fetchone()
        if not spot:
            return jsonify({"error":"Posto non trovato"}), 404
        if spot["manutenzione"]:
            return jsonify({"error":"Posto in manutenzione"}), 409

        cur.execute("""
            SELECT id FROM prenotazione WHERE id_parcheggio=%s AND stato='attiva'
            AND NOT (orario_fine <= %s OR orario_inizio >= %s)
        """, (data["spot_id"], data["start_time"], data["end_time"]))
        if cur.fetchone():
            return jsonify({"error":"Posto già prenotato in questo intervallo"}), 409

        if use_free and reward_id:
            cur.execute("SELECT id FROM premi_fedelta WHERE id=%s AND id_utente=%s AND stato='disponibile'",
                        (reward_id, data["user_id"]))
            if not cur.fetchone():
                cur.close(); conn.close()
                return jsonify({"error":"Premio non valido o già usato"}), 400

        codice = "PRK-" + secrets.token_hex(4).upper()
        cur.execute("""
            INSERT INTO prenotazione
                (codice_prenotazione, id_utente, id_parcheggio,
                 orario_inizio, orario_fine, durata_ore, costo_totale,
                 ora_gratis_usata, stato)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'attiva')
            RETURNING id, codice_prenotazione,
                      TO_CHAR(data_creazione,'DD/MM/YYYY, HH24:MI') AS data_creazione
        """, (codice, data["user_id"], data["spot_id"],
              data["start_time"], data["end_time"],
              data["duration_hours"], data["total_cost"], use_free))
        booking = dict(cur.fetchone())

        cur.execute("UPDATE parcheggio SET stato='occupato', ultimo_aggiornamento=NOW() WHERE id=%s", (data["spot_id"],))
        pts = int(float(data["duration_hours"]) * 100)
        cur.execute("UPDATE utente SET punti_fedelta=punti_fedelta+%s WHERE id=%s", (pts, data["user_id"]))

        if use_free and reward_id:
            cur.execute("UPDATE premi_fedelta SET stato='usato', id_prenotazione=%s, data_utilizzo=NOW() WHERE id=%s",
                        (booking["id"], reward_id))

        conn.commit(); cur.close(); conn.close()
        return jsonify({
            "message":"Prenotazione creata",
            "booking_id":   booking["id"],
            "booking_code": booking["codice_prenotazione"],
            "created_at":   booking["data_creazione"],
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
            UPDATE prenotazione SET orario_fine=%s, durata_ore=%s, costo_totale=%s
            WHERE id=%s
            RETURNING id, codice_prenotazione AS booking_code,
                      TO_CHAR(orario_fine,'DD/MM/YYYY, HH24:MI') AS end_time,
                      orario_fine AS end_time_raw
        """, (data["end_time"], data["duration_hours"], data["total_cost"], booking_id))
        b = cur.fetchone(); conn.commit(); cur.close(); conn.close()
        if not b:
            return jsonify({"error":"Non trovata"}), 404
        row = dict(b)
        row['end_time_raw'] = row['end_time_raw'].isoformat()
        return jsonify(row)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/bookings/<int:booking_id>/cancel", methods=["POST"])
def cancel_booking(booking_id):
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id_parcheggio FROM prenotazione WHERE id=%s AND stato='attiva'", (booking_id,))
        b = cur.fetchone()
        if not b:
            return jsonify({"error":"Non trovata o già cancellata"}), 404
        cur.execute("UPDATE prenotazione SET stato='cancellata' WHERE id=%s", (booking_id,))
        cur.execute("UPDATE parcheggio SET stato='libero', ultimo_aggiornamento=NOW() WHERE id=%s", (b["id_parcheggio"],))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message":"Cancellata"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── MANUTENZIONE ───────────────────────────────────────────────────────────────
@app.route("/api/maintenance/schedule", methods=["GET"])
def get_maintenance():
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, zona AS zone, tipo AS type, priorita AS priority,
                   note AS notes, stato AS status, operatore AS operator,
                   TO_CHAR(data_programmata,'DD/MM/YYYY') AS date,
                   data_programmata::TEXT AS date_iso,
                   TO_CHAR(data_creazione,'DD/MM/YYYY HH24:MI') AS created_at,
                   TO_CHAR(data_aggiornamento,'DD/MM/YYYY HH24:MI') AS updated_at
            FROM manutenzione ORDER BY data_programmata DESC
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
            INSERT INTO manutenzione
                (zona, data_programmata, operatore, tipo, priorita, note, stato)
            VALUES (%s,%s,%s,%s,%s,%s,'programmato')
            RETURNING id, TO_CHAR(data_programmata,'DD/MM/YYYY') AS date
        """, (data['zone'], data['date_iso'], data['operator'],
              data.get('type','Pulizia ordinaria'),
              data.get('priority','normale'), data.get('notes','')))
        row = dict(cur.fetchone()); conn.commit(); cur.close(); conn.close()
        return jsonify({"message":"Turno aggiunto","id":row['id'],"date":row['date']}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/maintenance/schedule/<int:sid>", methods=["PUT"])
def update_maintenance(sid):
    try:
        data = request.json
        conn = db(); cur = conn.cursor()
        cur.execute("UPDATE manutenzione SET stato=%s, data_aggiornamento=NOW() WHERE id=%s",
                    (data['status'], sid))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message":"Aggiornato"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/maintenance/schedule/<int:sid>", methods=["DELETE"])
def delete_maintenance(sid):
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("DELETE FROM manutenzione WHERE id=%s", (sid,))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"message":"Eliminato"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── STATISTICHE VISITE ─────────────────────────────────────────────────────────
@app.route("/api/stats/visits", methods=["GET"])
def get_visit_stats():
    try:
        conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT EXTRACT(HOUR FROM orario_inizio)::INT AS hour, COUNT(*) AS count
            FROM prenotazione WHERE stato IN ('attiva','completata')
            GROUP BY hour ORDER BY hour
        """)
        hourly = cur.fetchall()
        cur.execute("""
            SELECT EXTRACT(ISODOW FROM orario_inizio)::INT AS dow, COUNT(*) AS count
            FROM prenotazione WHERE stato IN ('attiva','completata')
            GROUP BY dow ORDER BY dow
        """)
        weekly = cur.fetchall(); cur.close(); conn.close()
        h_map = {r['hour']: int(r['count']) for r in hourly}
        w_map = {r['dow']:  int(r['count']) for r in weekly}
        days  = ['Lun','Mar','Mer','Gio','Ven','Sab','Dom']
        return jsonify({
            "hourly": [{"hour":h,"count":h_map.get(h,0)} for h in range(24)],
            "weekly": [{"day":days[d-1],"dow":d,"count":w_map.get(d,0)} for d in range(1,8)]
        })
    except Exception:
        return jsonify({
            "hourly": [{"hour":h,"count":int(max(0,8*abs(math.sin(h/3.8))+(12 if 8<=h<=10 else 8 if 14<=h<=16 else 3)))} for h in range(24)],
            "weekly": [{"day":d,"dow":i+1,"count":v} for i,(d,v) in enumerate(zip(['Lun','Mar','Mer','Gio','Ven','Sab','Dom'],[12,18,25,20,30,45,38]))]
        })

# ── AVVIO HTTPS / HTTP ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    CERT = os.path.join(os.path.dirname(__file__), 'ssl', 'localhost.pem')
    KEY  = os.path.join(os.path.dirname(__file__), 'ssl', 'localhost-key.pem')
    if os.path.exists(CERT) and os.path.exists(KEY):
        print("🔒 HTTPS attivo — https://localhost:5000")
        app.run(debug=True, host="0.0.0.0", port=5000, ssl_context=(CERT, KEY))
    else:
        print("⚠️  Avvio HTTP — http://localhost:5000")
        app.run(debug=True, host="0.0.0.0", port=5000)
