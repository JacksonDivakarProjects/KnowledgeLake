"""Small, dependency-free text helpers shared by extraction and retrieval."""
import re
from datetime import datetime

STOPWORDS = frozenset(
    """
    a an the and or but if then so to of in on at by for with about from into over under
    is are was were be been being am do does did doing have has had having i me my mine
    we our you your it its this that these those there here what which who whom when where
    why how all any some no not only just also very really more most less least can could
    should would will shall may might must need want wanna gonna get got go going thing things
    stuff something anything everything please pls thanks thank ok okay hey hi hello
    learn learning read reading do doing check look into try figure out start finish
    today tonight tomorrow now next first focus focusing work working week month
    """.split()
)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]*[a-z0-9+#]|[a-z0-9]")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def tokens(text: str | None) -> list[str]:
    """Lowercased content words (stopwords removed, light plural stripping)."""
    if not text:
        return []
    out = []
    for w in _WORD_RE.findall(_URL_RE.sub(" ", text.lower())):
        if w in STOPWORDS or len(w) < 2:
            continue
        if len(w) > 4 and w.isalpha() and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def token_set(*texts: str | None) -> set[str]:
    s: set[str] = set()
    for t in texts:
        s.update(tokens(t))
    return s


def overlap_score(query: set[str], doc: set[str]) -> float:
    """Fraction of query tokens present in doc (prefix match allowed for ≥4 chars)."""
    if not query or not doc:
        return 0.0
    hits = 0
    for q in query:
        if q in doc or (len(q) >= 4 and any(d.startswith(q) or q.startswith(d) for d in doc if len(d) >= 4)):
            hits += 1
    return hits / len(query)


# ── time hints ──────────────────────────────────────────────────────────────

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TIME_HINT_RE = re.compile(
    r"\b("
    r"(by|before|until|till|due|on|this|next|coming)\s+(" + "|".join(_WEEKDAYS) + r"|weekend|week|month|quarter|year|eod|eow|morning|evening|tonight)"
    r"|today|tonight|tomorrow|asap|right now|urgent(ly)?|this (morning|afternoon|evening)"
    r"|end of (the )?(day|week|month|quarter|year)|eod|eow"
    r"|in \d+ (hours?|days?|weeks?|months?)"
    r"|(by|before|on|due) \d{1,2}(st|nd|rd|th)?( of)? ?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    r"|(by|before|on|due) (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}"
    r"|someday|eventually|later|sometime"
    r")\b",
    re.IGNORECASE,
)


def detect_time_hint(text: str) -> str | None:
    m = _TIME_HINT_RE.search(text or "")
    return m.group(0).strip() if m else None


def urgency(time_hint: str | None, now: datetime | None = None) -> float:
    """Map a free-text time hint to 0.0 (none/someday) … 1.0 (now)."""
    if not time_hint:
        return 0.0
    h = time_hint.lower()
    if re.search(r"\b(someday|eventually|later|sometime)\b", h):
        return 0.05
    if re.search(r"\b(asap|urgent|urgently|right now|now|today|tonight|eod|end of (the )?day|this (morning|afternoon|evening))\b", h):
        return 1.0
    if re.search(r"\btomorrow\b", h):
        return 0.85
    for idx, day in enumerate(_WEEKDAYS):
        if day in h:
            if "next" in h:
                return 0.4
            today = (now or datetime.now()).weekday()
            days_ahead = (idx - today) % 7
            return max(0.5, 0.95 - 0.08 * days_ahead)
    m = re.search(r"in (\d+) (hour|day|week|month)", h)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"hour": n / 24, "day": n, "week": 7 * n, "month": 30 * n}[unit]
        return max(0.1, min(1.0, 1.0 - days / 30))
    if re.search(r"\b(this week|eow|end of (the )?week|weekend)\b", h):
        return 0.6
    if re.search(r"\bnext week\b", h):
        return 0.4
    if re.search(r"\b(this month|end of (the )?month)\b", h):
        return 0.35
    if re.search(r"\b(next month|quarter)\b", h):
        return 0.2
    if re.search(r"\byear\b", h):
        return 0.1
    if re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", h):
        return 0.4
    return 0.3


# ── heuristic item extraction (used when the LLM is unavailable) ────────────

