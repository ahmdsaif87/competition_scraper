from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import random
import re
import time
import unicodedata
from urllib.parse import urljoin, urlparse

import cloudscraper
import pymongo
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from pymongo import UpdateOne

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "")
MONGO_URI = os.environ.get("MONGO_URI")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DB_NAME = os.environ.get("DB_NAME", "competition_scraper")
COLLECTION = os.environ.get("COLLECTION", "competition")
IG_ACCOUNTS = [
    a.strip()
    for a in os.environ.get("IG_ACCOUNTS", "infolomba,infolomba_gratis,infolomba.olimpiade").split(",")
    if a.strip()
]
MAX_WEB_ITEMS = int(os.environ.get("MAX_WEB_ITEMS", "15"))
MAX_IG_POSTS_PER_ACCOUNT = int(os.environ.get("MAX_IG_POSTS_PER_ACCOUNT", "6"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s<>'\"`)\]}]+", re.IGNORECASE)
INSTAGRAM_SHORTCODE_RE = re.compile(r"/(?:p|reel)/([^/?#]+)/?")
TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:judul|title|nama\s+lomba|competition|event)\s*[:\-]\s*", re.IGNORECASE
)
OPEN_REGISTRATION_RE = re.compile(
    r"open\s+registration\s*[:\-]\s*([^\]\n]+)", re.IGNORECASE
)
WHITESPACE_RE = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# Keyword sets
# ---------------------------------------------------------------------------

REGISTRATION_KEYWORDS = {"daftar", "pendaftaran", "register", "registrasi", "registration", "apply", "submission", "submit", "link pendaftaran"}
NON_REGISTRATION_KEYWORDS = {"guidebook", "booklet", "juknis", "contact", "kontak", "narahubung", "email", "tiktok", "youtube", "disclaimer"}
TITLE_NOISE_KEYWORDS = {"link pendaftaran", "pendaftaran", "register", "registration", "apply now", "guidebook", "contact us", "deadline", "benefit", "prize", "hadiah", "timeline", "save the date", "open registration", "closed registration", "terbuka untuk", "untuk mahasiswa"}
MAHASISWA_KEYWORDS = {"mahasiswa", "mahasiswi", "universitas", "kampus", "s1", "d3", "d4", "umum", "undergraduate", "diploma", "student", "university"}
BLOCKED_SOCIAL_HOSTS = {"instagram.com", "facebook.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com", "wa.me", "api.whatsapp.com"}
FORM_HOSTS = {"forms.gle", "docs.google.com", "bit.ly", "s.id", "tinyurl.com", "lynk.id"}
DEDUP_STOPWORDS = {"the", "of", "and", "in", "on", "at", "to", "a", "an", "di", "ke", "se", "dan", "atau", "untuk", "dengan", "dalam", "dari", "oleh", "yang", "adalah", "ini", "itu"}
SOURCE_PRIORITY = {"infolomba.id": 0, "silomba.id": 1}

# ---------------------------------------------------------------------------
# Kategori system (REFACTORED — word-boundary matching)
# ---------------------------------------------------------------------------
#
# Setiap kategori punya:
#   - "phrases": multi-word phrases (high confidence, +20 per match)
#   - "words":   single words matched with \b boundary (+10 per match)
#   - "exclude": jika kata ini muncul, skip kategori ini (anti false-positive)
#   - "priority": tiebreaker (lower = lebih prioritas)
#
# PENTING: Semua matching pakai regex word-boundary (\b), bukan substring.
# Ini mencegah "it" match di "submit", "ai" match di "sampai", dll.
# ---------------------------------------------------------------------------

