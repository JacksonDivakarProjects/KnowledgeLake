"""Deterministic, regex-based intent rules.

Two jobs:
- `fast_path`: confidently classify unambiguous single-intent messages
  ("show my tags", "/help", "done with #12") without spending an LLM call.
- `fallback`: always produce *some* classification when every LLM provider
  is down or returns garbage, so the bot keeps working offline.
"""
import re

from app.intents.schema import Classification, Intent

FAST_CONFIDENCE = 0.95
FALLBACK_CONFIDENCE = 0.7

_ENERGY_WORDS = {
    "low": "low", "tired": "low", "exhausted": "low", "drained": "low", "sleepy": "low",
    "medium": "medium", "okay": "medium", "ok": "medium", "normal": "medium",
    "high": "high", "energetic": "high", "pumped": "high", "fresh": "high", "great": "high",
}

# Messages with these look multi-intent; leave them to the LLM.
_CLAUSE_WORDS = r"(?:show|list|add|done|find|search|what|whats|what's|how|which|mark|remind|save|set|tell)"
_MULTI_RE = re.compile(
    r"\b(and then|and also|also|then)\b|;"
    r"|\band\b.*\b" + _CLAUSE_WORDS + r"\b"
    r"|,\s*" + _CLAUSE_WORDS + r"\b"
)


def _norm(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"\s+", " ", t)
    # Menu buttons / casual messages start with emoji: "🎯 What should I focus on?"
    t = re.sub(r"^[^\w/#'\"]+", "", t)
    return t


def _strip_punct(text: str) -> str:
    return text.strip().strip(" .!?,:;\"'").strip()


def _intent(name: str, conf: float, text: str, **entities) -> Intent:
    clean = {k: v for k, v in entities.items() if v not in (None, "", [])}
    return Intent(name=name, confidence=conf, entities=clean, raw_span=text)


# ── individual matchers: return Intent | None ───────────────────────────────

_HELP_RE = re.compile(
    r"^(/?help|/start|commands|menu|"
    r"what (can|do) you do|how (do|does) (i|this|you) (use|work)( this| you)?|"
    r"what are (your|the) commands|how to use( this| you)?)$"
)


def _help(t: str, raw: str) -> Intent | None:
    if _HELP_RE.match(_strip_punct(t)):
        return _intent("help", FAST_CONFIDENCE, raw)
    return None


_TAGS_RE = re.compile(
    r"^(/tags|(show|list|view|display|give)( me)?( all)?( my| the)? tags|"
    r"(what|which) tags (do i have|are there|exist)|my tags|all tags|tags)$"
)


def _list_tags(t: str, raw: str) -> Intent | None:
    if _TAGS_RE.match(_strip_punct(t)):
        return _intent("list_tags", FAST_CONFIDENCE, raw)
    return None


_QUEUE_RE = re.compile(
    r"^(/queue|(show|list|view|display|give)( me)?( all)?( of)?( my| the)? "
    r"(queue|backlog|items|open items|list|everything|stuff|tasks|todos?|notes)( i have)?|"
    r"(what'?s|what is) in my (queue|backlog|list)|my (queue|backlog|items|list)|queue)$"
)


def _view_queue(t: str, raw: str) -> Intent | None:
    if _QUEUE_RE.match(_strip_punct(t)):
        return _intent("view_queue", FAST_CONFIDENCE, raw)
    return None


_RETRIEVE_RE = re.compile(
    r"^(/focus|/next|what should i (focus on|work on|do|study|learn|pick up|tackle)( next| now| today| first)?"
    r"|what('?s| is) next|what to (focus on|work on|do next)|"
    r"(help me )?(prioriti[sz]e|plan my (day|week))|"
    r"what('?s| is) (most )?(important|urgent)( now| today)?|suggest (something|what to do)|"
    r"where should i start|what next)"
    r"(\s+(for|on|about|in|regarding)\s+(?P<query>.+))?$"
)


def _retrieve(t: str, raw: str) -> Intent | None:
    m = _RETRIEVE_RE.match(_strip_punct(t))
    if m:
        return _intent("retrieve", FAST_CONFIDENCE, raw, query=(m.group("query") or "").strip() or None)
    return None


_GET_CTX_RE = re.compile(
    r"^(/context|((what|which) (are|is)|what'?s|what're) my (goals?|focus|current focus|context|energy( level)?|priorities)|"
    r"(show|tell)( me)? my (goals?|focus|current focus|context|energy)|"
    r"my (goals|context)|what am i (focusing|working) on( right now| currently| now)?)$"
)


