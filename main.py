import os, csv, io, json, logging, asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="CantierTrack API")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_FILE = Path("data/cantieri.json")
DATA_FILE.parent.mkdir(exist_ok=True)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HEADERS_MIT = {"User-Agent": "CantierTrack/1.0 (academic project)"}

# ── Fonti dati ────────────────────────────────────────────────
FONTI = [
    {"id": "mit_bandi", "nome": "MIT SCP — Bandi attivi",
     "url": "https://dati.mit.gov.it/scp/v_od_bandi.csv", "parser": "mit_bandi", "ente": "MIT"},
    {"id": "anac_cig_2025_05", "nome": "ANAC BandiCIG — Mag 2025",
     "url": "https://dati.anticorruzione.it/opendata/download/dataset/cig-2025/filesystem/cig_csv_2025_05.csv",
     "parser": "anac_cig", "ente": "ANAC"},
    {"id": "anac_cig_2025_04", "nome": "ANAC BandiCIG — Apr 2025",
     "url": "https://dati.anticorruzione.it/opendata/download/dataset/cig-2025/filesystem/cig_csv_2025_04.csv",
     "parser": "anac_cig", "ente": "ANAC"},
    {"id": "anac_cig_2025_03", "nome": "ANAC BandiCIG — Mar 2025",
     "url": "https://dati.anticorruzione.it/opendata/download/dataset/cig-2025/filesystem/cig_csv_2025_03.csv",
     "parser": "anac_cig", "ente": "ANAC"},
    {"id": "anac_cig_2025_02", "nome": "ANAC BandiCIG — Feb 2025",
     "url": "https://dati.anticorruzione.it/opendata/download/dataset/cig-2025/filesystem/cig_csv_2025_02.csv",
     "parser": "anac_cig", "ente": "ANAC"},
    {"id": "anac_pnrr", "nome": "ANAC — Bandi PNRR",
     "url": "https://dati.anticorruzione.it/opendata/download/dataset/pnrr/filesystem/pnrr_csv.csv",
     "parser": "anac_pnrr", "ente": "ANAC"},
]

# ── CSV helpers ───────────────────────────────────────────────
def read_csv(text: str, max_rows=15000):
    text = text.strip()
    if not text: return []
    first = text.split("\n")[0]
    delim = ";" if first.count(";") >= first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    rows = []
    for i, row in enumerate(reader):
        if i >= max_rows: break
        rows.append({k.strip(): (v or "").strip() for k, v in row.items()})
    return rows

def pfloat(v):
    try: return float(str(v).replace(",", ".").strip())
    except: return 0.0

def parse_mit_bandi(rows, fonte):
    items = []
    for r in rows:
        imp = pfloat(r.get("importo", "0"))
        if imp <= 0: continue
        stato_raw = (r.get("stato_bando") or "").upper()
        stato = "completato" if any(x in stato_raw for x in ["SCAD","CHIUSO","ANNULL"]) else \
                "sospeso" if "SOSP" in stato_raw else "attivo"
        items.append({
            "nome": r.get("oggetto") or "Bando SCP",
            "citta": r.get("luogo_esecuzione") or "—", "regione": "",
            "valore": imp, "stato": stato,
            "tipo": r.get("tipo_bando") or "Lavori",
            "tipoIntervento": r.get("tipo_intervento") or "—",
            "inizio": r.get("data_pubb_bando_scp") or "—",
            "fine_prevista": r.get("termine_pres_dom_off") or "—",
            "fonte": fonte["nome"], "ente": fonte["ente"],
            "cig": r.get("cig") or "—", "cup": r.get("cup") or "—",
            "rup": r.get("rup") or "—",
            "stazione": r.get("denominazione_stazione_appaltante") or "—",
            "tipoProcedura": r.get("tipo_procedura") or "—",
            "url": r.get("url") or None,
        })
    return items

def parse_anac_cig(rows, fonte):
    items = []
    for r in rows:
        imp = pfloat(r.get("importo_complessivo_gara") or r.get("importo") or "0")
        if imp <= 0: continue
        items.append({
            "nome": r.get("oggetto") or r.get("oggetto_gara") or "Appalto ANAC",
            "citta": r.get("provincia") or r.get("luogo_istat") or "—",
            "regione": r.get("regione") or "",
            "valore": imp, "stato": "attivo",
            "tipo": r.get("tipo_appalto") or "Lavori",
            "tipoIntervento": r.get("tipo_appalto") or "—",
            "inizio": r.get("data_pubblicazione") or "—",
            "fine_prevista": r.get("data_scadenza") or "—",
            "fonte": fonte["nome"], "ente": fonte["ente"],
            "cig": r.get("cig") or "—", "cup": r.get("cup") or "—",
            "rup": r.get("rup") or "—",
            "stazione": r.get("denominazione_amministrazione_appaltante") or "—",
            "tipoProcedura": r.get("scelta_contraente") or "—", "url": None,
        })
    return items

def parse_anac_pnrr(rows, fonte):
    items = []
    for r in rows:
        imp = pfloat(r.get("importo_complessivo_gara") or r.get("importo") or "0")
        if imp <= 0: continue
        nome = r.get("oggetto") or r.get("descrizione") or "Bando PNRR"
        items.append({
            "nome": "🇪🇺 " + nome,
            "citta": r.get("provincia") or "—",
            "regione": r.get("regione") or "",
            "valore": imp, "stato": "attivo",
            "tipo": "Lavori PNRR", "tipoIntervento": "PNRR",
            "inizio": r.get("data_pubblicazione") or "—",
            "fine_prevista": r.get("data_scadenza") or "—",
            "fonte": fonte["nome"], "ente": fonte["ente"],
            "cig": r.get("cig") or "—", "cup": r.get("cup") or "—",
            "rup": r.get("rup") or "—",
            "stazione": r.get("denominazione_amministrazione_appaltante") or "—",
            "tipoProcedura": r.get("scelta_contraente") or "—", "url": None,
        })
    return items

