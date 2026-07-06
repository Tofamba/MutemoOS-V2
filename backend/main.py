"""
Mutemo Desk — Zimbabwe Legal Practice Operating System
FastAPI backend v1.0
Mutemo = Law/Rule in Shona
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List
from contextlib import asynccontextmanager
import anthropic
import subprocess
import tempfile
import os
import json
import uuid
import re
import smtplib
import asyncio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta

# ── Load .env file if present (simple built-in loader, no extra dependency) ────
def _load_dotenv():
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value

_load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Module is fully loaded by the time this runs, so reminder_scheduler_loop exists
    asyncio.create_task(reminder_scheduler_loop())
    # Pre-load embedding model + ChromaDB in the background so the first
    # search isn't slow. Non-fatal if it fails — search falls back to keyword.
    async def warm_up():
        try:
            await asyncio.to_thread(get_embedding_model)
            await asyncio.to_thread(get_chroma_collections)
            print("[startup] semantic search ready")
        except Exception as e:
            print(f"[startup] semantic search unavailable, will use keyword fallback: {e}")
    asyncio.create_task(warm_up())
    yield

app = FastAPI(title="Mutemo Desk", version="1.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Request size limit (50MB) — prevents accidental memory exhaustion ──────────
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB

@app.middleware("http")
async def size_limit_middleware(request, call_next):
    if request.method == "POST":
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_UPLOAD_SIZE:
            return JSONResponse(
                status_code=413,
                content={"detail": "File too large. Maximum upload size is 50MB."}
            )
    return await call_next(request)

# ── Basic Auth (optional — enabled if MUTEMO_PASSWORD is set in .env) ───────────
import base64
import hmac

MUTEMO_USERNAME = os.environ.get("MUTEMO_USERNAME", "mutemo")
MUTEMO_PASSWORD = os.environ.get("MUTEMO_PASSWORD")  # if unset, auth is disabled

@app.middleware("http")
async def basic_auth_middleware(request, call_next):
    if not MUTEMO_PASSWORD:
        return await call_next(request)  # auth disabled — no password configured

    # Allow health check without auth (useful for monitoring)
    if request.url.path == "/api/health":
        return await call_next(request)

    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8")
            username, _, password = decoded.partition(":")
            user_ok = hmac.compare_digest(username, MUTEMO_USERNAME)
            pass_ok = hmac.compare_digest(password, MUTEMO_PASSWORD)
            if user_ok and pass_ok:
                return await call_next(request)
        except Exception:
            pass

    return JSONResponse(
        status_code=401,
        content={"detail": "Authentication required"},
        headers={"WWW-Authenticate": 'Basic realm="Mutemo Desk"'},
    )

frontend_path = os.path.join(os.path.dirname(__file__), "../frontend")
assets_path = os.path.join(frontend_path, "assets")
if os.path.exists(assets_path):
    app.mount("/assets", StaticFiles(directory=assets_path), name="assets")

client = anthropic.Anthropic()

# ── Semantic Search: embeddings + vector store ─────────────────────────────────
# Local embedding model (sentence-transformers) — no external API key needed.
# Model downloads once (~80MB) and is cached for subsequent runs.
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"

_embedding_model = None
_chroma_client = None
_firm_collection = None
_legal_collection = None
_zlr_collection = None

def get_embedding_model():
    """Lazily load the sentence-transformers model (avoids slow startup if unused)."""
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        print(f"[embeddings] loading model '{EMBEDDING_MODEL_NAME}' (first run downloads ~80MB)...")
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        print("[embeddings] model loaded")
    return _embedding_model

def embed_texts(texts: list) -> list:
    """Convert a list of strings into embedding vectors."""
    model = get_embedding_model()
    vectors = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
    return vectors.tolist()

def get_chroma_collections():
    """Lazily initialize ChromaDB and return (firm_collection, legal_collection, zlr_collection)."""
    global _chroma_client, _firm_collection, _legal_collection, _zlr_collection
    if _chroma_client is None:
        import chromadb
        chroma_path = os.path.join(os.path.dirname(__file__), "..", "data", "chroma")
        os.makedirs(chroma_path, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=chroma_path)
        _firm_collection = _chroma_client.get_or_create_collection(
            "firm_precedents", metadata={"hnsw:space": "cosine"}
        )
        _legal_collection = _chroma_client.get_or_create_collection(
            "legal_updates", metadata={"hnsw:space": "cosine"}
        )
        _zlr_collection = _chroma_client.get_or_create_collection(
            "zlr_index", metadata={"hnsw:space": "cosine"}
        )
        print("[vector_store] ChromaDB initialized")
    return _firm_collection, _legal_collection, _zlr_collection

# ── In-memory store (pilot) — persisted to disk as JSON ────────────────────────
matters_db: dict = {}
documents_db: dict = {}
chunks_db: list = []
calendar_db: list = []

# Legal Updates — separate collection for legislation & case law (ZimLII / Veritas etc.)
legal_updates_db: dict = {}
legal_update_chunks: list = []

# Zimbabwe Law Reports Index — dedicated collection for ZLR headnotes
zlr_db: dict = {}
zlr_chunks: list = []

# Reminder settings (pilot — single firm/user)
reminder_settings: dict = {
    "enabled": False,
    "recipient_email": None,
    "send_hour_utc": 5,  # default ~7am CAT (UTC+2)
    "last_run_date": None,  # tracks daily dedupe
}

# ── Persistence ──────────────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
STATE_FILE = os.path.join(DATA_DIR, "mutemo_state.json")

def save_state():
    """Persist all in-memory stores to a JSON file on disk."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        state = {
            "matters_db": matters_db,
            "documents_db": documents_db,
            "chunks_db": chunks_db,
            "calendar_db": calendar_db,
            "legal_updates_db": legal_updates_db,
            "legal_update_chunks": legal_update_chunks,
            "zlr_db": zlr_db,
            "zlr_chunks": zlr_chunks,
            "reminder_settings": reminder_settings,
        }
        tmp_path = STATE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp_path, STATE_FILE)  # atomic write
    except Exception as e:
        print(f"[persistence] failed to save state: {e}")

def load_state():
    """Load persisted state from disk into the in-memory stores, if present."""
    global matters_db, documents_db, chunks_db, calendar_db
    global legal_updates_db, legal_update_chunks, reminder_settings

    if not os.path.exists(STATE_FILE):
        print("[persistence] no existing state file — starting fresh")
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        matters_db.clear(); matters_db.update(state.get("matters_db", {}))
        documents_db.clear(); documents_db.update(state.get("documents_db", {}))
        chunks_db[:] = state.get("chunks_db", [])
        calendar_db[:] = state.get("calendar_db", [])
        legal_updates_db.clear(); legal_updates_db.update(state.get("legal_updates_db", {}))
        legal_update_chunks[:] = state.get("legal_update_chunks", [])
        zlr_db.clear(); zlr_db.update(state.get("zlr_db", {}))
        zlr_chunks[:] = state.get("zlr_chunks", [])
        reminder_settings.update(state.get("reminder_settings", {}))
        print(f"[persistence] loaded state: {len(matters_db)} matters, "
              f"{len(documents_db)} documents, {len(calendar_db)} calendar events, "
              f"{len(legal_updates_db)} legal updates")
    except Exception as e:
        print(f"[persistence] failed to load state: {e} — starting fresh")

load_state()

# ── Models ────────────────────────────────────────────────────────────────────

class MatterCreate(BaseModel):
    name: str
    number: Optional[str] = None
    matter_type: Optional[str] = None

class AffidavitRequest(BaseModel):
    matter_type: Optional[str] = None
    court: Optional[str] = "High Court of Zimbabwe"
    deponent_name: Optional[str] = None
    deponent_id: Optional[str] = None
    deponent_capacity: Optional[str] = None
    parties: Optional[str] = None
    matter_summary: str
    key_facts: Optional[str] = None
    relief: Optional[str] = None
    precedent_context: Optional[dict] = None

class SearchRequest(BaseModel):
    query: str
    matter_type: Optional[str] = None
    document_type: Optional[str] = None
    matter_id: Optional[str] = None
    limit: int = 8
    include_legal_updates: bool = True

class ExportRequest(BaseModel):
    affidavit_text: str
    deponent_name: Optional[str] = "Deponent"
    document_id: Optional[str] = "DOC"

class CalendarEvent(BaseModel):
    title: str
    matter_id: Optional[str] = None
    matter_name: Optional[str] = None
    event_type: str
    date: str
    time: Optional[str] = None
    court: Optional[str] = None
    notes: Optional[str] = None

class LegalUpdateSearchRequest(BaseModel):
    query: str
    source_type: Optional[str] = None  # "legislation" or "case_law"
    limit: int = 8

class ReminderSettings(BaseModel):
    enabled: bool
    recipient_email: str
    send_hour_utc: int = 5  # 0-23

# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    import shutil

    embeddings_ok = False
    embeddings_error = None
    try:
        if _embedding_model is not None:
            embeddings_ok = True  # already loaded
        else:
            import sentence_transformers  # cheap import check, doesn't load the model
            embeddings_ok = True
    except Exception as e:
        embeddings_error = str(e)

    deps = {
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "tesseract": shutil.which("tesseract") is not None,
        "pdftoppm": shutil.which("pdftoppm") is not None,
        "node": shutil.which("node") is not None,
        "smtp_configured": is_smtp_configured(),
        "semantic_search": embeddings_ok,
    }
    status = "ok" if deps["anthropic_key"] else "degraded"
    result = {
        "status": status,
        "version": "1.1.0",
        "service": "Mutemo Desk",
        "dependencies": deps,
        "notes": {
            "tesseract": "Required for OCR on scanned PDFs. Without it, scanned documents won't be searchable.",
            "node": "Required for DOCX export. Without it, affidavits can be copied as text but not downloaded as Word files.",
            "smtp_configured": "Required for email reminders. Without it, the Calendar reminder feature is disabled.",
            "semantic_search": "Powers meaning-based search. If false, search falls back to keyword matching.",
        }
    }
    if embeddings_error:
        result["embeddings_error"] = embeddings_error
    return result