KATEGORI_CONFIG = {
    "IT": {
        "phrases": {
            "machine learning", "data science", "artificial intelligence",
            "deep learning", "competitive programming", "web development",
            "software engineering", "cyber security", "capture the flag",
            "teknologi informasi", "ilmu komputer", "computer science",
        },
        "words": {
            "programming", "coding", "developer", "software", "database",
            "backend", "frontend", "fullstack", "python", "javascript",
            "java", "golang", "rust", "devops", "cybersecurity",
            "hacking", "ctf", "pemrograman", "programmer", "algorithm",
            "algoritma", "hackathon",
        },
        "exclude": set(),
        "priority": 1,
    },
    "Bisnis": {
        "phrases": {
            "business plan", "business case", "investor pitch", "studi kasus",
            "case study", "business model", "social entrepreneurship",
            "kewirausahaan sosial", "business competition",
        },
        "words": {
            "bisnis", "business", "entrepreneurship", "startup", "investor",
            "kewirausahaan", "wirausaha", "marketing", "finance", "accounting",
            "venture", "pitch",
        },
        "exclude": set(),
        "priority": 3,
    },
    "Webdev": {
        "phrases": {
            "web development", "web design", "web developer", "web designer",
            "web application", "front end", "back end", "full stack",
            "ui/ux", "user interface", "user experience",
        },
        "words": {
            "webdev", "html", "css", "react", "vue", "angular", "nextjs",
            "laravel", "django", "flask", "wordpress",
        },
        "exclude": set(),
        "priority": 2,
    },
    "Design": {
        "phrases": {
            "graphic design", "ui design", "ux design", "desain grafis",
            "desain komunikasi visual", "motion design", "brand identity",
            "visual identity",
        },
        "words": {
            "desain", "branding", "logo", "figma", "photoshop", "canva",
            "illustrator", "corel", "typography", "tipografi",
        },
        "exclude": {"puisi", "cerpen", "novel", "sastra", "pantun"},
        "priority": 4,
    },
    "Poster": {
        "phrases": {
            "desain poster", "lomba poster", "poster digital",
            "poster competition", "kreativitas visual", "visual design",
            "poster ilmiah", "poster scientific",
        },
        "words": {
            "poster", "infografis", "infographic",
        },
        "exclude": set(),
        "priority": 4,
    },
    "Data": {
        "phrases": {
            "data science", "data analytics", "data analysis", "big data",
            "machine learning", "data mining", "data engineer", "data analyst",
            "analisis data", "pengolahan data", "visualisasi data",
            "data visualization",
        },
        "words": {
            "tableau", "statistik", "statistics",
        },
        "exclude": {"data diri", "data peserta", "data pribadi", "isi data"},
        "priority": 2,
    },
    "Mobile": {
        "phrases": {
            "app development", "mobile app", "aplikasi mobile",
            "android app", "ios app", "mobile development",
            "react native", "flutter app",
        },
        "words": {
            "android", "flutter", "kotlin", "swift",
        },
        "exclude": set(),
        "priority": 2,
    },
    "Game": {
        "phrases": {
            "game development", "game design", "game dev", "kompetisi game",
            "game jam", "indie game",
        },
        "words": {
            "gaming", "esports", "unity", "unreal", "godot",
        },
        "exclude": set(),
        "priority": 3,
    },
    "Multimedia": {
        "phrases": {
            "motion graphic", "produksi video", "video editing",
            "short film", "film pendek", "after effects",
        },
        "words": {
            "multimedia", "cinematography", "sinematografi", "premiere",
            "animasi", "animation",
        },
        "exclude": {"video profil", "video ucapan"},
        "priority": 4,
    },
    "IoT": {
        "phrases": {
            "internet of things", "iot project", "smart device",
            "embedded system", "sistem embedded",
        },
        "words": {
            "iot", "microcontroller", "arduino", "raspberry", "sensor",
            "embedded",
        },
        "exclude": set(),
        "priority": 2,
    },
    "Robotics": {
        "phrases": {
            "lomba robot", "robot competition", "line follower",
            "sumo robot",
        },
        "words": {
            "robotics", "robot", "robotika", "mekatronika",
        },
        "exclude": set(),
        "priority": 2,
    },
    "Karya Tulis": {
        "phrases": {
            "karya tulis", "karya tulis ilmiah", "lomba essay", "lomba esai",
            "essay competition", "paper competition", "call for paper",
            "scientific paper", "research paper", "literature review",
            "lomba menulis", "writing competition", "lomba artikel",
        },
        "words": {
            "essay", "esai", "makalah", "jurnal", "skripsi", "artikel",
            "opini", "kolom",
        },
        "exclude": set(),
        "priority": 2,
    },
    "Debat": {
        "phrases": {
            "lomba debat", "debate competition", "english debate",
            "debat bahasa", "parliamentary debate", "asian parliamentary",
        },
        "words": {
            "debat", "debate", "moot", "argumentasi",
        },
        "exclude": set(),
        "priority": 2,
    },
    "Seni & Sastra": {
        "phrases": {
            "lomba puisi", "lomba cerpen", "baca puisi", "cipta puisi",
            "lomba sastra", "lomba pantun", "lomba fotografi", "photo contest",
            "lomba foto", "seni rupa", "seni tari", "seni musik",
            "lomba menyanyi", "lomba band", "festival seni",
        },
        "words": {
            "puisi", "cerpen", "sastra", "pantun", "novel",
            "fotografi", "photography", "kaligrafi",
            "tari", "musik", "menyanyi", "teater",
        },
        "exclude": set(),
        "priority": 3,
    },
}

# Pre-compile regex patterns per kategori untuk performa
_KATEGORI_COMPILED: dict[str, dict] = {}
for _kat, _cfg in KATEGORI_CONFIG.items():
    _compiled = {"priority": _cfg["priority"], "exclude": _cfg["exclude"]}

    # Compile phrase patterns (match as substring, case-insensitive)
    _compiled["phrase_patterns"] = [
        re.compile(re.escape(p), re.IGNORECASE) for p in _cfg["phrases"]
    ]
    # Compile word patterns (match with word boundary)
    _compiled["word_patterns"] = [
        re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE) for w in _cfg["words"]
    ]
    _KATEGORI_COMPILED[_kat] = _compiled

MONTH_MAP = {
    "januari": "Januari", "january": "Januari", "jan": "Januari",
    "februari": "Februari", "february": "Februari", "feb": "Februari",
    "maret": "Maret", "march": "Maret", "mar": "Maret",
    "april": "April", "apr": "April",
    "mei": "Mei", "may": "Mei",
    "juni": "Juni", "june": "Juni", "jun": "Juni",
    "juli": "Juli", "july": "Juli", "jul": "Juli",
    "agustus": "Agustus", "august": "Agustus", "aug": "Agustus",
    "september": "September", "sept": "September", "sep": "September",
    "oktober": "Oktober", "october": "Oktober", "oct": "Oktober",
    "november": "November", "nov": "November",
    "desember": "Desember", "december": "Desember", "dec": "Desember",
}

# Build regex alternation from valid month names only
_MONTH_NAMES_RE = "|".join(sorted(MONTH_MAP.keys(), key=len, reverse=True))

# FIXED: Date range pattern — only matches valid month names, not arbitrary words
TIMELINE_PATTERN = re.compile(
    r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s+(" + _MONTH_NAMES_RE + r")\s*(\d{4})?",
    re.IGNORECASE
)

# FIXED: Single date pattern — only matches valid month names
SINGLE_DATE_PATTERN = re.compile(
    r"(\d{1,2})\s+(" + _MONTH_NAMES_RE + r")(?:\s+(\d{4}))?",
    re.IGNORECASE
)

# Pattern: "BulanNama dd, yyyy" atau "BulanNama dd yyyy" (English-style dates)
ENGLISH_DATE_PATTERN = re.compile(
    r"(" + _MONTH_NAMES_RE + r")\s+(\d{1,2})(?:\s*,\s*|\s+)(\d{4})",
    re.IGNORECASE
)

# Pattern: "dd/mm/yyyy" atau "dd-mm-yyyy"
NUMERIC_DATE_PATTERN = re.compile(
    r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})"
)

# Month number to name mapping for numeric dates
_MONTH_NUM_MAP = {
    1: "Januari", 2: "Februari", 3: "Maret", 4: "April",
    5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus",
    9: "September", 10: "Oktober", 11: "November", 12: "Desember",
}


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def make_id(title: str, source: str) -> str:
    normalized = f"{source}_{title}".lower().strip()
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def is_mahasiswa(text: str) -> bool:
    lower = (text or "").lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", lower) for kw in MAHASISWA_KEYWORDS)


def normalize_space(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text or "").strip()


