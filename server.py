"""Multi-tenant RAG sales agent.
GET  /                  -> chat app (index.html)
GET  /dashboard         -> leads/demos dashboard
POST /chat              -> JSON reply
POST /chat/stream       -> SSE streaming reply + tool events
POST /lead              -> save a lead directly (form submission)
GET  /clients           -> list clients + brand meta
GET  /leads/<client>    -> saved leads for a client
GET  /demos/<client>    -> demo requests for a client
"""

import glob
import json
import os
import time
from collections import defaultdict
from pathlib import Path

import chromadb
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from openai import OpenAI
from pydantic import BaseModel

from docparse import extract_text
from ingest import index_client

load_dotenv()
llm = OpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"])
MODEL = os.environ["LLM_MODEL"]
fallback_llm = None
FALLBACK_MODEL = None
if os.environ.get("FALLBACK_LLM_API_KEY"):
    fallback_llm = OpenAI(
        base_url=os.environ.get(
            "FALLBACK_LLM_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
        ),
        api_key=os.environ["FALLBACK_LLM_API_KEY"],
    )
    FALLBACK_MODEL = os.environ.get("FALLBACK_LLM_MODEL", "gemini-2.5-flash")


def complete(messages: list, **kwargs):
    """Call the primary LLM, falling back to the secondary (e.g. Gemini free) on errors."""
    try:
        return llm.chat.completions.create(model=MODEL, messages=messages, **kwargs)
    except Exception:  # rate limits / provider errors -> fallback
        if fallback_llm is None:
            raise
        return fallback_llm.chat.completions.create(
            model=FALLBACK_MODEL or "gemini-2.5-flash", messages=messages, **kwargs
        )


ROOT = Path(__file__).parent
db = chromadb.PersistentClient(path=str(ROOT / "vectordb"))
sessions = defaultdict(list)  # (client, session) -> messages
calls = defaultdict(list)  # rate limit: (client, ip) -> timestamps

app = FastAPI(title="AI Sales Agent")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "capture_lead",
            "description": "Save a qualified lead when the visitor gives contact details (name + email, ideally phone) and shows buying intent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string", "description": "Optional phone number"},
                    "need": {
                        "type": "string",
                        "description": "Summary of what they want",
                    },
                    "score": {"type": "integer", "description": "Lead quality 1-5"},
                },
                "required": ["name", "email", "need"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_demo",
            "description": "Reserve a product demo / consultation slot when the visitor asks to book or agrees to one. Use their preferred date and time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "date": {
                        "type": "string",
                        "description": "YYYY-MM-DD preferred date",
                    },
                    "time": {"type": "string", "description": "HH:MM preferred time"},
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone e.g. Africa/Casablanca",
                    },
                },
                "required": ["name", "email", "date", "time"],
            },
        },
    },
]


class Msg(BaseModel):
    client_id: str
    session_id: str
    message: str


class LeadIn(BaseModel):
    client_id: str
    name: str
    email: str
    phone: str = ""
    need: str = ""
    score: int = 4


# ---------------- helpers ----------------


def load_cfg(client_id: str) -> dict:
    path = ROOT / "clients" / client_id / "config.yaml"
    if not path.exists():
        raise HTTPException(404, "unknown client")
    base = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults = {
        "brand_color": "#059669",
        "accent": "#f59e0b",
        "logo_text": base.get("brand_name", client_id),
        "welcome": f"Hi! I'm {base.get('agent_name', 'the assistant')} from {base.get('brand_name', client_id)}. How can I help?",
        "suggestions": [],
    }
    for k, v in defaults.items():
        base.setdefault(k, v)
    base["id"] = client_id
    return base


def brand_meta(client_id: str) -> dict:
    c = load_cfg(client_id)
    return {
        k: c[k]
        for k in (
            "id",
            "brand_name",
            "agent_name",
            "brand_color",
            "accent",
            "logo_text",
            "welcome",
            "suggestions",
            "handoff",
            "languages",
        )
    }


def client_ip(req: Request) -> str:
    return req.client.host if req.client else "unknown"


def rate_limited(client: str, ip: str, limit: int = 30) -> bool:
    now, key = time.time(), (client, ip)
    calls[key] = [t for t in calls[key] if now - t < 60]
    if len(calls[key]) >= limit:
        return True
    calls[key].append(now)
    return False


def retrieve(client: str, query: str) -> list:
    try:
        col = db.get_collection(client)
        docs = col.query(query_texts=[query], n_results=4)["documents"] or [[]]
        return docs[0] or []
    except Exception:  # noqa: BLE001 - unknown client -> no context
        return []


