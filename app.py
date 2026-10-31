import json
import os
import secrets
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
DATABASE = os.path.join(DATA_DIR, "database.db")
EVIDENCE_UPLOAD_DIR = os.path.join(DATA_DIR, "uploads", "prove")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower()
    in {"1", "true", "yes"},
)
MAX_IMAGE_SIZE = 10 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_SIZE + 1024 * 1024

CATEGORIE_GRADI_AGENTI = [
    (
        "Categoria 1 – Appuntati e Carabinieri",
        [
            "Carabiniere",
            "Carabiniere Scelto",
            "Appuntato",
            "Appuntato Scelto",
            "Appuntato Scelto – Qualifica Speciale",
        ],
    ),
    (
        "Categoria 2 – Sovrintendenti",
        [
            "Vicebrigadiere",
            "Brigadiere",
            "Brigadiere Capo",
            "Brigadiere Capo – Qualifica Speciale",
        ],
    ),
    (
        "Categoria 3 – Ispettori",
        [
            "Maresciallo",
            "Maresciallo Ordinario",
            "Maresciallo Capo",
            "Maresciallo Maggiore",
            "Luogotenente",
            "Luogotenente – Carica Speciale",
        ],
    ),
    (
        "Categoria 4 – Ufficiali inferiori",
        ["Sottotenente", "Tenente", "Capitano"],
    ),
    (
        "Categoria 4 – Ufficiali superiori",
        ["Maggiore", "Tenente Colonnello", "Colonnello"],
    ),
    (
        "Categoria 4 – Ufficiali generali",
        [
            "Generale di Brigata",
            "Generale di Divisione",
            "Generale di Corpo d'Armata – Comandante Generale",
        ],
    ),
]
GRADI_AGENTI = [
    grado
    for _, gradi_categoria in CATEGORIE_GRADI_AGENTI
    for grado in gradi_categoria
]
RUOLI_ACCESSO_AGENTI = ["Agente", "Staff", "Comandante", "Vice Comandante"]
RUOLI_GESTIONE_AGENTI = ("Staff", "Comandante", "Vice Comandante")
RUOLO_STAFF = "Staff"

IMAGE_SIGNATURES = {
    ".jpg": lambda header: header.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda header: header.startswith(b"\xff\xd8\xff"),
    ".png": lambda header: header.startswith(b"\x89PNG\r\n\x1a\n"),
    ".gif": lambda header: header.startswith((b"GIF87a", b"GIF89a")),
    ".webp": lambda header: (
        len(header) >= 12
        and header.startswith(b"RIFF")
        and header[8:12] == b"WEBP"
    ),
}


def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


@app.before_request
def verifica_sessione_agente():
    agente_id = session.get("agente_id")
    if agente_id is None or request.endpoint == "static":
        return None

    conn = get_db_connection()
    agente = conn.execute(
        """
        SELECT nome, grado, ruolo_accesso, attivo, eliminato
        FROM agenti
        WHERE id = ?
        """,
        (agente_id,),
    ).fetchone()
    conn.close()
    if agente is None or not agente["attivo"] or agente["eliminato"]:
        session.clear()
        if request.endpoint != "login":
            return redirect(url_for("login"))
        return None

    session["nome"] = agente["nome"]
    session["grado"] = agente["grado"]
    session["ruolo_accesso"] = agente["ruolo_accesso"]
    return None


