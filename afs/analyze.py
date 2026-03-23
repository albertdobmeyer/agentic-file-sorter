"""Content analysis via Ollama vision model + character identification.

Vision model analyzes file previews and identifies characters (Step 1 only).
"""

import base64
import json
import pathlib
import re

import requests


# Error type constants
ERR_FILE_READ = "file_read_error"
ERR_OLLAMA_UNREACHABLE = "ollama_unreachable"
ERR_MODEL_TIMEOUT = "model_timeout"
ERR_MODEL_ERROR = "model_error"
ERR_PARSE_FAILURE = "parse_failure"


def _classify_request_error(e: Exception) -> tuple[str, str]:
    """Classify a requests exception into (error_type, message)."""
    msg = str(e)
    if isinstance(e, requests.exceptions.ConnectionError):
        return ERR_OLLAMA_UNREACHABLE, msg
    if isinstance(e, (requests.exceptions.ReadTimeout, requests.exceptions.Timeout)):
        return ERR_MODEL_TIMEOUT, msg
    if isinstance(e, requests.exceptions.HTTPError):
        return ERR_MODEL_ERROR, msg
    return ERR_MODEL_ERROR, msg


def analyze_vision(
    preview_path: pathlib.Path,
    filename_hint: str = "",
    config: dict | None = None,
    photo_hint: bool = False,
    sample_descriptions: dict[str, str] | None = None,
) -> dict:
    """Analyze a preview image using the vision model.

    Returns {"topic": str, "keywords": list[str], "confidence": float}.
    On error, also returns {"error": str, "error_type": str}.
    """
    cfg = config or {}
    models = cfg.get("models", {})
    ollama_url = models.get("ollama_url", "http://localhost:11434")
    vision_model = models.get("vision_model", "llava:latest")
    vision_timeout = models.get("vision_timeout", 180)
    vision_ctx = models.get("vision_ctx", 4096)
    keep_alive = models.get("keep_alive", "30m")

    try:
        image_data = preview_path.read_bytes()
        image_base64 = base64.b64encode(image_data).decode("utf-8")
    except Exception as e:
        return {
            "topic": "unsorted", "keywords": [], "confidence": 0.0,
            "error": str(e), "error_type": ERR_FILE_READ,
        }

    filename_context = ""
    if filename_hint:
        filename_context = (
            f'\nThe original filename is: "{filename_hint}"\n'
            "Use this as a HINT — trust what you SEE over what the filename says.\n"
        )

    photo_context = ""
    if photo_hint:
        photo_context = (
            "\nThis is a CAMERA PHOTO. Naming priorities for photos:\n"
            "1. WHO: name people if recognizable, otherwise 'man', 'woman', 'child', 'couple', 'group'\n"
            "2. WHAT: main subject or activity (e.g. 'birthday', 'hiking', 'dinner')\n"
            "3. WHERE: location or setting (e.g. 'beach', 'kitchen', 'park')\n"
            "If the photo is dark/blurry with no discernible content, use just one keyword like 'dark' or 'blurry'.\n"
        )

    # Sample descriptions: text-only identification context
    sample_context = ""
    if sample_descriptions:
        lines = []
        for name, desc in sample_descriptions.items():
            lines.append(f"  - {name}: {desc}")
        sample_block = "\n".join(lines)
        sample_context = (
            f"\nKNOWN SUBJECTS (identify if any appear in this image):\n"
            f"{sample_block}\n"
            f"If you recognize any of these SPECIFIC subjects, set \"identified\" to their name.\n"
        )

    identified_field = ""
    if sample_descriptions:
        identified_field = '\n  "identified": "name of recognized subject or null",'

    prompt = f"""Analyze this image and respond with ONLY a JSON object (no other text):
{{
  "topic": "single PLURAL word — the broad category (e.g. politics, animals, science, vehicles, memes, comics, games, sports, architecture, nature, food, religion, mythology, history, finance, technology, education, emotions, celebrities, maps, code, documents, music, configs)",
  "phrase": "a short natural description (2-7 words) for a filename",
  "keywords": ["2-4 topic words for folder classification"],{identified_field}
  "confidence": 0.0 to 1.0
}}
{filename_context}{photo_context}{sample_context}
CRITICAL RULES:
- topic MUST be PLURAL and lowercase (animals not animal, comics not comic)
- "phrase" is a NATURAL DESCRIPTION like a human would name the file:
  GOOD: "shepherd sleeping under tree in alps", "cat wearing flower crown at festival"
  BAD: "shepherd tree alps sleeping green", "cat flower crown festival"
- Use connective words (in, at, with, under, on, of, and) to make the phrase read naturally
- Structure the phrase as: [main subject] [action or relationship] [context or location]
- If you recognize a SPECIFIC character (SpongeBob, Pepe, Wojak, etc.), START the phrase with their name
- If you recognize a SPECIFIC person, START the phrase with their name
- NEVER use these words in phrase or keywords: "image", "photo", "photograph", "picture", "person", "individual", "subject"
- Instead of "person" use: "man", "woman", "child", "couple", "group", "crowd"
- "keywords" are for folder classification ONLY — short topic words, NOT the filename
- Each keyword must add UNIQUE information — no synonyms
- If this looks like a text document or code, still analyze the CONTENT visible"""

    try:
        resp = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": vision_model,
                "prompt": prompt,
                "images": [image_base64],
                "stream": False,
                "options": {"num_ctx": vision_ctx, "temperature": 0.1},
                "keep_alive": keep_alive,
            },
            timeout=(30, vision_timeout),
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
    except Exception as e:
        err_type, err_msg = _classify_request_error(e)
        return {
            "topic": "unsorted", "keywords": [], "confidence": 0.0,
            "error": err_msg, "error_type": err_type,
        }

    data = parse_json(raw)
    if not data:
        return {
            "topic": "unsorted", "keywords": [], "confidence": 0.0,
            "error": f"unparseable response: {raw[:200]}",
            "error_type": ERR_PARSE_FAILURE,
        }
    # Safely extract fields — model may return wrong types
    topic = data.get("topic", "unsorted")
    if not isinstance(topic, str):
        topic = str(topic) if topic else "unsorted"
    phrase = data.get("phrase", "")
    if not isinstance(phrase, str):
        phrase = " ".join(phrase) if isinstance(phrase, list) else str(phrase) if phrase else ""
    keywords = data.get("keywords", [])
    if not isinstance(keywords, list):
        keywords = [str(keywords)] if keywords else []
    # Fallback: if no phrase but has keywords, construct a basic phrase
    if not phrase and keywords:
        phrase = " ".join(str(k) for k in keywords[:5])
    # Extract identified subject (from sample descriptions)
    identified = data.get("identified", None)
    if identified and isinstance(identified, str) and identified.lower() not in ("null", "none", ""):
        identified = identified.lower().strip()
    else:
        identified = None

    return {
        "topic": topic.lower().strip(),
        "phrase": phrase,
        "keywords": [str(k) for k in keywords],
        "identified": identified,
        "confidence": float(data.get("confidence", 0.0)),
    }


