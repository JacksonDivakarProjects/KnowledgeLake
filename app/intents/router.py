"""Message → intents → handlers → one consolidated reply.

`handle_message` is Telegram-agnostic so it can be exercised directly in tests
and from the /debug endpoints. It never raises: every failure path ends in a
friendly reply, and each intent runs inside its own SAVEPOINT so one failing
handler can't poison the others.
"""
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models import IntentLog, User
from app.intents import rules
from app.intents.classifier import actionable, classify
from app.intents.extractor import extract
from app.intents.schema import Classification, Intent
from app.services import context as ctx_svc
from app.services import items as item_svc
from app.services import retrieve as retrieve_svc
from app.services import users as user_svc
from app.services.tags import list_tags_with_counts
from app.services.text import clean_input

log = logging.getLogger(__name__)

QUEUE_LIMIT = 20

HELP_TEXT = (
    "I'm KnowledgeLake - your personal context hub.\n\n"
    "Just type naturally:\n"
    "• \"learn kafka consumer groups this weekend\" → saved as an item\n"
    "• \"what should I focus on?\" → ranked picks from your queue\n"
    "• \"done with kafka\" / \"done #12\" → mark complete\n"
    "• \"find anything about dbt\" → search\n"
    "• \"I'm focusing on GCP this month\" / \"my goal is AWS cert\" → set context\n"
    "• \"show my queue\" / \"show my tags\" / \"what are my goals\"\n\n"
    "Commands:\n"
    "/add <text> - store something\n"
    "/queue - open items\n"
    "/focus [topic] - what to work on next\n"
    "/done <#id or title> - mark done\n"
    "/search <words> - find items\n"
    "/tags - tags in use\n"
    "/context - your focus & goals\n"
    "/setfocus <topic> · /goal <goal> · /energy <low|medium|high>\n"
    "/help - this message"
)

Handler = Callable[[AsyncSession, User, Intent, str], Awaitable[str]]


def _span(intent: Intent, text: str, multi: bool) -> str:
    """The part of the message this intent is about."""
    span = (intent.raw_span or "").strip()
    if multi and span and len(span) >= 3:
        return span
    return text


# ── handlers ────────────────────────────────────────────────────────────────


async def _add_item(session: AsyncSession, user: User, intent: Intent, text: str) -> str:
    extracted, meta = await extract(text, hint=intent.entities)
    dup = await item_svc.find_duplicate(session, user.id, extracted.title)
    if dup is not None:
        changes = await item_svc.merge_into(session, dup, extracted)
        note = f"\n   updated: {'; '.join(changes)}" if changes else ""
        if dup.status == "open":
            return f"Already in your queue: #{dup.id} {dup.title}{note}"
        item_svc.reopen(dup)
        await session.flush()
        return f"You finished #{dup.id} {dup.title} before - reopened it.{note}"

    item = await item_svc.create_item(session, user.id, extracted, raw_message_id=intent.entities.get("_raw_id"))
    log.info(
        "add_item -> item=%s title=%r tags=%s via %s/%s %dms",
        item.id, item.title, [t.name for t in item.tags], meta["provider"], meta["model"], meta["latency_ms"],
    )
    details = [item.type]
    if item.time_hint:
        details.append(item.time_hint)
    if item.energy:
        details.append(f"{item.energy} energy")
    tag_str = f"\n   tags: {', '.join('#' + t.name for t in item.tags)}" if item.tags else ""
    return f"Saved #{item.id}: {item.title} ({' · '.join(details)}){tag_str}"


async def _view_queue(session: AsyncSession, user: User, intent: Intent, text: str) -> str:
    rows = await item_svc.list_items(session, user.id, status="open", limit=QUEUE_LIMIT)
    if not rows:
        return "Your queue is empty. Send me a topic, task or note to get started."
    total = await item_svc.count_items(session, user.id, "open")
    lines = [f"Open items ({total}):"]
    lines += [item_svc.format_item_line(i) for i in rows]
    if total > len(rows):
        lines.append(f"…and {total - len(rows)} more. Try /focus for the top picks.")
    return "\n".join(lines)


