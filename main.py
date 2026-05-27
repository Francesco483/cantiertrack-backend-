import os, json, logging
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="CantierTrack AI API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

@app.get("/")
def root():
    return {"status": "ok", "service": "CantierTrack AI API"}

@app.get("/health")
def health():
    return {"status": "ok"}

# ── Modelli Pydantic (tutti definiti prima degli endpoint) ────
class AISearchRequest(BaseModel):
    query: Optional[str] = ""
    categorie: list[str] = ["infrastrutture", "edilizia pubblica", "PNRR"]

class CantierInfoRequest(BaseModel):
    nome: str
    cig: Optional[str] = ""
    stazione: Optional[str] = ""
    valore: Optional[float] = 0
    citta: Optional[str] = ""

# ── Endpoint ricerca cantieri generica ────────────────────────
@app.post("/api/ai/cerca")
async def ai_cerca(req: AISearchRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="API key Anthropic non configurata")

    categorie = ", ".join(req.categorie) if req.categorie else "cantieri pubblici italiani"
    extra = f" Concentrati su: {req.query}." if req.query else ""

    prompt = f"""Sei un esperto di edilizia e appalti pubblici italiani. Cerca notizie recenti (2024-2025) sui principali cantieri nelle categorie: {categorie}.{extra}

Restituisci SOLO un JSON array valido:
[{{"nome":"nome cantiere","citta":"città","regione":"regione","valore":5000000,"stato":"attivo","tipo":"Lavori","tipoIntervento":"Nuova costruzione","stazione":"ente appaltante","inizio":"2024-01-15","fine_prevista":"2026-06-30","fonte":"nome giornale","url":"https://...","descrizione":"breve descrizione"}}]

Trova 15+ cantieri REALI con dati precisi. Rispondi SOLO con il JSON array."""

    async with httpx.AsyncClient(timeout=55) as client:
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

# ── Endpoint info su cantiere specifico ───────────────────────
@app.post("/api/ai/cerca-cantiere")
async def ai_cerca_cantiere(req: CantierInfoRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="API key Anthropic non configurata")

    valore_fmt = f"€{req.valore/1e6:.1f}M" if req.valore and req.valore >= 1e6 else f"€{req.valore:,.0f}" if req.valore else "—"

    prompt = f"""Cerca informazioni aggiornate su questo appalto pubblico italiano:

Nome: {req.nome}
CIG: {req.cig or "non disponibile"}
Stazione appaltante: {req.stazione or "non disponibile"}
Importo: {valore_fmt}
Città: {req.citta or "non disponibile"}

Cerca notizie, comunicati stampa, documenti ufficiali relativi a questo appalto.
Trova informazioni su: imprese aggiudicatarie, progettisti/architetti coinvolti, stato avanzamento lavori, eventuali controversie o varianti.

Rispondi in italiano con un breve report strutturato in HTML usando solo questi tag: <p>, <strong>, <ul>, <li>.
Se non trovi informazioni specifiche, dillo chiaramente. Non inventare dati."""

    async with httpx.AsyncClient(timeout=55) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1000,
                  "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                  "messages": [{"role": "user", "content": prompt}]}
        )
    if not resp.is_success:
        raise HTTPException(status_code=502, detail=f"Errore API: {resp.status_code}")
    data = resp.json()
    text_block = next((b for b in data.get("content", []) if b.get("type") == "text"), None)
    if not text_block:
        raise HTTPException(status_code=502, detail="Nessuna risposta dalla AI")
    return {"html": text_block["text"]}
