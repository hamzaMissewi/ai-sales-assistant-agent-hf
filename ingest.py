"""Usage: python ingest.py acme   -> indexes clients/acme/docs/* into a vector DB"""

import os
import pathlib
import sys

import chromadb

from docparse import extract_text

DB_PATH = os.environ.get("CHROMA_DB_PATH", "./vectordb")


def chunk(text, size=800, overlap=120):
    out, i = [], 0
    while i < len(text):
        out.append(text[i : i + size])
        i += size - overlap
    return out


def index_client(client, files=None, db_path=DB_PATH, rebuild=True):
    """(Re)build a client's collection. With files=None, indexes all of docs/.

    rebuild=False skips collections that already hold data (used at boot so a
    persistent disk's model cache keeps the startup fast and download-free).
    """
    pathlib.Path(db_path).mkdir(parents=True, exist_ok=True)
    db = chromadb.PersistentClient(path=db_path)
    exists = any(c.name == client for c in db.list_collections())
    if exists and db.get_collection(client).count() > 0:
        if not rebuild:
            print(f"{client}: already indexed, skipping")
            return 0
        db.delete_collection(client)
    col = db.create_collection(client)  # default local embedding model, free

    if files is None:
        files = sorted(pathlib.Path(f"clients/{client}/docs").glob("*"))

    ids, docs, metas = [], [], []
    for p in files:
        if not isinstance(p, pathlib.Path):
            p = pathlib.Path(p)
        if not p.is_file():
            continue
        text = extract_text(p)
        if not text.strip():
            continue
        kind = p.suffix.lower().lstrip(".") or "file"
        for n, c in enumerate(chunk(text)):
            ids.append(f"{p.name}-{n}")
            docs.append(c)
            metas.append({"source": p.name, "kind": kind})
    if not docs:
        return 0
    col.add(ids=ids, documents=docs, metadatas=metas)
    return len(docs)


def main(client):
    n = index_client(client, rebuild=False)
    if not n:
        print(f"No indexable documents found for {client} in clients/{client}/docs")
        return
    print(f"Indexed {n} chunks for {client}")


if __name__ == "__main__":
    main(sys.argv[1])
