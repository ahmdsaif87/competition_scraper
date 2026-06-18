from __future__ import annotations

import asyncio
import hashlib
import json
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
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


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

URL_RE = re.compile(r"https?://[^\s<>'\"`)}\]]+", re.IGNORECASE)
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
# Kategori system (word-boundary matching)
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

# Pre-compile regex patterns per kategori
_KATEGORI_COMPILED: dict[str, dict] = {}
for _kat, _cfg in KATEGORI_CONFIG.items():
    _compiled = {"priority": _cfg["priority"], "exclude": _cfg["exclude"]}
    _compiled["phrase_patterns"] = [
        re.compile(re.escape(p), re.IGNORECASE) for p in _cfg["phrases"]
    ]
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

_MONTH_NAMES_RE = "|".join(sorted(MONTH_MAP.keys(), key=len, reverse=True))

# Month order for comparison
_MONTH_ORDER = {
    "Januari": 1, "Februari": 2, "Maret": 3, "April": 4,
    "Mei": 5, "Juni": 6, "Juli": 7, "Agustus": 8,
    "September": 9, "Oktober": 10, "November": 11, "Desember": 12,
}

# Date range: "13 - 19 Mei 2026" or "13-19 Mei 2026"
TIMELINE_PATTERN = re.compile(
    r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s+(" + _MONTH_NAMES_RE + r")(?:\s+(\d{4}))?",
    re.IGNORECASE
)

# Cross-month range: "7 Juni - 5 Juli 2026" or "7 Jun - 5 Jul 2026"
CROSS_MONTH_PATTERN = re.compile(
    r"(\d{1,2})\s+(" + _MONTH_NAMES_RE + r")\s*[-–—]\s*(\d{1,2})\s+(" + _MONTH_NAMES_RE + r")(?:\s+(\d{4}))?",
    re.IGNORECASE
)

# Single date: "7 Juni 2026" or "7 Juni"
SINGLE_DATE_PATTERN = re.compile(
    r"(\d{1,2})\s+(" + _MONTH_NAMES_RE + r")(?:\s+(\d{4}))?",
    re.IGNORECASE
)

# English-style: "June 15, 2026"
ENGLISH_DATE_PATTERN = re.compile(
    r"(" + _MONTH_NAMES_RE + r")\s+(\d{1,2})(?:\s*,\s*|\s+)(\d{4})",
    re.IGNORECASE
)

# Numeric: "15/06/2026" or "15-06-2026"
NUMERIC_DATE_PATTERN = re.compile(
    r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})"
)

# Infolomba tanggal format in listing cards: "14 Jun - 22 Jul 2026"
INFOLOMBA_DATE_RE = re.compile(
    r"(\d{1,2})\s+(" + _MONTH_NAMES_RE + r")\s*-\s*(\d{1,2})\s+(" + _MONTH_NAMES_RE + r")\s+(\d{4})",
    re.IGNORECASE
)

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
    return any(kw in lower for kw in MAHASISWA_KEYWORDS)


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
    elif base_url and not url.startswith(("http://", "https://")):
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
# Timeline & Kategori extraction
# ---------------------------------------------------------------------------

def _normalize_month(month_str: str) -> str:
    return MONTH_MAP.get(month_str.lower().strip(), "")


def _is_valid_day(day: int) -> bool:
    return 1 <= day <= 31


def _date_tuple(day: int, month: str, year: str) -> tuple[int, int, int]:
    """Return (year, month_num, day) for comparison."""
    return (int(year), _MONTH_ORDER.get(month, 0), day)