async def _list_tags(session: AsyncSession, user: User, intent: Intent, text: str) -> str:
    rows = await list_tags_with_counts(session, user.id)
    if not rows:
        return "No tags yet - they're added automatically when you save items."
    return "Tags (open items):\n" + ", ".join(f"#{name} ({count})" for name, count in rows)


def _target_from(intent: Intent, text: str) -> str:
    target = str(intent.entities.get("target") or "").strip()
    if target:
        return target
    parsed = rules._mark_done(rules._norm(text), text)
    if parsed:
        return parsed.entities.get("target", "")
    return ""


async def _mark_done(session: AsyncSession, user: User, intent: Intent, text: str) -> str:
    target = _target_from(intent, text)
    if not target:
        return "Which item is done? Say \"done #<id>\" or \"done with <title>\"."

    matches = await item_svc.find_matches(session, user.id, target, status="open")
    if not matches:
        done = await item_svc.find_matches(session, user.id, target, status="done")
        if done and done[0].score >= 0.9:
            return f"#{done[0].item.id} {done[0].item.title} is already done."
        return f"Couldn't find an open item matching \"{target}\". Send /queue to see ids."

    top = matches[0]
    clear_winner = (
        len(matches) == 1
        or top.score >= 0.99
        or (top.score - matches[1].score) >= 0.15
    )
    if not clear_winner:
        options = "\n".join(f"• #{m.item.id} {m.item.title}" for m in matches[:5])
        return f"A few items match \"{target}\" - which one?\n{options}\nReply \"done #<id>\"."

    item_svc.mark_done(top.item)
    await session.flush()
    remaining = await item_svc.count_items(session, user.id, "open")
    return f"Marked done: #{top.item.id} {top.item.title} ✓  ({remaining} open)"


async def _update_context(session: AsyncSession, user: User, intent: Intent, text: str) -> str:
    field = intent.entities.get("field")
    value = intent.entities.get("value")
    if not field or value in (None, ""):
        parsed = rules._update_context(rules._norm(text), text)
        if parsed:
            field = field or parsed.entities.get("field")
            value = value if value not in (None, "") else parsed.entities.get("value")
    if not field:
        field = "current_focus"
        value = value or text
    try:
        return await ctx_svc.update_context(session, user.id, field, value)
    except ctx_svc.ContextError as e:
        return str(e)


async def _get_context(session: AsyncSession, user: User, intent: Intent, text: str) -> str:
    ctx = await ctx_svc.get_context(session, user.id)
    return ctx_svc.format_context(ctx_svc.view(ctx))


def _query_from(intent: Intent, text: str, matcher) -> str:
    q = intent.entities.get("query")
    if isinstance(q, str) and q.strip():
        return q.strip()
    parsed = matcher(rules._norm(text), text)
    if parsed and parsed.entities.get("query"):
        return parsed.entities["query"]
    return ""


async def _search(session: AsyncSession, user: User, intent: Intent, text: str) -> str:
    query = _query_from(intent, text, rules._search) or rules._strip_punct(text)
    if not query:
        return "What should I search for?"
    results = await item_svc.search(session, user.id, query)
    if not results:
        return f"Nothing found for \"{query}\"."
    lines = [f"Found {len(results)} for \"{query}\":"]
    lines += [item_svc.format_item_line(m.item, show_status=True) for m in results]
    return "\n".join(lines)


async def _retrieve(session: AsyncSession, user: User, intent: Intent, text: str) -> str:
    query = _query_from(intent, text, rules._retrieve) or None
    items = await item_svc.list_items(session, user.id, status="open")
    ctx = ctx_svc.view(await ctx_svc.get_context(session, user.id))
    ranked, matched = retrieve_svc.rank_items(items, ctx, query=query, limit=settings.kl_retrieve_limit)
    return retrieve_svc.format_ranked(ranked if items else [], query=query, query_matched=matched, ctx=ctx)