def invia_notifica_discord(modulo, titolo, campi):
    variabile = f"DISCORD_WEBHOOK_{modulo.upper()}"
    webhook_url = os.environ.get(variabile, "").strip()
    if not webhook_url:
        app.logger.warning("Webhook Discord non configurato: %s", variabile)
        return "missing"

    parsed_url = urlparse(webhook_url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname not in {"discord.com", "discordapp.com"}
        or not parsed_url.path.startswith("/api/webhooks/")
    ):
        app.logger.error("URL non valido per %s", variabile)
        return "failed"

    embed_fields = [
        {
            "name": str(nome)[:256],
            "value": str(valore or "—")[:1024],
            "inline": False,
        }
        for nome, valore in campi
    ]
    payload = {
        "username": "F.D.O | Centro operativo",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": titolo[:256],
                "color": 3447003,
                "fields": embed_fields[:25],
                "footer": {"text": "F.D.O | Notifica automatica"},
            }
        ],
    }
    request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    webhook_request = Request(
        webhook_url,
        data=request_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(webhook_request, timeout=5) as response:
            if not 200 <= response.status < 300:
                app.logger.error(
                    "Discord ha rifiutato la notifica %s con HTTP %s",
                    variabile,
                    response.status,
                )
                return "failed"
    except HTTPError as error:
        app.logger.error(
            "Discord ha rifiutato la notifica %s con HTTP %s",
            variabile,
            error.code,
        )
        return "failed"
    except (URLError, TimeoutError, OSError) as error:
        app.logger.error(
            "Invio notifica %s non riuscito (%s)",
            variabile,
            type(error).__name__,
        )
        return "failed"

    return "sent"


def avvisa_esito_notifica_discord(modulo, esito):
    if esito == "missing":
        flash(
            f"Registrazione salvata. Notifica Discord non inviata: configura DISCORD_WEBHOOK_{modulo.upper()}.",
            "warning",
        )
    elif esito == "failed":
        flash(
            "Registrazione salvata, ma l'invio della notifica Discord non è riuscito. Controlla la configurazione e i log.",
            "warning",
        )


def puo_gestire_agenti():
    agente_id = session.get("agente_id")
    if agente_id is None:
        return False

    conn = get_db_connection()
    agente = conn.execute(
        "SELECT ruolo_accesso, attivo, eliminato FROM agenti WHERE id = ?",
        (agente_id,),
    ).fetchone()
    conn.close()
    if agente is None or not agente["attivo"] or agente["eliminato"]:
        return False

    session["ruolo_accesso"] = agente["ruolo_accesso"]
    return agente["ruolo_accesso"] in RUOLI_GESTIONE_AGENTI


def e_staff_corrente():
    agente_id = session.get("agente_id")
    if agente_id is None:
        return False

    conn = get_db_connection()
    agente = conn.execute(
        "SELECT ruolo_accesso, attivo, eliminato FROM agenti WHERE id = ?",
        (agente_id,),
    ).fetchone()
    conn.close()
    if agente is None or not agente["attivo"] or agente["eliminato"]:
        return False

    session["ruolo_accesso"] = agente["ruolo_accesso"]
    return agente["ruolo_accesso"] == RUOLO_STAFF


# =========================
# STORICO DELLE ATTIVITÀ
# =========================

def registra_storico(agente_id, azione, descrizione):
    conn = get_db_connection()

    conn.execute(
        """
        INSERT INTO storico (agente_id, azione, descrizione)
        VALUES (?, ?, ?)
        """,
        (agente_id, azione, descrizione)
    )

    conn.commit()
    conn.close()


# =========================
# CREAZIONE DATABASE
# =========================

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_db_connection()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agenti (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            nome TEXT NOT NULL,
            grado TEXT NOT NULL,
            attivo INTEGER DEFAULT 1,
            ruolo_accesso TEXT NOT NULL DEFAULT 'Agente',
            eliminato INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    agenti_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(agenti)").fetchall()
    }
    if "ruolo_accesso" not in agenti_columns:
        conn.execute(
            "ALTER TABLE agenti ADD COLUMN ruolo_accesso TEXT NOT NULL DEFAULT 'Agente'"
        )
        conn.execute(
            "UPDATE agenti SET ruolo_accesso = 'Staff' WHERE username = 'admin'"
        )
    if "eliminato" not in agenti_columns:
        conn.execute(
            "ALTER TABLE agenti ADD COLUMN eliminato INTEGER NOT NULL DEFAULT 0"
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS criminali (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            discord_id TEXT,
            crimini TEXT,
            stato TEXT DEFAULT 'Libero',
            note TEXT,
            data_creazione TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mandati (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            criminale_id INTEGER,
            motivazione TEXT NOT NULL,
            prove TEXT,
            agente_id INTEGER,
            data_emissione TEXT DEFAULT CURRENT_TIMESTAMP,
            stato TEXT DEFAULT 'Attivo',
            approvato_da INTEGER,
            FOREIGN KEY (criminale_id) REFERENCES criminali (id),
            FOREIGN KEY (agente_id) REFERENCES agenti (id),
            FOREIGN KEY (approvato_da) REFERENCES agenti (id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS storico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agente_id INTEGER,
            azione TEXT NOT NULL,
            descrizione TEXT NOT NULL,
            data TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (agente_id) REFERENCES agenti (id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS arresti (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            criminale_id INTEGER,
            agente_id INTEGER,
            motivo TEXT NOT NULL,
            prova TEXT,
            stato TEXT DEFAULT 'Eseguito',
            data TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (criminale_id) REFERENCES criminali (id),
            FOREIGN KEY (agente_id) REFERENCES agenti (id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS denunce (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            criminale_id INTEGER,
            agente_id INTEGER,
            denunciante TEXT,
            descrizione TEXT NOT NULL,
            stato TEXT DEFAULT 'Aperta',
            data TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (criminale_id) REFERENCES criminali (id),
            FOREIGN KEY (agente_id) REFERENCES agenti (id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dichiarazioni (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dichiarante TEXT NOT NULL,
            persona_coinvolta TEXT,
            contenuto TEXT NOT NULL,
            agente_id INTEGER,
            data TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (agente_id) REFERENCES agenti (id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rapporti (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titolo TEXT NOT NULL,
            persona_coinvolta TEXT,
            descrizione TEXT NOT NULL,
            stato TEXT DEFAULT 'Inviato',
            agente_id INTEGER,
            data TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (agente_id) REFERENCES agenti (id)
        )
        """
    )

    denunce_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(denunce)").fetchall()
    }
    if "denunciante" not in denunce_columns:
        conn.execute("ALTER TABLE denunce ADD COLUMN denunciante TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prove (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            criminale_id INTEGER,
            agente_id INTEGER,
            tipo TEXT NOT NULL,
            descrizione TEXT NOT NULL,
            data TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (criminale_id) REFERENCES criminali (id),
            FOREIGN KEY (agente_id) REFERENCES agenti (id)
        )
        """
    )

    prove_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(prove)").fetchall()
    }
    if "image_path" not in prove_columns:
        conn.execute("ALTER TABLE prove ADD COLUMN image_path TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS multe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            criminale_id INTEGER,
            agente_id INTEGER,
            importo TEXT NOT NULL,
            motivo TEXT NOT NULL,
            stato TEXT DEFAULT 'Da pagare',
            data TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (criminale_id) REFERENCES criminali (id),
            FOREIGN KEY (agente_id) REFERENCES agenti (id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS veicoli (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            targa TEXT NOT NULL UNIQUE,
            proprietario TEXT,
            stato TEXT DEFAULT 'Controllato',
            note TEXT,
            data TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()
    conn.close()


def save_evidence_image(upload):
    if upload is None or not upload.filename:
        return None, None

    extension = os.path.splitext(upload.filename)[1].lower()
    signature_check = IMAGE_SIGNATURES.get(extension)
    if signature_check is None:
        return None, "Formato non supportato. Carica un'immagine JPG, PNG, GIF o WEBP."

    upload.stream.seek(0, os.SEEK_END)
    if upload.stream.tell() > MAX_IMAGE_SIZE:
        upload.stream.seek(0)
        return None, "L'immagine supera il limite consentito di 10 MB."
    upload.stream.seek(0)

    header = upload.stream.read(12)
    upload.stream.seek(0)
    if not signature_check(header):
        return None, "Il file selezionato non sembra un'immagine valida."

    filename = f"{uuid.uuid4().hex}{extension}"
    os.makedirs(EVIDENCE_UPLOAD_DIR, exist_ok=True)
    upload.save(os.path.join(EVIDENCE_UPLOAD_DIR, filename))
    return filename, None


def existing_criminal_id(conn, value):
    try:
        criminal_id = int(value)
    except (TypeError, ValueError):
        return None

    row = conn.execute(
        "SELECT id FROM criminali WHERE id = ?", (criminal_id,)
    ).fetchone()
    return criminal_id if row else None


# =========================
# CREAZIONE ADMIN
# =========================

def create_admin():
    """Crea l'amministratore iniziale solo se non esiste."""

    conn = get_db_connection()

    admin = conn.execute(
        "SELECT * FROM agenti WHERE username = ?",
        ("admin",),
    ).fetchone()

    if admin is None:
        password = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
        if len(password) < 12:
            conn.close()
            raise RuntimeError(
                "Per creare l'amministratore iniziale imposta "
                "INITIAL_ADMIN_PASSWORD con almeno 12 caratteri."
            )
        password_hash = generate_password_hash(password)

        conn.execute(
            """
            INSERT INTO agenti (username, password, nome, grado, ruolo_accesso)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "admin",
                password_hash,
                "Amministratore",
                "Generale di Corpo d'Armata – Comandante Generale",
                "Staff",
            ),
        )

        conn.commit()
        print("Amministratore iniziale creato.")

    conn.close()


# =========================
# DASHBOARD
# =========================

@app.route("/")
def home():
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()
    criminali_count = conn.execute("SELECT COUNT(*) AS total FROM criminali").fetchone()["total"]
    mandati_count = conn.execute("SELECT COUNT(*) AS total FROM mandati WHERE stato = 'Attivo'").fetchone()["total"]
    denunce_count = conn.execute("SELECT COUNT(*) AS total FROM denunce WHERE stato != 'Chiusa'").fetchone()["total"]
    agenti_count = conn.execute(
        "SELECT COUNT(*) AS total FROM agenti WHERE attivo = 1 AND eliminato = 0"
    ).fetchone()["total"]
    denunce_recenti = conn.execute(
        """
        SELECT denunce.id, denunce.descrizione, denunce.stato, denunce.data,
               criminali.nome AS criminale
        FROM denunce
        LEFT JOIN criminali ON criminali.id = denunce.criminale_id
        ORDER BY denunce.id DESC
        LIMIT 4
        """
    ).fetchall()
    mandati_attivi = conn.execute(
        """
        SELECT mandati.id, mandati.motivazione, mandati.data_emissione,
               criminali.nome AS criminale
        FROM mandati
        LEFT JOIN criminali ON criminali.id = mandati.criminale_id
        WHERE mandati.stato = 'Attivo'
        ORDER BY mandati.id DESC
        LIMIT 4
        """
    ).fetchall()
    agenti_attivi = conn.execute(
        """
        SELECT nome, grado, username
        FROM agenti
        WHERE attivo = 1 AND eliminato = 0
        ORDER BY nome
        LIMIT 8
        """
    ).fetchall()
    conn.close()

    return render_template(
        "dashboard.html",
        nome=session["nome"],
        grado=session["grado"],
        criminali_count=criminali_count,
        mandati_count=mandati_count,
        denunce_count=denunce_count,
        agenti_count=agenti_count,
        denunce_recenti=denunce_recenti,
        mandati_attivi=mandati_attivi,
        agenti_attivi=agenti_attivi,
    )


# =========================
# LOGIN
# =========================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db_connection()

        agente = conn.execute(
            """
            SELECT * FROM agenti
            WHERE username = ? AND attivo = 1 AND eliminato = 0
            """,
            (username,),
        ).fetchone()

        conn.close()

        if agente and check_password_hash(agente["password"], password):
            session["agente_id"] = agente["id"]
            session["nome"] = agente["nome"]
            session["grado"] = agente["grado"]
            session["ruolo_accesso"] = agente["ruolo_accesso"]

            return redirect(url_for("home"))

        return render_template(
            "login.html",
            errore="Username o password non corretti.",
        )

    return render_template("login.html")


# =========================
# LOGOUT
# =========================

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =========================
# SCHEDE CRIMINALI
# =========================

@app.route("/criminali", methods=["GET", "POST"])
def criminali():
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    # CREAZIONE DI UNA NUOVA SCHEDA
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        discord_id = request.form.get("discord_id", "").strip()
        crimini = request.form.get("crimini", "").strip()
        stato = request.form.get("stato", "Libero").strip()
        note = request.form.get("note", "").strip()

        if nome and crimini:
            conn.execute(
                """
                INSERT INTO criminali
                (nome, discord_id, crimini, stato, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    nome,
                    discord_id or None,
                    crimini,
                    stato,
                    note or None,
                ),
            )

            conn.commit()

            # Registriamo l'operazione nello storico
            registra_storico(
                session["agente_id"],
                "CREAZIONE SCHEDA",
                f"Ha creato la scheda criminale di: {nome}"
            )
            flash("Scheda registrata correttamente.", "success")
        else:
            flash("Inserisci nome e descrizione dei crimini.", "error")

        conn.close()

        return redirect(url_for("criminali"))

    # RICERCA
    ricerca = request.args.get("ricerca", "").strip()

    if ricerca:
        persone = conn.execute(
            """
            SELECT * FROM criminali
            WHERE nome LIKE ? OR discord_id LIKE ?
            ORDER BY id DESC
            """,
            (f"%{ricerca}%", f"%{ricerca}%"),
        ).fetchall()
    else:
        persone = conn.execute(
            "SELECT * FROM criminali ORDER BY id DESC"
        ).fetchall()

    conn.close()

    return render_template(
        "criminali.html",
        criminali=persone,
        ricerca=ricerca
    )


# =========================
# DETTAGLIO SCHEDA
# =========================

@app.route("/criminali/<int:criminale_id>")
def dettaglio_criminale(criminale_id):
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    criminale = conn.execute(
        "SELECT * FROM criminali WHERE id = ?",
        (criminale_id,),
    ).fetchone()

    conn.close()

    if criminale is None:
        return "Scheda non trovata.", 404

    return render_template(
        "dettaglio_criminale.html",
        criminale=criminale
    )


# =========================
# MODIFICA SCHEDA
# =========================

@app.route(
    "/criminali/<int:criminale_id>/modifica",
    methods=["GET", "POST"]
)
def modifica_criminale(criminale_id):

    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    criminale = conn.execute(
        "SELECT * FROM criminali WHERE id = ?",
        (criminale_id,),
    ).fetchone()

    if criminale is None:
        conn.close()
        return "Scheda non trovata.", 404

    if request.method == "POST":

        nome = request.form.get("nome", "").strip()
        discord_id = request.form.get("discord_id", "").strip()
        crimini = request.form.get("crimini", "").strip()
        stato = request.form.get("stato", "Libero").strip()
        note = request.form.get("note", "").strip()

        if nome and crimini:

            conn.execute(
                """
                UPDATE criminali
                SET nome = ?,
                    discord_id = ?,
                    crimini = ?,
                    stato = ?,
                    note = ?
                WHERE id = ?
                """,
                (
                    nome,
                    discord_id or None,
                    crimini,
                    stato,
                    note or None,
                    criminale_id,
                ),
            )

            conn.commit()

            # Registriamo la modifica
            registra_storico(
                session["agente_id"],
                "MODIFICA SCHEDA",
                f"Ha modificato la scheda criminale di: {nome}"
            )
            flash("Scheda aggiornata correttamente.", "success")
        else:
            flash("Nome e descrizione dei crimini sono obbligatori.", "error")

        conn.close()

        return redirect(
            url_for(
                "dettaglio_criminale",
                criminale_id=criminale_id
            )
        )

    conn.close()

    return render_template(
        "modifica_criminale.html",
        criminale=criminale
    )


# =========================
# ELIMINAZIONE SCHEDA
# =========================

@app.route(
    "/criminali/<int:criminale_id>/elimina",
    methods=["POST"]
)
def elimina_criminale(criminale_id):

    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    # Recuperiamo la scheda prima di eliminarla
    criminale = conn.execute(
        "SELECT * FROM criminali WHERE id = ?",
        (criminale_id,),
    ).fetchone()

    if criminale is None:
        conn.close()
        return "Scheda non trovata.", 404

    nome = criminale["nome"]

    # Eliminiamo la scheda
    conn.execute(
        "DELETE FROM criminali WHERE id = ?",
        (criminale_id,)
    )

    conn.commit()
    conn.close()

    # Registriamo l'eliminazione dopo aver recuperato il nome
    registra_storico(
        session["agente_id"],
        "ELIMINAZIONE SCHEDA",
        f"Ha eliminato la scheda criminale di: {nome}"
    )
    flash("Scheda eliminata.", "success")

    return redirect(url_for("criminali"))


# =========================
# PAGINA STORICO
# =========================

@app.route("/agenti", methods=["GET", "POST"])
def agenti():
    if "agente_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        if not puo_gestire_agenti():
            abort(403)

        username = request.form.get("username", "").strip()
        nome = request.form.get("nome", "").strip()
        grado = request.form.get("grado", "").strip()
        ruolo_accesso = request.form.get("ruolo_accesso", "").strip()
        password = request.form.get("password", "")

        if (
            not username
            or not nome
            or grado not in GRADI_AGENTI
            or ruolo_accesso not in RUOLI_ACCESSO_AGENTI
            or len(password) < 8
        ):
            flash("Verifica i campi e usa una password di almeno 8 caratteri.", "error")
            return redirect(url_for("agenti"))

        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO agenti (username, password, nome, grado, ruolo_accesso)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, generate_password_hash(password), nome, grado, ruolo_accesso),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            flash("Questo username è già in uso.", "error")
            return redirect(url_for("agenti"))
        conn.close()
        registra_storico(
            session["agente_id"],
            "AGENTE",
            f"Ha creato l'account agente {username}",
        )
        flash("Agente creato correttamente.", "success")
        return redirect(url_for("agenti"))

    conn = get_db_connection()
    agenti_list = conn.execute(
        """
        SELECT id, username, nome, grado, ruolo_accesso, attivo
        FROM agenti
        WHERE eliminato = 0
        ORDER BY id DESC
        """
    ).fetchall()
    conn.close()
    can_manage_agents = puo_gestire_agenti()
    can_edit_agents = e_staff_corrente()

    return render_template(
        "modulo.html",
        titolo="Agenti",
        descrizione="Elenco degli ufficiali e dei responsabili attivi nel sistema.",
        items=agenti_list,
        columns=["nome", "username", "grado", "ruolo_accesso", "attivo"],
        form_fields=[
            {"name": "username", "label": "Username", "type": "text"},
            {"name": "nome", "label": "Nome e cognome", "type": "text"},
            {
                "name": "grado",
                "label": "Grado",
                "type": "select",
                "groups": CATEGORIE_GRADI_AGENTI,
            },
            {
                "name": "ruolo_accesso",
                "label": "Ruolo di accesso",
                "type": "select",
                "options": RUOLI_ACCESSO_AGENTI,
            },
            {"name": "password", "label": "Password (almeno 8 caratteri)", "type": "password"},
        ],
        action_url=url_for("agenti"),
        submit_label="Crea agente",
        show_form=can_manage_agents,
        can_manage_agents=can_manage_agents,
        can_edit_agents=can_edit_agents,
        can_delete_agents=can_edit_agents,
    )


@app.route("/agenti/<int:agente_id>/modifica", methods=["GET", "POST"])
def modifica_agente(agente_id):
    if "agente_id" not in session:
        return redirect(url_for("login"))
    if not e_staff_corrente():
        abort(403)

    conn = get_db_connection()
    agente = conn.execute(
        """
        SELECT id, username, nome, grado, ruolo_accesso, attivo
        FROM agenti WHERE id = ? AND eliminato = 0
        """,
        (agente_id,),
    ).fetchone()
    if agente is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        nome = request.form.get("nome", "").strip()
        grado = request.form.get("grado", "").strip()
        ruolo_accesso = request.form.get("ruolo_accesso", "").strip()
        password = request.form.get("password", "")

        if (
            not username
            or not nome
            or (grado not in GRADI_AGENTI and grado != agente["grado"])
            or ruolo_accesso not in RUOLI_ACCESSO_AGENTI
            or (password and len(password) < 8)
        ):
            conn.close()
            flash("Verifica i campi e, se cambi la password, usa almeno 8 caratteri.", "error")
            return redirect(url_for("modifica_agente", agente_id=agente_id))

        active_managers = conn.execute(
            """
            SELECT COUNT(*) AS totale FROM agenti
            WHERE attivo = 1 AND eliminato = 0
                AND ruolo_accesso IN (?, ?, ?) AND id != ?
            """,
            (*RUOLI_GESTIONE_AGENTI, agente_id),
        ).fetchone()["totale"]
        if (
            agente["attivo"]
            and agente["ruolo_accesso"] in RUOLI_GESTIONE_AGENTI
            and ruolo_accesso not in RUOLI_GESTIONE_AGENTI
            and active_managers == 0
        ):
            conn.close()
            flash("Non puoi rimuovere l'ultimo account autorizzato a gestire gli agenti.", "error")
            return redirect(url_for("modifica_agente", agente_id=agente_id))

        values = [username, nome, grado, ruolo_accesso]
        update_password = ""
        if password:
            update_password = ", password = ?"
            values.append(generate_password_hash(password))
        values.append(agente_id)
        try:
            conn.execute(
                f"""
                UPDATE agenti
                SET username = ?, nome = ?, grado = ?, ruolo_accesso = ?{update_password}
                WHERE id = ?
                """,
                values,
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            flash("Questo username è già in uso.", "error")
            return redirect(url_for("modifica_agente", agente_id=agente_id))
        conn.close()
        registra_storico(
            session["agente_id"],
            "AGENTE",
            f"Ha aggiornato l'account agente {username}",
        )
        if agente_id == session["agente_id"]:
            session["nome"] = nome
            session["grado"] = grado
            session["ruolo_accesso"] = ruolo_accesso
        flash("Account agente aggiornato correttamente.", "success")
        return redirect(url_for("agenti"))

    conn.close()
    return render_template(
        "modifica_agente.html",
        agente=agente,
        categorie_gradi=CATEGORIE_GRADI_AGENTI,
        ruoli_accesso=RUOLI_ACCESSO_AGENTI,
    )


@app.route("/agenti/<int:agente_id>/stato", methods=["POST"])
def cambia_stato_agente(agente_id):
    if "agente_id" not in session:
        return redirect(url_for("login"))
    if not puo_gestire_agenti():
        abort(403)
    if agente_id == session["agente_id"]:
        flash("Non puoi disattivare il tuo stesso account.", "error")
        return redirect(url_for("agenti"))

    stato_richiesto = request.form.get("attivo")
    if stato_richiesto not in ("0", "1"):
        abort(400)

    conn = get_db_connection()
    agente = conn.execute(
        """
        SELECT id, username, attivo, ruolo_accesso
        FROM agenti
        WHERE id = ? AND eliminato = 0
        """,
        (agente_id,),
    ).fetchone()
    if agente is None:
        conn.close()
        abort(404)

    if (
        stato_richiesto == "0"
        and agente["attivo"]
        and agente["ruolo_accesso"] in RUOLI_GESTIONE_AGENTI
    ):
        active_managers = conn.execute(
            """
            SELECT COUNT(*) AS totale FROM agenti
            WHERE attivo = 1 AND eliminato = 0
                AND ruolo_accesso IN (?, ?, ?) AND id != ?
            """,
            (*RUOLI_GESTIONE_AGENTI, agente_id),
        ).fetchone()["totale"]
        if active_managers == 0:
            conn.close()
            flash("Non puoi disattivare l'ultimo account autorizzato a gestire gli agenti.", "error")
            return redirect(url_for("agenti"))

    conn.execute(
        "UPDATE agenti SET attivo = ? WHERE id = ?",
        (int(stato_richiesto), agente_id),
    )
    conn.commit()
    conn.close()
    descrizione_azione = "disattivato" if stato_richiesto == "0" else "riattivato"
    registra_storico(
        session["agente_id"],
        "AGENTE",
        f"Ha {descrizione_azione} l'account agente {agente['username']}",
    )
    flash(
        "Accesso agente revocato." if stato_richiesto == "0" else "Accesso agente riattivato.",
        "success",
    )
    return redirect(url_for("agenti"))


@app.route("/agenti/<int:agente_id>/elimina", methods=["POST"])
def elimina_agente(agente_id):
    if "agente_id" not in session:
        return redirect(url_for("login"))
    if not e_staff_corrente():
        abort(403)
    if agente_id == session["agente_id"]:
        flash("Non puoi eliminare il tuo stesso account.", "error")
        return redirect(url_for("agenti"))

    conn = get_db_connection()
    agente = conn.execute(
        """
        SELECT id, username, nome, attivo, ruolo_accesso
        FROM agenti
        WHERE id = ? AND eliminato = 0
        """,
        (agente_id,),
    ).fetchone()
    if agente is None:
        conn.close()
        abort(404)

    if agente["attivo"] and agente["ruolo_accesso"] in RUOLI_GESTIONE_AGENTI:
        active_managers = conn.execute(
            """
            SELECT COUNT(*) AS totale FROM agenti
            WHERE attivo = 1 AND eliminato = 0
                AND ruolo_accesso IN (?, ?, ?) AND id != ?
            """,
            (*RUOLI_GESTIONE_AGENTI, agente_id),
        ).fetchone()["totale"]
        if active_managers == 0:
            conn.close()
            flash(
                "Non puoi eliminare l'ultimo account autorizzato a gestire gli agenti.",
                "error",
            )
            return redirect(url_for("agenti"))

    conn.execute(
        "UPDATE agenti SET attivo = 0, eliminato = 1 WHERE id = ?",
        (agente_id,),
    )
    conn.commit()
    conn.close()
    registra_storico(
        session["agente_id"],
        "ELIMINAZIONE AGENTE",
        f"Ha rimosso l'account agente {agente['username']} ({agente['nome']}) dall'elenco.",
    )
    flash(
        "Agente eliminato dall'elenco e accesso revocato. I riferimenti storici sono conservati.",
        "success",
    )
    return redirect(url_for("agenti"))


@app.route("/arresti", methods=["GET", "POST"])
def arresti():
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    if request.method == "POST":
        criminale_id = existing_criminal_id(conn, request.form.get("criminale_id"))
        motivo = request.form.get("motivo", "").strip()
        prova = request.form.get("prova", "").strip()
        stato = request.form.get("stato", "Eseguito").strip()

        if criminale_id and motivo:
            criminale = conn.execute(
                "SELECT nome FROM criminali WHERE id = ?", (criminale_id,)
            ).fetchone()
            cursor = conn.execute(
                """
                INSERT INTO arresti (criminale_id, agente_id, motivo, prova, stato)
                VALUES (?, ?, ?, ?, ?)
                """,
                (criminale_id, session["agente_id"], motivo, prova or None, stato),
            )
            conn.commit()
            arresto_id = cursor.lastrowid
            registra_storico(
                session["agente_id"],
                "ARRESTO",
                f"Ha registrato un arresto per il criminale ID {criminale_id}",
            )
            flash("Arresto registrato correttamente.", "success")
            esito_notifica = invia_notifica_discord(
                "arresti",
                "Nuovo arresto registrato",
                [
                    ("Registrazione", f"AR-{arresto_id}"),
                    ("Persona coinvolta", criminale["nome"]),
                    ("Motivo", motivo),
                    ("Stato", stato),
                    ("Agente autore", session["nome"]),
                ],
            )
            avvisa_esito_notifica_discord("arresti", esito_notifica)
        else:
            flash("Seleziona un nominativo e inserisci il motivo dell'arresto.", "error")

        conn.close()
        return redirect(url_for("arresti"))

    arresti_list = conn.execute(
        """
        SELECT arresti.*, criminali.nome AS criminale
        FROM arresti
        LEFT JOIN criminali ON criminali.id = arresti.criminale_id
        ORDER BY arresti.id DESC
        """
    ).fetchall()

    criminali_list = conn.execute("SELECT id, nome FROM criminali ORDER BY nome").fetchall()
    conn.close()

    return render_template(
        "modulo.html",
        titolo="Arresti",
        descrizione="Gestione degli arresti eseguiti e delle relative evidenze.",
        items=arresti_list,
        columns=["id", "criminale", "motivo", "stato", "data"],
        form_fields=[
            {"name": "criminale_id", "label": "Criminale", "type": "select", "options": criminali_list},
            {"name": "motivo", "label": "Motivo", "type": "text"},
            {"name": "prova", "label": "Prova", "type": "text"},
            {"name": "stato", "label": "Stato", "type": "select", "options": ["Eseguito", "In attesa", "Rilasciato"]},
        ],
        action_url=url_for("arresti"),
        submit_label="Salva arresto",
        show_form=True,
    )


@app.route("/mandati", methods=["GET", "POST"])
def mandati():
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    if request.method == "POST":
        criminale_id = existing_criminal_id(conn, request.form.get("criminale_id"))
        motivazione = request.form.get("motivazione", "").strip()
        prova = request.form.get("prove", "").strip()
        stato = request.form.get("stato", "Attivo").strip()

        if criminale_id and motivazione:
            criminale = conn.execute(
                "SELECT nome FROM criminali WHERE id = ?", (criminale_id,)
            ).fetchone()
            cursor = conn.execute(
                """
                INSERT INTO mandati (criminale_id, motivazione, prove, agente_id, stato, approvato_da)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (criminale_id, motivazione, prova or None, session["agente_id"], stato, session["agente_id"]),
            )
            conn.commit()
            mandato_id = cursor.lastrowid
            registra_storico(
                session["agente_id"],
                "MANDATO",
                f"Ha emesso un mandato per il criminale ID {criminale_id}",
            )
            flash("Mandato registrato correttamente.", "success")
            esito_notifica = invia_notifica_discord(
                "mandati_di_arresto",
                "Nuovo mandato registrato",
                [
                    ("Registrazione", f"MA-{mandato_id}"),
                    ("Persona interessata", criminale["nome"]),
                    ("Motivazione", motivazione),
                    ("Stato", stato),
                    ("Agente autore", session["nome"]),
                ],
            )
            avvisa_esito_notifica_discord("mandati_di_arresto", esito_notifica)
        else:
            flash("Seleziona un nominativo e inserisci la motivazione.", "error")

        conn.close()
        return redirect(url_for("mandati"))

    mandati_list = conn.execute(
        """
        SELECT mandati.*, criminali.nome AS criminale
        FROM mandati
        LEFT JOIN criminali ON criminali.id = mandati.criminale_id
        ORDER BY mandati.id DESC
        """
    ).fetchall()

    criminali_list = conn.execute("SELECT id, nome FROM criminali ORDER BY nome").fetchall()
    conn.close()

    return render_template(
        "modulo.html",
        titolo="Mandati",
        descrizione="Emissione e controllo dei mandati di ricerca e di arresto.",
        items=mandati_list,
        columns=["id", "criminale", "motivazione", "stato", "data_emissione"],
        form_fields=[
            {"name": "criminale_id", "label": "Criminale", "type": "select", "options": criminali_list},
            {"name": "motivazione", "label": "Motivazione", "type": "text"},
            {"name": "prove", "label": "Prove", "type": "text"},
            {"name": "stato", "label": "Stato", "type": "select", "options": ["Attivo", "Eseguito", "Scaduto"]},
        ],
        action_url=url_for("mandati"),
        submit_label="Salva mandato",
        show_form=True,
    )


@app.route("/denunce", methods=["GET", "POST"])
def denunce():
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    if request.method == "POST":
        criminale_id = existing_criminal_id(conn, request.form.get("criminale_id"))
        denunciante = request.form.get("denunciante", "").strip()
        descrizione = request.form.get("descrizione", "").strip()
        stato = request.form.get("stato", "Aperta").strip()

        if criminale_id and denunciante and descrizione:
            criminale = conn.execute(
                "SELECT nome FROM criminali WHERE id = ?", (criminale_id,)
            ).fetchone()
            cursor = conn.execute(
                """
                INSERT INTO denunce
                    (criminale_id, agente_id, denunciante, descrizione, stato)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    criminale_id,
                    session["agente_id"],
                    denunciante,
                    descrizione,
                    stato,
                ),
            )
            conn.commit()
            denuncia_id = cursor.lastrowid
            registra_storico(
                session["agente_id"],
                "DENUNCIA",
                f"Ha aperto una denuncia per il criminale ID {criminale_id}",
            )
            flash("Denuncia registrata correttamente.", "success")
            esito_notifica = invia_notifica_discord(
                "denunce",
                "Nuova denuncia registrata",
                [
                    ("Registrazione", f"DE-{denuncia_id}"),
                    ("Persona denunciata", criminale["nome"]),
                    ("Denunciante / segnalante", denunciante),
                    ("Descrizione", descrizione),
                    ("Stato", stato),
                    ("Scritta da", session["nome"]),
                ],
            )
            avvisa_esito_notifica_discord("denunce", esito_notifica)
        else:
            flash(
                "Seleziona la persona denunciata e inserisci denunciante e descrizione.",
                "error",
            )

        conn.close()
        return redirect(url_for("denunce"))

    denunce_list = conn.execute(
        """
        SELECT denunce.*, criminali.nome AS criminale
        FROM denunce
        LEFT JOIN criminali ON criminali.id = denunce.criminale_id
        ORDER BY denunce.id DESC
        """
    ).fetchall()
    criminali_list = conn.execute("SELECT id, nome FROM criminali ORDER BY nome").fetchall()
    conn.close()

    return render_template(
        "modulo.html",
        titolo="Denunce",
        descrizione="Gestione delle denunce aperte e delle relative attività investigative.",
        items=denunce_list,
        columns=["id", "criminale", "denunciante", "descrizione", "stato", "data"],
        form_fields=[
            {"name": "criminale_id", "label": "Criminale", "type": "select", "options": criminali_list},
            {"name": "denunciante", "label": "Denunciante / segnalante", "type": "text"},
            {"name": "descrizione", "label": "Descrizione", "type": "text"},
            {"name": "stato", "label": "Stato", "type": "select", "options": ["Aperta", "In corso", "Chiusa"]},
        ],
        action_url=url_for("denunce"),
        submit_label="Salva denuncia",
        show_form=True,
    )


@app.route("/dichiarazioni", methods=["GET", "POST"])
def dichiarazioni():
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()
    if request.method == "POST":
        dichiarante = request.form.get("dichiarante", "").strip()
        persona_coinvolta = request.form.get("persona_coinvolta", "").strip()
        contenuto = request.form.get("contenuto", "").strip()

        if not dichiarante or not contenuto:
            conn.close()
            flash("Inserisci il nome del dichiarante e il contenuto della dichiarazione.", "error")
            return redirect(url_for("dichiarazioni"))

        cursor = conn.execute(
            """
            INSERT INTO dichiarazioni
                (dichiarante, persona_coinvolta, contenuto, agente_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                dichiarante,
                persona_coinvolta or None,
                contenuto,
                session["agente_id"],
            ),
        )
        conn.commit()
        dichiarazione_id = cursor.lastrowid
        conn.close()

        registra_storico(
            session["agente_id"],
            "DICHIARAZIONE",
            f"Ha registrato la dichiarazione DE-{dichiarazione_id} di {dichiarante}",
        )
        flash("Dichiarazione registrata correttamente.", "success")
        esito_notifica = invia_notifica_discord(
            "dichiarazioni",
            "Nuova dichiarazione registrata",
            [
                ("Registrazione", f"DI-{dichiarazione_id}"),
                ("Dichiarante", dichiarante),
                ("Persona coinvolta", persona_coinvolta or "Non specificata"),
                ("Dichiarazione", contenuto),
                ("Scritta da", session["nome"]),
            ],
        )
        avvisa_esito_notifica_discord("dichiarazioni", esito_notifica)
        return redirect(url_for("dichiarazioni"))

    dichiarazioni_list = conn.execute(
        """
        SELECT dichiarazioni.*, agenti.nome AS agente
        FROM dichiarazioni
        LEFT JOIN agenti ON agenti.id = dichiarazioni.agente_id
        ORDER BY dichiarazioni.id DESC
        """
    ).fetchall()
    conn.close()

    return render_template(
        "modulo.html",
        titolo="Dichiarazioni",
        descrizione="Raccolta e consultazione delle dichiarazioni registrate.",
        items=dichiarazioni_list,
        columns=["id", "dichiarante", "persona_coinvolta", "contenuto", "agente", "data"],
        form_fields=[
            {"name": "dichiarante", "label": "Dichiarante", "type": "text"},
            {
                "name": "persona_coinvolta",
                "label": "Persona coinvolta (facoltativa)",
                "type": "text",
                "required": False,
            },
            {"name": "contenuto", "label": "Dichiarazione", "type": "textarea"},
        ],
        action_url=url_for("dichiarazioni"),
        submit_label="Salva dichiarazione",
        show_form=True,
    )


@app.route("/rapporti", methods=["GET", "POST"])
def rapporti():
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()
    if request.method == "POST":
        titolo = request.form.get("titolo", "").strip()
        persona_coinvolta = request.form.get("persona_coinvolta", "").strip()
        descrizione = request.form.get("descrizione", "").strip()
        stato = request.form.get("stato", "Inviato").strip()

        if not titolo or not descrizione:
            conn.close()
            flash("Inserisci il titolo e la descrizione del rapporto.", "error")
            return redirect(url_for("rapporti"))

        cursor = conn.execute(
            """
            INSERT INTO rapporti
                (titolo, persona_coinvolta, descrizione, stato, agente_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                titolo,
                persona_coinvolta or None,
                descrizione,
                stato,
                session["agente_id"],
            ),
        )
        conn.commit()
        rapporto_id = cursor.lastrowid
        conn.close()

        registra_storico(
            session["agente_id"],
            "RAPPORTO",
            f"Ha registrato il rapporto RA-{rapporto_id}: {titolo}",
        )
        flash("Rapporto registrato correttamente.", "success")
        esito_notifica = invia_notifica_discord(
            "rapporti",
            "Nuovo rapporto registrato",
            [
                ("Registrazione", f"RA-{rapporto_id}"),
                ("Titolo", titolo),
                ("Persona coinvolta", persona_coinvolta or "Non specificata"),
                ("Descrizione", descrizione),
                ("Stato", stato),
                ("Redatto da", session["nome"]),
            ],
        )
        avvisa_esito_notifica_discord("rapporti", esito_notifica)
        return redirect(url_for("rapporti"))

    rapporti_list = conn.execute(
        """
        SELECT rapporti.*, agenti.nome AS agente
        FROM rapporti
        LEFT JOIN agenti ON agenti.id = rapporti.agente_id
        ORDER BY rapporti.id DESC
        """
    ).fetchall()
    conn.close()

    return render_template(
        "modulo.html",
        titolo="Rapporti",
        descrizione="Redazione e consultazione dei rapporti operativi.",
        items=rapporti_list,
        columns=["id", "titolo", "persona_coinvolta", "descrizione", "stato", "agente", "data"],
        form_fields=[
            {"name": "titolo", "label": "Titolo del rapporto", "type": "text"},
            {
                "name": "persona_coinvolta",
                "label": "Persona coinvolta (facoltativa)",
                "type": "text",
                "required": False,
            },
            {"name": "descrizione", "label": "Descrizione", "type": "textarea"},
            {
                "name": "stato",
                "label": "Stato",
                "type": "select",
                "options": ["Inviato", "In corso", "Chiuso"],
            },
        ],
        action_url=url_for("rapporti"),
        submit_label="Salva rapporto",
        show_form=True,
    )


@app.route("/prove", methods=["GET", "POST"])
def prove():
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    if request.method == "POST":
        criminale_id = request.form.get("criminale_id")
        tipo = request.form.get("tipo", "").strip()
        descrizione = request.form.get("descrizione", "").strip()
        try:
            criminale_id = int(criminale_id)
        except (TypeError, ValueError):
            conn.close()
            flash("Seleziona un nominativo valido.", "error")
            return redirect(url_for("prove"))

        criminale = conn.execute(
            "SELECT id, nome FROM criminali WHERE id = ?", (criminale_id,)
        ).fetchone()
        if criminale is None or not tipo or not descrizione:
            conn.close()
            flash("Seleziona un nominativo e compila tipo e descrizione.", "error")
            return redirect(url_for("prove"))

        image_path, image_error = save_evidence_image(request.files.get("immagine"))
        if image_error:
            conn.close()
            flash(image_error, "error")
            return redirect(url_for("prove"))

        try:
            conn.execute(
                """
                INSERT INTO prove
                    (criminale_id, agente_id, tipo, descrizione, image_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (criminale_id, session["agente_id"], tipo, descrizione, image_path),
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            if image_path:
                saved_path = os.path.join(EVIDENCE_UPLOAD_DIR, image_path)
                if os.path.exists(saved_path):
                    os.remove(saved_path)
            raise
        finally:
            conn.close()

        registra_storico(
            session["agente_id"],
            "PROVA",
            f"Ha inserito una nuova prova per il criminale ID {criminale_id}",
        )
        flash("Prova registrata correttamente.", "success")
        return redirect(url_for("prove"))

    prove_list = conn.execute(
        """
        SELECT prove.*, criminali.nome AS criminale
        FROM prove
        LEFT JOIN criminali ON criminali.id = prove.criminale_id
        ORDER BY prove.id DESC
        """
    ).fetchall()
    criminali_list = conn.execute("SELECT id, nome FROM criminali ORDER BY nome").fetchall()
    conn.close()

    return render_template(
        "modulo.html",
        titolo="Prove",
        descrizione="Archivio informativo delle prove raccolte, analizzate e associate ai casi.",
        items=prove_list,
        columns=["id", "criminale", "tipo", "descrizione", "immagine", "data"],
        form_fields=[
            {"name": "criminale_id", "label": "Criminale", "type": "select", "options": criminali_list},
            {"name": "tipo", "label": "Tipo", "type": "text"},
            {"name": "descrizione", "label": "Descrizione", "type": "textarea"},
            {
                "name": "immagine",
                "label": "Immagine (JPG, PNG, GIF o WEBP, max 10 MB)",
                "type": "file",
                "required": False,
            },
        ],
        action_url=url_for("prove"),
        submit_label="Salva prova",
        show_form=True,
    )


@app.route("/prove/<int:prova_id>/immagine")
def immagine_prova(prova_id):
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()
    prova = conn.execute(
        "SELECT image_path FROM prove WHERE id = ?", (prova_id,)
    ).fetchone()
    conn.close()

    if prova is None or not prova["image_path"]:
        abort(404)

    return send_from_directory(EVIDENCE_UPLOAD_DIR, prova["image_path"])


@app.route("/multe", methods=["GET", "POST"])
def multe():
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    if request.method == "POST":
        criminale_id = existing_criminal_id(conn, request.form.get("criminale_id"))
        importo = request.form.get("importo", "").strip()
        motivo = request.form.get("motivo", "").strip()
        stato = request.form.get("stato", "Da pagare").strip()

        try:
            importo_valido = Decimal(importo)
            if not importo_valido.is_finite() or importo_valido <= 0:
                raise InvalidOperation
        except InvalidOperation:
            importo_valido = None

        if criminale_id and importo_valido and motivo:
            criminale = conn.execute(
                "SELECT nome FROM criminali WHERE id = ?", (criminale_id,)
            ).fetchone()
            cursor = conn.execute(
                """
                INSERT INTO multe (criminale_id, agente_id, importo, motivo, stato)
                VALUES (?, ?, ?, ?, ?)
                """,
                (criminale_id, session["agente_id"], str(importo_valido), motivo, stato),
            )
            conn.commit()
            multa_id = cursor.lastrowid
            registra_storico(
                session["agente_id"],
                "MULTA",
                f"Ha registrato una multa per il criminale ID {criminale_id}",
            )
            flash("Multa registrata correttamente.", "success")
            esito_notifica = invia_notifica_discord(
                "multe",
                "Nuova multa registrata",
                [
                    ("Registrazione", f"MU-{multa_id}"),
                    ("Persona sanzionata", criminale["nome"]),
                    ("Importo", f"{importo_valido} €"),
                    ("Motivo", motivo),
                    ("Stato", stato),
                    ("Agente autore", session["nome"]),
                ],
            )
            avvisa_esito_notifica_discord("multe", esito_notifica)
        else:
            flash("Seleziona un nominativo e inserisci importo positivo e motivo.", "error")

        conn.close()
        return redirect(url_for("multe"))

    multe_list = conn.execute(
        """
        SELECT multe.*, criminali.nome AS criminale
        FROM multe
        LEFT JOIN criminali ON criminali.id = multe.criminale_id
        ORDER BY multe.id DESC
        """
    ).fetchall()
    criminali_list = conn.execute("SELECT id, nome FROM criminali ORDER BY nome").fetchall()
    conn.close()

    return render_template(
        "modulo.html",
        titolo="Multe",
        descrizione="Gestione delle sanzioni applicate e dello stato di pagamento.",
        items=multe_list,
        columns=["id", "criminale", "importo", "motivo", "stato"],
        form_fields=[
            {"name": "criminale_id", "label": "Criminale", "type": "select", "options": criminali_list},
            {"name": "importo", "label": "Importo (€)", "type": "number"},
            {"name": "motivo", "label": "Motivo", "type": "text"},
            {"name": "stato", "label": "Stato", "type": "select", "options": ["Da pagare", "Pagata", "In sospeso"]},
        ],
        action_url=url_for("multe"),
        submit_label="Salva multa",
        show_form=True,
    )


@app.route("/veicoli", methods=["GET", "POST"])
def veicoli():
    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    if request.method == "POST":
        targa = request.form.get("targa", "").strip()
        proprietario = request.form.get("proprietario", "").strip()
        stato = request.form.get("stato", "Controllato").strip()
        note = request.form.get("note", "").strip()

        if targa:
            try:
                conn.execute(
                    """
                    INSERT INTO veicoli (targa, proprietario, stato, note)
                    VALUES (?, ?, ?, ?)
                    """,
                    (targa.upper(), proprietario or None, stato, note or None),
                )
                conn.commit()
                registra_storico(
                    session["agente_id"],
                    "VEICOLO",
                    f"Ha registrato il veicolo con targa {targa.upper()}",
                )
                flash("Veicolo registrato correttamente.", "success")
            except sqlite3.IntegrityError:
                flash("Esiste già un veicolo con questa targa.", "error")
        else:
            flash("Inserisci la targa del veicolo.", "error")

        conn.close()
        return redirect(url_for("veicoli"))

    veicoli_list = conn.execute("SELECT * FROM veicoli ORDER BY id DESC").fetchall()
    conn.close()

    return render_template(
        "modulo.html",
        titolo="Veicoli",
        descrizione="Registro veicoli controllati, verificati e monitorati.",
        items=veicoli_list,
        columns=["id", "targa", "proprietario", "stato", "data"],
        form_fields=[
            {"name": "targa", "label": "Targa", "type": "text"},
            {"name": "proprietario", "label": "Proprietario", "type": "text"},
            {"name": "stato", "label": "Stato", "type": "select", "options": ["Controllato", "Ricercato", "Sbloccato"]},
            {"name": "note", "label": "Note", "type": "text"},
        ],
        action_url=url_for("veicoli"),
        submit_label="Salva veicolo",
        show_form=True,
    )


@app.route("/storico")
def storico():

    if "agente_id" not in session:
        return redirect(url_for("login"))

    conn = get_db_connection()

    attivita = conn.execute(
        """
        SELECT
            storico.*,
            agenti.nome
        FROM storico
        LEFT JOIN agenti
            ON storico.agente_id = agenti.id
        ORDER BY storico.id DESC
        """
    ).fetchall()

    conn.close()

    return render_template(
        "storico.html",
        attivita=attivita
    )


# =========================
# AVVIO DEL PROGRAMMA
# =========================

if __name__ == "__main__":
    init_db()
    create_admin()
    app.run(debug=os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true"})
