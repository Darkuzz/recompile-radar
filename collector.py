
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

USER_AGENT = "RecompileRadar/0.1 (public project discovery)"
TIMEOUT = 20
MAX_ITEMS_PER_SOURCE = 50

KEYWORDS = (
    "recompil", "decompil", "source port", "source-port",
    "browser port", "web port", "webassembly", "wasm",
    "webgl", "play in browser", "playable in browser",
    "html5 game", "game engine port", "reverse engineering",
    "open source game", "classic game", "port of",
)

# Источники-кандидаты. Ошибка одного источника не остановит остальные.
SOURCES = [
    {
        "name": "GamingOnLinux",
        "url": "https://www.gamingonlinux.com/article_rss.php",
    },
    {
        "name": "GitHub: browser ports",
        "url": "https://github.com/search.atom?q=browser+game+port&type=repositories",
    },
    {
        "name": "GitHub: WebAssembly games",
        "url": "https://github.com/search.atom?q=game+WebAssembly&type=repositories",
    },
    {
        "name": "GitHub: source ports",
        "url": "https://github.com/search.atom?q=game+source+port&type=repositories",
    },
]


def clean_text(value):
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_url(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def child_text(element, names):
    for child in element.iter():
        if local_name(child.tag) in names and child.text:
            text = clean_text(child.text)
            if text:
                return text
    return ""


def entry_link(entry):
    # Atom: <link href="..."/>; RSS: <link>...</link>
    for child in entry:
        if local_name(child.tag) == "link":
            href = child.attrib.get("href")
            if href:
                return href.strip()
            if child.text:
                return child.text.strip()
    return ""


def parse_feed(xml_data):
    root = ET.fromstring(xml_data)
    entries = []

    for element in root.iter():
        if local_name(element.tag) not in ("item", "entry"):
            continue

        title = child_text(element, {"title"})
        link = entry_link(element)
        summary = child_text(
            element,
            {"description", "summary", "content", "encoded"},
        )
        published = child_text(
            element,
            {"pubdate", "published", "updated", "date"},
        )

        if title and link:
            entries.append({
                "title": title,
                "url": link,
                "summary": summary[:1500],
                "published": published,
            })

        if len(entries) >= MAX_ITEMS_PER_SOURCE:
            break

    return entries


def matches_keywords(item):
    text = f"{item['title']} {item['summary']}".lower()
    return any(keyword in text for keyword in KEYWORDS)


def load_json(path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"Не удалось прочитать {path}: {error}") from error


def normalise_url(url):
    return url.strip().rstrip("/")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    candidates_data = load_json(CANDIDATES_FILE, {
        "schema_version": 1,
        "last_updated": None,
        "candidates": [],
    })
    known = {
        normalise_url(item.get("url", ""))
        for item in candidates_data.get("candidates", [])
        if item.get("url")
    }

    # Не перезаписываем проверенную базу. Читаем её только для дедупликации.
    games_data = load_json(GAMES_FILE, {"games": []})
    known_games = {
        normalise_url(item.get("source_url", ""))
        for item in games_data.get("games", [])
        if item.get("source_url")
    }
    known_games.update(
        normalise_url(item.get("build_url", ""))
        for item in games_data.get("games", [])
        if item.get("build_url")
    )

    added = 0
    errors = []

    for source in SOURCES:
        try:
            entries = parse_feed(fetch_url(source["url"]))
            print(f"{source['name']}: получено записей — {len(entries)}")

            for entry in entries:
                url = normalise_url(entry["url"])

                if not url or url in known or url in known_games:
                    continue
                if not matches_keywords(entry):
                    continue

                candidates_data["candidates"].append({
                    "title": entry["title"],
                    "url": entry["url"],
                    "source_name": source["name"],
                    "published": entry["published"],
                    "summary": entry["summary"],
                    "status": "unverified",
                    "browser_playable": "unknown",
                    "found_at": datetime.now(timezone.utc).isoformat(),
                })
                known.add(url)
                added += 1

            # Не перегружаем внешние сайты частыми запросами.
            time.sleep(1)

        except (urllib.error.URLError, TimeoutError, ET.ParseError, OSError) as error:
            message = f"{source['name']}: {error}"
            errors.append(message)
            print(f"ОШИБКА источника: {message}")
        except Exception as error:
            message = f"{source['name']}: {type(error).__name__}: {error}"
            errors.append(message)
            print(f"ОШИБКА обработки: {message}")

    candidates_data["last_updated"] = datetime.now(timezone.utc).isoformat()
    candidates_data["candidates"].sort(
        key=lambda item: item.get("found_at", ""),
        reverse=True,
    )

    with CANDIDATES_FILE.open("w", encoding="utf-8") as file:
        json.dump(candidates_data, file, ensure_ascii=False, indent=2)
        file.write("\n")

    print(f"Новых кандидатов добавлено: {added}")
    print(f"Всего кандидатов в базе: {len(candidates_data['candidates'])}")
    print(f"Ошибок источников: {len(errors)}")


if __name__ == "__main__":
    main()
