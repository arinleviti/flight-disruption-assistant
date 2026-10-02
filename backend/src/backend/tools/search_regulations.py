from backend.rag.knowledge_base import search_knowledge_base


def search_regulations(query: str) -> dict:
    """Search the EU261 knowledge base (regulation, court rulings) by meaning."""
    query = query.strip()
    if not query:
        return {"error": "The search query is empty."}

    try:
        passages = search_knowledge_base(query, n_results=3)
    except Exception as e:
        return {"error": f"The knowledge base could not be searched: {e}"}

    return {"passages": passages, "count": len(passages)}


SEARCH_REGULATIONS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_regulations",
        "description": (
            "Searches the EU261 knowledge base (the regulation and rulings of the EU Court of "
            "Justice) by meaning, and returns the 3 most relevant passages with their source."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look up, in plain words, e.g. 'is a strike by the airline's own pilots an extraordinary circumstance'.",
                },
            },
            "required": ["query"],
        },
    },
}