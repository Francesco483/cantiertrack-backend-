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

class AISearchRequest(BaseModel):
    query: Optional[str] = ""
    categorie: list[str] = ["infrastrutture", "edilizia pubblica", "PNRR"]

class CantierInfoRequest(BaseModel):
    nome: str
    cig: Optional[str] = ""
    stazione: Optional[str] = ""
    valore: Optional[float] = 0
    citta: Optional[str] = ""
    cerca_articoli: Optional[bool] = False

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


@app.post("/api/ai/cerca-cantiere")
async def ai_cerca_cantiere(req: CantierInfoRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="API key Anthropic non configurata")

    valore_fmt = (
        f"€{req.valore/1e6:.1f}M" if req.valore and req.valore >= 1e6
        else f"€{req.valore:,.0f}" if req.valore
        else "—"
    )

    prompt = f"""Sei un ricercatore specializzato in appalti pubblici italiani con accesso alla ricerca web.

Devi trovare ARTICOLI DI GIORNALE e notizie reali su questo appalto pubblico:
- Nome gara: {req.nome}
- CIG: {req.cig or "n/d"}
- Stazione appaltante: {req.stazione or "n/d"}
- Importo: {valore_fmt}
- Città/luogo: {req.citta or "n/d"}

ISTRUZIONI:
1. Cerca su Google News, testate locali e nazionali italiane articoli che menzionino questa gara o questi lavori.
2. Per ogni articolo trovato crea una voce con: titolo dell'articolo, nome della testata, data di pubblicazione, link cliccabile completo, e 1-2 righe di riassunto.
3. Cerca anche: aggiudicatario della gara (impresa vincitrice), eventuali ricorsi al TAR, avanzamento lavori, inaugurazioni.
4. Se trovi risultati sul portale ANAC o MIT, includili.

Formato risposta HTML OBBLIGATORIO — usa SOLO questi tag:
- <p> per testo normale
- <strong> per titoli sezione  
- <ul><li> per liste
- <a href="URL_COMPLETO"> per ogni link — l'URL deve essere completo (https://...)

Esempio formato articolo:
<ul>
<li><strong>Titolo articolo</strong> — <a href="https://www.testata.it/articolo">La Repubblica, 12 marzo 2025</a><br>Breve riassunto di 1-2 righe.</li>
</ul>

Se non trovi articoli specifici su questa gara, cerca notizie sui lavori in quella città/zona e sulla stazione appaltante.
NON inventare URL. Se un URL non è verificabile, scrivi solo il testo senza link.
Massimo 300 parole. Niente markdown o backtick."""

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 2000,
                "system": "Sei un ricercatore di appalti pubblici italiani. Rispondi SEMPRE direttamente in HTML, senza frasi introduttive come 'Faro una ricerca' o 'Cerchero'. Inizia subito con il contenuto trovato.",
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            }
        )
    if not resp.is_success:
        raise HTTPException(status_code=502, detail=f"Errore API: {resp.status_code}")

    data = resp.json()
    content_blocks = data.get("content", [])

    # Raccoglie TUTTI i blocchi di testo — Claude risponde in piu blocchi
    # dopo aver usato il tool web_search internamente
    testi = [b["text"] for b in content_blocks if b.get("type") == "text"]

    if not testi:
        raise HTTPException(status_code=502, detail="Nessuna risposta dalla AI")

    html_finale = "\n".join(testi).strip()

    # Rimuove eventuali frasi introduttive generiche prima del vero contenuto
    frasi_da_rimuovere = [
        "faro una ricerca", "procedo con la ricerca", "sto cercando",
        "effettuero una ricerca", "cerchero informazioni", "effettuo una ricerca"
    ]
    testo_lower = html_finale.lower()
    for frase in frasi_da_rimuovere:
        if frase in testo_lower:
            for tag in ["<ul>", "<p>", "<strong>"]:
                idx = html_finale.find(tag)
                if idx > 0:
                    html_finale = html_finale[idx:]
                    break

    return {"html": html_finale}


