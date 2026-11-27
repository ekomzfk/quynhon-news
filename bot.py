#!/usr/bin/env python3
"""Новостной бот: локальные новости Куинёна + главное по Вьетнаму для экспатов.

Запускается каждые 10 минут (GitHub Actions):
  * каждый запуск — обрабатывает нажатые кнопки модерации и сразу публикует одобренное;
  * раз в час — собирает новости, Claude оценивает их (этап 1), для отобранных читает полный текст
    статьи и пишет подробный пересказ (этап 2), затем присылает черновики или публикует сам.
Состояние хранится в state.json.
"""
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from sources import SOURCES

log = logging.getLogger("newsbot")
ICT = timezone(timedelta(hours=7))  # время Вьетнама
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ----------------------------------------------------------------- настройки
def _env(name, default=""):
    v = os.environ.get(name)
    return default if v is None else v.strip()


def _int(name, default):
    v = _env(name)
    return int(v) if v else default


TG_TOKEN = _env("TELEGRAM_BOT_TOKEN")
RU_CHANNEL = _env("RU_CHANNEL")
EN_CHANNEL = _env("EN_CHANNEL")
ADMIN_CHAT_ID = _env("ADMIN_CHAT_ID")
MODERATION = _env("MODERATION", "1") == "1"
MIN_SCORE = _int("MIN_SCORE", 6)               # порог для общевьетнамских новостей 0–10
MIN_SCORE_LOCAL = _int("MIN_SCORE_LOCAL", 3)   # порог для новостей Куинёна (мягче)
MAX_POSTS_PER_RUN = _int("MAX_POSTS_PER_RUN", 3)
MAX_AGE_HOURS = _int("MAX_AGE_HOURS", 36)
MAX_ITEMS_PER_RUN = _int("MAX_ITEMS_PER_RUN", 40)
COLLECT_EVERY_MIN = _int("COLLECT_EVERY_MIN", 55)   # как часто собирать новости
FORCE_COLLECT = _env("FORCE_COLLECT").lower() == "true"  # ручной запуск — собирать сразу
QUIET_HOURS = _env("QUIET_HOURS", "23-7")
MODEL = _env("MODEL") or "claude-haiku-5-5"
STATE_FILE = _env("STATE_FILE", "state.json")
DRY_RUN = _env("DRY_RUN") == "1"

CHANNELS = [(lang, ch) for lang, ch in (("ru", RU_CHANNEL), ("en", EN_CHANNEL)) if ch]
LANGS = [lang for lang, _ in CHANNELS] or ["ru"]
LABELS = {
    "ru": {"source": "Источник", "local": "#Куинён", "national": "#Вьетнам"},
    "en": {"source": "Source", "local": "#QuyNhon", "national": "#Vietnam"},
}


# ----------------------------------------------------------------- утилиты
def now_utc():
    return datetime.now(timezone.utc)


def item_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def clean_text(s, limit=600):
    s = BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", s).strip()[:limit]


def esc(s):
    return html.escape(s or "", quote=False)


def parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=ICT)
    except ValueError:
        return None


def is_quiet(now=None):
    if not QUIET_HOURS or QUIET_HOURS.lower() == "off":
        return False
    start, end = (int(x) for x in QUIET_HOURS.split("-"))
    h = (now or now_utc()).astimezone(ICT).hour
    return (h >= start or h < end) if start > end else (start <= h < end)


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en,ru;q=0.9,vi;q=0.8"})


def http_get(url):
    r = SESSION.get(url, timeout=25)
    r.raise_for_status()
    # некоторые сайты (например, ru.laodong.vn) сначала ставят cookie скриптом и перезагружают страницу
    if len(r.text) < 5000 and "document.cookie" in r.text:
        m = re.search(r"document\.cookie\s*=\s*[\"']\s*([^=\"']+)=([^;\"']*)", r.text)
        if m:
            SESSION.cookies.set(m.group(1).strip(), m.group(2), domain=urlparse(r.url).hostname)
            r = SESSION.get(url, timeout=25)
            r.raise_for_status()
    return r


def page_meta(page, *names):
    for n in names:
        tag = page.find("meta", attrs={"property": n}) or page.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"]
    return ""