def _get_context(t: str, raw: str) -> Intent | None:
    if _GET_CTX_RE.match(_strip_punct(t)):
        return _intent("get_context", FAST_CONFIDENCE, raw)
    return None


_FOCUS_SET_RE = re.compile(
    r"^(i'?m|i am|im) (now )?(focusing|focussing|concentrating|working) on (?P<v>.+)$|"
    r"^(my )?(current )?focus (is|=|:) ?(?P<v2>.+)$|"
    r"^(set|change|update) (my )?(current )?focus (to|as|=|:) ?(?P<v3>.+)$|"
    r"^focus on (?P<v4>.+?) (this|for the) (week|month|quarter|year)$"
)
_FOCUS_TRAILER_RE = re.compile(
    r"\s+((for )?(this|the|the next|next) (week|month|quarter|year|few weeks|sprint)|today|now|right now|for now|these days)$"
)
_GOAL_SET_RE = re.compile(
    r"^(my )?(new )?goal (is|=|:) ?(?P<v>.+)$|"
    r"^(add|set) (a )?(new )?goal:? (?P<v2>.+)$"
)
_ENERGY_SET_RE = re.compile(
    r"^(my )?energy (is|level is|=|:)? ?(?P<v>\w+)( today| now| right now)?$|"
    r"^(i'?m|i am|im|feeling|i feel) (feeling )?(very |really |pretty |a bit |kind of )?(?P<v2>\w+)( today| now| right now)?$|"
    r"^(set|change) (my )?energy (to|as) (?P<v3>\w+)$"
)
_TZ_SET_RE = re.compile(r"^(set|change|update) (my )?time ?zone (to|as|=|:) ?(?P<v>[\w/+\-]+)$")


def _update_context(t: str, raw: str) -> Intent | None:
    s = _strip_punct(t)
    raw_s = _strip_punct(raw)

    def orig(value: str) -> str:
        # recover original casing for the value from the raw message
        idx = raw_s.lower().rfind(value)
        return raw_s[idx : idx + len(value)] if idx >= 0 else value

    m = _FOCUS_SET_RE.match(s)
    if m:
        v = next(g for g in (m.group("v"), m.group("v2"), m.group("v3"), m.group("v4")) if g)
        v = _FOCUS_TRAILER_RE.sub("", v.strip()).strip() or v.strip()
        return _intent("update_context", FAST_CONFIDENCE, raw, field="current_focus", value=orig(v))
    m = _GOAL_SET_RE.match(s)
    if m:
        v = m.group("v") or m.group("v2")
        return _intent("update_context", FAST_CONFIDENCE, raw, field="goals", value=orig(v.strip()))
    m = _ENERGY_SET_RE.match(s)
    if m:
        word = next(g for g in (m.group("v"), m.group("v2"), m.group("v3")) if g)
        level = _ENERGY_WORDS.get(word)
        if level:
            return _intent("update_context", FAST_CONFIDENCE, raw, field="energy_pref", value=level)
    m = _TZ_SET_RE.match(s)
    if m:
        return _intent("update_context", FAST_CONFIDENCE, raw, field="timezone", value=orig(m.group("v")))
    return None


_VAGUE_TARGETS = {"it", "that", "this", "i", "i am", "i'm", "im", "we", "we are", "all", "everything", "today"}

_DONE_RE = re.compile(
    r"^(/done\s+(?P<a>.+)|"
    r"(i'?m |i am |im )?(done|finished|through) (with )?(?P<b>.+)|"
    r"(i )?(finished|completed|did|wrapped up|closed) (?P<c>.+)|"
    r"(mark|set) (?P<d>.+?) (as )?(done|complete|completed|finished)|"
    r"(?P<e>#\d+|.+?) (is )?(done|completed|finished)|"
    r"(done|complete|close) (?P<f>#\d+))$"
)


def _mark_done(t: str, raw: str) -> Intent | None:
    m = _DONE_RE.match(_strip_punct(t))
    if not m:
        return None
    target = next((g for g in (m.group(k) for k in "abcdef") if g), "").strip()
    target = re.sub(r"^(the|my|that|this) ", "", target)
    if not target or target in _VAGUE_TARGETS:
        return None
    return _intent("mark_done", FAST_CONFIDENCE, raw, target=target)