class BOTRequest(BaseModel):
    nome: str
    cig: Optional[str] = ""
    stazione: Optional[str] = ""
    valore: Optional[float] = 0
    citta: Optional[str] = ""
    tipo: Optional[str] = ""
    tipoIntervento: Optional[str] = ""
    tipoProcedura: Optional[str] = ""

@app.post("/api/ai/bot-ontology")
async def ai_bot_ontology(req: BOTRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="API key Anthropic non configurata")

    valore_fmt = (
        f"€{req.valore/1e6:.1f}M" if req.valore and req.valore >= 1e6
        else f"€{req.valore:,.0f}" if req.valore
        else "—"
    )

    prompt = f"""Sei un esperto di ontologie BIM e della Building Topology Ontology (BOT) W3C.

Analizza questo appalto pubblico italiano e genera la struttura BOT dei lavori:
- Nome gara: {req.nome}
- Tipo intervento: {req.tipoIntervento or req.tipo or "n/d"}
- Importo: {valore_fmt}
- Città: {req.citta or "n/d"}
- Stazione appaltante: {req.stazione or "n/d"}
- CIG: {req.cig or "n/d"}

REGOLE FONDAMENTALI:
1. Il grafo BOT rappresenta SOLO le parti oggetto dei lavori, NON l'intero edificio/infrastruttura.
2. Se è una nuova costruzione: includi tutta la struttura.
3. Se è una ristrutturazione/manutenzione parziale: includi SOLO le parti toccate dai lavori.
4. Distingui tra nodi CERTI (esplicitamente menzionati nella descrizione) e DEDOTTI (inferiti dal tipo di lavoro).

Rispondi SOLO con un JSON valido con questa struttura esatta:
{{
  "site": {{
    "label": "nome città o zona geografica",
    "certo": true
  }},
  "building": {{
    "label": "nome opera o edificio (max 3 parole)",
    "certo": true
  }},
  "storeys": [
    {{"label": "nome piano o zona (max 3 parole)", "certo": true}},
    {{"label": "altro piano se presente", "certo": false}}
  ],
  "spaces": [
    {{"label": "zona o ambiente (max 3 parole)", "storey_index": 0, "certo": true}},
    {{"label": "altra zona", "storey_index": 0, "certo": false}}
  ],
  "elements": [
    {{"label": "componente edilizio (max 3 parole)", "space_index": 0, "certo": true}},
    {{"label": "altro componente", "space_index": 1, "certo": false}}
  ],
  "sub_elements": [
    {{"label": "sotto-componente (max 3 parole)", "element_index": 0, "certo": false}}
  ]
}}

Linee guida per i nodi:
- storeys: 1-3 elementi (piani, livelli, tratte, zone geografiche dell'opera)
- spaces: 2-5 elementi (ambienti, zone funzionali, parti dell'opera)
- elements: 2-5 elementi (componenti costruttivi toccati dai lavori)
- sub_elements: 0-2 elementi (sotto-componenti solo se chiaramente inferibili)
- Etichette brevi e precise, in italiano, max 3 parole
- Rispondi SOLO con il JSON, nessun testo prima o dopo."""

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1200,
                "system": "Sei un esperto BOT/BIM. Rispondi SOLO con JSON valido, zero testo aggiuntivo.",
                "messages": [{"role": "user", "content": prompt}]
            }
        )
    if not resp.is_success:
        raise HTTPException(status_code=502, detail=f"Errore API: {resp.status_code}")

    data = resp.json()
    text_block = next((b for b in data.get("content", []) if b.get("type") == "text"), None)
    if not text_block:
        raise HTTPException(status_code=502, detail="Nessuna risposta dalla AI")

    text = text_block["text"].strip().replace("```json", "").replace("```", "").strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        raise HTTPException(status_code=502, detail="Formato JSON non valido")

    bot_data = json.loads(text[s:e+1])
    return bot_data