def extract_article(page, limit=6000):
    """Основной текст статьи: блок, в котором больше всего текста в абзацах <p>."""
    for t in page(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
        t.decompose()
    cands = []
    for el in page.find_all(["article", "div", "section", "main"]):
        n = sum(len(p.get_text(strip=True)) for p in el.find_all("p"))
        if n:
            cands.append((n, len(el.get_text(strip=True)), el))
    if not cands:
        return ""
    # из блоков, где почти весь текст абзацев, берём самый «узкий» — без меню и боковых колонок
    top = max(c[0] for c in cands)
    best = min((c for c in cands if c[0] >= 0.85 * top), key=lambda c: c[1])[2]
    paras = [re.sub(r"\s+", " ", p.get_text(" ", strip=True)) for p in best.find_all("p")]
    text = "\n".join(p for p in paras if len(p) > 40)
    return text[:limit]


# ----------------------------------------------------------------- состояние
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("seen", {})
    st.setdefault("queue", [])
    st.setdefault("pending", {})
    st.setdefault("recent", [])
    st.setdefault("tg_offset", 0)
    st.setdefault("last_collect", 0)
    return st


def save_state(st):
    t = time.time()
    st["seen"] = {k: v for k, v in st["seen"].items() if v > t - 14 * 86400}
    st["recent"] = st["recent"][-60:]
    st["queue"] = [i for i in st["queue"] if i.get("queued_at", 0) > t - 86400]
    st["pending"] = {k: v for k, v in st["pending"].items() if v.get("queued_at", 0) > t - 3 * 86400}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ----------------------------------------------------------------- сбор
def fetch_rss(src):
    feed = feedparser.parse(http_get(src["url"]).content)
    out = []
    for e in feed.entries[:40]:
        url = e.get("link")
        if not url:
            continue
        title = clean_text(e.get("title", ""), 300)
        source, snippet = src["name"], clean_text(e.get("summary", ""))
        if "news.google.com" in src["url"]:
            gsrc = (e.get("source") or {}).get("title")
            if gsrc:
                source = gsrc
                if title.endswith(" - " + gsrc):
                    title = title[: -len(gsrc) - 3]
            snippet = ""
        pub = None
        if e.get("published_parsed"):
            pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        out.append({"url": url, "title": title, "text": snippet, "source": source,
                    "published": pub.isoformat() if pub else None})
    return out


def fetch_html(src, seen):
    soup = BeautifulSoup(http_get(src["url"]).text, "html.parser")
    pat = re.compile(src["link_pattern"])
    links = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(src["url"], a["href"]).split("#")[0].split("?")[0]
        if pat.search(url):
            text = a.get_text(" ", strip=True)
            if len(text) > len(links.get(url, "")):
                links[url] = text
    if not links:
        log.warning("  %s: на странице не нашлось ссылок на статьи (проверьте link_pattern)", src["name"])
    out = []
    for url, text in links.items():
        if item_id(url) in seen:
            continue
        if len(out) >= 15:
            break
        try:
            page = BeautifulSoup(http_get(url).text, "html.parser")
        except Exception as ex:  # noqa: BLE001
            log.warning("  не открылась %s: %s", url, ex)
            continue
        pub = parse_dt(page_meta(page, "article:published_time", "pubdate", "date",
                                 "og:article:published_time"))
        out.append({
            "url": url,
            "title": clean_text(page_meta(page, "og:title") or text, 300),
            "text": clean_text(page_meta(page, "og:description", "description")),
            "source": src["name"],
            "published": pub.isoformat() if pub else None,
        })
        time.sleep(0.5)
    return out


def collect(state):
    seen = state["seen"]
    bootstrap = not seen
    max_age = timedelta(hours=12 if bootstrap else MAX_AGE_HOURS)
    fresh, by_id = [], set()
    for src in SOURCES:
        try:
            got = fetch_rss(src) if src["type"] == "rss" else fetch_html(src, seen)
        except Exception as ex:  # noqa: BLE001
            log.warning("Источник %s (%s) не сработал: %s", src["name"], src["url"], ex)
            continue
        log.info("%s: %d статей", src["name"], len(got))
        for it in got:
            iid = item_id(it["url"])
            if iid in seen or iid in by_id or not it["title"]:
                continue
            pub = parse_dt(it["published"])
            if (pub and now_utc() - pub > max_age) or (not pub and bootstrap):
                seen[iid] = time.time()
                continue
            it.update(id=iid, scope=src["scope"], publish_all=bool(src.get("publish_all")))
            by_id.add(iid)
            fresh.append(it)
    fresh.sort(key=lambda i: i["published"] or "9999", reverse=True)
    return fresh[:MAX_ITEMS_PER_RUN]


# ----------------------------------------------------------------- ИИ: этап 1 — отбор
GEO = """Geography: since July 2025 the former Binh Dinh province is part of Gia Lai province, and Quy Nhon is
the provincial capital (wards Quy Nhơn, Quy Nhơn Bắc/Nam/Đông/Tây, nearby Ghềnh Ráng, Nhơn Lý, Nhơn Hải,
Cù Lao Xanh, Eo Gió, Kỳ Co, Phù Cát airport, An Nhơn, Tuy Phước, Hoài Nhơn). Pleiku and the Central
Highlands are ~170 km inland."""

SCORE_PROMPT = """You are the editor of a Telegram news channel for foreigners (expats) living in Quy Nhon, Vietnam.
Today is {today}.
""" + GEO + """

Two kinds of items:
 A) LOCAL — news about Quy Nhon and the coastal part of Gia Lai (former Binh Dinh). Readers want almost all
    of it: incidents, accidents, crime, construction, urban planning, real estate, new shops and venues,
    events, weather, schools, prices, transport — score these 4–10 by importance. Only purely bureaucratic
    items (party/government meetings, rankings of wards, plans/reports, emulation movements, awards,
    officials' visits) score 0–2. News only about the inland highlands (Pleiku etc.) is NOT local:
    judge it like national news.
 B) NATIONAL — everything else, scored strictly by usefulness to an expat in Quy Nhon:
    9–10 directly affects daily life: storms/floods/heat warnings for the area, power or water outages,
         new rules on visas/residence/work permits/driving/taxes/property for foreigners, serious safety
         incidents, new flights to Quy Nhon (Phu Cat);
    7–8  important national news (prices, fuel, exchange rate, laws for everyone, big events, health alerts);
    4–6  generic but mildly interesting;
    0–3  propaganda, awards, routine reports, crime unrelated to foreigners, minor sports, celebrities,
         news from other countries unrelated to Vietnam.
An item with only a headline and no text gets at most 7, unless it is an urgent warning.
Set duplicate=true if an item tells the same story as one in "already_published" or as an earlier item in
this batch (keep only the most informative version; prefer items that have text).
scope: "quy_nhon" for LOCAL items, otherwise "vietnam" (highlands-only news is "vietnam").
Return a decision for EVERY input item via submit_scores."""

SCORE_TOOL = {
    "name": "submit_scores",
    "description": "Editorial decision for every input item.",
    "input_schema": {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "score": {"type": "integer"},
                "duplicate": {"type": "boolean"},
                "scope": {"type": "string", "enum": ["quy_nhon", "vietnam"]},
            },
            "required": ["id", "score", "duplicate", "scope"],
        }}},
        "required": ["items"],
    },
}

