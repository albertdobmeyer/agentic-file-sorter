"""Web search confirmation — text-only, no image upload.

Optional Layer 4 identification: when the vision model is uncertain about
a character or celebrity, generate a text search query and use DuckDuckGo's
Instant Answer API to confirm. Only text goes out, only text comes back.

Gated behind web_search_assist: false (default). Requires explicit opt-in.
Privacy: no images, file paths, or personal data are ever sent.
"""

import re

import requests

from afs.analyze import parse_json


def search_for_context(
    phrase: str,
    keywords: list[str],
    config: dict | None = None,
) -> dict | None:
    """Search DuckDuckGo for context about an image's content.

    Args:
        phrase: the vision model's description of the image
        keywords: topic keywords from vision analysis

    Returns:
        {
            "query": str,           # what was searched
            "heading": str,         # main result heading (e.g., "Pepe the Frog")
            "abstract": str,        # short description
            "related": list[str],   # related topic labels
            "suggested_name": str,  # extracted proper name if found
        }
        or None if no useful results.
    """
    cfg = config or {}
    if not cfg.get("processing", {}).get("web_search_assist", False):
        return None

    # Build a search query from the phrase and keywords
    query = _build_search_query(phrase, keywords)
    if not query:
        return None

    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
            },
            timeout=10,
            headers={"User-Agent": "AFS/1.0 (Agentic File Sorter)"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    heading = data.get("Heading", "").strip()
    abstract = data.get("AbstractText", "").strip()

    # Extract related topics
    related = []
    for topic in data.get("RelatedTopics", [])[:5]:
        if isinstance(topic, dict) and topic.get("Text"):
            related.append(topic["Text"][:100])

    if not heading and not abstract and not related:
        return None

    # Try to extract a proper name from the heading
    suggested_name = _extract_proper_name(heading) if heading else None

    return {
        "query": query,
        "heading": heading,
        "abstract": abstract[:300] if abstract else "",
        "related": related,
        "suggested_name": suggested_name,
    }


def build_search_context_text(search_result: dict) -> str:
    """Format search results as text context for the vision model."""
    if not search_result:
        return ""

    parts = []
    if search_result.get("heading"):
        parts.append(f"Web search suggests: {search_result['heading']}")
    if search_result.get("abstract"):
        parts.append(search_result["abstract"][:200])

    return ". ".join(parts) if parts else ""


def _build_search_query(phrase: str, keywords: list[str]) -> str:
    """Build a search query from vision model output.

    Strategy: extract proper nouns and distinctive terms.
    DuckDuckGo Instant Answers works best with specific topic names.
    """
    # Combine phrase and keywords
    all_words = []
    if phrase:
        all_words.extend(phrase.split())
    for kw in keywords:
        for w in kw.split():
            if w.lower() not in [aw.lower() for aw in all_words]:
                all_words.append(w)

    # Look for proper nouns first (capitalized words that aren't sentence-start)
    proper_nouns = [w for w in all_words if w[0].isupper() and len(w) > 2]
    if proper_nouns:
        return " ".join(proper_nouns[:4])

    # Fall back to distinctive content words
    stop_words = {
        "a", "an", "the", "in", "at", "on", "with", "of", "and", "or",
        "is", "are", "was", "were", "for", "to", "from", "by",
        "man", "woman", "child", "group", "people", "crowd",
        "wearing", "holding", "sitting", "standing", "looking",
        "dark", "blurry", "bright", "colorful", "small", "large", "old", "new",
    }
    filtered = [w for w in all_words
                if w.lower() not in stop_words and len(w) > 2 and not w.isdigit()]

    if not filtered:
        return ""

    query = " ".join(filtered[:5])

    # Add context hint for cartoon/meme content
    meme_signals = {"cartoon", "meme", "animated", "comic", "frog", "drawing"}
    if any(w.lower() in meme_signals for w in filtered):
        query += " meme character"

    return query


def _extract_proper_name(heading: str) -> str | None:
    """Extract a usable proper name from a DuckDuckGo heading."""
    if not heading:
        return None

    # Remove parenthetical disambiguation
    heading = re.sub(r"\s*\(.*?\)\s*", "", heading).strip()

    # If it's a short, capitalized name, use it
    words = heading.split()
    if 1 <= len(words) <= 4:
        return heading.lower().replace(" ", "-")

    return None
