import re
from pathlib import Path

import chromadb
from chromadb.config import Settings

# backend/src/backend/rag/knowledge_base.py -> parents[3] is the backend root folder
DATA_DIR = Path(__file__).resolve().parents[3] / "data"
DOCS_DIR = DATA_DIR / "eu261"
INDEX_DIR = DATA_DIR / "chroma"

COLLECTION_NAME = "eu261"


def get_client() -> chromadb.PersistentClient:
    """Open the vector store on disk (created if it doesn't exist)."""
    return chromadb.PersistentClient(
        path=str(INDEX_DIR),
        settings=Settings(anonymized_telemetry=False),
    )


def split_into_chunks(text: str) -> list[tuple[str, str]]:
    """Split one knowledge-base file into (section_title, chunk_text) pairs.

    Each "## " heading starts a new chunk, so a search returns one focused section.
    The file's intro (its "# " title and "Topic:" line) is added to the top of
    every chunk, so each one still says what it's about, and the intro is not
    stored as a separate, nearly empty chunk.
    """
    parts = re.split(r"\n(?=## )", text.strip())
    intro = parts[0].strip()      # everything before the first "## " heading
    sections = parts[1:]

    if not sections:              # a file with no "## " headings: keep it whole
        return [("Whole document", intro)]

    chunks = []
    for section in sections:
        section = section.strip()
        section_title = section.splitlines()[0].lstrip("# ").strip()
        chunks.append((section_title, f"{intro}\n\n{section}"))
    return chunks


def build_knowledge_base() -> None:
    """Rebuild the vector index from the Markdown files in data/eu261.

    Runs at every server start, like the flight inventory, so editing a
    file in data/eu261 is all it takes to update what the agent can find.
    """
    client = get_client()

    # Start fresh on every build
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass  # the collection didn't exist yet

    # "cosine" measures how close two meanings are, whatever the text length
    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    ids, documents, metadatas = [], [], []
    for path in sorted(DOCS_DIR.glob("*.md")):
        for number, (section_title, chunk) in enumerate(split_into_chunks(path.read_text(encoding="utf-8"))):
            ids.append(f"{path.stem}-{number}")
            documents.append(chunk)
            metadatas.append({"source": path.name, "section": section_title})

    # Chroma embeds every chunk with its built-in local model while adding them
    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    print(f"KNOWLEDGE BASE: indexed {len(ids)} chunks from {DOCS_DIR.name}/")


def search_knowledge_base(query: str, n_results: int = 4) -> list[dict]:
    """Return the chunks whose meaning is closest to the query, best first."""
    collection = get_client().get_collection(COLLECTION_NAME)
    results = collection.query(query_texts=[query], n_results=n_results)

    # Chroma returns one list per query; we only sent one query, hence the [0]
    passages = []
    for text, metadata, distance in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        passages.append({
            "source": metadata["source"],
            "section": metadata["section"],
            "relevance": round(1 - distance, 2),   # 1 = identical meaning, 0 = unrelated
            "text": text,
        })
    return passages