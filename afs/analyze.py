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

    prompt = f"""Analyze this image and respond with ONLY a JSON object (no other text):
{{
  "topic": "single PLURAL word — the broad category (e.g. politics, animals, science, vehicles, memes, comics, games, sports, architecture, nature, food, religion, mythology, history, finance, technology, education, emotions, celebrities, maps, code, documents, music, configs)",
  "keywords": ["2-5 SPECIFIC descriptive words for a good filename"],
  "confidence": 0.0 to 1.0
}}
{filename_context}
CRITICAL RULES:
- topic MUST be PLURAL (animals not animal, comics not comic)
- If you recognize a SPECIFIC character (SpongeBob, Pepe, Mickey Mouse, Wojak, etc.), put their name in keywords
- If you recognize a SPECIFIC person, put their name in keywords
- Prefer SPECIFIC proper nouns over generic descriptions
- Keywords should form a good filename when joined: ["spongebob", "birthday", "cake"] → spongebob-birthday-cake.jpg
- Keep keywords SHORT (1-2 words each), max 5 keywords total
- topic must be lowercase
- If this looks like a text document or code, still analyze the CONTENT visible in the image"""

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
    return {
        "topic": data.get("topic", "unsorted").lower().strip(),
        "keywords": data.get("keywords", []),
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


def enhance_with_character(keywords: list[str], character_name: str) -> list[str]:
    """Inject identified character name into keywords, replacing generic terms."""
    name_parts = character_name.lower().split()
    new_kw = list(name_parts)

    for kw in keywords:
        kw_lower = kw.lower()
        skip = any(kw_lower in term or term in kw_lower for term in GENERIC_TRIGGERS)
        if not skip and kw_lower not in new_kw:
            new_kw.append(kw_lower)

    return new_kw[:5]


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
