import os, csv, io, json, logging, asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATA_FILE = Path("data/cantieri.json")
DATA_FILE.parent.mkdir(exist_ok=True)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HEADERS_MIT = {"User-Agent": "CantierTrack/1.0"}

# Scarica UNA fonte alla volta con timeout breve
FONTI = [
    {"id": "mit_bandi", "nome": "MIT SCP — Bandi attivi",
     "url": "https://dati.mit.gov.it/scp/v_od_bandi.csv", "parser": "mit_bandi", "ente": "MIT"},
    {"id": "anac_cig_2025_04", "nome": "ANAC BandiCIG — Apr 2025",
     "url": "https://dati.anticorruzione.it/opendata/download/dataset/cig-2025/filesystem/cig_csv_2025_04.csv",
     "parser": "anac_cig", "ente": "ANAC"},
    {"id": "anac_pnrr", "nome": "ANAC — Bandi PNRR",
     "url": "https://dati.anticorruzione.it/opendata/download/dataset/pnrr/filesystem/pnrr_csv.csv",
     "parser": "anac_pnrr", "ente": "ANAC"},
]

def read_csv(text, max_rows=8000):
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
            "nome": r.get("oggetto") or "Appalto ANAC",
            "citta": r.get("provincia") or "—",
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
        nome = r.get("oggetto") or "Bando PNRR"
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

async def fetch_all_fonti():
    all_cantieri = []
    results = []
    # Scarica ogni fonte separatamente con timeout di 25s ciascuna
    for fonte in FONTI:
        log.info(f"Scarico {fonte['id']}...")
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                resp = await client.get(fonte["url"], headers=HEADERS_MIT)
                resp.raise_for_status()
            for enc in ["utf-8", "latin-1"]:
                try: text = resp.content.decode(enc); break
                except: continue
            rows = read_csv(text)
            items = PARSERS[fonte["parser"]](rows, fonte)
            for item in items:
                item["id"] = len(all_cantieri) + 1
                item["lat"] = None
                item["lng"] = None
            all_cantieri.extend(items)
            results.append({"id": fonte["id"], "nome": fonte["nome"], "count": len(items), "ok": True})
            log.info(f"  OK: {len(items)} cantieri")
            # Pausa tra una fonte e l'altra per non sovraccaricare
            await asyncio.sleep(1)
        except Exception as e:
            log.error(f"  ERRORE {fonte['id']}: {e}")
            results.append({"id": fonte["id"], "nome": fonte["nome"], "count": 0, "ok": False, "errore": str(e)})

    output = {
        "aggiornato": datetime.now(timezone.utc).isoformat(),
        "totale": len(all_cantieri),
        "fonti": results,
        "cantieri": all_cantieri,
    }
    DATA_FILE.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    log.info(f"Salvati {len(all_cantieri)} cantieri totali")
    return output

# Avvia download in background all'avvio del server
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Avvio CantierTrack — scarico dati in background...")
    asyncio.create_task(fetch_all_fonti())
    yield

app = FastAPI(title="CantierTrack API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    for path in [Path("frontend/index.html"), Path("/app/frontend/index.html")]:
        if path.exists():
            return HTMLResponse(content=path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Frontend non trovato</h1>")

@app.get("/api/cantieri")
async def get_cantieri(aggiorna: bool = False):
    if aggiorna:
        asyncio.create_task(fetch_all_fonti())
        return {"messaggio": "Aggiornamento avviato in background. Riprova tra 60 secondi.", "totale": 0, "cantieri": []}
    if DATA_FILE.exists():
        return JSONResponse(content=json.loads(DATA_FILE.read_text()))
    return {"messaggio": "Dati non ancora disponibili, attendi...", "totale": 0, "cantieri": [], "aggiornato": None}

@app.get("/api/status")
def status():
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text())
        return {"aggiornato": data.get("aggiornato"), "totale": data.get("totale"), "fonti": data.get("fonti")}
    return {"aggiornato": None, "totale": 0, "stato": "download in corso..."}

class AISearchRequest(BaseModel):
    query: Optional[str] = ""
    categorie: list[str] = ["infrastrutture", "edilizia pubblica", "PNRR"]

@app.post("/api/ai/cerca")
async def ai_cerca(req: AISearchRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="API key Anthropic non configurata")
    categorie = ", ".join(req.categorie) if req.categorie else "cantieri pubblici italiani"
    extra = f" Concentrati su: {req.query}." if req.query else ""
    prompt = f"""Sei un esperto di edilizia e appalti pubblici italiani. Cerca notizie recenti (2024-2025) sui principali cantieri nelle categorie: {categorie}.{extra}
Restituisci SOLO un JSON array:
[{{"nome":"nome","citta":"città","regione":"regione","valore":5000000,"stato":"attivo","tipo":"Lavori","tipoIntervento":"tipo","stazione":"ente","inizio":"2024-01-15","fine_prevista":"2026-06-30","fonte":"giornale","url":"https://...","descrizione":"descrizione"}}]
Trova 15+ cantieri REALI. Rispondi SOLO con il JSON."""

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 4000,
                  "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                  "messages": [{"role": "user", "content": prompt}]}
        )
    if not resp.is_success:
        raise HTTPException(status_code=502, detail=f"Errore API: {resp.status_code}")
    data = resp.json()
    text_block = next((b for b in data.get("content", []) if b.get("type") == "text"), None)
    if not text_block:
        raise HTTPException(status_code=502, detail="Nessuna risposta dalla AI")
    text = text_block["text"].strip().replace("```json", "").replace("```", "").strip()
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e == -1:
        raise HTTPException(status_code=502, detail="Formato non valido")
    cantieri = json.loads(text[s:e+1])
    for i, c in enumerate(cantieri):
        c["id"] = f"ai_{i}"
        c["lat"] = None
        c["lng"] = None
        c["ente"] = "AI News"
    return {"cantieri": cantieri, "totale": len(cantieri)}