async def _help(session: AsyncSession, user: User, intent: Intent, text: str) -> str:
    return HELP_TEXT


HANDLERS: dict[str, Handler] = {
    "add_item": _add_item,
    "view_queue": _view_queue,
    "list_tags": _list_tags,
    "mark_done": _mark_done,
    "update_context": _update_context,
    "get_context": _get_context,
    "search": _search,
    "retrieve": _retrieve,
    "help": _help,
}

FAILURE_REPLY = "Sorry - something went wrong on my side. Please try again in a moment."


def _session_factory() -> async_sessionmaker:
    from app.db.engine import SessionLocal

    return SessionLocal


async def _log_intent(session: AsyncSession, user: User, raw_id: int, cls: Classification, meta: dict) -> None:
    try:
        async with session.begin_nested():
            top = cls.top
            session.add(IntentLog(
                user_id=user.id,
                raw_message_id=raw_id,
                model=str(meta.get("model", ""))[:64],
                provider=str(meta.get("provider", ""))[:16],
                prompt_version=str(meta.get("prompt_version", "v1"))[:16],
                predicted=cls.model_dump(),
                confidence=top.confidence if top else None,
                latency_ms=meta.get("latency_ms"),
            ))
    except Exception as e:  # logging must never break the reply
        log.warning("intent log write failed: %s", e)


async def handle_message(
    telegram_id: int,
    text: str,
    *,
    username: str | None = None,
    update_id: int | None = None,
    forced: Intent | None = None,
    session_factory: async_sessionmaker | None = None,
) -> str | None:
    """Process one user message. Returns the reply, or None for duplicate updates."""
    text = clean_input(text)
    if not text and forced is None:
        return "Send me a topic, task or question - or /help."

    if forced is not None:
        cls = Classification(intents=[forced], source="command")
        meta = {"provider": "command", "model": "none", "latency_ms": 0, "prompt_version": "-"}
    else:
        cls, meta = await classify(text)
    log.info(
        "classified user=%s -> %s (clarify=%s, source=%s) via %s/%s %dms",
        telegram_id, [(i.name, round(i.confidence, 2)) for i in cls.intents],
        cls.needs_clarification, cls.source, meta["provider"], meta["model"], meta["latency_ms"],
    )

    factory = session_factory or _session_factory()
    try:
        async with factory() as session:
            if await user_svc.already_processed(session, update_id):
                log.info("skipping duplicate update_id=%s", update_id)
                return None

            user = await user_svc.get_or_create_user(session, telegram_id, username)
            raw = await user_svc.store_raw(session, user, text, update_id)
            await _log_intent(session, user, raw.id, cls, meta)

            to_run = [forced] if forced is not None else actionable(cls)
            if not to_run:
                reply = cls.clarification_question or "Not sure what you meant - can you rephrase? (/help)"
            else:
                replies = await _run_intents(session, user, to_run, text, raw.id)
                reply = "\n\n".join(r for r in replies if r) or "Done."

            await user_svc.mark_processed(session, update_id)
            await session.commit()
            return reply
    except Exception as e:
        log.exception("handle_message failed for user=%s: %s", telegram_id, e)
        return FAILURE_REPLY


async def _run_intents(session: AsyncSession, user: User, intents: list[Intent], text: str, raw_id: int) -> list[str]:
    multi = len(intents) > 1
    replies: list[str] = []
    # mark_done before retrieve so "done with X, what's next?" ranks without X.
    order = {"update_context": 0, "add_item": 1, "mark_done": 2}
    for intent in sorted(intents, key=lambda i: order.get(i.name, 5)):
        handler = HANDLERS.get(intent.name)
        if handler is None:
            continue
        intent.entities.setdefault("_raw_id", raw_id)
        try:
            async with session.begin_nested():
                replies.append(await handler(session, user, intent, _span(intent, text, multi)))
        except Exception as e:
            log.exception("intent %s failed: %s", intent.name, e)
            replies.append(f"Couldn't complete \"{intent.name.replace('_', ' ')}\" - please try again.")
    return replies
