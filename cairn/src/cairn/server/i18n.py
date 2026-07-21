from pathlib import Path
import json
from functools import lru_cache

# Lightweight server-side i18n helper. Reads JSON language packs from the
# static/locales directory and provides a simple t(key, lang) accessor.
# This is intentionally minimal: it is meant for small server-generated
# user-facing messages. API field names, DB columns and machine-readable
# strings MUST NOT be translated.

STATIC_DIR = Path(__file__).parent / "static"


@lru_cache(maxsize=8)
def load_locale(lang: str):
    path = STATIC_DIR / "locales" / f"{lang}.json"
    if not path.exists():
        path = STATIC_DIR / "locales" / "en.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def t(key: str, lang: str = "en") -> str:
    """Return translated string for key in given lang, fallback to key."""
    loc = load_locale(lang)
    return loc.get(key, key)