def _extract_deadline_context(text: str) -> str:
    """
    Extract lines containing deadline keywords + surrounding context.
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
            context = " ".join(lines[idx:min(len(lines), idx + 3)])
            deadline_lines.append(context)

    return " ".join(deadline_lines) if deadline_lines else ""


def extract_timeline(text: str) -> str:
    """
    Extract timeline/deadline. Strategies:
    1. Search deadline context first
    2. Fallback to full text
    3. Only match valid month names
    4. Support cross-month ranges (e.g., "7 Juni - 5 Juli 2026")
    5. Validate day ranges properly
    """
    if not text:
        return ""

    deadline_ctx = _extract_deadline_context(text)
    search_texts = [deadline_ctx, text] if deadline_ctx else [text]

    for search_text in search_texts:
        result = _extract_dates_from_text(search_text)
        if result:
            return result

    return ""


def _extract_dates_from_text(text: str) -> str:
    if not text:
        return ""

    # Strategy 1: Cross-month range "7 Juni - 5 Juli 2026"
    cross_matches = []
    for match in CROSS_MONTH_PATTERN.finditer(text):
        d1, m1, d2, m2, y = match.groups()
        m1_norm = _normalize_month(m1)
        m2_norm = _normalize_month(m2)
        if not (m1_norm and m2_norm):
            continue
        day1, day2 = int(d1), int(d2)
        if not (_is_valid_day(day1) and _is_valid_day(day2)):
            continue
        year = y if y else "2026"
        # Validate chronological order (month1 should be <= month2)
        t1 = _date_tuple(day1, m1_norm, year)
        t2 = _date_tuple(day2, m2_norm, year)
        if t2 < t1:
            # Swap if reversed
            day1, day2 = day2, day1
            m1_norm, m2_norm = m2_norm, m1_norm
        cross_matches.append({
            "start_day": day1, "start_month": m1_norm,
            "end_day": day2, "end_month": m2_norm,
            "year": year, "start_pos": match.start(),
        })

    if cross_matches:
        cross_matches.sort(key=lambda x: x["start_pos"])
        # Use the last cross-month range (most likely the deadline)
        last = cross_matches[-1]
        if last["start_month"] == last["end_month"]:
            return f"{last['start_day']}-{last['end_day']} {last['start_month']} {last['year']}"
        return f"{last['start_day']} {last['start_month']} - {last['end_day']} {last['end_month']} {last['year']}"

    # Strategy 2: Same-month date range "13-19 Mei 2026"
    range_matches = []
    for match in TIMELINE_PATTERN.finditer(text):
        day_start_s, day_end_s, month_s, year_s = match.groups()
        month_norm = _normalize_month(month_s)
        if not month_norm:
            continue
        day_start, day_end = int(day_start_s), int(day_end_s)
        if not (_is_valid_day(day_start) and _is_valid_day(day_end)):
            continue
        # Same month: end should >= start; if not, swap
        if day_end < day_start:
            day_start, day_end = day_end, day_start
        year_str = year_s if year_s else "2026"
        range_matches.append({
            "start_day": day_start, "end_day": day_end,
            "month": month_norm, "year": year_str,
            "start_pos": match.start(),
        })

    if range_matches:
        range_matches.sort(key=lambda x: x["start_pos"])
        # Use the last match (most likely the deadline/closing date)
        last = range_matches[-1]
        return f"{last['start_day']}-{last['end_day']} {last['month']} {last['year']}"

    # Strategy 3: English-style dates "June 15, 2026"
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
            "day": day, "month": month_norm,
            "year": year_s, "start_pos": match.start(),
        })

    if eng_matches:
        eng_matches.sort(key=lambda x: x["start_pos"])
        if len(eng_matches) >= 2:
            first, last = eng_matches[0], eng_matches[-1]
            if first["month"] == last["month"]:
                d1, d2 = min(first["day"], last["day"]), max(first["day"], last["day"])
                return f"{d1}-{d2} {first['month']} {last['year']}"
            return f"{first['day']} {first['month']} - {last['day']} {last['month']} {last['year']}"
        m = eng_matches[0]
        return f"{m['day']} {m['month']} {m['year']}"

    # Strategy 4: Single dates "7 Juni 2026"
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
            "day": day, "month": month_norm,
            "year": year_str, "start_pos": match.start(),
        })

    if single_matches:
        single_matches.sort(key=lambda x: x["start_pos"])
        if len(single_matches) >= 2:
            first, last = single_matches[0], single_matches[-1]
            year_str = last["year"]
            if first["month"] == last["month"]:
                d1, d2 = min(first["day"], last["day"]), max(first["day"], last["day"])
                return f"{d1}-{d2} {first['month']} {year_str}"
            # Different months — ensure chronological order
            t1 = _date_tuple(first["day"], first["month"], year_str)
            t2 = _date_tuple(last["day"], last["month"], year_str)
            if t2 < t1:
                first, last = last, first
            return f"{first['day']} {first['month']} - {last['day']} {last['month']} {year_str}"
        m = single_matches[0]
        return f"{m['day']} {m['month']} {m['year']}"

    # Strategy 5: Numeric dates "15/06/2026"
    num_matches = []
    for match in NUMERIC_DATE_PATTERN.finditer(text):
        d, m, y = int(match.group(1)), int(match.group(2)), match.group(3)
        if 1 <= m <= 12 and _is_valid_day(d):
            month_name = _MONTH_NUM_MAP.get(m, "")
            if month_name:
                num_matches.append({
                    "day": d, "month": month_name,
                    "year": y, "start_pos": match.start(),
                })

    if num_matches:
        num_matches.sort(key=lambda x: x["start_pos"])
        if len(num_matches) >= 2:
            first, last = num_matches[0], num_matches[-1]
            if first["month"] == last["month"]:
                d1, d2 = min(first["day"], last["day"]), max(first["day"], last["day"])
                return f"{d1}-{d2} {first['month']} {last['year']}"
            return f"{first['day']} {first['month']} - {last['day']} {last['month']} {last['year']}"
        m = num_matches[0]
        return f"{m['day']} {m['month']} {m['year']}"

    return ""


def extract_kategori(text: str, title: str = "") -> str:
    combined = f"{title} {text}"
    combined_lower = combined.lower()

    scores: dict[str, int] = {}

    for kategori, compiled in _KATEGORI_COMPILED.items():
        if compiled["exclude"] and any(ex in combined_lower for ex in compiled["exclude"]):
            continue

        score = 0
        for pat in compiled["phrase_patterns"]:
            if pat.search(combined):
                score += 20
        for pat in compiled["word_patterns"]:
            if pat.search(combined):
                score += 10

        if score > 0:
            scores[kategori] = score

    if not scores:
        return "Lainnya"

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


# Images to skip when looking for posters
_POSTER_SKIP_PATTERNS = (
    "logo", "avatar", "profile", "favicon", "default-share",
    "user.png", "coin.png", "map.png", "calendar.png",
    "apple-touch-icon", "site.webmanifest",
    "wave-header", "bg-header",
)


def _is_poster_url(src: str) -> bool:
    """Check if a URL looks like an actual competition poster."""
    lower = src.lower()
    return not any(skip in lower for skip in _POSTER_SKIP_PATTERNS)


def best_poster_from_soup(soup: BeautifulSoup, base_url: str = "", source: str = "") -> str:
    """
    FIXED: Extract poster image, skipping logos and default images.

    For infolomba.id:
    - Skip og:image because it's always the default-share.png logo
    - Prioritize images from images/event/poster/ path
    - Skip logo images (images/event/logo/)

    For other sources: use og:image as fallback only if it's not a site logo.
    """

    # Strategy 1: Look for poster-specific images first (infolomba pattern)
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        url = clean_url(src, base_url)
        if url and "images/event/poster/" in url.lower():
            return url

    # Strategy 2: Look for image inside image-link or img-container
    for container in soup.select("a.image-link, .img-container, .event-poster"):
        img = container.find("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            url = clean_url(src, base_url)
            if url and _is_poster_url(url):
                return url

    # Strategy 3: og:image / twitter:image (but skip known logos)
    if "infolomba" not in source.lower():
        for tag, attr in [("meta", "og:image"), ("meta", "twitter:image")]:
            node = soup.find(tag, attrs={"property": attr} if "og:" in attr else {"name": attr})
            if node:
                url = clean_url(node.get("content", ""), base_url)
                if url and _is_poster_url(url):
                    return url

    # Strategy 4: Any large img that's not a logo/icon
    for img in soup.find_all("img"):
        src = clean_url(
            img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "", base_url
        )
        if src and _is_poster_url(src):
            # Extra check: skip very small icons by checking width/height attributes
            w = img.get("width", "")
            h = img.get("height", "")
            style = img.get("style", "")
            if w and str(w).isdigit() and int(w) < 50:
                continue
            if h and str(h).isdigit() and int(h) < 50:
                continue
            if "height: 30px" in style or "height: 40px" in style:
                continue
            return src

    return ""


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------

def _line_has_url(line: str) -> bool:
    return bool(URL_RE.search(line))


def _is_noise_title(line: str) -> bool:
    if _line_has_url(line) or len(normalize_space(line)) < 6:
        return True
    return _keywords_in(line, TITLE_NOISE_KEYWORDS)


def _score_title(line: str, position: int) -> int:
    lower = line.lower()
    score = 100 - (position * 5)

    score += 40 * any(w in lower for w in {"lomba", "olimpiade", "competition", "contest"})
    score += 35 * any(w in lower for w in {"national", "nasional", "se-indonesia"})
    score += 30 * any(w in lower for w in {"championship", "tournament", "kompetisi"})
    score += 20 * any(w in lower for w in {"conference", "summit", "bootcamp", "program", "award"})

    score += 15 * (line.isupper() and len(line) > 8)
    score += 10 * bool(re.search(r"\b20\d{2}\b", line))

    return max(0, score)


def extract_title_from_caption(caption: str) -> str:
    lines = [normalize_space(l) for l in (caption or "").splitlines() if normalize_space(l)]
    candidates: list[tuple[int, str]] = []

    for idx, line in enumerate(lines[:20]):
        match = OPEN_REGISTRATION_RE.search(strip_emoji_and_symbols(line))
        if match:
            title = clean_title(match.group(1))
            if title != "Tanpa Judul":
                candidates.append((_score_title(title, idx) + 50, title))

    for idx, line in enumerate(lines[:25]):
        if _is_noise_title(line):
            continue
        title = clean_title(line)
        if title != "Tanpa Judul":
            score = _score_title(title, idx)
            if line.isupper() and len(line) > 10:
                score += 30
            candidates.append((score, title))

    if candidates:
        best = max(candidates)[1]
        if best and best not in {"Tanpa Judul", "Hello Everyone"}:
            return best

    for line in lines:
        if not _is_noise_title(line):
            title = clean_title(line)
            if title != "Tanpa Judul" and len(title) > 5:
                return title

    return "Tanpa Judul"


# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------

def extract_urls_from_text(text: str) -> list[str]:
    seen, result = set(), []
    for m in URL_RE.finditer(text or ""):
        url = clean_url(m.group(0))
        if url and url not in seen and not is_low_value_url(url):
            seen.add(url)
            result.append(url)
    return result


def extract_registration_links(text: str = "", anchors: list[dict] | None = None) -> list[str]:
    anchors = anchors or []
    found: list[str] = []
    seen = set()

    all_text_urls = extract_urls_from_text(text)
    all_anchor_urls = [row.get("url", "") for row in anchors if row.get("url")]

    for raw_url in all_text_urls + all_anchor_urls:
        url = clean_url(raw_url)
        if url and url not in seen and not is_low_value_url(url):
            netloc = urlparse(url).netloc.lower()
            if any(host in netloc for host in FORM_HOSTS):
                found.append(url)
                seen.add(url)

    for row in anchors:
        url = clean_url(row.get("url", ""))
        if not url or url in seen or is_low_value_url(url):
            continue
        label = row.get("label", "")
        if is_registration_context(label) and not is_non_registration_context(label):
            found.append(url)
            seen.add(url)

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
    if not OPENROUTER_API_KEY:
        print("[LLM] OPENROUTER_API_KEY not set, skipping LLM processing")
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
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 2000,
            },
            timeout=60
        )

        if response.status_code != 200:
            print(f"[LLM] Error {response.status_code}: {response.text}")
            return []

        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        data = safe_json_loads(content)
        return data if isinstance(data, list) else []

    except Exception as exc:
        print(f"[LLM] Error: {exc}")
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
    has_deadline = bool(item.get("deadline"))
    has_kategori = item.get("kategori", "Lainnya") != "Lainnya"
    has_penyelenggara = bool(item.get("penyelenggara"))
    return not (has_deadline and has_kategori and has_penyelenggara)


def process_batch_with_openrouter(batch: list) -> list:
    if not batch:
        return batch

    for item in batch:
        caption = item.get("caption", "")
        if not item.get("deadline"):
            item["deadline"] = extract_timeline(caption)
        if not item.get("kategori") or item.get("kategori") == "Lainnya":
            item["kategori"] = extract_kategori(caption, item.get("judul", ""))

    needs_llm = [item for item in batch if _item_needs_llm(item)]
    if not needs_llm:
        print(f"[LLM] All {len(batch)} items already complete, skipping LLM call.")
        return batch

    print(f"[LLM] {len(needs_llm)}/{len(batch)} items need LLM processing.")

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

        if row.get("judul"):
            title = clean_title(row["judul"])
            if title != "Tanpa Judul":
                item["judul"] = title

        if row.get("deadline"):
            item["deadline"] = row["deadline"]

        llm_kat = row.get("kategori", "")
        if llm_kat and llm_kat in _VALID_KATEGORI and llm_kat != "Lainnya":
            item["kategori"] = llm_kat

        if row.get("penyelenggara"):
            item["penyelenggara"] = row["penyelenggara"]

    time.sleep(1)
    return batch


# ---------------------------------------------------------------------------
# Data structure builder
# ---------------------------------------------------------------------------

def _build_item(uid, source, title, poster, caption, links, direct_url) -> dict:
    return {
        "id": uid,
        "caption": caption,
        "deadline": extract_timeline(caption),
        "judul": title,
        "kategori": extract_kategori(caption, title),
        "link_direct": direct_url,
        "link_pendaftaran": links,
        "penyelenggara": "",
        "poster": poster,
        "sumber": source,
    }


# ---------------------------------------------------------------------------
# Scraper: infolomba.id  (REWRITTEN)
# ---------------------------------------------------------------------------

def _parse_infolomba_cards(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """
    Parse competition cards from infolomba.id listing page.
    
    The page structure has two sections:
    - .most-wanted (swiper): featured competitions
    - .event-list (#eventsContainer): regular listing
    
    Each card has:
    - a[href="info-..."] link to detail page
    - img[src="images/event/poster/..."] poster image
    - h4.event-title > a: title
    - .tanggal: date info
    - .penyelenggara span: organizer
    """
    cards = []
    seen_links = set()

    # Parse from event listing containers
    for container in soup.select(".event-container"):
        card = {}

        # Extract link and title
        title_link = container.select_one("h4.event-title a")
        if not title_link:
            continue

        href = title_link.get("href", "")
        if not href or not href.startswith("info-"):
            continue
        link = urljoin(base_url, href)
        if link in seen_links:
            continue
        seen_links.add(link)

        card["link"] = link
        card["title"] = normalize_space(title_link.get_text(" "))

        # Extract poster from img-container
        img_container = container.select_one("a.img-container img, .img-container img")
        if img_container:
            src = img_container.get("src") or img_container.get("data-src") or ""
            poster_url = clean_url(src, base_url)
            if poster_url and "images/event/poster/" in poster_url:
                card["poster"] = poster_url

        # Extract date
        tanggal = container.select_one(".tanggal")
        if tanggal:
            card["date_text"] = normalize_space(tanggal.get_text(" "))

        # Extract penyelenggara
        penyelenggara = container.select_one(".penyelenggara span:not(.subtitle)")
        if penyelenggara:
            card["penyelenggara"] = normalize_space(penyelenggara.get_text())

        # Extract target (peserta)
        target = container.select_one(".target")
        if target:
            card["target"] = normalize_space(target.get_text(" "))

        cards.append(card)

    # Also parse from "most wanted" swiper section (different structure)
    for slide in soup.select(".event-most-container"):
        title_link = slide.select_one("h4.event-title a")
        if not title_link:
            continue

        href = title_link.get("href", "")
        if not href or not href.startswith("info-"):
            continue
        link = urljoin(base_url, href)
        if link in seen_links:
            continue
        seen_links.add(link)

        card = {"link": link, "title": normalize_space(title_link.get_text(" "))}

        img = slide.select_one(".img-container img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            poster_url = clean_url(src, base_url)
            if poster_url and "images/event/poster/" in poster_url:
                card["poster"] = poster_url

        cards.append(card)

    return cards


def scrape_infolomba(seen_ids: set) -> list:
    print("[infolomba] Starting...")
    base_url = "https://infolomba.id"
    scraper = cloudscraper.create_scraper()
    results = []

    try:
        resp = scraper.get(base_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Parse cards from the listing page directly
        cards = _parse_infolomba_cards(soup, base_url)
        print(f"[infolomba] Found {len(cards)} cards on listing page")

        for card in cards[:MAX_WEB_ITEMS]:
            link = card["link"]
            title = clean_title(card.get("title", ""))
            uid = make_id(title, "infolomba.id")
            if uid in seen_ids:
                continue

            try:
                res = scraper.get(link, headers=HEADERS, timeout=30)
                if res.status_code != 200:
                    print(f"[infolomba] Skip {link}: HTTP {res.status_code}")
                    continue

                dsoup = BeautifulSoup(res.text, "html.parser")
                full_text = dsoup.get_text("\n")
                if not is_mahasiswa(full_text):
                    print(f"[infolomba] Skip {title}: not mahasiswa")
                    continue

                # Get better title from detail page
                title_tag = dsoup.select_one("h4.event-title, h3.event-title, h1, h2")
                if title_tag:
                    detail_title = clean_title(title_tag.get_text(" "))
                    if detail_title != "Tanpa Judul":
                        title = detail_title

                # Extract caption from event-description-container
                desc_container = dsoup.select_one(".event-description-container")
                if desc_container:
                    caption = "\n".join(l.strip() for l in desc_container.get_text("\n").splitlines() if l.strip())
                elif "Daftar Sekarang" in full_text and "Laporkan Lomba" in full_text:
                    body = full_text.split("Daftar Sekarang")[-1].split("Laporkan Lomba")[0]
                    caption = "\n".join(l.strip() for l in body.splitlines() if l.strip())
                else:
                    caption = "\n".join(l.strip() for l in full_text.splitlines() if l.strip())[:2500]

                # Get poster: prefer card poster > detail page poster
                poster = card.get("poster", "")
                if not poster:
                    poster = best_poster_from_soup(dsoup, base_url, source="infolomba.id")

                # Get penyelenggara from card or detail page
                penyelenggara = card.get("penyelenggara", "")
                if not penyelenggara:
                    penyelenggara_el = dsoup.select_one(".profile-event-details-container h5.name")
                    if penyelenggara_el:
                        penyelenggara = normalize_space(penyelenggara_el.get_text())

                # Get deadline from card date or caption
                deadline = ""
                date_text = card.get("date_text", "")
                if date_text:
                    deadline = extract_timeline(date_text)
                if not deadline:
                    # Try from detail page tanggal section
                    tanggal_div = dsoup.select_one(".event-details-container .tanggal")
                    if tanggal_div:
                        deadline = extract_timeline(tanggal_div.get_text(" "))
                if not deadline:
                    deadline = extract_timeline(caption)

                item = _build_item(
                    uid, "infolomba.id", title, poster, caption,
                    extract_registration_links(full_text, anchor_rows(dsoup, base_url)),
                    link,
                )
                # Override deadline and penyelenggara with better extracted values
                if deadline:
                    item["deadline"] = deadline
                if penyelenggara:
                    item["penyelenggara"] = penyelenggara

                results.append(item)
                seen_ids.add(uid)
                print(f"[infolomba] ✓ {title}")

            except Exception as exc:
                print(f"[infolomba] Skip {link}: {exc}")

    except Exception as exc:
        print(f"[infolomba] Error: {exc}")

    print(f"[infolomba] Done: {len(results)} items")
    return results


# ---------------------------------------------------------------------------
# Scraper: silomba.id  (REWRITTEN)
# ---------------------------------------------------------------------------

async def scrape_silomba(seen_ids: set) -> list:
    print("[silomba] Starting...")
    base_url = "https://silomba.id"
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=HEADERS["User-Agent"])

            # Navigate to the page
            await page.goto(base_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the competition section to appear
            try:
                await page.wait_for_selector("#competition-section", timeout=15000)
            except Exception:
                print("[silomba] competition-section not found, trying alternative selectors")

            # Scroll down to trigger lazy loading and wait for competitions to load
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await page.wait_for_timeout(3000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(3000)

            # Wait for competition cards to appear (multiple selector strategies)
            card_selectors = [
                'a[href*="/lomba/"]',
                '#competition-section a',
                '[data-analytics-section="competition"] a',
            ]

            cards_found = False
            for selector in card_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=10000)
                    cards_found = True
                    print(f"[silomba] Found cards with selector: {selector}")
                    break
                except Exception:
                    continue

            if not cards_found:
                print("[silomba] No competition cards found after waiting")
                # Try one more scroll + wait cycle
                for _ in range(3):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(2000)

            soup = BeautifulSoup(await page.content(), "html.parser")
            await page.close()

            # Find competition links — try multiple strategies
            competition_links = []
            seen_hrefs = set()

            # Strategy 1: Links with /lomba/ path
            for a_tag in soup.find_all("a", href=lambda h: h and "/lomba/" in h):
                href = a_tag.get("href", "")
                full_url = urljoin(base_url, href)
                if full_url not in seen_hrefs:
                    seen_hrefs.add(full_url)
                    competition_links.append((full_url, a_tag))

            # Strategy 2: If no /lomba/ links, look for cards in competition section
            if not competition_links:
                section = soup.find(id="competition-section")
                if section:
                    for a_tag in section.find_all("a", href=True):
                        href = a_tag.get("href", "")
                        if href.startswith("#") or href.startswith("javascript"):
                            continue
                        full_url = urljoin(base_url, href)
                        if full_url not in seen_hrefs and full_url != base_url:
                            seen_hrefs.add(full_url)
                            competition_links.append((full_url, a_tag))

            print(f"[silomba] Found {len(competition_links)} competition links")

            for link_url, card_tag in competition_links[:MAX_WEB_ITEMS]:
                # Extract title from card
                raw_title = (
                    card_tag.get("aria-label", "").replace("Lihat detail kompetisi ", "").strip()
                    or (
                        (h := card_tag.find(["h1", "h2", "h3", "h4"])) and h.get_text(" ").strip()
                    )
                    or "Tanpa Judul"
                )
                title = clean_title(raw_title)
                uid = make_id(title, "silomba.id")
                if uid in seen_ids:
                    continue

                # Extract poster from card
                card_poster = ""
                card_img = card_tag.find("img")
                if card_img:
                    src = card_img.get("src") or card_img.get("data-src") or ""
                    card_poster = clean_url(src, base_url)

                poster = card_poster
                caption = ""
                links = []

                try:
                    dp = await browser.new_page(user_agent=HEADERS["User-Agent"])
                    await dp.goto(link_url, wait_until="domcontentloaded", timeout=45000)
                    await dp.wait_for_timeout(3000)  # Wait for content to render
                    dsoup = BeautifulSoup(await dp.content(), "html.parser")
                    await dp.close()

                    full_text = dsoup.get_text("\n")
                    if not is_mahasiswa(full_text):
                        continue

                    # Get poster from detail page if not from card
                    if not poster or not _is_poster_url(poster):
                        poster = best_poster_from_soup(dsoup, base_url, source="silomba.id")

                    # Extract caption
                    if "Deskripsi Lomba" in full_text:
                        caption = full_text.split("Deskripsi Lomba")[-1].strip()
                    else:
                        caption = "\n".join(
                            l.strip() for l in full_text.splitlines() if l.strip()
                        )[:2500]

                    links = extract_registration_links(full_text, anchor_rows(dsoup, base_url))

                    # Better title from detail page
                    detail_title_el = dsoup.find(["h1", "h2"])
                    if detail_title_el:
                        detail_title = clean_title(detail_title_el.get_text(" "))
                        if detail_title != "Tanpa Judul" and len(detail_title) > len(title):
                            title = detail_title

                except Exception as exc:
                    print(f"[silomba] Detail failed {link_url}: {exc}")

                item = _build_item(uid, "silomba.id", title, poster, caption, links, link_url)
                results.append(item)
                seen_ids.add(uid)
                print(f"[silomba] ✓ {title}")

        except Exception as exc:
            print(f"[silomba] Error: {exc}")
        finally:
            await browser.close()

    print(f"[silomba] Done: {len(results)} items")
    return results


# ---------------------------------------------------------------------------
# Scraper: Instagram
# ---------------------------------------------------------------------------

def _normalize_ig_caption(raw: str) -> str:
    caption = re.sub(r"^\s*[^:\n]{1,80}\s+on Instagram:\s*", "", (raw or "").strip(), flags=re.I)
    return re.sub(r'^\s*"|\"\\s*$', "", caption).strip()


def _ig_shortcode(url: str) -> str:
    match = INSTAGRAM_SHORTCODE_RE.search(url or "")
    return match.group(1) if match else url


def _build_chrome_driver() -> webdriver.Chrome:
    opts = Options()
    for arg in ("--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
                 f"user-agent={HEADERS['User-Agent']}"):
        opts.add_argument(arg)
    if os.path.exists("/opt/chrome/chrome"):
        opts.binary_location = "/opt/chrome/chrome"
    service = Service("/usr/bin/chromedriver") if os.path.exists("/usr/bin/chromedriver") else Service()
    return webdriver.Chrome(service=service, options=opts)


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


def _collect_post_urls(driver, account: str) -> list[str]:
    driver.get(f"https://www.instagram.com/{account}/")
    time.sleep(random.randint(4, 6))
    if "page not found" in driver.title.lower():
        return []

    seen, urls = set(), []
    last_height = driver.execute_script("return document.body.scrollHeight")
    for _ in range(3):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.randint(2, 4))
        for el in driver.find_elements(By.XPATH, '//a[contains(@href,"/p/") or contains(@href,"/reel/")]'):
            href = el.get_attribute("href")
            if href and href not in seen:
                seen.add(href)
                urls.append(href)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height
    return urls


def _scrape_ig_post(driver, url: str, account: str, seen_ids: set) -> dict | None:
    driver.get(url)
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "article")))
    except Exception:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "img")))
    time.sleep(random.randint(3, 5))

    caption = ""
    if h1s := driver.find_elements(By.XPATH, "//article//h1"):
        caption = h1s[0].text
    if not caption:
        if metas := driver.find_elements(By.XPATH, '//meta[@property="og:description"]'):
            raw = metas[0].get_attribute("content") or ""
            caption = raw.split(": ", 1)[1] if ": " in raw else raw

    caption = _normalize_ig_caption(caption or driver.title)
    if not caption or not is_mahasiswa(caption):
        return None

    uid = make_id(_ig_shortcode(url), f"IG @{account}")
    if uid in seen_ids:
        return None

    poster = driver.execute_script(_IG_POSTER_JS)
    if not poster:
        if og := driver.find_elements(By.XPATH, '//meta[@property="og:image"]'):
            poster = og[0].get_attribute("content") or ""

    title = extract_title_from_caption(caption)
    return _build_item(
        uid, f"IG @{account}",
        title,
        poster, caption,
        extract_registration_links(caption),
        url,
    )


def scrape_instagram(seen_ids: set) -> list:
    if not IG_SESSION_ID:
        print("[IG] IG_SESSION_ID not set, skipping.")
        return []

    print("[IG] Starting...")
    results = []
    driver = _build_chrome_driver()

    try:
        driver.get("https://www.instagram.com/")
        time.sleep(3)
        driver.add_cookie({"name": "sessionid", "value": IG_SESSION_ID, "domain": ".instagram.com"})
        driver.refresh()
        time.sleep(5)
        if "login" in driver.current_url.lower():
            print("[IG] Invalid session.")
            return []

        for account in IG_ACCOUNTS:
            post_urls = _collect_post_urls(driver, account)
            for url in post_urls[:MAX_IG_POSTS_PER_ACCOUNT]:
                try:
                    item = _scrape_ig_post(driver, url, account, seen_ids)
                    if item:
                        results.append(item)
                        seen_ids.add(item["id"])
                except Exception as exc:
                    print(f"[IG] Skip post {url}: {exc}")

    except Exception as exc:
        print(f"[IG] Error: {exc}")
    finally:
        driver.quit()

    print(f"[IG] Done: {len(results)} items")
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
            print(f"[DEDUP] Skip duplicate URL: {link}")
            continue
        if any(_jaccard(token, db_tok) >= threshold for db_tok in db_tokens):
            print(f"[DEDUP] Skip title similar to DB: {item['judul']!r}")
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
                print(f"[DEDUP] Replace {existing['sumber']} -> {item['sumber']}: {item['judul']!r}")
            else:
                unique[dup_idx]["link_pendaftaran"] = merged
                print(f"[DEDUP] Merge links {item['sumber']} -> {existing['sumber']}: {item['judul']!r}")
        else:
            unique.append(item)

    print(f"[DEDUP] {len(new_items)} -> {len(unique)} unique items.")
    return unique


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not set.")

    print("[INFO] Connecting to MongoDB...")
    client = pymongo.MongoClient(MONGO_URI)
    collection = client[DB_NAME][COLLECTION]

    db_data = list(collection.find({}, {"id": 1, "link_direct": 1, "judul": 1, "_id": 0}))
    seen_ids = {d["id"] for d in db_data if "id" in d}
    print(f"[INFO] {len(seen_ids)} existing records in DB.")

    batches = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba, seen_ids),
        scrape_silomba(seen_ids),
        asyncio.to_thread(scrape_instagram, seen_ids),
    )
    raw = [item for batch in batches if isinstance(batch, list) for item in batch]
    print(f"[INFO] {len(raw)} new raw items found.")

    if not raw:
        print("[INFO] No new data.")
        client.close()
        return

    processed = []
    for i in range(0, len(raw), 15):
        batch = raw[i: i + 15]
        print(f"[LLM] Batch {i // 15 + 1} ({len(batch)} items)...")
        processed.extend(process_batch_with_openrouter(batch))

    final = dedup_results(processed, db_data)
    if not final:
        print("[INFO] All items are duplicates.")
        client.close()
        return

    result = collection.bulk_write(
        [UpdateOne({"id": item["id"]}, {"$set": item}, upsert=True) for item in final]
    )
    print(f"[INFO] Saved: {result.upserted_count} new, {result.modified_count} updated.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