# ----------------------------------------------------------------- ИИ: этап 2 — текст поста
WRITE_PROMPT = """You write posts for a Telegram news channel for foreigners living in Quy Nhon, Vietnam.
""" + GEO + """

For every item you get the headline and the article text (it may be empty). Write a post in each language
from "languages" (fields <lang>_title and <lang>_text):
 - title: headline, max ~90 characters, no emoji;
 - text: a retelling in your own words, 2–3 short paragraphs separated by an empty line, 5–8 sentences in
   total (about 600–1000 characters). Cover what happened, where, when, key numbers and dates, and what it
   changes for people. Amounts in VND with approximate USD;
 - if the article text is empty or very short, write only 1–3 sentences based on the headline;
 - never copy sentences from the article; use ONLY facts from the given text, invent nothing;
 - never mention missing details or "see the source"; no opinions, no commentary on why it matters to
   readers, no calls to action;
 - Russian: natural Russian; place names exactly: Gia Lai = Зялай, Quy Nhon = Куинён, Pleiku = Плейку,
   Binh Dinh = Биньдинь, Phu Cat = Фукат, Ho Chi Minh City = Хошимин, Hanoi = Ханой, Da Nang = Дананг.
Also give one fitting emoji. Return every item via submit_posts."""

WRITE_TOOL = {
    "name": "submit_posts",
    "description": "Finished posts.",
    "input_schema": {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "emoji": {"type": "string"},
                "ru_title": {"type": "string"},
                "ru_text": {"type": "string"},
                "en_title": {"type": "string"},
                "en_text": {"type": "string"},
            },
            "required": ["id", "emoji"],
        }}},
        "required": ["items"],
    },
}