# --- Character identification ---

GENERIC_TRIGGERS = {
    "cartoon character", "animated character", "cartoon", "animated",
    "frog character", "yellow character", "anthropomorphic",
    "fictional character", "character", "mascot",
    "unknown character", "unidentified",
}

CHARACTER_PROMPT = (
    "This appears to be a cartoon/character. Common characters include: "
    "SpongeBob, Patrick Star, Squidward, Pepe the Frog, Wojak, "
    "Mickey Mouse, Donald Duck, Goofy, Homer Simpson, Bart Simpson, "
    "Shrek, Donkey, Mario, Luigi, Pikachu, Garfield, Grinch, "
    "Rick Sanchez, Morty Smith, Peter Griffin, Stewie Griffin, "
    "Bugs Bunny, Daffy Duck, Tom, Jerry, Scooby-Doo, Shaggy, "
    "Winnie the Pooh, Tigger, Elmo, Cookie Monster, Kermit the Frog, "
    "Sonic the Hedgehog, Kirby, Link, Yoshi, Toad, "
    "Dora the Explorer, Finn the Human, Jake the Dog, "
    "Bender, Fry, SpongeBob SquarePants, Sandy Cheeks, "
    "Thanos, Iron Man, Spider-Man, Batman, Superman, Joker, "
    "Darth Vader, Baby Yoda, Grogu, Minion, Shiba Inu, Doge, "
    "Trollface, Chad, NPC, Gigachad, Soyjak, Amogus, Among Us. "
    "Which specific character is this? Respond with ONLY the character name, "
    "or UNKNOWN if you cannot identify them."
)


def needs_identification(
    topic: str,
    keywords: list[str],
    confidence: float,
    config: dict | None = None,
) -> bool:
    """Check if the analysis is generic enough to warrant character identification."""
    cfg = config or {}
    threshold = cfg.get("processing", {}).get("confidence_threshold", 0.5)
    if confidence < threshold:
        return True

    combined = " ".join(kw.lower() for kw in keywords) + " " + topic.lower()
    return any(term in combined for term in GENERIC_TRIGGERS)


def identify_character(
    preview_path: pathlib.Path,
    config: dict | None = None,
) -> str | None:
    """Re-query the vision model with targeted character identification prompt."""
    cfg = config or {}
    models = cfg.get("models", {})
    ollama_url = models.get("ollama_url", "http://localhost:11434")
    vision_model = models.get("vision_model", "llava:latest")
    keep_alive = models.get("keep_alive", "30m")

    try:
        image_data = preview_path.read_bytes()
        image_base64 = base64.b64encode(image_data).decode("utf-8")
    except Exception:
        return None

    try:
        resp = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": vision_model,
                "prompt": CHARACTER_PROMPT,
                "images": [image_base64],
                "stream": False,
                "options": {"num_ctx": 2048, "temperature": 0.1},
                "keep_alive": keep_alive,
            },
            timeout=(30, 30),
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
    except Exception:
        return None

    name = raw.strip().strip('"').strip("'").strip(".")

    if not name or name.upper() == "UNKNOWN" or len(name) > 50:
        return None

    if any(phrase in name.lower() for phrase in [
        "i cannot", "i can't", "i don't", "i'm not sure",
        "it appears", "this is", "the character",
    ]):
        return None

    return name


def enhance_with_character(
    phrase: str, keywords: list[str], character_name: str,
) -> tuple[str, list[str]]:
    """Inject identified character name into phrase and keywords."""
    name_lower = character_name.lower()

    # Phrase: prepend character name if not already present
    if name_lower not in phrase.lower():
        phrase = f"{name_lower} {phrase}".strip()

    # Keywords: replace generic terms with character name
    name_parts = name_lower.split()
    new_kw = list(name_parts)
    for kw in keywords:
        kw_lower = kw.lower()
        skip = any(kw_lower in term or term in kw_lower for term in GENERIC_TRIGGERS)
        if not skip and kw_lower not in new_kw:
            new_kw.append(kw_lower)

    return phrase, new_kw[:5]


def parse_json(text: str) -> dict:
    """Extract JSON from model response, handling markdown fences and think tags."""
    text = text.strip()
    if "```" in text:
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()
    # Strip think tags from qwen3
    text = re.sub(r"</?no_think>", "", text).strip()
    text = re.sub(r"</?think>.*?</think>", "", text, flags=re.DOTALL).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {}