_ENERGY_HINTS = (
    (re.compile(r"\b(quick|small|tiny|5 ?min(ute)?s?|easy|minor|simple|short)\b", re.I), "low"),
    (re.compile(r"\b(deep ?dive|in[- ]depth|big|hard|complex|thorough|master|from scratch|whole)\b", re.I), "high"),
)

_ACTION_VERBS = re.compile(
    r"^(fix|email|call|send|buy|book|pay|write|update|deploy|review|refactor|implement|build|"
    r"create|setup|set up|schedule|cancel|renew|submit|finish|clean|migrate|ping|reply|"
    r"message|text|file|prepare|plan|move|install|upgrade|test|debug|ship|merge|order)\b",
    re.I,
)
_TOPIC_VERBS = re.compile(
    r"^(learn|study|understand|explore|research|read up on|look into|practice|master|deep ?dive|dig into|get better at)\b",
    re.I,
)
_EFFORT_PREFIX = re.compile(r"^(a |an )?(quick|small|tiny|easy|minor|simple|short|big|hard)\s+", re.I)
_RESOURCE_RE = re.compile(r"https?://|www\.|\b(book|article|video|podcast|course|paper|blog|tutorial|talk|repo)\b", re.I)

_LEADING_FILLER = re.compile(
    r"^(/add\s+|add\s+|remember( that| to)?\s+|save\s+|store\s+|note( that)?:?\s+|todo:?\s+|to-?do:?\s+|"
    r"remind me to\s+|idea:?\s+|bookmark:?\s+|capture:?\s+|i (need|want|have|should|must) to\s+|"
    r"i should\s+|need to\s+|want to\s+|gotta\s+|gonna\s+)+",
    re.I,
)


def strip_filler(text: str) -> str:
    return _LEADING_FILLER.sub("", (text or "").strip()).strip()


def guess_type(text: str) -> str:
    t = _EFFORT_PREFIX.sub("", strip_filler(text))
    if _RESOURCE_RE.search(t):
        return "resource"
    if _TOPIC_VERBS.match(t):
        return "topic"
    if _ACTION_VERBS.match(t):
        return "action"
    return "note"


def guess_energy(text: str) -> str | None:
    for rx, level in _ENERGY_HINTS:
        if rx.search(text or ""):
            return level
    return None


def make_title(text: str, max_words: int = 10, max_chars: int = 120) -> str:
    t = strip_filler(text)
    t = _URL_RE.sub("", t).strip() or t or (text or "").strip()
    hint = detect_time_hint(t)
    if hint and t.lower() != hint.lower():
        t = re.sub(re.escape(hint), "", t, flags=re.I).strip(" ,.-")
    t = re.sub(r"\s+", " ", t).strip(" .,;:-")
    words = t.split(" ")
    if len(words) > max_words:
        t = " ".join(words[:max_words])
    t = t[:max_chars].strip()
    if not t:
        url = _URL_RE.search(text or "")
        t = url.group(0)[:max_chars] if url else "Untitled"
    if _URL_RE.match(t):
        return t
    return t[0].upper() + t[1:]


_TAG_NOISE = frozenset(
    _WEEKDAYS
    + (
        "weekend", "quick", "small", "tiny", "easy", "minor", "simple", "short", "deep", "dive", "big", "hard",
        "fix", "email", "call", "send", "write", "update", "review", "build", "create", "set", "asap", "urgent",
        "later", "someday", "eod", "eow", "before", "until", "due", "end", "day", "days", "hours", "weeks",
        "months", "year", "quarter", "morning", "evening", "good", "great", "nice", "new",
    )
)


def guess_tags(text: str, limit: int = 3) -> list[str]:
    """Content words as tags, in message order (raw form, no plural stripping)."""
    seen: list[str] = []
    body = _URL_RE.sub(" ", strip_filler(text).lower())
    for w in _WORD_RE.findall(body):
        if w in STOPWORDS or w in _TAG_NOISE or w.isdigit() or len(w) < 3 or w in seen:
            continue
        seen.append(w)
        if len(seen) >= limit:
            break
    return seen


def normalize_tag(name: str) -> str:
    t = (name or "").strip().lower().lstrip("#")
    t = re.sub(r"\s+", "-", t)
    t = re.sub(r"[^a-z0-9+#.\-_]", "", t)
    return t[:64].strip("-.")


def chunk_message(text: str, limit: int = 4000) -> list[str]:
    """Split text for Telegram's 4096-char limit, preferring line breaks."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
