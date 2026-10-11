
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
GAMES_FILE = DATA_DIR / "games.json"
CANDIDATES_FILE = DATA_DIR / "candidates.json"

USER_AGENT = "RecompileRadar/0.2 (public game-port discovery)"
TIMEOUT = 25
MAX_ITEMS_PER_SOURCE = 50

# Ищем рекомпиляции, порты классических игр и браузерные версии.
SOURCES = [
    {
        "name": "GamingOnLinux",
        "url": "https://www.gamingonlinux.com/article_rss.php",
    },
    {
        "name": "GitHub: browser ports",
        "url": "https://github.com/search.atom?q=game+browser+port&type=repositories",
    },
    {
        "name": "GitHub: WebAssembly games",
        "url": "https://github.com/search.atom?q=game+WebAssembly+OR+wasm&type=repositories",
    },
    {
        "name": "GitHub: source ports",
        "url": "https://github.com/search.atom?q=game+source+port&type=repositories",
    },
    {
        "name": "GitHub: static recompilation",
        "url": "https://github.com/search.atom?q=game+recompilation&type=repositories",
    },
    {
        "name": "GitHub: Android game ports",
        "url": "https://github.com/search.atom?q=game+port+android&type=repositories",
    },
]

# Сильные признаки нужного проекта.
POSITIVE_PATTERNS = [
    r"\bstatic recompil",
    r"\bstatic re-compil",
    r"\bdecompil(?:ation|ed|ing)?\b",
    r"\brecompil(?:ation|ed|ing)?\b",
    r"\bsource port\b",
    r"\bsource-port\b",
    r"\bgame port\b",
    r"\bport of\b",
    r"\bport to (?:android|windows|linux|web|browser)\b",
    r"\bweb port\b",
    r"\bbrowser port\b",
    r"\bplay(?:able)? in (?:the )?browser\b",
    r"\bwebassembly\b",
    r"\bwasm\b",
    r"\bwebgl\b",
    r"\bhtml5 game\b",
    r"\bgame preservation\b",
    r"\bopen[- ]source port\b",
    r"\bport of the original game\b",
    r"\b(?:android|windows|linux) port of\b",
]

# Признаки, что репозиторий, вероятно, не является портом самой игры.
# Исключаем его только при отсутствии сильных игровых признаков.
NOISE_PATTERNS = [
    r"\bportfolio\b",
    r"\bgamebanfix\b",
    r"\bmetamod\b",
    r"\bplugin(?:s)?\b",
    r"\btheme for\b",
    r"\bawesome[- ]list\b",
    r"\bcollection of tools\b",
    r"\bporting toolkit\b",
    r"\bgame porting tool(?:kit)?\b",
    r"\bport any .* source code\b",
    r"\bsource code samples\b",
]

# Эти платформы сами по себе не являются нашей основной целью.
# Не исключаем проект, если он также поддерживает PC, Android или браузер.
EXCLUSIVE_PLATFORM_PATTERNS = [
    r"\b(?:ios|iphone|ipad|vision pro)\b",
    r"\bplaystation vita\b",
    r"\bps vita\b",
    r"\baltstore\b",
    r"\bsidestore\b",
]

BROWSER_PATTERNS = [
    r"\bbrowser\b",
    r"\bwebassembly\b",
    r"\bwasm\b",
    r"\bwebgl\b",
    r"\bhtml5\b",
    r"\bplay(?:able)? in (?:the )?browser\b",
    r"\bweb port\b",
]

ANDROID_PATTERNS = [
    r"\bandroid\b",
    r"\bapk\b",
]

PC_PATTERNS = [
    r"\bwindows\b",
    r"\blinux\b",
    r"\bdesktop\b",
    r"\bpc\b",
    r"\bwin32\b",
]

USER_AGENT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "application/atom+xml, application/rss+xml, "
        "application/xml, text/xml"
    ),
}


def clean_text(value):
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_url(url):
    request = urllib.request.Request(url, headers=USER_AGENT_HEADERS)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def child_text(element, names):
    for child in element.iter():
        if local_name(child.tag) in names and child.text:
            value = clean_text(child.text)
            if value:
                return value
    return ""


def entry_link(entry):
    links = []

    for child in entry:
        if local_name(child.tag) != "link":
            continue

        href = child.attrib.get("href")
        rel = child.attrib.get("rel", "alternate")

        if href:
            links.append((rel, href.strip()))
        elif child.text:
            links.append((rel, child.text.strip()))

    # Для Atom предпочитаем ссылку на сам проект.
    for rel, href in links:
        if rel == "alternate":
            return href

    return links[0][1] if links else ""


def parse_feed(xml_data):
    root = ET.fromstring(xml_data)
    entries = []

    for element in root.iter():
        if local_name(element.tag) not in ("item", "entry"):
            continue

        title = child_text(element, {"title"})
        url = entry_link(element)

        summary = child_text(
            element,
            {"description", "summary", "content", "encoded"},
        )

        published = child_text(
            element,
            {"pubdate", "published", "updated", "date"},
        )

        if title and url:
            entries.append({
                "title": title,
                "url": url,
                "summary": summary[:2000],
                "published": published,
            })

        if len(entries) >= MAX_ITEMS_PER_SOURCE:
            break

    return entries