def clean_url(url: str, base_url: str = "") -> str:
    if not url:
        return ""
    url = url.strip().strip(".,;:!?\"'`)]}")
    if url.startswith("//"):
        url = "https:" + url
    elif base_url and url.startswith("/"):
        url = urljoin(base_url, url)
    return url if url.startswith(("http://", "https://")) else ""


def strip_emoji_and_symbols(text: str) -> str:
    return "".join(
        " " if (unicodedata.category(ch).startswith("S") and ch not in {"&", "+", "#"}) else ch
        for ch in (text or "")
    )


def clean_title(text: str) -> str:
    text = strip_emoji_and_symbols(text)
    text = TITLE_PREFIX_RE.sub("", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[@#*_`>|\"']+", " ", text)
    text = re.sub(r"\b(?:caption|repost|info lomba|infolomba)\b", " ", text, flags=re.I)
    text = re.sub(r"\s*[-–—|:]\s*(?:open registration|registration|pendaftaran).*$", "", text, flags=re.I)
    return normalize_space(text)[:140].strip(" -:|") or "Tanpa Judul"


def safe_json_loads(text: str) -> list:
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\[.*\]", text or "", re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return []


def is_low_value_url(url: str) -> bool:
    return urlparse(url).netloc.lower() in BLOCKED_SOCIAL_HOSTS


def _keywords_in(text: str, keywords: set) -> bool:
    lower = (text or "").lower()
    return any(kw in lower for kw in keywords)


def is_registration_context(text: str) -> bool:
    return _keywords_in(text, REGISTRATION_KEYWORDS)


def is_non_registration_context(text: str) -> bool:
    return _keywords_in(text, NON_REGISTRATION_KEYWORDS)


# ---------------------------------------------------------------------------
# Timeline & Kategori extraction (REFACTORED)
# ---------------------------------------------------------------------------

def _normalize_month(month_str: str) -> str:
    """Normalize bulan ke format Bulan penuh"""
    return MONTH_MAP.get(month_str.lower().strip(), "")


def _is_valid_month(month_str: str) -> bool:
    """Check apakah string adalah nama bulan yang valid"""
    return month_str.lower().strip() in MONTH_MAP


def _is_valid_day(day: int) -> bool:
    """Check apakah day masuk akal (1-31)"""
    return 1 <= day <= 31


def _extract_deadline_context(text: str) -> str:
    """
    Cari bagian text yang berisi konteks deadline/pendaftaran.
    Prioritaskan kalimat yang mengandung keyword deadline.
    """
    deadline_keywords = {
        "deadline", "batas", "pendaftaran", "registrasi", "registration",
        "penutupan", "terakhir", "close", "closing", "due date", "submit",
        "submission", "pengumpulan", "tenggat", "berakhir",
    }
    
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    deadline_lines = []
    
    for idx, line in enumerate(lines):
        lower = line.lower()
        if any(kw in lower for kw in deadline_keywords):
            # Ambil baris ini + 2 baris setelahnya sebagai konteks
            context = " ".join(lines[idx:min(len(lines), idx + 3)])
            deadline_lines.append(context)
    
    return " ".join(deadline_lines) if deadline_lines else ""


def extract_timeline(text: str) -> str:
    """
    REFACTORED: Extract timeline/deadline dengan akurat.
    
    Strategi:
    1. Cari konteks deadline dulu (baris dengan keyword "deadline", "pendaftaran", dll)
    2. Dari konteks tersebut, extract tanggal
    3. Jika tidak ada konteks deadline, fallback ke semua text
    4. Hanya match nama bulan yang VALID (dari MONTH_MAP)
    5. Validasi day range (1-31)
    
    Format output:
    - "dd-dd Bulan yyyy" (range dalam 1 bulan)
    - "dd Bulan - dd Bulan yyyy" (range antar bulan)
    - "dd Bulan yyyy" (single date)
    - "" (tidak ditemukan)
    """
    if not text:
        return ""
    
    # Coba cari dari konteks deadline dulu
    deadline_ctx = _extract_deadline_context(text)
    
    # Urutan pencarian: konteks deadline > full text
    search_texts = [deadline_ctx, text] if deadline_ctx else [text]
    
    for search_text in search_texts:
        result = _extract_dates_from_text(search_text)
        if result:
            return result
    
    return ""


def _extract_dates_from_text(text: str) -> str:
    """
    Extract tanggal dari text. Hanya match bulan yang valid.
    Returns formatted date string atau empty string.
    """
    if not text:
        return ""
    
    # Strategy 1: Date range "13 - 19 Mei 2026" atau "13-19 Mei 2026"
    range_matches = []
    for match in TIMELINE_PATTERN.finditer(text):
        day_start_s, day_end_s, month_s, year_s = match.groups()
        month_norm = _normalize_month(month_s)
        if not month_norm:  # Bulan tidak valid
            continue
        day_start, day_end = int(day_start_s), int(day_end_s)
        if not (_is_valid_day(day_start) and _is_valid_day(day_end)):
            continue
        if day_end < day_start:  # End harus >= start dalam satu bulan
            continue
        year_str = year_s if year_s else "2026"
        range_matches.append({
            "start_day": day_start,
            "end_day": day_end,
            "month": month_norm,
            "year": year_str,
            "start_pos": match.start(),
        })
    
    if range_matches:
        range_matches.sort(key=lambda x: x["start_pos"])
        first = range_matches[0]
        all_months = set(m["month"] for m in range_matches)
        
        if len(all_months) == 1:
            min_day = min(m["start_day"] for m in range_matches)
            max_day = max(m["end_day"] for m in range_matches)
            return f"{min_day}-{max_day} {first['month']} {first['year']}"
        else:
            last = range_matches[-1]
            return f"{first['start_day']} {first['month']} - {last['end_day']} {last['month']} {last['year']}"
    
    # Strategy 2: English-style dates "June 15, 2026"
    eng_matches = []
    for match in ENGLISH_DATE_PATTERN.finditer(text):
        month_s, day_s, year_s = match.groups()
        month_norm = _normalize_month(month_s)
        if not month_norm:
            continue
        day = int(day_s)
        if not _is_valid_day(day):
            continue
        eng_matches.append({
            "day": day,
            "month": month_norm,
            "year": year_s,
            "start_pos": match.start(),
        })
    
    if eng_matches:
        eng_matches.sort(key=lambda x: x["start_pos"])
        if len(eng_matches) >= 2:
            first, last = eng_matches[0], eng_matches[-1]
            if first["month"] == last["month"]:
                return f"{first['day']}-{last['day']} {first['month']} {last['year']}"
            return f"{first['day']} {first['month']} - {last['day']} {last['month']} {last['year']}"
        m = eng_matches[0]
        return f"{m['day']} {m['month']} {m['year']}"
    
    # Strategy 3: Single dates "7 Juni 2026" atau "7 Juni"
    single_matches = []
    for match in SINGLE_DATE_PATTERN.finditer(text):
        day_s, month_s, year_s = match.groups()
        month_norm = _normalize_month(month_s)
        if not month_norm:
            continue
        day = int(day_s)
        if not _is_valid_day(day):
            continue
        year_str = year_s if year_s else "2026"
        single_matches.append({
            "day": day,
            "month": month_norm,
            "year": year_str,
            "start_pos": match.start(),
        })
    
    if single_matches:
        single_matches.sort(key=lambda x: x["start_pos"])
        if len(single_matches) >= 2:
            first, last = single_matches[0], single_matches[-1]
            year_str = last["year"]
            if first["month"] == last["month"]:
                return f"{first['day']}-{last['day']} {first['month']} {year_str}"
            return f"{first['day']} {first['month']} - {last['day']} {last['month']} {year_str}"
        m = single_matches[0]
        return f"{m['day']} {m['month']} {m['year']}"
    
    # Strategy 4: Numeric dates "15/06/2026" atau "15-06-2026"
    num_matches = []
    for match in NUMERIC_DATE_PATTERN.finditer(text):
        d, m, y = int(match.group(1)), int(match.group(2)), match.group(3)
        if 1 <= m <= 12 and _is_valid_day(d):
            month_name = _MONTH_NUM_MAP.get(m, "")
            if month_name:
                num_matches.append({
                    "day": d,
                    "month": month_name,
                    "year": y,
                    "start_pos": match.start(),
                })
    
    if num_matches:
        num_matches.sort(key=lambda x: x["start_pos"])
        if len(num_matches) >= 2:
            first, last = num_matches[0], num_matches[-1]
            if first["month"] == last["month"]:
                return f"{first['day']}-{last['day']} {first['month']} {last['year']}"
            return f"{first['day']} {first['month']} - {last['day']} {last['month']} {last['year']}"
        m = num_matches[0]
        return f"{m['day']} {m['month']} {m['year']}"
    
    return ""


def extract_kategori(text: str, title: str = "") -> str:
    """
    REFACTORED: Extract kategori dengan word-boundary regex matching.

    Perbaikan utama:
    - TIDAK lagi pakai substring matching (yang menyebabkan "it" match di "submit").
    - Multi-word phrases diberi skor lebih tinggi (+20) daripada single words (+10).
    - Exclusion keywords: jika "puisi" muncul, kategori Design di-skip.
    - Minimum score threshold: butuh minimal 10 point untuk masuk kategori.
    """
    combined = f"{title} {text}"
    combined_lower = combined.lower()

    scores: dict[str, int] = {}

    for kategori, compiled in _KATEGORI_COMPILED.items():
        # Check exclusion keywords dulu (substring ok untuk exclusion)
        if compiled["exclude"] and any(ex in combined_lower for ex in compiled["exclude"]):
            continue

        score = 0

        # Score phrases (+20 each — high confidence)
        for pat in compiled["phrase_patterns"]:
            if pat.search(combined):
                score += 20

        # Score words (+10 each — medium confidence, word-boundary)
        for pat in compiled["word_patterns"]:
            if pat.search(combined):
                score += 10

        if score > 0:
            scores[kategori] = score

    if not scores:
        return "Lainnya"

    # Sort by score descending, then by priority ascending (lower = better)
    best = max(
        scores.items(),
        key=lambda x: (x[1], -_KATEGORI_COMPILED[x[0]]["priority"]),
    )
    return best[0]


# ---------------------------------------------------------------------------
# HTML / soup helpers
# ---------------------------------------------------------------------------

def anchor_rows(soup: BeautifulSoup, base_url: str = "") -> list[dict]:
    return [
        {"url": href, "label": normalize_space(a.get_text(" "))}
        for a in soup.find_all("a", href=True)
        if (href := clean_url(a["href"], base_url))
    ]


def best_poster_from_soup(soup: BeautifulSoup, base_url: str = "") -> str:
    for tag, attr in [("meta", "og:image"), ("meta", "twitter:image")]:
        node = soup.find(tag, attrs={"property": attr} if "og:" in attr else {"name": attr})
        if node and (url := clean_url(node.get("content", ""), base_url)):
            return url

    for img in soup.find_all("img"):
        src = clean_url(
            img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "", base_url
        )
        if src and not any(skip in src.lower() for skip in ("logo", "avatar", "profile")):
            return src
    return ""


# ---------------------------------------------------------------------------
# Title extraction (REFACTORED)
# ---------------------------------------------------------------------------

def _line_has_url(line: str) -> bool:
    return bool(URL_RE.search(line))


def _is_noise_title(line: str) -> bool:
    if _line_has_url(line) or len(normalize_space(line)) < 6:
        return True
    return _keywords_in(line, TITLE_NOISE_KEYWORDS)


def _score_title(line: str, position: int) -> int:
    """IMPROVED: Better scoring untuk title extraction"""
    lower = line.lower()
    score = 100 - (position * 5)  # Prefer earlier lines
    
    # High-value keywords
    score += 40 * any(w in lower for w in {"lomba", "olimpiade", "competition", "contest"})
    score += 35 * any(w in lower for w in {"national", "nasional", "se-indonesia"})
    score += 30 * any(w in lower for w in {"championship", "tournament", "kompetisi"})
    score += 20 * any(w in lower for w in {"conference", "summit", "bootcamp", "program", "award"})
    
    # Structural indicators
    score += 15 * (line.isupper() and len(line) > 8)
    score += 10 * bool(re.search(r"\b20\d{2}\b", line))
    
    return max(0, score)


def extract_title_from_caption(caption: str) -> str:
    """
    IMPROVED: Extract title dengan logic yang lebih smart
    - Prioritas 1: Official headers (CAPS, h1/h2 text)
    - Prioritas 2: Lines dengan competition keywords
    - Prioritas 3: First non-noise line
    """
    lines = [normalize_space(l) for l in (caption or "").splitlines() if normalize_space(l)]
    candidates: list[tuple[int, str]] = []

    # Pass 1: Cari lines dengan "open registration" pattern
    for idx, line in enumerate(lines[:20]):
        match = OPEN_REGISTRATION_RE.search(strip_emoji_and_symbols(line))
        if match:
            title = clean_title(match.group(1))
            if title != "Tanpa Judul":
                candidates.append((_score_title(title, idx) + 50, title))

    # Pass 2: Score all non-noise lines
    for idx, line in enumerate(lines[:25]):
        if _is_noise_title(line):
            continue
        
        title = clean_title(line)
        if title != "Tanpa Judul":
            score = _score_title(title, idx)
            # Bonus untuk lines yang match CAPS pattern (biasanya official title)
            if line.isupper() and len(line) > 10:
                score += 30
            candidates.append((score, title))

    if candidates:
        # Return highest scored title
        best = max(candidates)[1]
        # Avoid returning obvious non-titles
        if best and best not in {"Tanpa Judul", "Hello Everyone"}:
            return best

    # Fallback: return first substantial non-noise line
    for line in lines:
        if not _is_noise_title(line):
            title = clean_title(line)
            if title != "Tanpa Judul" and len(title) > 5:
                return title
    
    return "Tanpa Judul"


# ---------------------------------------------------------------------------
# Link extraction (REFACTORED)
# ---------------------------------------------------------------------------

def extract_urls_from_text(text: str) -> list[str]:
    """Extract semua URLs dari text"""
    seen, result = set(), []
    for m in URL_RE.finditer(text or ""):
        url = clean_url(m.group(0))
        if url and url not in seen and not is_low_value_url(url):
            seen.add(url)
            result.append(url)
    return result


def extract_registration_links(text: str = "", anchors: list[dict] | None = None) -> list[str]:
    """
    REFACTORED: Extract registration links dengan prioritas lebih jelas:
    1. Form hosts (bit.ly, forms.gle, etc) - PRIMARY
    2. Anchor links dengan registration context - SECONDARY
    3. Text URLs dengan registration context - TERTIARY
    """
    anchors = anchors or []
    found: list[str] = []
    seen = set()

    # Strategy 1: Extract form hosts (highest priority)
    all_text_urls = extract_urls_from_text(text)
    all_anchor_urls = [row.get("url", "") for row in anchors if row.get("url")]
    
    for raw_url in all_text_urls + all_anchor_urls:
        url = clean_url(raw_url)
        if url and url not in seen and not is_low_value_url(url):
            netloc = urlparse(url).netloc.lower()
            if any(host in netloc for host in FORM_HOSTS):
                found.append(url)
                seen.add(url)

    # Strategy 2: Anchor links dengan registration context
    for row in anchors:
        url = clean_url(row.get("url", ""))
        if not url or url in seen or is_low_value_url(url):
            continue
        
        label = row.get("label", "")
        if is_registration_context(label) and not is_non_registration_context(label):
            found.append(url)
            seen.add(url)

    # Strategy 3: Text URLs dalam registration context
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for idx, line in enumerate(lines):
        urls = extract_urls_from_text(line)
        if not urls:
            continue
        
        context = " ".join(lines[max(0, idx - 1): min(len(lines), idx + 2)])
        
        if is_registration_context(context) and not is_non_registration_context(line):
            for url in urls:
                if url not in seen:
                    found.append(url)
                    seen.add(url)

    return list(dict.fromkeys(found))


# ---------------------------------------------------------------------------
# LLM (OpenRouter DeepSeek) helpers
# ---------------------------------------------------------------------------

def _call_openrouter(prompt: str) -> list:
    """
    Call OpenRouter API dengan DeepSeek v4 Flash (free tier)
    """
    if not OPENROUTER_API_KEY:
        logger.warning("[LLM] OPENROUTER_API_KEY not set, skipping LLM processing")
        return []
    
    try:
        response = requests.post(
            url="https://openrouter.io/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://github.com",
                "X-Title": "Competition Scraper",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek/deepseek-v4-flash:free",
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "temperature": 0.3,
                "max_tokens": 2000,
            },
            timeout=60
        )
        
        if response.status_code != 200:
            logger.error("[LLM] Error %s: %s", response.status_code, response.text)
            return []
        
        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        # Try to parse JSON from response
        data = safe_json_loads(content)
        return data if isinstance(data, list) else []
        
    except Exception as exc:
        logger.exception("[LLM] Error: %s", exc)
        return []


_VALID_KATEGORI = frozenset(KATEGORI_CONFIG.keys()) | {"Lainnya"}

_LLM_PROMPT_PREFIX = (
    "Rapikan data lomba untuk mahasiswa. Untuk setiap item, kembalikan JSON array "
    '{"id":"id","judul":"judul resmi","deadline":"timeline","kategori":"kategori","penyelenggara":"penyelenggara"}. '
    "Field judul harus berupa nama lomba/program/event saja, tanpa emoji, sapaan, label "
    "pendaftaran, tanggal, atau URL. "
    "Field deadline adalah timeline gabungan dari tanggal awal sampai akhir "
    "(cth: '23-28 Januari 2026' atau '23 Januari - 28 Februari 2026'), kosongkan jika tidak ada. "
    "Field kategori HARUS salah satu dari: "
    "IT, Bisnis, Webdev, Design, Poster, Data, Mobile, Game, Multimedia, IoT, Robotics, "
    "Karya Tulis, Debat, Seni & Sastra, Lainnya. "
    "Field penyelenggara adalah nama organisasi atau institusi penyelenggara (kosongkan jika tidak ada). "
    "Ekstrak dari caption yang diberikan. "
    "Jika tidak yakin, pertahankan data yang sudah ada atau isi dengan string kosong.\n\nData: "
)


def _item_needs_llm(item: dict) -> bool:
    """Check apakah item butuh LLM processing (ada field kosong/lemah)."""
    has_deadline = bool(item.get("deadline"))
    has_kategori = item.get("kategori", "Lainnya") != "Lainnya"
    has_penyelenggara = bool(item.get("penyelenggara"))
    # Butuh LLM jika salah satu field masih kosong
    return not (has_deadline and has_kategori and has_penyelenggara)


def process_batch_with_openrouter(batch: list) -> list:
    """
    Process batch items dengan OpenRouter DeepSeek.
    OPTIMIZED: Skip LLM call jika semua field sudah terisi.
    """
    if not batch:
        return batch

    # Pre-extract timeline dan kategori dari text
    for item in batch:
        caption = item.get("caption", "")
        if not item.get("deadline"):
            item["deadline"] = extract_timeline(caption)
        if not item.get("kategori") or item.get("kategori") == "Lainnya":
            item["kategori"] = extract_kategori(caption, item.get("judul", ""))

    # Filter: hanya kirim item yang masih butuh LLM
    needs_llm = [item for item in batch if _item_needs_llm(item)]
    if not needs_llm:
        logger.info("[LLM] All %d items already complete, skipping LLM call.", len(batch))
        return batch

    logger.info("[LLM] %d/%d items need LLM processing.", len(needs_llm), len(batch))

    payload = [
        {
            "id": item["id"],
            "judul": item.get("judul", ""),
            "caption": item.get("caption", "")[:1000],
            "deadline": item.get("deadline", ""),
            "kategori": item.get("kategori", ""),
        }
        for item in needs_llm
    ]

    llm_results = _call_openrouter(_LLM_PROMPT_PREFIX + json.dumps(payload, ensure_ascii=False))

    llm_map = {
        row.get("id"): row
        for row in llm_results
        if isinstance(row, dict) and row.get("id")
    }

    for item in batch:
        row = llm_map.get(item["id"], {})
        if not row:
            continue

        # Update judul jika LLM return yang lebih baik
        if row.get("judul"):
            title = clean_title(row["judul"])
            if title != "Tanpa Judul":
                item["judul"] = title

        # Update deadline
        if row.get("deadline"):
            item["deadline"] = row["deadline"]

        # Update kategori — VALIDATE terhadap daftar kategori yang valid
        llm_kat = row.get("kategori", "")
        if llm_kat and llm_kat in _VALID_KATEGORI and llm_kat != "Lainnya":
            item["kategori"] = llm_kat

        # Update penyelenggara
        if row.get("penyelenggara"):
            item["penyelenggara"] = row["penyelenggara"]

    time.sleep(1)
    return batch


# ---------------------------------------------------------------------------
# Data structure builder
# ---------------------------------------------------------------------------

def _build_item(uid, source, title, poster, caption, links, direct_url) -> dict:
    """
    Build item dengan struktur JSON baru
    """
    return {
        "id": uid,
        "caption": caption,
        "deadline": extract_timeline(caption),
        "judul": title,
        "kategori": extract_kategori(caption, title),
        "link_direct": direct_url,
        "link_pendaftaran": links,
        "penyelenggara": "",  # Diisi oleh LLM atau parsing manual
        "poster": poster,
        "sumber": source,
    }


# ---------------------------------------------------------------------------
# Retry & HTTP helpers
# ---------------------------------------------------------------------------

def retry_with_backoff(max_attempts: int = 3, base_delay: float = 2.0):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except (requests.ConnectionError, requests.Timeout) as e:
                    if attempt == max_attempts - 1:
                        raise
                    logger.warning("Retry %d/%d for %s: %s", attempt + 1, max_attempts, func.__name__, e)
                    time.sleep(base_delay * (2 ** attempt))
            return None
        return wrapper
    return decorator


def _create_scraper() -> requests.Session:
    try:
        return cloudscraper.create_scraper()
    except Exception as exc:
        logger.warning("cloudscraper failed (%s), falling back to requests.Session", exc)
        session = requests.Session()
        session.headers.update(HEADERS)
        return session


# ---------------------------------------------------------------------------
# Scraper: infolomba.id
# ---------------------------------------------------------------------------

def scrape_infolomba(seen_ids: set) -> list:
    logger.info("[infolomba] Starting...")
    base_url = "https://infolomba.id"
    scraper = _create_scraper()
    results = []

    try:
        resp = scraper.get(base_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        unique_links = {
            urljoin(base_url, a["href"]): a
            for a in soup.find_all("a", href=lambda h: h and "info-" in h)
            if urljoin(base_url, a.get("href", "")).startswith(base_url + "/")
        }

        # Attempt pagination: follow "next page" or page-2 style links
        pagination_links = set()
        next_page = soup.find("a", string=re.compile(r"(next|selanjutnya|berikutnya|\d+)", re.I))
        if not next_page:
            next_page = soup.find("a", class_=re.compile(r"(next|pagination|page)", re.I))
        if next_page and (href := next_page.get("href")):
            pagination_links.add(urljoin(base_url, href))

        for page_url in pagination_links:
            try:
                presp = scraper.get(page_url, headers=HEADERS, timeout=30)
                if presp.status_code == 200:
                    psoup = BeautifulSoup(presp.text, "html.parser")
                    for a in psoup.find_all("a", href=lambda h: h and "info-" in h):
                        href = urljoin(base_url, a["href"])
                        if href.startswith(base_url + "/") and href not in unique_links:
                            unique_links[href] = a
            except requests.RequestException:
                pass

        logger.info("[infolomba] Found %d links (incl. pagination)", len(unique_links))

        @retry_with_backoff(max_attempts=2)
        def _fetch_page(url: str) -> requests.Response | None:
            return scraper.get(url, headers=HEADERS, timeout=30)

        for link, anchor in list(unique_links.items())[:MAX_WEB_ITEMS]:
            try:
                res = _fetch_page(link)
                if res is None or res.status_code != 200:
                    continue

                dsoup = BeautifulSoup(res.text, "html.parser")
                full_text = dsoup.get_text("\n")
                if not is_mahasiswa(full_text):
                    continue

                title_tag = dsoup.find(["h1", "h2"])
                slug_title = (
                    "-".join(link.rstrip("/").split("/")[-1].replace("info-", "", 1).split("-")[:-1])
                    .replace("-", " ").title()
                )
                title = clean_title(title_tag.get_text(" ") if title_tag else slug_title)
                uid = make_id(title, "infolomba.id")
                if uid in seen_ids:
                    continue

                if "Daftar Sekarang" in full_text and "Laporkan Lomba" in full_text:
                    body = full_text.split("Daftar Sekarang")[-1].split("Laporkan Lomba")[0]
                    caption = "\n".join(l.strip() for l in body.splitlines() if l.strip())
                else:
                    caption = "\n".join(l.strip() for l in full_text.splitlines() if l.strip())[:2500]

                poster = best_poster_from_soup(dsoup, base_url)
                if not poster:
                    img = anchor.find("img") or {}
                    poster = clean_url(img.get("src") or img.get("data-src") or "", base_url)

                results.append(_build_item(
                    uid, "infolomba.id", title, poster, caption,
                    extract_registration_links(full_text, anchor_rows(dsoup, base_url)),
                    link,
                ))
                seen_ids.add(uid)

            except requests.RequestException as exc:
                logger.warning("[infolomba] Skip %s: %s", link, exc)
            except Exception as exc:
                logger.exception("[infolomba] Skip %s: %s", link, exc)

    except requests.RequestException as exc:
        logger.error("[infolomba] Error: %s", exc)
    except Exception as exc:
        logger.exception("[infolomba] Error: %s", exc)

    logger.info("[infolomba] Done: %d items", len(results))
    return results


# ---------------------------------------------------------------------------
# Scraper: silomba.id
# ---------------------------------------------------------------------------

async def scrape_silomba(seen_ids: set) -> list:
    logger.info("[silomba] Starting...")
    base_url = "https://silomba.id"
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=HEADERS["User-Agent"])
            await page.goto(base_url, wait_until="networkidle", timeout=45000)
            await page.wait_for_selector("#competition-section", timeout=15000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            soup = BeautifulSoup(await page.content(), "html.parser")
            await page.close()

            section = soup.find(id="competition-section")
            if not section:
                return results

            for card in section.find_all("a", href=lambda h: h and h.startswith("/lomba/"))[:MAX_WEB_ITEMS]:
                raw_title = (
                    card.get("aria-label", "").replace("Lihat detail kompetisi ", "").strip()
                    or (
                        (h := card.find(["h1", "h2", "h3", "h4"])) and h.get_text(" ").strip()
                    )
                    or "Tanpa Judul"
                )
                title = clean_title(raw_title)
                uid = make_id(title, "silomba.id")
                if uid in seen_ids:
                    continue

                link_detail = urljoin(base_url, card["href"])
                poster = caption = ""
                links = []

                try:
                    dp = await browser.new_page(user_agent=HEADERS["User-Agent"])
                    await dp.goto(link_detail, wait_until="networkidle", timeout=45000)
                    dsoup = BeautifulSoup(await dp.content(), "html.parser")
                    await dp.close()

                    full_text = dsoup.get_text("\n")
                    if not is_mahasiswa(full_text):
                        continue

                    poster = best_poster_from_soup(dsoup, base_url)
                    caption = (
                        full_text.split("Deskripsi Lomba")[-1].strip()
                        if "Deskripsi Lomba" in full_text
                        else "\n".join(l.strip() for l in full_text.splitlines() if l.strip())[:2500]
                    )
                    links = extract_registration_links(full_text, anchor_rows(dsoup, base_url))

                except Exception as exc:
                    logger.exception("[silomba] Detail failed %s: %s", link_detail, exc)

                results.append(_build_item(uid, "silomba.id", title, poster, caption, links, link_detail))
                seen_ids.add(uid)

        except Exception as exc:
            logger.exception("[silomba] Error: %s", exc)
        finally:
            await browser.close()

    logger.info("[silomba] Done: %d items", len(results))
    return results


# ---------------------------------------------------------------------------
# Scraper: Instagram
# ---------------------------------------------------------------------------

def _normalize_ig_caption(raw: str) -> str:
    caption = re.sub(r"^\s*[^:\n]{1,80}\s+on Instagram:\s*", "", (raw or "").strip(), flags=re.I)
    return re.sub(r'^\s*"|\"\s*$', "", caption).strip()


def _ig_shortcode(url: str) -> str:
    match = INSTAGRAM_SHORTCODE_RE.search(url or "")
    return match.group(1) if match else url


_IG_POSTER_JS = """
const imgs = Array.from(document.querySelectorAll('article img'));
for (const img of imgs) {
  const src = img.currentSrc || img.src || '';
  const alt = (img.alt || '').toLowerCase();
  if (!src || alt.includes('profile') || src.includes('150x150')) continue;
  if (src.includes('scontent') || src.includes('cdninstagram')) return src;
}
return '';
"""


async def _collect_post_urls(page, account: str) -> list[str]:
    await page.goto(f"https://www.instagram.com/{account}/", wait_until="networkidle")
    await page.wait_for_timeout(random.randint(4000, 6000))

    if "page not found" in (await page.title()).lower():
        return []

    seen, urls = set(), []
    last_height = await page.evaluate("document.body.scrollHeight")
    for _ in range(3):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(random.randint(2000, 4000))

        hrefs = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]'))
                .map(el => el.href)
        """)
        for href in hrefs:
            if href and href not in seen:
                seen.add(href)
                urls.append(href)

        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height
    return urls


async def _scrape_ig_post(browser, url: str, account: str, seen_ids: set) -> dict | None:
    page = await browser.new_page(user_agent=HEADERS["User-Agent"])
    try:
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(random.randint(3000, 5000))

        try:
            await page.wait_for_selector("article", timeout=10000)
        except Exception:
            await page.wait_for_selector("img", timeout=10000)

        caption = ""
        h1s = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('article h1')).map(el => el.textContent)
        """)
        if h1s:
            caption = h1s[0]
        if not caption:
            meta = await page.evaluate("""() => {
                const el = document.querySelector('meta[property="og:description"]');
                return el ? el.content : '';
            }""")
            if meta:
                caption = meta.split(": ", 1)[1] if ": " in meta else meta

        caption = _normalize_ig_caption(caption or await page.title())
        if not caption or not is_mahasiswa(caption):
            return None

        uid = make_id(_ig_shortcode(url), f"IG @{account}")
        if uid in seen_ids:
            return None

        poster = await page.evaluate(_IG_POSTER_JS)
        if not poster:
            poster = await page.evaluate("""() => {
                const el = document.querySelector('meta[property="og:image"]');
                return el ? el.content : '';
            }""")

        title = extract_title_from_caption(caption)
        return _build_item(
            uid, f"IG @{account}",
            title, poster, caption,
            extract_registration_links(caption),
            url,
        )
    finally:
        await page.close()