_SEARCH_RE = re.compile(
    r"^(/search\s+|(search|find|look up|lookup|look for|search for|grep)( for)?( me)?( anything| something| everything| stuff| items| notes)?"
    r"( about| on| related to| regarding| with| tagged)?\s+|"
    r"(what|anything) (do i have|did i save|have i saved|have i stored|is there) (about|on|regarding|related to|for)\s+|"
    r"(do i have|have i saved) (anything|something|notes|items) (about|on|regarding|related to)\s+)"
    r"(?P<q>.+)$"
)


def _search(t: str, raw: str) -> Intent | None:
    m = _SEARCH_RE.match(_strip_punct(t))
    if m:
        q = m.group("q").strip()
        if q:
            return _intent("search", FAST_CONFIDENCE, raw, query=q)
    return None


_ADD_RE = re.compile(
    r"^(/add\s+|add\s+|remember( that| to)?\s+|save\s+|store\s+|note( that)?:?\s+|todo:?\s+|"
    r"to-?do:?\s+|remind me to\s+|idea:?\s+|bookmark:?\s+|capture:?\s+)(?P<v>.+)$"
)


def _add_item(t: str, raw: str) -> Intent | None:
    m = _ADD_RE.match(t)
    if m and m.group("v").strip():
        if re.match(r"^(a |new )?goal\b", m.group("v")):
            return None
        return _intent("add_item", FAST_CONFIDENCE, raw)
    return None


_CHITCHAT = (
    ("greeting", re.compile(
        r"^(h+i+|hey+|hello+|heya|hiya|yo|sup|hola|namaste|namaskar|vanakkam|howdy|greetings|"
        r"good (morning|afternoon|evening|day)|morning|gm)"
        r"( there| bot| buddy| friend| knowledgelake| again)?$")),
    ("smalltalk", re.compile(
        r"^(how are (you|u|things)|how'?s it going|how r u|how do you do|what'?s up|wh?a?ssup|"
        r"who are you|what are you|are you (a bot|real|there|alive))( today| doing)?$")),
    ("thanks", re.compile(
        r"^(thanks?( you)?|thank u|thx|ty|tysm|cheers|much appreciated|appreciate it|great,? thanks)"
        r"( so much| a lot| very much| bot| buddy)?$")),
    ("bye", re.compile(r"^(bye+|bye bye|goodbye|good night|gn|see (you|ya)( later| soon)?|cya|later|ttyl|take care)$")),
    ("ack", re.compile(
        r"^(ok(ay)?|k+|cool|nice|great|awesome|got it|alright|sure|fine|perfect|sounds good|noted|"
        r"lol|haha+|hmm+|yes|yeah|yep|no|nope|nah|wow|done deal|good)$")),
)


def _chitchat(t: str, raw: str) -> Intent | None:
    s = _strip_punct(t)
    for kind, rx in _CHITCHAT:
        if rx.match(s):
            return _intent("chitchat", FAST_CONFIDENCE, raw, kind=kind)
    return None


_OFF_TOPIC_RE = re.compile(
    r"^(tell me (a |an |some )?(joke|story|fact|riddle|fun fact)s?"
    r"|(what'?s|what is|how'?s|how is) the (weather|temperature|time|date|news|score)"
    r"|(weather|temperature|news|score|stock price) (in|for|of|today|now)"
    r"|who (won|is winning|is the (president|prime minister|ceo|pm))"
    r"|what (time|day|date) is it"
    r"|(sing|write|compose) (me )?(a |an )?(song|poem|story|essay|joke|limerick|rap)"
    r"|(translate|calculate|solve) "
    r"|play (a |some )?(game|music|song)"
    r"|recommend (me )?(a |some )?(movie|song|restaurant|show)s?"
    r"|are you (chatgpt|gpt|an ai|human)"
    r")\b"
)


def _off_topic(t: str, raw: str) -> Intent | None:
    if _OFF_TOPIC_RE.match(_strip_punct(t)):
        return _intent("chitchat", FAST_CONFIDENCE, raw, kind="off_topic")
    return None


_GREETING_PREFIX_RE = re.compile(
    r"^\W*(h+i+|hey+|hello+|heya|hiya|yo|hola|namaste|good (morning|afternoon|evening))"
    r"( there| bot| buddy| friend)?[\s,!.:;\-]+(?=\w)",
    re.IGNORECASE,
)


def strip_greeting(message: str) -> str:
    """'hi! add learn rust' -> 'add learn rust'; a bare greeting is left alone."""
    if _chitchat(_norm(message or ""), message or ""):
        return message or ""  # "hello there", "hey bot" - the whole thing is the greeting
    rest = _GREETING_PREFIX_RE.sub("", message or "", count=1).strip()
    return rest or (message or "")