def ask_claude(system, tool, payload):
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return {str(d.get("id")): d for d in block.input.get("items", [])}
    return {}


def score(items, recent):
    system = SCORE_PROMPT.format(today=now_utc().astimezone(ICT).strftime("%Y-%m-%d"))
    decisions = {}
    for i in range(0, len(items), 15):
        batch = items[i:i + 15]
        decisions.update(ask_claude(system, SCORE_TOOL, {
            "already_published": recent[-50:],
            "items": [{k: it[k] for k in ("id", "source", "scope", "title", "text", "published")}
                      for it in batch],
        }))
    return decisions


def select(items, decisions):
    chosen = []
    for it in items:
        d = decisions.get(it["id"])
        if not d:
            continue
        threshold = 0 if it.get("publish_all") else (
            MIN_SCORE_LOCAL if d.get("scope") == "quy_nhon" else MIN_SCORE)
        log.info("  [%2s%s] %s", d.get("score"), " dup" if d.get("duplicate") else "", it["title"][:90])
        if d.get("duplicate") or int(d.get("score", 0)) < threshold:
            continue
        it.update(score=d.get("score"), scope=d.get("scope"))
        chosen.append(it)
    return chosen


def write_posts(items):
    for it in items:  # полный текст статьи
        try:
            r = http_get(it["url"])
            if "news.google.com" in urlparse(r.url).netloc:
                it["body"] = ""  # ссылка Google News без перехода на сайт — пишем коротко по заголовку
            else:
                it["url"] = r.url
                it["body"] = extract_article(BeautifulSoup(r.text, "html.parser"))
        except Exception as ex:  # noqa: BLE001
            log.warning("  не удалось прочитать статью %s: %s", it["url"], ex)
            it["body"] = ""
        if not it["body"]:
            it["body"] = it.get("text", "")
        time.sleep(0.5)
    ready = []
    for i in range(0, len(items), 4):
        batch = items[i:i + 4]
        posts = ask_claude(WRITE_PROMPT, WRITE_TOOL, {
            "languages": LANGS,
            "items": [{"id": it["id"], "source": it["source"], "title": it["title"],
                       "article_text": it["body"]} for it in batch],
        })
        for it in batch:
            p = posts.get(it["id"])
            if p and any(p.get(f"{lang}_title") and p.get(f"{lang}_text") for lang in LANGS):
                it.update({k: v for k, v in p.items() if k != "id"})
                it.pop("body", None)
                ready.append(it)
    return ready


# ----------------------------------------------------------------- Telegram
def tg(method, **params):
    if DRY_RUN:
        print(f"\n--- {method} ---\n{params.get('text', json.dumps(params, ensure_ascii=False)[:300])}")
        return [] if method == "getUpdates" else {"message_id": 0}
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params, timeout=30)
        data = r.json()
        if data.get("ok"):
            return data["result"]
        retry = (data.get("parameters") or {}).get("retry_after")
        if retry:
            time.sleep(retry + 1)
            continue
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    raise RuntimeError(f"Telegram {method}: too many retries")


def format_post(it, lang):
    title, text = it.get(f"{lang}_title"), it.get(f"{lang}_text")
    if not (title and text):
        return None
    lab = LABELS[lang]
    local = it.get("scope") == "quy_nhon"
    emoji = it.get("emoji") or ("🌊" if local else "🇻🇳")
    url = html.escape(it["url"], quote=True)
    return (f"{emoji} <b>{esc(title)}</b>\n\n{esc(text.strip())}\n\n"
            f'{lab["source"]}: <a href="{url}">{esc(it["source"])}</a>\n'
            f'{lab["local"] if local else lab["national"]}')