def system_prompt(cfg: dict, context: str) -> str:
    rules = "\n".join(f"- {r}" for r in cfg["rules"])
    qs = "\n".join(f"- {q}" for q in cfg["qualification_questions"])
    return f"""You are {cfg["agent_name"]}, the sales expert for {cfg["brand_name"]}.
Tone: {cfg["tone"]}. Reply in the visitor's language (supported: {", ".join(cfg["languages"])}).
Goal: {cfg["goal"]}.
Qualification questions (weave in naturally, at most one per message):
{qs}
Rules:
{rules}
- When the visitor wants to book a demo/consultation, use book_demo with their details.
- Quote prices/specs only from CONTEXT; never invent anything.

CONTEXT (company knowledge, the only source of facts):
{context}"""


def save_lead(client: str, args: dict, source: str = "chat"):
    row = {
        "name": args.get("name", ""),
        "email": args.get("email", ""),
        "phone": args.get("phone", ""),
        "need": args.get("need", ""),
        "score": int(args.get("score", 4) or 4),
        "source": source,
        "ts": time.time(),
    }
    with open(ROOT / f"leads_{client}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def save_demo(client: str, args: dict):
    row = {
        "name": args.get("name", ""),
        "email": args.get("email", ""),
        "date": args.get("date", ""),
        "time": args.get("time", ""),
        "timezone": args.get("timezone", ""),
        "ts": time.time(),
    }
    with open(ROOT / f"demos_{client}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def read_rows(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001, S112 - skip corrupt rows
            continue
    return rows


# ---------------- documents (knowledge base) ----------------

DOC_EXTS = {
    ".md",
    ".markdown",
    ".txt",
    ".text",
    ".csv",
    ".tsv",
    ".xlsx",
    ".xls",
    ".pdf",
    ".docx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
}


def docs_dir(client: str) -> Path:
    load_cfg(client)
    d = ROOT / "clients" / client / "docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    return Path(name or "").name


def list_docs(client: str) -> list:
    out = []
    for f in sorted(docs_dir(client).glob("*")):
        if not f.is_file():
            continue
        out.append(
            {
                "name": f.name,
                "size": f.stat().st_size,
                "kind": f.suffix.lower().lstrip(".") or "file",
                "indexed": bool(extract_text(f).strip()),
            }
        )
    return out


def dispatch(client: str, name: str, args: dict):
    if name == "capture_lead":
        row = save_lead(client, args)
        return (
            "Thank you, we saved your details. Tell the visitor a human from the team "
            "will contact them shortly."
        ), {"type": "lead_saved", "lead": row}
    if name == "book_demo":
        row = save_demo(client, args)
        return (
            f"Demo confirmed for {row['date']} at {row['time']}. "
            "Tell the visitor a confirmation email is on its way."
        ), {"type": "demo_booked", "demo": row}
    return "Tool executed.", None


def run_tools(client: str, msgs: list):
    """Executes any tool calls the model makes. Returns final msgs, text answer, and events."""
    events = []
    for _ in range(3):
        r = complete(msgs, tools=TOOLS, temperature=0.4)
        msg = r.choices[0].message
        if not getattr(msg, "tool_calls", None):
            return msgs, msg.content or "", events
        msgs.append(msg)
        for tc in msg.tool_calls or []:
            fn = getattr(tc, "function", None)
            if not fn:
                continue
            try:
                args = json.loads(fn.arguments)
            except Exception:  # noqa: BLE001 - malformed tool args
                args = {}
            result, ev = dispatch(client, fn.name, args)
            if ev:
                events.append(ev)
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return msgs, "", events


# ---------------- routes ----------------


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


@app.get("/dashboard")
def dashboard():
    return FileResponse(ROOT / "dashboard.html")


@app.get("/clients")
def clients():
    out = []
    for p in sorted(glob.glob(str(ROOT / "clients" / "*" / "config.yaml"))):
        cid = Path(p).parent.name
        try:
            out.append(brand_meta(cid))
        except Exception:  # noqa: BLE001, S112 - skip broken client dirs
            continue
    return out


@app.get("/leads/{client}")
def leads(client: str):
    load_cfg(client)
    return read_rows(ROOT / f"leads_{client}.jsonl")


@app.get("/demos/{client}")
def demos(client: str):
    load_cfg(client)
    return read_rows(ROOT / f"demos_{client}.jsonl")


@app.get("/documents")
def documents_page():
    return FileResponse(ROOT / "documents.html")


@app.get("/api/documents")
def documents_all():
    out = []
    for p in sorted(glob.glob(str(ROOT / "clients" / "*" / "config.yaml"))):
        cid = Path(p).parent.name
        try:
            out.append({"client": cid, "docs": list_docs(cid)})
        except HTTPException:
            continue
    return out


@app.get("/api/documents/{client}")
def documents(client: str):
    return {"client": client, "docs": list_docs(client)}


@app.post("/api/documents/{client}")
async def documents_upload(client: str, file: UploadFile = File(...)):  # noqa: B008 - FastAPI dependency marker
    d = docs_dir(client)
    name = safe_name(file.filename or "upload")
    ext = Path(name).suffix.lower()
    if ext not in DOC_EXTS:
        raise HTTPException(
            415,
            f"Unsupported type '{ext or 'none'}'. Allowed: {', '.join(sorted(DOC_EXTS))}",
        )
    data = await file.read()
    if not data:
        raise HTTPException(422, "Empty file")
    (d / name).write_bytes(data)
    chunks = index_client(client, files=[d / name], db_path=str(ROOT / "vectordb"))
    return {"ok": True, "name": name, "size": len(data), "chunks": chunks}


@app.delete("/api/documents/{client}/{name}")
def documents_delete(client: str, name: str):
    f = docs_dir(client) / safe_name(name)
    if f.exists():
        f.unlink()
    chunks = index_client(client, db_path=str(ROOT / "vectordb"))
    return {"ok": True, "chunks": chunks}


@app.post("/lead")
def lead(l: LeadIn):
    load_cfg(l.client_id)
    row = save_lead(l.client_id, l.model_dump(), source="form")
    return {"ok": True, "lead": row}


@app.post("/chat")
def chat(m: Msg, req: Request):
    cfg = load_cfg(m.client_id)
    if rate_limited(m.client_id, client_ip(req)):
        raise HTTPException(429, "Too many messages. Please slow down.")
    key = (m.client_id, m.session_id)
    hist = sessions[key]
    msgs = [
        {
            "role": "system",
            "content": system_prompt(
                cfg, "\n---\n".join(retrieve(m.client_id, m.message))
            ),
        }
    ]
    msgs += hist[-14:]
    msgs.append({"role": "user", "content": m.message})
    hist.append({"role": "user", "content": m.message})
    msgs, final, events = run_tools(m.client_id, msgs)
    if not final:
        final = f"Let me connect you with our team so we can take it from here: {cfg['handoff']}"
    hist.append({"role": "assistant", "content": final})
    return {"reply": final, "events": events}


@app.post("/chat/stream")
def chat_stream(m: Msg, req: Request):
    cfg = load_cfg(m.client_id)
    if rate_limited(m.client_id, client_ip(req)):
        raise HTTPException(429, "Too many messages. Please slow down.")
    key = (m.client_id, m.session_id)
    hist = sessions[key]
    msgs = [
        {
            "role": "system",
            "content": system_prompt(
                cfg, "\n---\n".join(retrieve(m.client_id, m.message))
            ),
        }
    ]
    msgs += hist[-14:]
    msgs.append({"role": "user", "content": m.message})
    hist.append({"role": "user", "content": m.message})
    msgs, final, events = run_tools(m.client_id, msgs)
    if not final:
        final = f"Let me connect you with our team so we can take it from here: {cfg['handoff']}"

    def ev_stream():
        for ev in events:
            yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
        acc = []
        try:
            stream = complete(msgs, tools=TOOLS, stream=True, temperature=0.4)
            for chunk in stream:
                delta = getattr(chunk.choices[0].delta, "content", None) or ""
                if delta:
                    acc.append(delta)
                    yield f"event: token\ndata: {json.dumps(delta)}\n\n"
                if getattr(chunk.choices[0], "finish_reason", None) == "stop":
                    break
        except Exception:  # noqa: BLE001 - tool call already handled; stream failed
            try:
                r = complete(msgs, tools=TOOLS, temperature=0.4)
                text = r.choices[0].message.content or ""
                acc = [text]
                yield f"event: token\ndata: {json.dumps(text)}\n\n"
            except Exception as e2:  # noqa: BLE001
                yield f"event: error\ndata: {json.dumps({'message': str(e2)})}\n\n"
        if not acc:
            acc = [final]
        hist.append(
            {"role": "assistant", "content": "".join(acc)}
            if "".join(acc)
            else {"role": "assistant", "content": final}
        )
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(ev_stream(), media_type="text/event-stream")