PARSERS = {"mit_bandi": parse_mit_bandi, "anac_cig": parse_anac_cig, "anac_pnrr": parse_anac_pnrr}

# ── Fetch dati istituzionali ──────────────────────────────────
async def fetch_all_fonti():
    all_cantieri = []
    results = []
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for fonte in FONTI:
            log.info(f"Scarico {fonte['id']}...")
            try:
                resp = await client.get(fonte["url"], headers=HEADERS_MIT)
                resp.raise_for_status()
                for enc in [resp.encoding, "utf-8", "latin-1"]:
                    try: text = resp.content.decode(enc or "utf-8"); break
                    except: continue
                rows = read_csv(text)
                items = PARSERS[fonte["parser"]](rows, fonte)
                for item in items:
                    item["id"] = len(all_cantieri) + 1
                    item["lat"] = None
                    item["lng"] = None
                all_cantieri.extend(items)
                results.append({"id": fonte["id"], "nome": fonte["nome"], "count": len(items), "ok": True})
                log.info(f"  {fonte['id']}: {len(items)} cantieri")
            except Exception as e:
                log.error(f"  {fonte['id']}: {e}")
                results.append({"id": fonte["id"], "nome": fonte["nome"], "count": 0, "ok": False})

    output = {
        "aggiornato": datetime.now(timezone.utc).isoformat(),
        "totale": len(all_cantieri),
        "fonti": results,
        "cantieri": all_cantieri,
    }
    DATA_FILE.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    log.info(f"Salvati {len(all_cantieri)} cantieri totali")
    return output

# ── Endpoints ─────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "CantierTrack API"}

@app.get("/api/cantieri")
async def get_cantieri(aggiorna: bool = False):
    """Restituisce tutti i cantieri. Con ?aggiorna=true riscarica dai server."""
    if aggiorna or not DATA_FILE.exists():
        return await fetch_all_fonti()
    data = json.loads(DATA_FILE.read_text())
    return data

@app.post("/api/aggiorna")
async def aggiorna_cantieri(background_tasks: BackgroundTasks):
    """Avvia aggiornamento in background."""
    background_tasks.add_task(fetch_all_fonti)
    return {"status": "aggiornamento avviato"}

@app.get("/api/status")
def status():
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text())
        return {"aggiornato": data.get("aggiornato"), "totale": data.get("totale"), "fonti": data.get("fonti")}
    return {"aggiornato": None, "totale": 0, "fonti": []}

# ── AI Search ─────────────────────────────────────────────────
class AISearchRequest(BaseModel):
    query: Optional[str] = ""
    categorie: list[str] = ["infrastrutture", "edilizia pubblica", "PNRR"]

@app.post("/api/ai/cerca")
async def ai_cerca(req: AISearchRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="API key Anthropic non configurata")

    categorie = ", ".join(req.categorie) if req.categorie else "cantieri pubblici italiani"
    extra = f" Concentrati su: {req.query}." if req.query else ""

    prompt = f"""Sei un esperto di edilizia e appalti pubblici italiani. Cerca notizie recenti (2024-2025) sui principali cantieri e progetti infrastrutturali in Italia nelle seguenti categorie: {categorie}.{extra}

Per ogni cantiere trovato restituisci SOLO un JSON array valido:
[{{"nome":"nome cantiere","citta":"città","regione":"regione","valore":5000000,"stato":"attivo","tipo":"Lavori","tipoIntervento":"Nuova costruzione","stazione":"ente appaltante","inizio":"2024-01-15","fine_prevista":"2026-06-30","fonte":"nome giornale","url":"https://...","descrizione":"breve descrizione del progetto"}}]

Trova almeno 15 cantieri REALI con dati precisi. Stato può essere: attivo, pianificato, completato. Valore in euro come numero intero. Rispondi SOLO con il JSON array, nessun testo prima o dopo."""

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 4000,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            }
        )

    if not resp.is_success:
        raise HTTPException(status_code=502, detail=f"Errore API Anthropic: {resp.status_code}")

    data = resp.json()
    text_block = next((b for b in data.get("content", []) if b.get("type") == "text"), None)
    if not text_block:
        raise HTTPException(status_code=502, detail="Nessuna risposta testuale dalla AI")

    text = text_block["text"].strip()
    text = text.replace("```json", "").replace("```", "").strip()
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e == -1:
        raise HTTPException(status_code=502, detail="Formato risposta AI non valido")

    cantieri = json.loads(text[s:e+1])
    for i, c in enumerate(cantieri):
        c["id"] = f"ai_{i}"
        c["lat"] = None
        c["lng"] = None
        c["ente"] = "AI News"

    return {"cantieri": cantieri, "totale": len(cantieri)}

# ── Serve frontend ────────────────────────────────────────────
from fastapi.responses import HTMLResponse

@app.get("/app", response_class=HTMLResponse)
@app.get("/app/", response_class=HTMLResponse)
async def frontend():
    html_file = Path("frontend/index.html")
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text())
    return HTMLResponse(content="<h1>Frontend non trovato</h1>", status_code=404)

if Path("frontend").exists():
    app.mount("/static", StaticFiles(directory="frontend"), name="static")