async def scrape_instagram(seen_ids: set) -> list:
    if not IG_SESSION_ID:
        logger.warning("[IG] IG_SESSION_ID not set, skipping.")
        return []

    logger.info("[IG] Starting...")
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await context.new_page()

        try:
            await page.goto("https://www.instagram.com/", wait_until="networkidle")
            await page.wait_for_timeout(3000)

            await context.add_cookies([{
                "name": "sessionid",
                "value": IG_SESSION_ID,
                "domain": ".instagram.com",
                "path": "/",
            }])

            await page.reload()
            await page.wait_for_timeout(5000)

            if "login" in page.url.lower():
                logger.error("[IG] Invalid session.")
                return []

            for account in IG_ACCOUNTS:
                try:
                    post_urls = await _collect_post_urls(page, account)
                    for url in post_urls[:MAX_IG_POSTS_PER_ACCOUNT]:
                        try:
                            item = await _scrape_ig_post(browser, url, account, seen_ids)
                            if item:
                                results.append(item)
                                seen_ids.add(item["id"])
                        except Exception as exc:
                            logger.exception("[IG] Skip post %s: %s", url, exc)
                except Exception as exc:
                    logger.exception("[IG] Error collecting posts for %s: %s", account, exc)

        except Exception as exc:
            logger.exception("[IG] Error: %s", exc)
        finally:
            await browser.close()

    logger.info("[IG] Done: %d items", len(results))
    return results


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _tokenize(title: str) -> set:
    return {
        t for t in re.sub(r"[^\w\s]", " ", (title or "").lower()).split()
        if t not in DEDUP_STOPWORDS and len(t) > 1
    }


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def dedup_results(new_items: list, db_data: list, threshold: float = 0.6) -> list:
    db_direct_urls = {d["link_direct"] for d in db_data if d.get("link_direct")}
    db_tokens = [_tokenize(d["judul"]) for d in db_data if d.get("judul")]
    unique: list[dict] = []

    for item in new_items:
        item["judul"] = clean_title(item.get("judul", ""))
        item["link_pendaftaran"] = list(dict.fromkeys(
            u for raw in item.get("link_pendaftaran", []) if (u := clean_url(raw))
        ))

        token = _tokenize(item.get("judul", ""))
        link = item.get("link_direct", "")

        if link and link in db_direct_urls:
            logger.info("[DEDUP] Skip duplicate URL: %s", link)
            continue
        if any(_jaccard(token, db_tok) >= threshold for db_tok in db_tokens):
            logger.info("[DEDUP] Skip title similar to DB: %r", item['judul'])
            continue

        dup_idx = next(
            (i for i, ex in enumerate(unique) if _jaccard(token, _tokenize(ex.get("judul", ""))) >= threshold),
            None,
        )

        if dup_idx is not None:
            existing = unique[dup_idx]
            merged = list(dict.fromkeys(existing.get("link_pendaftaran", []) + item.get("link_pendaftaran", [])))
            if SOURCE_PRIORITY.get(item["sumber"], 2) < SOURCE_PRIORITY.get(existing["sumber"], 2):
                item["link_pendaftaran"] = merged
                unique[dup_idx] = item
                logger.info("[DEDUP] Replace %s -> %s: %r", existing['sumber'], item['sumber'], item['judul'])
            else:
                unique[dup_idx]["link_pendaftaran"] = merged
                logger.info("[DEDUP] Merge links %s -> %s: %r", item['sumber'], existing['sumber'], item['judul'])
        else:
            unique.append(item)

    logger.info("[DEDUP] %d -> %d unique items.", len(new_items), len(unique))
    return unique


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not set.")

    if MONGO_URI and not MONGO_URI.startswith("mongodb"):
        raise RuntimeError("MONGO_URI does not look valid (must start with mongodb:// or mongodb+srv://).")

    if IG_ACCOUNTS and not IG_SESSION_ID:
        logger.warning("IG_ACCOUNTS set but IG_SESSION_ID is empty — Instagram will be skipped.")

    logger.info("[INFO] Connecting to MongoDB...")
    client = pymongo.MongoClient(MONGO_URI)
    collection = client[DB_NAME][COLLECTION]

    db_data = list(collection.find({}, {"id": 1, "link_direct": 1, "judul": 1, "_id": 0}))
    seen_ids = {d["id"] for d in db_data if "id" in d}
    logger.info("[INFO] %d existing records in DB.", len(seen_ids))

    batches = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba, seen_ids),
        scrape_silomba(seen_ids),
        scrape_instagram(seen_ids),
    )
    raw = [item for batch in batches if isinstance(batch, list) for item in batch]
    logger.info("[INFO] %d new raw items found.", len(raw))

    if not raw:
        logger.info("[INFO] No new data.")
        client.close()
        return

    processed = []
    for i in range(0, len(raw), 15):
        batch = raw[i: i + 15]
        logger.info("[LLM] Batch %d (%d items)...", i // 15 + 1, len(batch))
        processed.extend(process_batch_with_openrouter(batch))

    final = dedup_results(processed, db_data)
    if not final:
        logger.info("[INFO] All items are duplicates.")
        client.close()
        return

    result = collection.bulk_write(
        [UpdateOne({"id": item["id"]}, {"$set": item}, upsert=True) for item in final]
    )
    logger.info("[INFO] Saved: %d new, %d updated.", result.upserted_count, result.modified_count)
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