def chitchat_kind(text: str) -> str | None:
    hit = _chitchat(_norm(text), text)
    return hit.entities.get("kind") if hit else None


# Order matters: more specific matchers first.
_MATCHERS = (
    _help,
    _chitchat,
    _off_topic,
    _list_tags,
    _view_queue,
    _get_context,
    _retrieve,
    _update_context,
    _search,
    _add_item,
    _mark_done,
)


def _looks_multi(t: str) -> bool:
    return bool(_MULTI_RE.search(t)) or t.count("?") > 1 or len(re.findall(r"[.!?]\s+\w", t)) >= 1


def _emoji_only(message: str | None) -> bool:
    """No letters/digits, but at least one non-ASCII symbol (emoji). '??' or '...' don't count."""
    m = (message or "").strip()
    return bool(m) and not any(ch.isalnum() for ch in m) and any(ord(ch) > 127 for ch in m)


def fast_path(message: str) -> Classification | None:
    """High-confidence single-intent classification, or None to defer to the LLM."""
    message = strip_greeting(message)
    t = _norm(message)
    if not t and _emoji_only(message):
        # Only emoji ("👍", "🙂") - treat as a friendly acknowledgement.
        return Classification(intents=[_intent("chitchat", FAST_CONFIDENCE, message.strip(), kind="ack")], source="rules")
    if not t or _looks_multi(t):
        return None
    for matcher in _MATCHERS:
        intent = matcher(t, message.strip())
        if intent:
            return Classification(intents=[intent], source="rules")
    return None


_QUESTION_START = re.compile(
    r"^(what|which|who|where|when|why|how|do|does|did|is|are|can|could|should|any|have)\b"
)


def _is_gibberish(t: str) -> bool:
    letters = re.sub(r"[^a-z]", "", t)
    if len(letters) < 2:
        return True
    words = re.findall(r"[a-z]+", t)
    if len(words) == 1 and len(letters) > 5:
        vowels = len(re.findall(r"[aeiouy]", letters))
        if vowels / len(letters) < 0.2:
            return True
    return False


_CLAUSE_SPLIT_RE = re.compile(
    r"\s*(?:;|,\s*(?:and\s+|then\s+)?(?=" + _CLAUSE_WORDS + r"\b)|\s+(?:and then|then|and also|and)\s+(?=" + _CLAUSE_WORDS + r"\b))\s*",
    re.IGNORECASE,
)


def fallback(message: str) -> Classification:
    """Best-effort classification used when no LLM is reachable. Never raises."""
    message = strip_greeting(message)
    t = _norm(message)
    if not t and _emoji_only(message):
        return Classification(
            intents=[_intent("chitchat", FALLBACK_CONFIDENCE + 0.1, message.strip(), kind="ack")], source="fallback"
        )
    if not t and (message or "").strip():  # punctuation only: "??", "..."
        return Classification(intents=[_intent("unknown", 0.9, message)], source="fallback")
    if not t:
        return Classification(
            intents=[],
            needs_clarification=True,
            clarification_question="Send me something to store, or ask what to focus on.",
            source="fallback",
        )

    # Multi-intent: every clause must match a specific rule, else treat as one message.
    clauses = [c for c in _CLAUSE_SPLIT_RE.split(message.strip()) if c and c.strip(" ,;")]
    if len(clauses) > 1:
        found: list[Intent] = []
        for clause in clauses:
            clause = clause.strip(" ,;")
            hit = next((m(_norm(clause), clause) for m in _MATCHERS if m(_norm(clause), clause)), None)
            if hit is None:
                found = []
                break
            hit.confidence = FALLBACK_CONFIDENCE + 0.1
            found.append(hit)
        if found:
            return Classification(intents=found, source="fallback")

    # Try every specific matcher, ignoring the multi-intent guard.
    for matcher in _MATCHERS:
        intent = matcher(t, message.strip())
        if intent:
            intent.confidence = FALLBACK_CONFIDENCE + 0.1
            return Classification(intents=[intent], source="fallback")

    if _is_gibberish(t):
        return Classification(intents=[_intent("unknown", 0.9, message)], source="fallback")

    if t.endswith("?") or _QUESTION_START.match(t):
        query = _strip_punct(message)
        return Classification(
            intents=[_intent("search", FALLBACK_CONFIDENCE, message, query=query)],
            source="fallback",
        )

    return Classification(intents=[_intent("add_item", FALLBACK_CONFIDENCE, message)], source="fallback")