@app.post("/api/admin/reindex")
async def reindex_semantic_search():
    """
    One-time migration: embed all existing chunks (uploaded before semantic
    search was added) and store them in ChromaDB. Safe to call multiple times
    — re-adding the same IDs simply overwrites them.
    """
    try:
        firm_col, legal_col = get_chroma_collections()
        before_firm = firm_col.count()
        before_legal = legal_col.count()

        await asyncio.to_thread(index_chunks_in_chroma, chunks_db, "firm")
        await asyncio.to_thread(index_chunks_in_chroma, legal_update_chunks, "legal")

        return {
            "reindexed": True,
            "firm_chunks": len(chunks_db),
            "legal_chunks": len(legal_update_chunks),
            "firm_collection_count": firm_col.count(),
            "legal_collection_count": legal_col.count(),
            "before": {"firm": before_firm, "legal": before_legal},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Re-index failed: {e}")

# ── Matters ───────────────────────────────────────────────────────────────────

@app.get("/api/matters")
async def list_matters():
    matters = list(matters_db.values())
    matters.sort(key=lambda x: x["created_at"], reverse=True)
    return matters

@app.post("/api/matters")
async def create_matter(matter: MatterCreate):
    mid = str(uuid.uuid4())
    obj = {
        "id": mid,
        "name": matter.name,
        "number": matter.number,
        "matter_type": matter.matter_type,
        "created_at": datetime.utcnow().isoformat(),
        "document_count": 0,
    }
    matters_db[mid] = obj
    save_state()
    return obj

@app.delete("/api/matters/{matter_id}")
async def delete_matter(matter_id: str):
    if matter_id not in matters_db:
        raise HTTPException(status_code=404, detail="Matter not found")
    del matters_db[matter_id]
    to_remove = [d for d, v in documents_db.items() if v["matter_id"] == matter_id]
    for d in to_remove:
        del documents_db[d]
    global chunks_db
    removed_chunk_ids = [c["id"] for c in chunks_db if c["matter_id"] == matter_id]
    chunks_db = [c for c in chunks_db if c["matter_id"] != matter_id]
    await asyncio.to_thread(remove_chunks_from_chroma, removed_chunk_ids, "firm")
    save_state()
    return {"deleted": True}

# ── Documents ─────────────────────────────────────────────────────────────────

@app.get("/api/matters/{matter_id}/documents")
async def list_documents(matter_id: str):
    docs = [d for d in documents_db.values() if d["matter_id"] == matter_id]
    docs.sort(key=lambda x: x["uploaded_at"], reverse=True)
    return docs

@app.post("/api/upload")
async def upload_document(
    file: UploadFile = File(...),
    matter_id: str = Form(...),
):
    if matter_id not in matters_db:
        raise HTTPException(status_code=404, detail="Matter not found")

    content = await file.read()
    filename = file.filename or "document"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "bin"

    text = ""
    word_count = 0
    page_count = 1
    ocr_used = False

    try:
        if ext == "pdf":
            text, page_count, ocr_used = extract_pdf_text(content)
        elif ext in ("docx", "doc"):
            text = extract_docx_text(content)
        elif ext in ("txt", "eml", "msg"):
            text = content.decode("utf-8", errors="replace")
        else:
            text = content.decode("utf-8", errors="replace")
        word_count = len(text.split())
    except Exception:
        text = ""
        word_count = 0

    doc_id = str(uuid.uuid4())
    metadata = {}
    if text:
        try:
            metadata = await asyncio.to_thread(classify_document_sync, text[:2000])
        except Exception:
            metadata = {}

    doc = {
        "id": doc_id,
        "matter_id": matter_id,
        "filename": filename,
        "document_type": metadata.get("document_type"),
        "parties": metadata.get("parties"),
        "doc_date": metadata.get("doc_date"),
        "court": metadata.get("court"),
        "word_count": word_count,
        "chunk_count": 0,
        "status": "processing",
        "ocr_used": ocr_used,
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    documents_db[doc_id] = doc
    matters_db[matter_id]["document_count"] = matters_db[matter_id].get("document_count", 0) + 1

    if text:
        new_chunks = chunk_text(text, page_count, doc_id, matter_id)
        chunks_db.extend(new_chunks)
        doc["chunk_count"] = len(new_chunks)
        doc["status"] = "complete"
        await asyncio.to_thread(index_chunks_in_chroma, new_chunks, "firm")
    else:
        doc["status"] = "error"
        doc["error_message"] = "Could not extract text"

    save_state()
    return doc

# ── Legal Updates (Legislation & Case Law — ZimLII / Veritas etc.) ─────────────

@app.get("/api/legal-updates")
async def list_legal_updates(source_type: Optional[str] = None):
    items = list(legal_updates_db.values())
    if source_type:
        items = [i for i in items if i["source_type"] == source_type]
    items.sort(key=lambda x: x["uploaded_at"], reverse=True)
    return items

@app.post("/api/legal-updates/upload")
async def upload_legal_update(
    file: UploadFile = File(...),
    source_type: str = Form(...),      # "legislation" or "case_law"
    source_name: str = Form("ZimLII"), # attribution — e.g. "ZimLII", "Veritas"
    reference: Optional[str] = Form(None),  # e.g. "SI 76 of 2025", "HH 123-25"
):
    if source_type not in ("legislation", "case_law"):
        raise HTTPException(status_code=400, detail="source_type must be 'legislation' or 'case_law'")

    content = await file.read()
    filename = file.filename or "document"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "bin"

    text = ""
    word_count = 0
    page_count = 1
    ocr_used = False

    try:
        if ext == "pdf":
            text, page_count, ocr_used = extract_pdf_text(content)
        elif ext in ("docx", "doc"):
            text = extract_docx_text(content)
        else:
            text = content.decode("utf-8", errors="replace")
        word_count = len(text.split())
    except Exception:
        text = ""
        word_count = 0

    item_id = str(uuid.uuid4())
    metadata = {}
    if text:
        try:
            metadata = await asyncio.to_thread(classify_document_sync, text[:2000])
        except Exception:
            metadata = {}

    item = {
        "id": item_id,
        "filename": filename,
        "source_type": source_type,           # legislation | case_law
        "source_name": source_name,            # attribution e.g. "ZimLII"
        "reference": reference,                # e.g. "SI 76 of 2025"
        "document_type": metadata.get("document_type"),
        "matter_type": metadata.get("matter_type"),
        "doc_date": metadata.get("doc_date"),
        "court": metadata.get("court"),
        "word_count": word_count,
        "chunk_count": 0,
        "status": "processing",
        "ocr_used": ocr_used,
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    legal_updates_db[item_id] = item

    if text:
        new_chunks = chunk_text(text, page_count, item_id, "legal_updates")
        for c in new_chunks:
            c["source_type"] = source_type
            c["source_name"] = source_name
            c["reference"] = reference
        legal_update_chunks.extend(new_chunks)
        item["chunk_count"] = len(new_chunks)
        item["status"] = "complete"
        await asyncio.to_thread(index_chunks_in_chroma, new_chunks, "legal")
    else:
        item["status"] = "error"
        item["error_message"] = "Could not extract text"

    save_state()
    return item

@app.delete("/api/legal-updates/{item_id}")
async def delete_legal_update(item_id: str):
    if item_id not in legal_updates_db:
        raise HTTPException(status_code=404, detail="Not found")
    del legal_updates_db[item_id]
    global legal_update_chunks
    removed_chunk_ids = [c["id"] for c in legal_update_chunks if c["document_id"] == item_id]
    legal_update_chunks = [c for c in legal_update_chunks if c["document_id"] != item_id]
    await asyncio.to_thread(remove_chunks_from_chroma, removed_chunk_ids, "legal")
    save_state()
    return {"deleted": True}

@app.post("/api/legal-updates/search")
async def search_legal_updates(req: LegalUpdateSearchRequest):
    if not legal_update_chunks:
        return {"answer": None, "results": [], "message": "No legislation or case law indexed yet. Upload documents to Legal Updates."}

    query_words = set(req.query.lower().split())
    scored = []

    for chunk in legal_update_chunks:
        if req.source_type and chunk.get("source_type") != req.source_type:
            continue
        item = legal_updates_db.get(chunk["document_id"], {})

        chunk_words = set(chunk["text"].lower().split())
        overlap = len(query_words & chunk_words)
        total = len(query_words | chunk_words)
        score = overlap / total if total > 0 else 0
        if req.query.lower() in chunk["text"].lower():
            score += 0.3
        if score > 0:
            scored.append((score, chunk, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:req.limit]

    if not top:
        return {"answer": None, "results": [], "message": f'No relevant legislation or case law found for: "{req.query}"'}

    results = []
    for score, chunk, item in top:
        results.append({
            "chunk_id": chunk["id"],
            "text": chunk["text"],
            "similarity": round(score, 3),
            "document_id": chunk["document_id"],
            "filename": item.get("filename", "Unknown"),
            "source_type": item.get("source_type"),
            "source_name": item.get("source_name"),
            "reference": item.get("reference"),
            "document_type": item.get("document_type"),
            "doc_date": item.get("doc_date"),
            "court": item.get("court"),
            "page_number": chunk.get("page_number"),
            "chunk_index": chunk["chunk_index"],
        })

    return {"answer": None, "results": results}

def extract_pdf_text(content: bytes):
    """Extract text from PDF. Falls back to OCR for scanned/image-only pages.
    Returns (text, page_count, ocr_used)"""
    try:
        import pdfplumber, io
        pages = []
        needs_ocr_pages = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for i, page in enumerate(pdf.pages):
                t = page.extract_text()
                if t and t.strip():
                    pages.append(t)
                else:
                    pages.append(None)
                    needs_ocr_pages.append(i)

        total_pages = len(pages)
        ocr_used = bool(needs_ocr_pages)

        if needs_ocr_pages:
            ocr_results = ocr_pdf_pages(content, needs_ocr_pages)
            for i, ocr_text in ocr_results.items():
                pages[i] = ocr_text

        final_pages = [p for p in pages if p and p.strip()]
        return "\n\n".join(final_pages), max(1, total_pages), ocr_used
    except Exception:
        return content.decode("utf-8", errors="replace"), 1, False


def ocr_pdf_pages(content: bytes, page_indices: list) -> dict:
    """
    Run OCR on specific pages of a PDF (scanned/image-only pages).
    Uses pdftoppm to rasterize pages, then tesseract for text recognition.
    Returns {page_index: extracted_text}
    """
    import subprocess as sp

    results = {}
    if not page_indices:
        return results

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        with open(pdf_path, "wb") as f:
            f.write(content)

        for idx in page_indices:
            page_num = idx + 1  # pdftoppm is 1-indexed
            try:
                img_prefix = os.path.join(tmpdir, f"page_{page_num}")
                sp.run(
                    ["pdftoppm", "-png", "-r", "200", "-f", str(page_num), "-l", str(page_num), pdf_path, img_prefix],
                    capture_output=True, timeout=60, check=False
                )
                candidates = [f"{img_prefix}-{page_num}.png", f"{img_prefix}.png", f"{img_prefix}-1.png"]
                img_path = next((c for c in candidates if os.path.exists(c)), None)
                if not img_path:
                    for fn in os.listdir(tmpdir):
                        if fn.startswith(f"page_{page_num}") and fn.endswith(".png"):
                            img_path = os.path.join(tmpdir, fn)
                            break
                if not img_path:
                    continue

                ocr_result = sp.run(
                    ["tesseract", img_path, "stdout", "-l", "eng"],
                    capture_output=True, text=True, timeout=60, check=False
                )
                text = ocr_result.stdout.strip()
                if text:
                    results[idx] = text
            except Exception:
                continue

    return results

def extract_docx_text(content: bytes):
    try:
        import docx, io
        doc = docx.Document(io.BytesIO(content))
        return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
    except Exception:
        return content.decode("utf-8", errors="replace")

def chunk_text(text: str, page_count: int, doc_id: str, matter_id: str) -> list:
    CHUNK_WORDS = 500
    OVERLAP_WORDS = 50
    words = text.split()
    if not words:
        return []
    chunks = []
    start = 0
    idx = 0
    while start < len(words):
        end = min(start + CHUNK_WORDS, len(words))
        chunk_str = " ".join(words[start:end])
        progress = start / len(words)
        page_num = max(1, round(progress * page_count))
        chunks.append({
            "id": str(uuid.uuid4()),
            "document_id": doc_id,
            "matter_id": matter_id,
            "text": chunk_str,
            "chunk_index": idx,
            "page_number": page_num,
        })
        idx += 1
        start = end - OVERLAP_WORDS
        if start >= len(words) - OVERLAP_WORDS:
            break
    return chunks

def index_chunks_in_chroma(chunks: list, collection_type: str = "firm"):
    """
    Embed a list of chunks and store them in the appropriate ChromaDB collection.
    collection_type: "firm", "legal", or "zlr"
    """
    if not chunks:
        return
    try:
        firm_col, legal_col, zlr_col = get_chroma_collections()
        if collection_type == "firm":
            collection = firm_col
        elif collection_type == "legal":
            collection = legal_col
        else:
            collection = zlr_col

        texts = [c["text"] for c in chunks]
        ids = [c["id"] for c in chunks]
        embeddings = embed_texts(texts)

        metadatas = []
        for c in chunks:
            meta = {
                "document_id": c["document_id"],
                "matter_id": c.get("matter_id", "zlr"),
                "chunk_index": c["chunk_index"],
                "page_number": c.get("page_number") or 0,
            }
            metadatas.append(meta)

        collection.add(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    except Exception as e:
        print(f"[vector_store] failed to index chunks ({collection_type}): {e}")

def remove_chunks_from_chroma(chunk_ids: list, collection_type: str = "firm"):
    """Remove chunks from ChromaDB by ID. Non-fatal on failure."""
    if not chunk_ids:
        return
    try:
        firm_col, legal_col, zlr_col = get_chroma_collections()
        if collection_type == "firm":
            collection = firm_col
        elif collection_type == "legal":
            collection = legal_col
        else:
            collection = zlr_col
        collection.delete(ids=chunk_ids)
    except Exception as e:
        print(f"[vector_store] failed to remove chunks ({collection_type}): {e}")

def classify_document_sync(text_preview: str) -> dict:
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            messages=[{"role": "user", "content": f"""Zimbabwean law firm document classifier.
Return ONLY valid JSON with keys:
document_type, parties, matter_type, doc_date (YYYY-MM-DD or null), court (or null)

document_type options: affidavit, founding_affidavit, opposing_affidavit, replying_affidavit, lease_agreement, heads_of_argument, correspondence, court_order, summons, declaration, plea, notice_of_motion, deed_of_settlement, power_of_attorney, will_and_testament, contract, opinion, other

matter_type options: eviction, estate, employment, commercial_property, commercial_contract, customary_law, matrimonial, company_law, criminal, constitutional, other

Excerpt:
{text_preview[:2000]}

JSON only:"""}]
        )
        raw = msg.content[0].text
        m = re.search(r'\{[\s\S]*\}', raw)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}

# ── Zimbabwe Law Reports Index ─────────────────────────────────────────────────
# Dedicated collection for ZLR headnotes — photographed from physical volumes
# or downloaded from ZimLII. Separate from firm precedents and general legal updates.
# Each entry stores the structured headnote data extracted from the ZLR format.

JURISDICTION_MAP = {
    "ZimLII": "Zimbabwe",
    "ZLR": "Zimbabwe",
    "ZLR-Rhodesia": "Zimbabwe (Rhodesia)",
    "SAFLII": "South Africa",
    "SALR": "South Africa",
    "SACR": "South Africa",
    "BCLR": "South Africa",
    "BAILII": "England & Wales",
    "AllER": "England & Wales",
    "WLR": "England & Wales",
    "AC": "England & Wales",
    "Privy Council": "Privy Council",
    "Other": "Other",
}

AUTHORITY_WEIGHT = {
    "Zimbabwe": "Binding",
    "Zimbabwe (Rhodesia)": "Binding",
    "South Africa": "Highly Persuasive",
    "England & Wales": "Persuasive",
    "Privy Council": "Persuasive",
    "Other": "Persuasive",
}

def get_jurisdiction(source: str) -> str:
    return JURISDICTION_MAP.get(source, "Other")

def get_authority_weight(source: str) -> str:
    return AUTHORITY_WEIGHT.get(get_jurisdiction(source), "Persuasive")

ZLR_SUBJECT_TAXONOMY = {
    "constitutional": "Constitutional Law",
    "administrative": "Administrative Law & Review",
    "civil procedure": "Civil Procedure",
    "appeal": "Appeals & Review",
    "contract": "Contract Law",
    "property": "Property Law",
    "family": "Family Law & Matrimonial",
    "matrimonial": "Family Law & Matrimonial",
    "customary": "Customary Law & Succession",
    "succession": "Customary Law & Succession",
    "company": "Company & Commercial Law",
    "commercial": "Company & Commercial Law",
    "employment": "Employment & Labour Law",
    "labour": "Employment & Labour Law",
    "delict": "Delict",
    "criminal": "Criminal Law & Procedure",
    "revenue": "Revenue & Tax Law",
    "tax": "Revenue & Tax Law",
    "insolvency": "Insolvency & Sequestration",
    "liquidation": "Insolvency & Sequestration",
    "intellectual property": "Intellectual Property",
    "mining": "Environmental & Mining Law",
    "environmental": "Environmental & Mining Law",
    "human rights": "Human Rights",
    "stock exchange": "Company & Commercial Law",
    "banking": "Company & Commercial Law",
    "land": "Property Law",
    "evidence": "Civil Procedure",
    "prescription": "Civil Procedure",
    "costs": "Civil Procedure",
    "interdict": "Civil Procedure",
    "urgent": "Civil Procedure",
}

def classify_zlr_subject(subject_chains: list) -> str:
    """Map ZLR subject chains to our taxonomy category."""
    text = " ".join(subject_chains).lower()
    for keyword, category in ZLR_SUBJECT_TAXONOMY.items():
        if keyword in text:
            return category
    return "General"

def parse_zlr_headnote(text: str) -> dict:
    """
    Parse a ZLR headnote page (OCR'd from physical volume or copied from ZimLII).
    Extracts: citation, court, judge, case type, dates, subject chains, summary.

    ZLR format example:
      Gwatidzo NO v First Transfer Securities (Pvt) Ltd & Ors
      2014 (1) ZLR 459 (H)
      High Court, Harare        Judgment No. HH-165-14
      Makoni J
      Chamber application
      11 November 2013; CAV
      Date of Judgment: 3 April 2014
      [italic subject chains]
      [summary text]
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    result = {
        "citation": None,
        "case_name": None,
        "court": None,
        "judgment_number": None,
        "judge": None,
        "case_type": None,
        "hearing_date": None,
        "judgment_date": None,
        "subject_chains": [],
        "taxonomy_category": None,
        "summary": None,
        "zimlii_url": None,
    }

    # Extract ZLR citation (e.g. "2014 (1) ZLR 459 (H)")
    import re
    for line in lines:
        if re.search(r'\d{4}\s*\(\d+\)\s*ZLR\s*\d+', line):
            result["citation"] = line.strip()
            break
        # Also handle ZimLII format (e.g. "HH-165-14" or "SC 45/2023")
        if re.search(r'(HH|SC|CCZ|LC|HB|HM|HMT)-?\d+[-/]\d+', line):
            result["judgment_number"] = line.strip()

    # Extract judgment number (HH-165-14 pattern)
    for line in lines:
        m = re.search(r'(?:Judgment No\.?\s*)?((?:HH|SC|CCZ|LC|HB|HM|HMT)[-\s]?\d+[-/]\d+)', line, re.IGNORECASE)
        if m:
            result["judgment_number"] = m.group(1).strip()
            break

    # Extract court
    courts = ["High Court, Harare", "High Court, Bulawayo", "High Court, Masvingo",
              "High Court, Mutare", "Supreme Court", "Constitutional Court",
              "Labour Court", "Administrative Court", "Magistrates Court"]
    for line in lines:
        for court in courts:
            if court.lower() in line.lower():
                result["court"] = court
                break

    # Extract case name (usually first substantive line, all caps or mixed)
    for line in lines[:5]:
        if re.search(r'\bv\b', line, re.IGNORECASE) and len(line) > 10:
            if not re.search(r'\d{4}.*ZLR', line):
                result["case_name"] = line.strip()
                break

    # Extract judge (ends in J, JA, CJ, DCJ, AJA, JP)
    for line in lines:
        if re.search(r'\b(J|JA|CJ|DCJ|AJA|JP|AJ)\b$', line.strip()):
            result["judge"] = line.strip()
            break

    # Extract case type
    case_types = ["Chamber application", "Urgent application", "Appeal", "Review",
                  "Action", "Application", "Trial", "Motion"]
    for line in lines:
        for ct in case_types:
            if ct.lower() == line.lower().strip():
                result["case_type"] = ct
                break

    # Extract dates
    for line in lines:
        if "Date of Judgment" in line or "Judgment date" in line.lower():
            result["judgment_date"] = re.sub(r'Date of Judgment:?\s*', '', line).strip()
        elif re.search(r'\d+\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}', line):
            if not result["hearing_date"]:
                result["hearing_date"] = line.strip()

    # Extract subject chains — lines with " – " or " — " pattern (ZLR style)
    chains = []
    for line in lines:
        if ' – ' in line or ' — ' in line or ' - ' in line:
            # Looks like a subject chain
            if not re.search(r'\d{4}.*ZLR', line):  # not a citation
                chains.append(line.strip())

    # Also catch ZimLII format: "Category — Sub-category — Specific point"
    for line in lines:
        if re.search(r'[A-Z][a-z]+ (law|procedure|Act|rights) —', line):
            if line not in chains:
                chains.append(line.strip())

    result["subject_chains"] = chains
    result["taxonomy_category"] = classify_zlr_subject(chains)

    # Summary — first substantial paragraph (50+ chars, not a chain or citation)
    for line in lines:
        if (len(line) > 50
                and ' – ' not in line and ' — ' not in line
                and not re.search(r'\d{4}.*ZLR', line)
                and not re.search(r'(HH|SC|CCZ)-?\d+', line)
                and line != result.get("case_name")
                and not re.search(r'\b(J|JA|CJ)\b$', line)):
            result["summary"] = line.strip()
            break

    return result

class ZLRUploadRequest(BaseModel):
    source: str = "ZLR"  # "ZLR" (physical volume) or "ZimLII" (downloaded)
    volume_year: Optional[str] = None  # e.g. "2014 (1)"
    zimlii_url: Optional[str] = None

@app.get("/api/zlr")
async def list_zlr_entries(category: Optional[str] = None, limit: int = 50):
    items = list(zlr_db.values())
    if category:
        items = [i for i in items if i.get("taxonomy_category") == category]
    items.sort(key=lambda x: x.get("uploaded_at", ""), reverse=True)
    return items[:limit]

@app.get("/api/zlr/categories")
async def zlr_categories():
    """Return all taxonomy categories and their case counts."""
    counts = {}
    for item in zlr_db.values():
        cat = item.get("taxonomy_category", "General")
        counts[cat] = counts.get(cat, 0) + 1
    return sorted([{"category": k, "count": v} for k, v in counts.items()],
                  key=lambda x: x["count"], reverse=True)

@app.post("/api/zlr/upload")
async def upload_zlr_document(
    file: UploadFile = File(...),
    source: str = Form("ZLR"),
    volume_year: Optional[str] = Form(None),
    zimlii_url: Optional[str] = Form(None),
):
    """Upload a ZLR headnote page (photographed from physical volume or ZimLII PDF)."""
    content = await file.read()
    filename = file.filename or "zlr_entry"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "bin"

    text = ""
    page_count = 1
    ocr_used = False

    try:
        if ext == "pdf":
            text, page_count, ocr_used = extract_pdf_text(content)
        elif ext in ("txt",):
            text = content.decode("utf-8", errors="replace")
        elif ext in ("jpg", "jpeg", "png", "webp"):
            # Image upload — run OCR directly
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                import subprocess as sp
                ocr_result = sp.run(
                    ["tesseract", tmp_path, "stdout", "-l", "eng"],
                    capture_output=True, text=True, timeout=60
                )
                text = ocr_result.stdout.strip()
                ocr_used = True
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        else:
            text = content.decode("utf-8", errors="replace")
    except Exception as e:
        text = ""

    if not text:
        raise HTTPException(status_code=422, detail="Could not extract text from document. Try a clearer photo or PDF.")

    # Parse the ZLR headnote structure
    parsed = parse_zlr_headnote(text)

    item_id = str(uuid.uuid4())
    jurisdiction = get_jurisdiction(source)
    authority_weight = get_authority_weight(source)
    item = {
        "id": item_id,
        "filename": filename,
        "source": source,
        "jurisdiction": jurisdiction,
        "authority_weight": authority_weight,
        "volume_year": volume_year,
        "zimlii_url": zimlii_url or parsed.get("zimlii_url"),
        "case_name": parsed.get("case_name") or filename,
        "citation": parsed.get("citation"),
        "judgment_number": parsed.get("judgment_number"),
        "court": parsed.get("court"),
        "judge": parsed.get("judge"),
        "case_type": parsed.get("case_type"),
        "hearing_date": parsed.get("hearing_date"),
        "judgment_date": parsed.get("judgment_date"),
        "subject_chains": parsed.get("subject_chains", []),
        "taxonomy_category": parsed.get("taxonomy_category", "General"),
        "summary": parsed.get("summary"),
        "raw_text": text,
        "word_count": len(text.split()),
        "chunk_count": 0,
        "ocr_used": ocr_used,
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    zlr_db[item_id] = item

    # Chunk and index
    # Each chunk gets enriched with ZLR metadata for better search context
    enriched_text = f"""CASE: {item['case_name'] or ''}
CITATION: {item['citation'] or ''}
JUDGMENT: {item['judgment_number'] or ''}
COURT: {item['court'] or ''}
JUDGE: {item['judge'] or ''}
CATEGORY: {item['taxonomy_category'] or ''}
SUBJECT: {' | '.join(item['subject_chains'])}
SUMMARY: {item['summary'] or ''}

FULL TEXT:
{text}"""

    new_chunks = chunk_text(enriched_text, page_count, item_id, "zlr")
    for c in new_chunks:
        c["zlr_item_id"] = item_id
        c["citation"] = item.get("citation")
        c["case_name"] = item.get("case_name")
        c["taxonomy_category"] = item.get("taxonomy_category")

    zlr_chunks.extend(new_chunks)
    item["chunk_count"] = len(new_chunks)
    await asyncio.to_thread(index_chunks_in_chroma, new_chunks, "zlr")

    save_state()
    return item

@app.delete("/api/zlr/{item_id}")
async def delete_zlr_entry(item_id: str):
    if item_id not in zlr_db:
        raise HTTPException(status_code=404, detail="Not found")
    del zlr_db[item_id]
    global zlr_chunks
    removed_ids = [c["id"] for c in zlr_chunks if c["document_id"] == item_id]
    zlr_chunks = [c for c in zlr_chunks if c["document_id"] != item_id]
    await asyncio.to_thread(remove_chunks_from_chroma, removed_ids, "zlr")
    save_state()
    return {"deleted": True}

@app.post("/api/zlr/search")
async def search_zlr(req: LegalUpdateSearchRequest):
    """Dedicated semantic search across the ZLR Index."""
    if not zlr_chunks:
        return {"results": [], "message": "No ZLR entries indexed yet."}

    results = await asyncio.to_thread(_zlr_semantic_search, req.query, req.source_type, req.limit)
    return {"results": results, "count": len(results)}

def _zlr_semantic_search(query: str, category_filter: Optional[str], limit: int) -> list:
    """Semantic search over ZLR index with optional category filter."""
    results = []
    try:
        _, _, zlr_col = get_chroma_collections()
        if zlr_col.count() > 0:
            query_vec = embed_texts([query])[0]
            res = zlr_col.query(
                query_embeddings=[query_vec],
                n_results=min(limit * 3, zlr_col.count())
            )
            ids = res["ids"][0] if res["ids"] else []
            distances = res["distances"][0] if res["distances"] else []
            chunk_by_id = {c["id"]: c for c in zlr_chunks}

            seen_items = set()
            for cid, dist in zip(ids, distances):
                chunk = chunk_by_id.get(cid)
                if not chunk:
                    continue
                item_id = chunk["document_id"]
                if item_id in seen_items:
                    continue
                item = zlr_db.get(item_id, {})
                if category_filter and item.get("taxonomy_category") != category_filter:
                    continue
                seen_items.add(item_id)
                similarity = max(0.0, 1.0 - dist)
                results.append({
                    "item_id": item_id,
                    "similarity": round(similarity, 3),
                    "case_name": item.get("case_name"),
                    "citation": item.get("citation"),
                    "judgment_number": item.get("judgment_number"),
                    "court": item.get("court"),
                    "judge": item.get("judge"),
                    "taxonomy_category": item.get("taxonomy_category"),
                    "subject_chains": item.get("subject_chains", []),
                    "summary": item.get("summary"),
                    "judgment_date": item.get("judgment_date"),
                    "source": item.get("source"),
                    "zimlii_url": item.get("zimlii_url"),
                    "relevant_excerpt": chunk["text"][:400],
                })
                if len(results) >= limit:
                    break
    except Exception as e:
        print(f"[zlr_search] error: {e}")
        # Keyword fallback
        query_words = set(query.lower().split())
        scored = []
        for item in zlr_db.values():
            if category_filter and item.get("taxonomy_category") != category_filter:
                continue
            text = " ".join([
                item.get("case_name", ""),
                item.get("citation", ""),
                item.get("summary", ""),
                " ".join(item.get("subject_chains", [])),
            ]).lower()
            score = len(query_words & set(text.split())) / max(len(query_words), 1)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        for score, item in scored[:limit]:
            results.append({
                "item_id": item["id"],
                "similarity": round(score, 3),
                "case_name": item.get("case_name"),
                "citation": item.get("citation"),
                "judgment_number": item.get("judgment_number"),
                "court": item.get("court"),
                "taxonomy_category": item.get("taxonomy_category"),
                "subject_chains": item.get("subject_chains", []),
                "summary": item.get("summary"),
                "judgment_date": item.get("judgment_date"),
                "source": item.get("source"),
                "zimlii_url": item.get("zimlii_url"),
                "relevant_excerpt": item.get("summary", ""),
            })
    return results

# ── Search ────────────────────────────────────────────────────────────────────

@app.post("/api/search")
async def search_documents(req: SearchRequest):
    results = await asyncio.to_thread(semantic_search_firm, req)
    legal_results = []
    if req.include_legal_updates:
        legal_results = await asyncio.to_thread(semantic_search_legal, req)

    # Include ZLR results if index has content
    zlr_results = []
    if zlr_chunks:
        zlr_req = LegalUpdateSearchRequest(query=req.query, limit=3)
        raw_zlr = await asyncio.to_thread(_zlr_semantic_search, req.query, None, 3)
        for r in raw_zlr:
            zlr_results.append({
                "result_source": "zlr",
                "chunk_id": r.get("item_id"),
                "text": r.get("relevant_excerpt", ""),
                "similarity": r.get("similarity", 0),
                "document_id": r.get("item_id"),
                "filename": r.get("case_name") or r.get("citation") or "ZLR Entry",
                "citation": r.get("citation"),
                "judgment_number": r.get("judgment_number"),
                "taxonomy_category": r.get("taxonomy_category"),
                "subject_chains": r.get("subject_chains", []),
                "summary": r.get("summary"),
                "court": r.get("court"),
                "doc_date": r.get("judgment_date"),
                "source": r.get("source"),
                "zimlii_url": r.get("zimlii_url"),
            })

    all_results = results + legal_results + zlr_results

    if not all_results:
        return {"answer": None, "results": [], "message": f'No relevant documents found for: "{req.query}"'}

    answer = await asyncio.to_thread(synthesise_answer_sync, req.query, results[:5], legal_results[:3])
    return {"answer": answer, "results": all_results}

def semantic_search_firm(req) -> list:
    """
    Semantic search over firm precedent chunks using ChromaDB + embeddings.
    Falls back to keyword overlap if the vector store is empty or unavailable
    (e.g. embedding model failed to load).
    """
    results = []
    used_semantic = False

    try:
        firm_col, _ = get_chroma_collections()
        if firm_col.count() > 0:
            used_semantic = True
            query_vec = embed_texts([req.query])[0]

            # Chroma 'where' filter — only matter_id is directly on chunk metadata
            where = {}
            if req.matter_id:
                where["matter_id"] = req.matter_id

            # Over-fetch to allow for post-filtering by document_type/matter_type
            n_fetch = max(req.limit * 4, 20)
            query_kwargs = {"query_embeddings": [query_vec], "n_results": n_fetch}
            if where:
                query_kwargs["where"] = where

            res = firm_col.query(**query_kwargs)

            ids = res["ids"][0] if res["ids"] else []
            docs_text = res["documents"][0] if res["documents"] else []
            metadatas = res["metadatas"][0] if res["metadatas"] else []
            distances = res["distances"][0] if res["distances"] else []

            chunk_by_id = {c["id"]: c for c in chunks_db}

            for cid, text, meta, dist in zip(ids, docs_text, metadatas, distances):
                chunk = chunk_by_id.get(cid)
                if not chunk:
                    continue
                doc = documents_db.get(chunk["document_id"], {})
                matter = matters_db.get(chunk["matter_id"], {})

                if req.document_type and doc.get("document_type") != req.document_type:
                    continue
                if req.matter_type and matter.get("matter_type") != req.matter_type:
                    continue

                # Cosine distance -> similarity (0..1, higher is better)
                similarity = max(0.0, 1.0 - dist)

                results.append({
                    "result_source": "firm",
                    "chunk_id": chunk["id"],
                    "text": chunk["text"],
                    "similarity": round(similarity, 3),
                    "document_id": chunk["document_id"],
                    "filename": doc.get("filename", "Unknown"),
                    "document_type": doc.get("document_type"),
                    "doc_date": doc.get("doc_date"),
                    "parties": doc.get("parties"),
                    "court": doc.get("court"),
                    "matter_id": chunk["matter_id"],
                    "matter_name": matter.get("name", "Unknown matter"),
                    "matter_number": matter.get("number"),
                    "matter_type": matter.get("matter_type"),
                    "page_number": chunk.get("page_number"),
                    "chunk_index": chunk["chunk_index"],
                })
                if len(results) >= req.limit:
                    break
    except Exception as e:
        print(f"[search] semantic search failed, falling back to keyword: {e}")
        used_semantic = False

    if used_semantic:
        return results

    # ── Keyword fallback (used if vector store empty or embedding failed) ──────
    query_words = set(req.query.lower().split())
    scored = []
    for chunk in chunks_db:
        if req.matter_id and chunk["matter_id"] != req.matter_id:
            continue
        doc = documents_db.get(chunk["document_id"], {})
        matter = matters_db.get(chunk["matter_id"], {})
        if req.document_type and doc.get("document_type") != req.document_type:
            continue
        if req.matter_type and matter.get("matter_type") != req.matter_type:
            continue

        chunk_words = set(chunk["text"].lower().split())
        overlap = len(query_words & chunk_words)
        total = len(query_words | chunk_words)
        score = overlap / total if total > 0 else 0
        if req.query.lower() in chunk["text"].lower():
            score += 0.3
        if score > 0:
            scored.append((score, chunk, doc, matter))

    scored.sort(key=lambda x: x[0], reverse=True)
    for score, chunk, doc, matter in scored[:req.limit]:
        results.append({
            "result_source": "firm",
            "chunk_id": chunk["id"],
            "text": chunk["text"],
            "similarity": round(score, 3),
            "document_id": chunk["document_id"],
            "filename": doc.get("filename", "Unknown"),
            "document_type": doc.get("document_type"),
            "doc_date": doc.get("doc_date"),
            "parties": doc.get("parties"),
            "court": doc.get("court"),
            "matter_id": chunk["matter_id"],
            "matter_name": matter.get("name", "Unknown matter"),
            "matter_number": matter.get("number"),
            "matter_type": matter.get("matter_type"),
            "page_number": chunk.get("page_number"),
            "chunk_index": chunk["chunk_index"],
        })
    return results

def semantic_search_legal(req) -> list:
    """Semantic search over legal updates (legislation & case law), with keyword fallback."""
    legal_results = []
    used_semantic = False

    try:
        _, legal_col = get_chroma_collections()
        if legal_col.count() > 0:
            used_semantic = True
            query_vec = embed_texts([req.query])[0]
            res = legal_col.query(query_embeddings=[query_vec], n_results=3)

            ids = res["ids"][0] if res["ids"] else []
            distances = res["distances"][0] if res["distances"] else []
            chunk_by_id = {c["id"]: c for c in legal_update_chunks}

            for cid, dist in zip(ids, distances):
                chunk = chunk_by_id.get(cid)
                if not chunk:
                    continue
                item = legal_updates_db.get(chunk["document_id"], {})
                similarity = max(0.0, 1.0 - dist)
                legal_results.append({
                    "result_source": "legal_update",
                    "chunk_id": chunk["id"],
                    "text": chunk["text"],
                    "similarity": round(similarity, 3),
                    "document_id": chunk["document_id"],
                    "filename": item.get("filename", "Unknown"),
                    "source_type": item.get("source_type"),
                    "source_name": item.get("source_name"),
                    "reference": item.get("reference"),
                    "document_type": item.get("document_type"),
                    "doc_date": item.get("doc_date"),
                    "court": item.get("court"),
                    "page_number": chunk.get("page_number"),
                    "chunk_index": chunk["chunk_index"],
                })
    except Exception as e:
        print(f"[search] legal semantic search failed, falling back to keyword: {e}")
        used_semantic = False

    if used_semantic:
        return legal_results

    # ── Keyword fallback ─────────────────────────────────────────────────────
    if not legal_update_chunks:
        return []
    query_words = set(req.query.lower().split())
    legal_scored = []
    for chunk in legal_update_chunks:
        item = legal_updates_db.get(chunk["document_id"], {})
        chunk_words = set(chunk["text"].lower().split())
        overlap = len(query_words & chunk_words)
        total = len(query_words | chunk_words)
        score = overlap / total if total > 0 else 0
        if req.query.lower() in chunk["text"].lower():
            score += 0.3
        if score > 0:
            legal_scored.append((score, chunk, item))

    legal_scored.sort(key=lambda x: x[0], reverse=True)
    for score, chunk, item in legal_scored[:3]:
        legal_results.append({
            "result_source": "legal_update",
            "chunk_id": chunk["id"],
            "text": chunk["text"],
            "similarity": round(score, 3),
            "document_id": chunk["document_id"],
            "filename": item.get("filename", "Unknown"),
            "source_type": item.get("source_type"),
            "source_name": item.get("source_name"),
            "reference": item.get("reference"),
            "document_type": item.get("document_type"),
            "doc_date": item.get("doc_date"),
            "court": item.get("court"),
            "page_number": chunk.get("page_number"),
            "chunk_index": chunk["chunk_index"],
        })
    return legal_results


def synthesise_answer_sync(query: str, results: list, legal_results: list = None) -> str:
    if not results and not legal_results:
        return None
    try:
        sections = []
        if results:
            firm_context = "\n\n---\n\n".join([
                f"[FIRM PRECEDENT: {r['filename']} | {r.get('document_type','document')} | {r['matter_name']}]\n{r['text']}"
                for r in results
            ])
            sections.append(firm_context)

        if legal_results:
            legal_context = "\n\n---\n\n".join([
                f"[{ 'LEGISLATION' if r.get('source_type')=='legislation' else 'CASE LAW' } — {r.get('reference') or r['filename']} (source: {r.get('source_name','ZimLII')}, CC BY-NC)]\n{r['text']}"
                for r in legal_results
            ])
            sections.append(legal_context)

        context = "\n\n===\n\n".join(sections)

        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": f"""Legal research assistant for Sawyer & Mkushi, Zimbabwe.

Query: "{query}"

Excerpts (firm precedents and/or public legislation & case law):
{context}

Answer directly and practically:
- If firm precedents are present, identify patterns (clause structures, argument strategies) and note them by filename
- If legislation or case law is present, summarise the relevant legal position and cite by reference (e.g. "SI 76 of 2025" or case citation) — note this is public material (ZimLII, CC BY-NC)
- Flag variations over time
- For drafting queries, suggest specific language from the firm precedents, informed by the current law

Professional, direct, max 4 paragraphs. Clearly distinguish firm precedent from public legal sources."""}]
        )
        return msg.content[0].text
    except Exception:
        total = len(results) + len(legal_results or [])
        return f"Found {total} relevant excerpt(s). Review the sources below."

# ── Affidavit Generator ───────────────────────────────────────────────────────

AFFIDAVIT_SYSTEM = """You are a legal drafting assistant for Sawyer & Mkushi Legal Practitioners, Harare.
Draft affidavits in proper Zimbabwe High Court form per SI 202/2021.
- Full court caption with case number, party names and designations
- Opening: deponent full name, ID, capacity, competency declaration
- Numbered paragraphs, first person, chronological facts
- Prayer paragraph with specific relief
- Commissioner of oaths block at end
- Use [_____] for unknown specifics"""

@app.post("/api/generate-affidavit")
async def generate_affidavit(req: AffidavitRequest):
    precedent_block = ""
    if req.precedent_context:
        fname = req.precedent_context.get("filename", "precedent")
        mname = req.precedent_context.get("matter_name", "")
        text = str(req.precedent_context.get("text", ""))[:2000]
        precedent_block = f"\n\nFIRM PRECEDENT ({fname} — {mname}):\n---\n{text}\n---\nAdopt this drafting style and clause structure."

    prompt = f"""Draft a complete supporting affidavit.

MATTER: {req.matter_type or 'General'} | {req.court or 'High Court of Zimbabwe'}
PARTIES: {req.parties or '[TO BE COMPLETED]'}
DEPONENT: {req.deponent_name or '[FULL NAME]'}{f', ID {req.deponent_id}' if req.deponent_id else ''}
CAPACITY: {req.deponent_capacity or 'the deponent herein'}

SUMMARY:
{req.matter_summary}

FACTS:
{req.key_facts or '[FACTS TO BE INSERTED]'}

RELIEF:
{req.relief or '[RELIEF TO BE SPECIFIED]'}
{precedent_block}

Draft the complete affidavit:"""

    try:
        msg = await asyncio.to_thread(
            client.messages.create,
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=AFFIDAVIT_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        return {"affidavit": msg.content[0].text, "document_id": str(uuid.uuid4())[:8].upper()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Document Generator (letters, summons, applications, heads etc.) ────────────

class DocumentRequest(BaseModel):
    doc_type: str
    plaintiff: Optional[str] = None
    defendant: Optional[str] = None
    court: Optional[str] = "High Court of Zimbabwe"
    case_number: Optional[str] = None
    facts: str
    instructions: Optional[str] = None
    precedent_context: Optional[dict] = None
    # Type-specific fields
    extra: Optional[dict] = None

DOC_TYPE_PROMPTS = {
    "summons_matrimonial": """You are drafting a Matrimonial Summons for the Zimbabwe High Court.
Follow Zimbabwe Matrimonial Causes Act [Chapter 5:13] and High Court Rules SI 202/2021.
Include: full court caption, case number, parties with designations, grounds for divorce/relief claimed,
return date, service instructions, registrar's endorsement block. Use formal Zimbabwe High Court summons format.""",

    "summons_civil": """You are drafting a Civil Summons for Zimbabwe courts.
Follow High Court Rules SI 202/2021. Include: court caption, parties, cause of action clearly stated,
amount or relief claimed, return date, defendant's right to appear. Proper summons format throughout.""",

    "court_application": """You are drafting a Court Application (Notice of Motion) for Zimbabwe.
Include: Notice of Motion with return date, draft order sought, founding affidavit reference,
certificate of service. Follow High Court Rules. Relief must be specific and numbered.""",

    "urgent_chamber": """You are drafting an Urgent Chamber Application for Zimbabwe High Court.
Include: Certificate of Urgency (explaining why matter cannot wait for normal set-down),
Notice of Motion for urgent relief, draft order with interim and final relief,
grounds of urgency clearly articulated. Follow Zimbabwe urgent application practice.""",

    "notice_of_appeal": """You are drafting a Notice of Appeal for Zimbabwe courts.
Specify: court appealing from, court appealing to, judgment/order being appealed,
date of judgment, grounds of appeal (numbered, specific), relief sought on appeal.
Follow Supreme Court of Zimbabwe Act and relevant rules.""",

    "letter_of_demand": """You are drafting a formal Letter of Demand for a Zimbabwe law firm.
Include: firm letterhead block, date, addressee, subject line, formal demand with legal basis,
deadline for compliance, consequences of non-compliance, formal closing.
Professional tone, firm but not inflammatory.""",

    "review": """You are drafting an Application for Review (judicial review) for Zimbabwe.
Include: grounds of review (illegality, irrationality, procedural impropriety),
decision being reviewed, date of decision, decision-maker, relief sought.
Follow Administrative Justice Act and High Court practice for review applications.""",

    "heads_of_argument": """You are drafting Heads of Argument for Zimbabwe courts.
Structure: introduction/overview, issues for determination (numbered), 
argument on each issue (with case law citations where possible, noting Zimbabwe cases preferred),
conclusion and relief sought. Logical, concise, persuasive. Number all paragraphs.
Reference Zimbabwe case law and Roman-Dutch common law principles as appropriate.""",

    "legal_opinion": """You are drafting a formal Legal Opinion for a Zimbabwe law firm (Sawyer & Mkushi).
Structure: instruction/question posed, brief facts, applicable law (Acts, case law, common law),
analysis, conclusion/advice, qualifications/caveats. Professional, precise, hedged appropriately.
Cite Zimbabwe legislation and case law where relevant.""",

    "client_letter": """You are drafting a formal client letter for Sawyer & Mkushi Legal Practitioners, Harare.
Include: firm header block, date, client address, reference/matter heading, formal salutation,
clear body paragraphs, action points if any, formal closing. Professional Zimbabwe legal correspondence style.""",

    "agreement": """You are drafting a legal Agreement/Contract governed by Zimbabwe law.
Include: parties clause, recitals/background, definitions, operative clauses,
representations and warranties where appropriate, breach and remedies,
governing law (Zimbabwe), dispute resolution, signature blocks.
Follow Zimbabwe contract law principles (Roman-Dutch common law base).""",

    "freeform": """You are a legal drafting assistant for Sawyer & Mkushi Legal Practitioners, Harare, Zimbabwe.
Draft the legal document described below following Zimbabwe law, court rules, and legal practice.
Use appropriate formal legal language. Structure the document correctly for its type.
Include all standard components for this kind of document in Zimbabwe legal practice.""",

    "joint_venture": """You are drafting a Joint Venture / Shareholders Agreement governed by Zimbabwe law.
Follow the Companies and Other Business Entities Act [Chapter 24:31] (COBE Act).
Include: parties, recitals, purpose and scope of joint venture, capital contributions,
shareholding structure, management and decision-making (board composition, reserved matters,
quorum, voting), profit distribution, intellectual property, confidentiality, restraint of trade,
deadlock resolution, exit provisions (drag-along, tag-along, pre-emptive rights),
dissolution/winding up, governing law (Zimbabwe), dispute resolution (arbitration or litigation),
signature blocks with witness attestation. Comprehensive commercial drafting throughout.""",

    "agreement_of_sale": """You are drafting an Agreement of Sale of Immoveable Property governed by Zimbabwe law.
Follow the Deeds Registries Act [Chapter 20:05] and Conveyancing practice in Zimbabwe.
Include: full property description (stand number, township, extent, held under Deed of Transfer number),
purchase price (in USD or ZWG as specified), deposit terms, balance payment date,
occupational interest if applicable, voetstoots clause, fixtures and fittings,
conditions of sale, breach provisions, transfer costs (who bears transfer duty, conveyancing fees),
ZIMRA RTGS and capital gains tax obligations, conveyancer appointment,
warranties as to title and encumbrances, signature blocks with witness attestation.
Reference Zimbabwe Deeds Registry requirements throughout.""",

    "acknowledgement_of_debt": """You are drafting an Acknowledgement of Debt (Deed of Acknowledgement) governed by Zimbabwe law.
Include: debtor's full details (name, ID number, address), creditor's full details,
acknowledgement of the debt amount (in figures and words), original cause of debt,
repayment terms (lump sum or instalments with schedule), interest rate if applicable,
consent to judgment clause (confession of judgment), default provisions,
costs clause (attorney and client scale), governing law (Zimbabwe),
signature block for debtor with witness attestation. This document must be enforceable
as a liquid document under Zimbabwe law to found a claim without trial.""",

    "power_of_attorney_transfer": """You are drafting a Power of Attorney to Pass Transfer for Zimbabwe conveyancing.
This is a formal conveyancing document used in the Zimbabwe Deeds Registry.
Include: grantor's full details (seller/transferor — full name, ID number, address),
attorney's details (conveyancer or firm), full property description
(stand number, township, Deed of Transfer number, extent), purchase price,
purchaser's full details, scope of authority (to appear before Registrar of Deeds,
sign transfer documents, pass transfer), ratification clause, revocation terms,
formal attestation block (signed before Commissioner of Oaths or Notary Public).
Follow Deeds Registries Act [Chapter 20:05] requirements precisely.""",

    "declaration_transferor": """You are drafting a Declaration by Transferor for Zimbabwe Deeds Registry transfer purposes.
This is a statutory declaration required under the Deeds Registries Act [Chapter 20:05].
The transferor (seller) declares: full name and identity details, marital status and matrimonial regime
(in community of property or out of community — critical for Zimbabwe conveyancing),
that they are the registered owner of the property described, that the property is not
subject to any undisclosed encumbrances, any Capital Gains Tax obligations,
ZIMRA compliance status, that spousal consent has been obtained where required under
the Matrimonial Causes Act or customary law. Formal sworn declaration format before
Commissioner of Oaths. Include all standard conveyancing declarations required by the
Registrar of Deeds, Harare.""",

    "declaration_transferee": """You are drafting a Declaration by Transferee for Zimbabwe Deeds Registry transfer purposes.
This is a statutory declaration required under the Deeds Registries Act [Chapter 20:05].
The transferee (purchaser/buyer) declares: full name and identity details,
marital status and matrimonial regime (in community of property or out of community —
critical as it determines how title will be registered), citizenship/residency status,
that they accept transfer of the property described, that the purchase price stated
is the true and full consideration, any ZIMRA obligations (transfer duty),
spousal details where property will be registered in community of property.
Formal sworn declaration format before Commissioner of Oaths. Follow Deeds Registry
requirements for Harare precisely.""",

    "special_power_of_attorney": """You are drafting a Special Power of Attorney governed by Zimbabwe law.
Unlike a general power of attorney, this is limited to a specific transaction or purpose.
Include: grantor's full details (name, ID number, address, capacity),
attorney/agent's full details, precise and limited scope of authority
(exactly what the attorney is authorised to do — no wider),
duration/expiry of the authority, specific transaction details if applicable,
ratification clause (confirming all acts done within scope),
revocation provisions, formal execution block (signed before Notary Public
or Commissioner of Oaths as appropriate for the transaction).
Common uses: property transactions, court appearances, banking, signing specific contracts.
Tailor the scope precisely to what has been described.""",

    "sale_of_business": """You are drafting a Sale of Business Agreement governed by Zimbabwe law.
Include: parties (seller and purchaser with full details), description of the business
(name, nature, location), assets being sold (goodwill, stock, equipment, debtors,
contracts, intellectual property — itemised or by schedule), excluded assets,
purchase price (allocation between goodwill, stock at valuation, fixed assets),
payment terms and conditions, transfer of employees (Labour Act [Chapter 28:01] obligations),
transfer of contracts and leases (consent requirements), restraint of trade
(seller not to compete — reasonable in scope, area and time under Zimbabwe law),
completion date and conditions precedent, warranties and representations by seller,
indemnities, risk and benefit, ZIMRA obligations (VAT on going concern — zero-rated if applicable,
capital gains tax considerations), breach and remedies, governing law Zimbabwe,
dispute resolution, signature blocks with witness attestation.""",

    "memorandum_of_understanding": """You are drafting a Memorandum of Understanding (MOU) governed by Zimbabwe law.
Include: parties with full details, background and purpose, subject matter of the understanding,
binding vs non-binding clauses (clearly distinguished — key for Zimbabwe commercial practice),
obligations of each party, exclusivity period if applicable, confidentiality obligations,
intellectual property ownership during the MOU period, costs and expenses,
no partnership or agency clause (important — an MOU must not inadvertently create a partnership
under the Partnership Act or COBE Act), duration and termination,
conditions for proceeding to formal agreement, governing law (Zimbabwe),
dispute resolution. Draft with appropriate hedging language where provisions
are intended to be non-binding, and clear mandatory language where binding.""",
}

DOCUMENT_SYSTEM = """You are a senior legal drafting assistant for Sawyer & Mkushi Legal Practitioners, Harare, Zimbabwe.
You have deep expertise in:
- Zimbabwe High Court Rules SI 202/2021
- Roman-Dutch common law as applied in Zimbabwe
- Zimbabwe statutory law and practice
- Formal Zimbabwe legal document drafting conventions
- Customary law as applied in Zimbabwe courts

Always produce complete, properly formatted documents ready for use.
Use formal legal English as practised in Zimbabwe courts.
Leave [_____] for information not provided."""

@app.post("/api/generate-document")
async def generate_document(req: DocumentRequest):
    doc_system_addition = DOC_TYPE_PROMPTS.get(req.doc_type, DOC_TYPE_PROMPTS["freeform"])

    precedent_block = ""
    if req.precedent_context:
        fname = req.precedent_context.get("filename", "precedent")
        mname = req.precedent_context.get("matter_name", "")
        text = str(req.precedent_context.get("text", ""))[:2000]
        precedent_block = f"\n\nFIRM PRECEDENT — adopt this drafting style ({fname}, {mname}):\n---\n{text}\n---"

    prompt = f"""Draft a complete {req.doc_type.replace('_', ' ').title()} for Zimbabwe courts.

PARTIES:
- Plaintiff/Applicant: {req.plaintiff or '[TO BE COMPLETED]'}
- Defendant/Respondent: {req.defendant or '[TO BE COMPLETED]'}

COURT: {req.court or 'High Court of Zimbabwe'}
CASE NUMBER: {req.case_number or '[TO BE ALLOCATED]'}

FACTS AND BACKGROUND:
{req.facts}

INSTRUCTIONS:
{req.instructions or 'Draft in standard Zimbabwe legal form, complete and ready for use.'}

EXTRA DETAILS:
{json.dumps(req.extra) if req.extra else 'None provided.'}
{precedent_block}

Draft the complete document now:"""

    try:
        msg = await asyncio.to_thread(
            client.messages.create,
            model="claude-sonnet-4-5",
            max_tokens=6000,
            system=DOCUMENT_SYSTEM + "\n\n" + doc_system_addition,
            messages=[{"role": "user", "content": prompt}]
        )
        doc_id = str(uuid.uuid4())[:8].upper()
        return {"document": msg.content[0].text, "document_id": doc_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class DocxExportRequest(BaseModel):
    content_html: str
    filename: Optional[str] = "document"

@app.post("/api/export-document-docx")
async def export_document_docx(req: DocxExportRequest):
    """Export rich text HTML content to DOCX."""
    doc_id = str(uuid.uuid4())[:8].upper()
    safe_name = req.filename.replace(" ", "_")
    output_filename = f"{safe_name}_{doc_id}.docx"
    output_path = os.path.join(tempfile.gettempdir(), output_filename)

    escaped_html = json.dumps(req.content_html)

    js = f"""
const {{ Document, Packer, Paragraph, TextRun, AlignmentType, BorderStyle, PageNumber }} = require('docx');
const fs = require('fs');

// Parse HTML content to docx paragraphs
const html = {escaped_html};
const lines = html.replace(/<br\s*\/?>/gi, '\\n')
  .replace(/<\/p>/gi, '\\n').replace(/<\/div>/gi, '\\n')
  .replace(/<\/h[1-6]>/gi, '\\n').replace(/<\/li>/gi, '\\n')
  .replace(/<[^>]+>/g, '').split('\\n');

const children = [];
lines.forEach(line => {{
  const t = line.trim();
  if (!t) {{ children.push(new Paragraph({{ spacing: {{ after: 120 }} }})); return; }}
  const isCentered = /^IN THE|^BETWEEN:|^AND:|^v\\.?$|^-and-$/i.test(t);
  children.push(new Paragraph({{
    alignment: isCentered ? AlignmentType.CENTER : AlignmentType.JUSTIFIED,
    spacing: {{ after: 160, line: 360 }},
    children: [new TextRun({{ text: t, font: "Times New Roman", size: 24 }})]
  }}));
}});

const doc = new Document({{
  styles: {{ default: {{ document: {{ run: {{ font: "Times New Roman", size: 24 }} }} }} }},
  sections: [{{
    properties: {{ page: {{ size: {{ width: 11906, height: 16838 }},
      margin: {{ top: 1440, right: 1440, bottom: 1800, left: 1800 }} }} }},
    footers: {{ default: {{ children: [new Paragraph({{
      alignment: AlignmentType.CENTER,
      border: {{ top: {{ style: BorderStyle.SINGLE, size: 4, color: "999999", space: 8 }} }},
      children: [
        new TextRun({{ text: "Page ", font: "Times New Roman", size: 20, color: "666666" }}),
        new TextRun({{ children: [PageNumber.CURRENT], font: "Times New Roman", size: 20, color: "666666" }}),
        new TextRun({{ text: " of ", font: "Times New Roman", size: 20, color: "666666" }}),
        new TextRun({{ children: [PageNumber.TOTAL_PAGES], font: "Times New Roman", size: 20, color: "666666" }}),
      ]
    }})] }} }},
    children
  }}]
}});
Packer.toBuffer(doc).then(buf => {{ fs.writeFileSync('{output_path}', buf); }}).catch(e => {{ console.error(e); process.exit(1); }});
"""
    js_file = os.path.join(tempfile.gettempdir(), f"gendoc_{doc_id}.js")
    with open(js_file, "w") as f:
        f.write(js)
    try:
        result = subprocess.run(["node", js_file], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Export failed: {result.stderr}")
        if not os.path.exists(output_path):
            raise HTTPException(status_code=500, detail="File not created")
        return FileResponse(path=output_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=output_filename)
    finally:
        if os.path.exists(js_file):
            os.remove(js_file)



@app.post("/api/export-docx")
async def export_docx(req: ExportRequest):
    doc_id = req.document_id or str(uuid.uuid4())[:8].upper()
    safe_name = (req.deponent_name or "Deponent").replace(" ", "_")
    output_filename = f"Affidavit_{safe_name}_{doc_id}.docx"
    output_path = os.path.join(tempfile.gettempdir(), output_filename)
    escaped = json.dumps(req.affidavit_text)

    js = f"""
const {{ Document, Packer, Paragraph, TextRun, AlignmentType, BorderStyle, PageNumber }} = require('docx');
const fs = require('fs');
const raw = {escaped};
const lines = raw.split('\\n');
const children = [];
lines.forEach(line => {{
  const t = line.trim();
  if (!t) {{ children.push(new Paragraph({{ spacing: {{ after: 120 }} }})); return; }}
  const isCentered = t === t.toUpperCase() && t.length > 3 && !/^\\d+\\./.test(t) && t.length < 80;
  const isSignature = t.startsWith('____');
  const isJuratHead = t.startsWith('SWORN') || t.startsWith('AFFIRMED');
  const isCommissioner = t.includes('COMMISSIONER OF OATHS');
  if (isCentered) {{
    children.push(new Paragraph({{ alignment: AlignmentType.CENTER, spacing: {{ before: 160, after: 160 }},
      children: [new TextRun({{ text: t, bold: true, size: 26, font: "Times New Roman" }})] }}));
  }} else if (isSignature) {{
    children.push(new Paragraph({{ spacing: {{ before: 480, after: 80 }},
      children: [new TextRun({{ text: t, font: "Times New Roman", size: 24 }})] }}));
  }} else {{
    children.push(new Paragraph({{ alignment: AlignmentType.JUSTIFIED, spacing: {{ after: 160, line: 360 }},
      children: [new TextRun({{ text: t, font: "Times New Roman", size: 24,
        bold: isJuratHead || isCommissioner }})] }}));
  }}
}});
const doc = new Document({{
  styles: {{ default: {{ document: {{ run: {{ font: "Times New Roman", size: 24 }} }} }} }},
  sections: [{{
    properties: {{ page: {{ size: {{ width: 11906, height: 16838 }},
      margin: {{ top: 1440, right: 1440, bottom: 1800, left: 1800 }} }} }},
    footers: {{ default: {{ children: [new Paragraph({{
      alignment: AlignmentType.CENTER,
      border: {{ top: {{ style: BorderStyle.SINGLE, size: 4, color: "999999", space: 8 }} }},
      children: [
        new TextRun({{ text: "Page ", font: "Times New Roman", size: 20, color: "666666" }}),
        new TextRun({{ children: [PageNumber.CURRENT], font: "Times New Roman", size: 20, color: "666666" }}),
        new TextRun({{ text: " of ", font: "Times New Roman", size: 20, color: "666666" }}),
        new TextRun({{ children: [PageNumber.TOTAL_PAGES], font: "Times New Roman", size: 20, color: "666666" }}),
      ]
    }})] }} }},
    children
  }}]
}});
Packer.toBuffer(doc).then(buf => {{ fs.writeFileSync('{output_path}', buf); }}).catch(e => {{ console.error(e); process.exit(1); }});
"""
    js_file = os.path.join(tempfile.gettempdir(), f"gen_{doc_id}.js")
    with open(js_file, "w") as f:
        f.write(js)
    try:
        result = subprocess.run(["node", js_file], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Export failed: {result.stderr}")
        if not os.path.exists(output_path):
            raise HTTPException(status_code=500, detail="File not created")
        return FileResponse(path=output_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=output_filename)
    finally:
        if os.path.exists(js_file):
            os.remove(js_file)

# ── Calendar ──────────────────────────────────────────────────────────────────

@app.get("/api/calendar")
async def list_events():
    return sorted(calendar_db, key=lambda x: (x["date"], x.get("time", "")))

@app.post("/api/calendar")
async def create_event(event: CalendarEvent):
    obj = event.dict()
    obj["id"] = str(uuid.uuid4())
    obj["created_at"] = datetime.utcnow().isoformat()
    calendar_db.append(obj)
    save_state()
    return obj

@app.delete("/api/calendar/{event_id}")
async def delete_event(event_id: str):
    global calendar_db
    calendar_db = [e for e in calendar_db if e["id"] != event_id]
    save_state()
    return {"deleted": True}

@app.get("/api/calendar/upcoming")
async def upcoming_events():
    today = datetime.utcnow().date().isoformat()
    upcoming = [e for e in calendar_db if e["date"] >= today]
    upcoming.sort(key=lambda x: (x["date"], x.get("time", "")))
    return upcoming[:10]

# ── Email Reminders ─────────────────────────────────────────────────────────────

@app.get("/api/reminders/settings")
async def get_reminder_settings():
    # Don't expose whether SMTP env vars are set in detail, just whether sending is configured
    settings = dict(reminder_settings)
    settings["smtp_configured"] = bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER"))
    return settings

@app.post("/api/reminders/settings")
async def update_reminder_settings(settings: ReminderSettings):
    reminder_settings["enabled"] = settings.enabled
    reminder_settings["recipient_email"] = settings.recipient_email
    reminder_settings["send_hour_utc"] = max(0, min(23, settings.send_hour_utc))
    save_state()
    return reminder_settings

@app.post("/api/reminders/send-test")
async def send_test_reminder():
    """Send a test reminder email immediately, regardless of schedule."""
    if not reminder_settings.get("recipient_email"):
        raise HTTPException(status_code=400, detail="Set a recipient email first.")
    if not is_smtp_configured():
        raise HTTPException(status_code=500, detail="Email is not configured on the server. Set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM environment variables.")

    upcoming = get_upcoming_for_reminders()
    try:
        send_reminder_email(reminder_settings["recipient_email"], upcoming, test=True)
        return {"sent": True, "event_count": len(upcoming)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send: {e}")

def is_smtp_configured() -> bool:
    return bool(
        os.environ.get("SMTP_HOST") and
        os.environ.get("SMTP_USER") and
        os.environ.get("SMTP_PASSWORD")
    )

def get_upcoming_for_reminders(within_days: int = 7) -> list:
    """Return events from today up to `within_days` from now, with computed urgency."""
    today = datetime.utcnow().date()
    cutoff = today + timedelta(days=within_days)
    upcoming = []
    for e in calendar_db:
        try:
            event_date = datetime.strptime(e["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if today <= event_date <= cutoff:
            days_until = (event_date - today).days
            item = dict(e)
            item["days_until"] = days_until
            upcoming.append(item)
    upcoming.sort(key=lambda x: (x["date"], x.get("time", "")))
    return upcoming

def escape_ics(text: str) -> str:
    """Escape special characters per RFC 5545 (ICS format)."""
    if not text:
        return ""
    return (text.replace("\\", "\\\\")
                .replace(";", "\\;")
                .replace(",", "\\,")
                .replace("\n", "\\n"))

def build_ics(events: list) -> str:
    """Build a simple .ics calendar file containing the given events."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Mutemo Desk//Court Calendar//EN",
        "CALSCALE:GREGORIAN",
    ]
    for e in events:
        date_str = e["date"].replace("-", "")
        time_str = (e.get("time") or "09:00").replace(":", "") + "00"
        dtstart = f"{date_str}T{time_str}"
        uid = f"{e.get('id', uuid.uuid4())}@mutemodesk"
        summary = escape_ics((e.get("title") or "Event").replace("\n", " "))
        desc_parts = []
        if e.get("matter_name"):
            desc_parts.append(f"Matter: {e['matter_name']}")
        if e.get("court"):
            desc_parts.append(f"Court: {e['court']}")
        if e.get("notes"):
            desc_parts.append(e["notes"])
        description = escape_ics(" | ".join(desc_parts))

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTART:{dtstart}",
            f"SUMMARY:{summary}",
        ]
        if description:
            lines.append(f"DESCRIPTION:{description}")
        if e.get("court"):
            lines.append(f"LOCATION:{escape_ics(e['court'])}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)

EVENT_TYPE_LABELS = {
    "hearing": "Court Hearing",
    "deadline": "Deadline",
    "filing": "Filing Date",
    "meeting": "Client Meeting",
    "other": "Event",
}

def build_reminder_email_body(events: list) -> tuple[str, str]:
    """Returns (plain_text_body, html_body)"""
    if not events:
        text = "Good morning. You have no court dates, deadlines, or filings scheduled in the next 7 days.\n\n— Mutemo Desk"
        html = "<p>Good morning. You have no court dates, deadlines, or filings scheduled in the next 7 days.</p><p style='color:#6b6b64'>— Mutemo Desk</p>"
        return text, html

    today_items = [e for e in events if e["days_until"] == 0]
    tomorrow_items = [e for e in events if e["days_until"] == 1]
    week_items = [e for e in events if e["days_until"] > 1]

    def fmt_text(e):
        bits = [EVENT_TYPE_LABELS.get(e.get("event_type"), "Event") + ":", e.get("title", "")]
        if e.get("time"):
            bits.append(f"at {e['time']}")
        if e.get("court"):
            bits.append(f"— {e['court']}")
        if e.get("matter_name"):
            bits.append(f"({e['matter_name']})")
        return " ".join(bits)

    text_lines = ["Good morning. Here is your Mutemo Desk reminder summary:\n"]
    if today_items:
        text_lines.append("TODAY:")
        for e in today_items:
            text_lines.append(f"  • {fmt_text(e)}")
        text_lines.append("")
    if tomorrow_items:
        text_lines.append("TOMORROW:")
        for e in tomorrow_items:
            text_lines.append(f"  • {fmt_text(e)}")
        text_lines.append("")
    if week_items:
        text_lines.append("LATER THIS WEEK:")
        for e in week_items:
            day_label = e["date"]
            text_lines.append(f"  • {day_label} — {fmt_text(e)}")
        text_lines.append("")
    text_lines.append("A calendar file (.ics) is attached — open it to add these to your phone or computer calendar.")
    text_lines.append("\n— Mutemo Desk")
    text = "\n".join(text_lines)

    # HTML version
    def fmt_html(e):
        type_chip = EVENT_TYPE_LABELS.get(e.get("event_type"), "Event")
        parts = [f"<strong>{escape_html(e.get('title',''))}</strong>"]
        meta = []
        if e.get("time"):
            meta.append(e["time"])
        if e.get("court"):
            meta.append(escape_html(e["court"]))
        if e.get("matter_name"):
            meta.append(escape_html(e["matter_name"]))
        meta_str = " · ".join(meta)
        return f"""<div style="padding:8px 0;border-bottom:1px solid #e8e4da">
            <span style="font-size:11px;font-weight:700;color:#b8922a;text-transform:uppercase;letter-spacing:0.5px">{type_chip}</span><br/>
            {parts[0]}<br/>
            <span style="font-size:13px;color:#6b6b64">{meta_str}</span>
        </div>"""

    html_sections = []
    if today_items:
        html_sections.append('<h3 style="color:#b83232;margin:16px 0 8px">Today</h3>' + "".join(fmt_html(e) for e in today_items))
    if tomorrow_items:
        html_sections.append('<h3 style="color:#b8922a;margin:16px 0 8px">Tomorrow</h3>' + "".join(fmt_html(e) for e in tomorrow_items))
    if week_items:
        html_sections.append('<h3 style="color:#1b4d2e;margin:16px 0 8px">Later This Week</h3>' + "".join(
            f'<div style="padding:8px 0;border-bottom:1px solid #e8e4da"><span style="font-size:12px;color:#6b6b64">{e["date"]}</span><br/>{fmt_html(e)}</div>' for e in week_items
        ))

    html = f"""<div style="font-family:Georgia,serif;color:#1a1a18;max-width:560px">
        <div style="background:#1b4d2e;color:white;padding:16px 20px;border-radius:6px 6px 0 0">
            <strong style="font-size:18px">⚖ Mutemo Desk</strong><br/>
            <span style="font-size:13px;opacity:0.8">Daily Court Calendar Reminder</span>
        </div>
        <div style="padding:16px 20px;border:1px solid #d8d3c8;border-top:none;border-radius:0 0 6px 6px">
            <p>Good morning. Here is your reminder summary for the next 7 days.</p>
            {''.join(html_sections)}
            <p style="margin-top:16px;font-size:13px;color:#6b6b64">A calendar file (.ics) is attached — open it to add these events to your phone or computer calendar.</p>
        </div>
    </div>"""

    return text, html

def escape_html(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def send_reminder_email(recipient: str, events: list, test: bool = False):
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    from_addr = os.environ.get("SMTP_FROM", smtp_user)

    text_body, html_body = build_reminder_email_body(events)
    if test:
        text_body = "[TEST EMAIL]\n\n" + text_body
        html_body = '<p style="background:#fdf6e8;padding:8px;border-radius:4px;font-size:13px"><strong>This is a test email.</strong></p>' + html_body

    msg = MIMEMultipart("mixed")
    subject_prefix = "[TEST] " if test else ""
    if any(e["days_until"] == 0 for e in events):
        subject = f"{subject_prefix}⚖ Mutemo Desk — Court date TODAY + upcoming"
    elif events:
        subject = f"{subject_prefix}⚖ Mutemo Desk — Daily reminder ({len(events)} upcoming)"
    else:
        subject = f"{subject_prefix}⚖ Mutemo Desk — Daily reminder (nothing upcoming)"

    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = recipient

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain"))
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    if events:
        ics_content = build_ics(events)
        ics_part = MIMEBase("text", "calendar", method="PUBLISH")
        ics_part.set_payload(ics_content)
        encoders.encode_base64(ics_part)
        ics_part.add_header("Content-Disposition", "attachment", filename="mutemo-desk-events.ics")
        msg.attach(ics_part)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(from_addr, [recipient], msg.as_string())

async def reminder_scheduler_loop():
    """Background loop — checks every 30 minutes whether it's time to send the daily reminder."""
    while True:
        try:
            if reminder_settings.get("enabled") and reminder_settings.get("recipient_email") and is_smtp_configured():
                now = datetime.utcnow()
                today_str = now.date().isoformat()
                target_hour = reminder_settings.get("send_hour_utc", 5)
                already_sent = reminder_settings.get("last_run_date") == today_str
                if now.hour == target_hour and not already_sent:
                    upcoming = get_upcoming_for_reminders()
                    # Mark as attempted BEFORE sending — if it fails, we retry
                    # tomorrow rather than every 30 min for the rest of this hour.
                    reminder_settings["last_run_date"] = today_str
                    save_state()
                    try:
                        send_reminder_email(reminder_settings["recipient_email"], upcoming)
                    except Exception as e:
                        print(f"[reminder] failed to send: {e}")
        except Exception as e:
            print(f"[reminder] scheduler error: {e}")
        await asyncio.sleep(30 * 60)  # check every 30 minutes

# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_frontend():
    index = os.path.join(frontend_path, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "Mutemo Desk API v1.0", "docs": "/docs"}

@app.get("/{path:path}")
async def catch_all(path: str):
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found")
    index = os.path.join(frontend_path, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    raise HTTPException(status_code=404)

if __name__ == "__main__":
    import uvicorn
    import signal
    import sys

    def handle_shutdown(signum, frame):
        print("\n[shutdown] saving state before exit...")
        save_state()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