def publish(it):
    for lang, channel in CHANNELS:
        post = format_post(it, lang)
        if post:
            tg("sendMessage", chat_id=channel, text=post, parse_mode="HTML",
               link_preview_options={"url": it["url"], "prefer_large_media": True})
            time.sleep(2)


def send_draft(state, it):
    parts = [p for p in (format_post(it, lang) for lang in LANGS) if p]
    note = "публикуется всё с этого сайта" if it.get("publish_all") else f"оценка {it.get('score')}/10"
    text = ("\n\n───────\n\n".join(parts) +
            f"\n\n<i>{note} · после нажатия пост выйдет в течение ~10–20 минут</i>")
    kb = {"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"pub:{it['id']}"},
        {"text": "❌ Пропустить", "callback_data": f"skip:{it['id']}"},
    ]]}
    tg("sendMessage", chat_id=ADMIN_CHAT_ID, text=text, parse_mode="HTML",
       reply_markup=kb, link_preview_options={"is_disabled": True})
    state["pending"][it["id"]] = it


def process_callbacks(state):
    updates = tg("getUpdates", offset=state["tg_offset"], timeout=0,
                 allowed_updates=["callback_query"])
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq or str(cq["from"]["id"]) != str(ADMIN_CHAT_ID):
            continue
        action, _, iid = (cq.get("data") or "").partition(":")
        it = state["pending"].pop(iid, None)
        status = "⏳ уже обработано или устарело"
        if it and action == "pub":
            try:
                publish(it)
                status = "✅ Опубликовано"
            except Exception as ex:  # noqa: BLE001
                log.error("Не удалось опубликовать: %s", ex)
                state["pending"][iid] = it
                status = "⚠️ Ошибка, нажмите ещё раз"
        elif it and action == "skip":
            status = "❌ Пропущено"
        log.info("Кнопка: %s", status)
        msg = cq.get("message") or {}
        try:
            tg("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
               reply_markup={"inline_keyboard": [[{"text": status, "callback_data": "noop"}]]})
            tg("answerCallbackQuery", callback_query_id=cq["id"], text=status)
        except Exception:  # noqa: BLE001  (старые кнопки Telegram менять не даёт — не страшно)
            pass


def dispatch(state):
    queue = sorted(state["queue"], key=lambda i: i.get("score", 0), reverse=True)
    for it in queue[:MAX_POSTS_PER_RUN]:
        state["queue"].remove(it)
        try:
            if MODERATION:
                send_draft(state, it)
            else:
                publish(it)
        except Exception as ex:  # noqa: BLE001
            log.error("Ошибка отправки «%s»: %s", it.get("title"), ex)


# ----------------------------------------------------------------- main
def check_config():
    problems = []
    if not DRY_RUN and not TG_TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("ANTHROPIC_API_KEY")
    if not CHANNELS and not DRY_RUN:
        problems.append("RU_CHANNEL или EN_CHANNEL")
    if MODERATION and not ADMIN_CHAT_ID and not DRY_RUN:
        problems.append("ADMIN_CHAT_ID (нужен для модерации)")
    if problems:
        sys.exit("Не заданы настройки: " + ", ".join(problems))


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    check_config()
    state = load_state()
    try:
        if MODERATION:
            process_callbacks(state)

        due = FORCE_COLLECT or time.time() - state["last_collect"] >= COLLECT_EVERY_MIN * 60
        if not due:
            log.info("Сбор новостей не по графику — только проверил кнопки")
            return
        state["last_collect"] = time.time()

        items = collect(state)
        log.info("Новых статей для оценки: %d", len(items))
        if items:
            decisions = score(items, state["recent"])
            for it in items:  # просмотренными помечаем только после успешной оценки
                state["seen"][it["id"]] = time.time()
            chosen = select(items, decisions)
            for it in chosen:
                state["recent"].append(it["title"])
            log.info("Отобрано: %d, пишу тексты…", len(chosen))
            for it in write_posts(chosen):
                it["queued_at"] = time.time()
                state["queue"].append(it)
            log.info("В очереди: %d", len(state["queue"]))

        if is_quiet():
            log.info("Тихие часы — публикации отложены")
        else:
            dispatch(state)
    finally:
        if not DRY_RUN:
            save_state(state)


if __name__ == "__main__":
    main()