def matches_any(text, patterns):
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def classify_platforms(text):
    platforms = []

    if matches_any(text, BROWSER_PATTERNS):
        platforms.append("browser")

    if matches_any(text, ANDROID_PATTERNS):
        platforms.append("android")

    if matches_any(text, PC_PATTERNS):
        platforms.append("pc")

    return platforms


def is_relevant(item):
    title = item.get("title", "")
    summary = item.get("summary", "")
    url = item.get("url", "")

    text = f"{title} {summary} {url}".lower()

    # Требуем явный признак рекомпиляции, порта или браузерной версии.
    if not matches_any(text, POSITIVE_PATTERNS):
        return False

    # Отсекаем инструменты, плагины и портфолио,
    # если они не имеют более конкретного признака игрового порта.
    if matches_any(text, NOISE_PATTERNS):
        strong_game_port = any(
            phrase in text
            for phrase in (
                "source port of ",
                "port of the ",
                "browser port",
                "web port of ",
                "static recompilation",
                "static recomp of ",
                "native android port of ",
                "native windows port of ",
            )
        )
        if not strong_game_port:
            return False

    # Не берем проект только для неподдерживаемой нами платформы.
    platforms = classify_platforms(text)
    if (
        matches_any(text, EXCLUSIVE_PLATFORM_PATTERNS)
        and not any(p in platforms for p in ("pc", "android", "browser"))
    ):
        return False

    return True


def detect_browser_playable(item):
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()

    # Наличие веб-порта не всегда означает готовую игру в браузере.
    if re.search(
        r"\bplay(?:able)? in (?:the )?browser\b|"
        r"\bplay online\b|"
        r"\bplay:\s*https?://",
        text,
    ):
        return "possible"

    if matches_any(text, BROWSER_PATTERNS):
        return "unknown"

    return "no_evidence"


def normalise_url(url):
    url = (url or "").strip()
    if not url:
        return ""

    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return url.rstrip("/")

    # Убираем завершающий слеш и фрагмент страницы.
    # Остальные параметры сохраняем, чтобы не объединить разные ссылки.
    return urllib.parse.urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        parts.query,
        "",
    ))


def load_json(path, default):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(
            f"Не удалось прочитать {path}: {error}"
        ) from error

    if not isinstance(data, dict):
        raise RuntimeError(f"Ожидался JSON-объект в файле {path}")

    return data


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    candidates_data = load_json(CANDIDATES_FILE, {
        "schema_version": 1,
        "last_updated": None,
        "candidates": [],
    })

    if not isinstance(candidates_data.get("candidates"), list):
        raise RuntimeError(
            "В candidates.json поле candidates должно быть списком"
        )

    known = {
        normalise_url(item.get("url", ""))
        for item in candidates_data["candidates"]
        if isinstance(item, dict) and item.get("url")
    }

    # Проверенную базу игр не изменяем.
    games_data = load_json(GAMES_FILE, {"games": []})
    known_games = set()

    for item in games_data.get("games", []):
        if not isinstance(item, dict):
            continue

        for field in ("source_url", "build_url"):
            if item.get(field):
                known_games.add(normalise_url(item[field]))

    added = 0
    errors = []

    for source in SOURCES:
        try:
            entries = parse_feed(fetch_url(source["url"]))
            source_added = 0

            print(
                f"{source['name']}: "
                f"получено записей — {len(entries)}"
            )

            for entry in entries:
                url = normalise_url(entry["url"])

                if not url or url in known or url in known_games:
                    continue

                if not is_relevant(entry):
                    continue

                text = (
                    f"{entry.get('title', '')} "
                    f"{entry.get('summary', '')}"
                )

                candidate = {
                    "title": entry["title"],
                    "url": entry["url"],
                    "source_name": source["name"],
                    "published": entry["published"],
                    "summary": entry["summary"],
                    "status": "unverified",
                    "browser_playable": detect_browser_playable(entry),
                    "platforms": classify_platforms(text),
                    "found_at": datetime.now(timezone.utc).isoformat(),
                }

                candidates_data["candidates"].append(candidate)
                known.add(url)
                added += 1
                source_added += 1

            print(
                f"{source['name']}: "
                f"новых кандидатов — {source_added}"
            )

            # Не перегружаем внешние сайты частыми запросами.
            time.sleep(1)

        except (
            urllib.error.URLError,
            TimeoutError,
            ET.ParseError,
            OSError,
        ) as error:
            message = f"{source['name']}: {error}"
            errors.append(message)
            print(f"ОШИБКА источника: {message}")

        except Exception as error:
            message = (
                f"{source['name']}: "
                f"{type(error).__name__}: {error}"
            )
            errors.append(message)
            print(f"ОШИБКА обработки: {message}")

    candidates_data["last_updated"] = datetime.now(timezone.utc).isoformat()
    candidates_data["candidates"].sort(
        key=lambda item: item.get("found_at", ""),
        reverse=True,
    )

    with CANDIDATES_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            candidates_data,
            file,
            ensure_ascii=False,
            indent=2,
        )
        file.write("\n")

    print(f"Новых кандидатов добавлено: {added}")
    print(
        "Всего кандидатов в базе: "
        f"{len(candidates_data['candidates'])}"
    )
    print(f"Ошибок источников: {len(errors)}")


if __name__ == "__main__":
    main()
