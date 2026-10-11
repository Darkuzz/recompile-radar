
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

USER_AGENT = "RecompileRadar/0.2 (public game project discovery)"
TIMEOUT = 25
MAX_ITEMS_PER_SOURCE = 50
REQUEST_DELAY = 2

KEYWORDS = (
    "recompil", "decompil", "source port", "source-port",
    "browser port", "web port", "webassembly", "wasm",
    "webgl", "play in browser", "playable in browser",
    "html5 game", "game engine port", "reverse engineering",
    "open source game", "classic game", "port of",
    "liberation", "reimplementation", "game remake",
    "playable", "dos game", "game port",
)

# GitHub Search API возвращает JSON, а не Atom/XML.
# Остальные источники используют RSS/Atom.
SOURCES = [
    {
        "name": "GamingOnLinux",
        "type": "feed",
        "url": "https://www.gamingonlinux.com/article_rss.php",
    },
    {
        "name": "GitHub: browser ports",
        "type": "github",
        "url": "https://api.github.com/search/repositories?"
               "q=game+browser+port&sort=updated&order=desc&per_page=50",
    },
    {
        "name": "GitHub: WebAssembly games",
        "type": "github",
        "url": "https://api.github.com/search/repositories?"
               "q=game+WebAssembly&sort=updated&order=desc&per_page=50",
    },
    {
        "name": "GitHub: source ports",
        "type": "github",
        "url": "https://api.github.com/search/repositories?"
               "q=game+source+port&sort=updated&order=desc&per_page=50",
    },
]


def clean_text(value):
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_url(url, source_type="feed"):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/vnd.github+json"
            if source_type == "github"
            else "application/atom+xml, application/rss+xml, "
                 "application/xml, text/xml"
        ),
    }

    request = urllib.request.Request(url, headers=headers)

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


def parse_github(data):
    payload = json.loads(data.decode("utf-8"))
    entries = []

    for repo in payload.get("items", [])[:MAX_ITEMS_PER_SOURCE]:
        title = clean_text(repo.get("full_name", ""))
        description = clean_text(repo.get("description", ""))

        if not title or not repo.get("html_url"):
            continue

        topics = repo.get("topics", [])
        summary = description

        if topics:
            summary += " Topics: " + ", ".join(topics)

        entries.append({
            "title": title,
            "url": repo["html_url"],
            "summary": summary[:1500],
            "published": repo.get("updated_at", ""),
        })

    return entries


def matches_keywords(item):
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    return any(keyword in text for keyword in KEYWORDS)


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


def normalise_url(url):
    url = (url or "").strip()

    if not url:
        return ""

    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()

        if not scheme or not host:
            return url.rstrip("/")

        port = parsed.port
        netloc = host

        if port and not (
            (scheme == "https" and port == 443)
            or (scheme == "http" and port == 80)
        ):
            netloc = f"{host}:{port}"

        path = parsed.path.rstrip("/")

        # Удаляем только типичные tracking-параметры.
        query_items = urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
        query_items = [
            (key, value)
            for key, value in query_items
            if not key.lower().startswith("utm_")
            and key.lower() not in {"ref", "fbclid"}
        ]
        query = urllib.parse.urlencode(query_items)

        return urllib.parse.urlunsplit(
            (scheme, netloc, path, query, "")
        )
    except (ValueError, TypeError):
        return url.rstrip("/")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    candidates_data = load_json(CANDIDATES_FILE, {
        "schema_version": 1,
        "last_updated": None,
        "candidates": [],
    })

    if not isinstance(candidates_data.get("candidates"), list):
        raise RuntimeError(
            "Поле candidates в candidates.json должно быть списком"
        )

    known = {
        normalise_url(item.get("url", ""))
        for item in candidates_data["candidates"]
        if isinstance(item, dict) and item.get("url")
    }

    # games.json используется только для проверки дублей.
    # Сборщик не изменяет проверенную базу игр.
    games_data = load_json(GAMES_FILE, {"games": []})
    games = games_data.get("games", [])

    if not isinstance(games, list):
        raise RuntimeError("Поле games в games.json должно быть списком")

    known_games = set()

    for game in games:
        if not isinstance(game, dict):
            continue

        for field in ("source_url", "build_url"):
            value = normalise_url(game.get(field, ""))
            if value:
                known_games.add(value)

    added = 0
    errors = []

    for index, source in enumerate(SOURCES):
        try:
            raw_data = fetch_url(source["url"], source["type"])

            if source["type"] == "github":
                entries = parse_github(raw_data)
            else:
                entries = parse_feed(raw_data)

            print(
                f"{source['name']}: получено записей — {len(entries)}"
            )

            source_added = 0

            for entry in entries:
                url = normalise_url(entry.get("url", ""))

                if not url or url in known or url in known_games:
                    continue

                if not matches_keywords(entry):
                    continue

                candidate = {
                    "title": entry["title"],
                    "url": entry["url"],
                    "source_name": source["name"],
                    "published": entry.get("published", ""),
                    "summary": entry.get("summary", ""),
                    "status": "unverified",
                    "browser_playable": "unknown",
                    "found_at": datetime.now(timezone.utc).isoformat(),
                }

                candidates_data["candidates"].append(candidate)
                known.add(url)
                added += 1
                source_added += 1

            print(
                f"{source['name']}: новых кандидатов — {source_added}"
            )

        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            details = clean_text(details)[:500]

            if error.code == 403 and "rate limit" in details.lower():
                reason = "GitHub API: достигнут лимит запросов"
            elif error.code == 406:
                reason = "HTTP 406: сервер отклонил формат запроса"
            else:
                reason = f"HTTP {error.code}: {details or error.reason}"

            message = f"{source['name']}: {reason}"
            errors.append(message)
            print(f"ОШИБКА источника: {message}")

        except (
            urllib.error.URLError,
            TimeoutError,
            ET.ParseError,
            json.JSONDecodeError,
            OSError,
        ) as error:
            message = f"{source['name']}: {type(error).__name__}: {error}"
            errors.append(message)
            print(f"ОШИБКА источника: {message}")

        except Exception as error:
            message = (
                f"{source['name']}: {type(error).__name__}: {error}"
            )
            errors.append(message)
            print(f"ОШИБКА обработки: {message}")

        if index < len(SOURCES) - 1:
            time.sleep(REQUEST_DELAY)

    candidates_data["last_updated"] = datetime.now(
        timezone.utc
    ).isoformat()

    candidates_data["candidates"].sort(
        key=lambda item: item.get("found_at", ""),
        reverse=True,
    )

    temporary_file = CANDIDATES_FILE.with_suffix(".json.tmp")

    try:
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(
                candidates_data,
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.write("\n")

        temporary_file.replace(CANDIDATES_FILE)

    except OSError:
        if temporary_file.exists():
            temporary_file.unlink()
        raise

    print(f"Новых кандидатов добавлено: {added}")
    print(
        "Всего кандидатов в базе: "
        f"{len(candidates_data['candidates'])}"
    )
    print(f"Ошибок источников: {len(errors)}")

    if errors:
        print("Некоторые источники не сработали; остальные обработаны.")


if __name__ == "__main__":
    main()
