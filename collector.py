
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

USER_AGENT = "RecompileRadar/0.2 (public project discovery)"
TIMEOUT = 20
MAX_ITEMS_PER_SOURCE = 50

KEYWORDS = (
    "recompil", "decompil", "source port", "source-port",
    "browser port", "web port", "webassembly", "wasm",
    "webgl", "play in browser", "playable in browser",
    "html5 game", "game engine port", "reverse engineering",
    "open source game", "classic game", "port of",
    "android port", "static recompilation", "game port",
)

# RSS-новости и публичный GitHub REST API.
SOURCES = [
    {
        "name": "GamingOnLinux",
        "type": "rss",
        "url": "https://www.gamingonlinux.com/article_rss.php",
    },
    {
        "name": "GitHub: browser ports",
        "type": "github",
        "query": '"browser port" game',
    },
    {
        "name": "GitHub: WebAssembly games",
        "type": "github",
        "query": "game WebAssembly",
    },
    {
        "name": "GitHub: source ports",
        "type": "github",
        "query": '"source port" game',
    },
    {
        "name": "GitHub: static recompilation",
        "type": "github",
        "query": '"static recompilation" OR recomp game',
    },
    {
        "name": "GitHub: Android game ports",
        "type": "github",
        "query": "android game port",
    },
]


def clean_text(value):
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_url(url, accept):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
        },
    )
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


def fetch_github_repositories(query):
    params = urllib.parse.urlencode({
        "q": query,
        "sort": "updated",
        "order": "desc",
        "per_page": MAX_ITEMS_PER_SOURCE,
    })
    url = "https://api.github.com/search/repositories?" + params

    raw = fetch_url(
        url,
        "application/vnd.github+json",
    )
    data = json.loads(raw.decode("utf-8"))

    entries = []
    for repo in data.get("items", []):
        title = repo.get("full_name") or repo.get("name") or ""
        link = repo.get("html_url") or ""
        description = repo.get("description") or ""
        topics = repo.get("topics") or []
        summary = description

        if topics:
            topic_text = ", ".join(topics)
            summary = f"{summary} Topics: {topic_text}".strip()

        published = (
            repo.get("pushed_at")
            or repo.get("updated_at")
            or repo.get("created_at")
            or ""
        )

        if title and link:
            entries.append({
                "title": title,
                "url": link,
                "summary": summary[:1500],
                "published": published,
            })

    print(
        "GitHub API: найдено всего по запросу — "
        f"{data.get('total_count', len(entries))}"
    )
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
        raise RuntimeError(
            f"Не удалось прочитать {path}: {error}"
        ) from error


def normalise_url(url):
    return (url or "").strip().rstrip("/")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    candidates_data = load_json(
        CANDIDATES_FILE,
        {
            "schema_version": 1,
            "last_updated": None,
            "candidates": [],
        },
    )

    # Сохраняем существующую базу кандидатов.
    candidates_data.setdefault("schema_version", 1)
    candidates_data.setdefault("candidates", [])

    known = {
        normalise_url(item.get("url", ""))
        for item in candidates_data["candidates"]
        if item.get("url")
    }

    # Проверенные игры используем только для исключения дублей.
    games_data = load_json(GAMES_FILE, {"games": []})
    known_games = set()

    for item in games_data.get("games", []):
        for field in ("source_url", "build_url"):
            url = normalise_url(item.get(field, ""))
            if url:
                known_games.add(url)

    added = 0
    errors = []

    for source in SOURCES:
        try:
            if source["type"] == "rss":
                xml_data = fetch_url(
                    source["url"],
                    "application/rss+xml, application/atom+xml, "
                    "application/xml, text/xml",
                )
                entries = parse_feed(xml_data)
            else:
                entries = fetch_github_repositories(
                    source["query"]
                )

            print(
                f"{source['name']}: получено записей — "
                f"{len(entries)}"
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
                    "found_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }

                candidates_data["candidates"].append(candidate)
                known.add(url)
                added += 1
                source_added += 1

            print(
                f"{source['name']}: новых кандидатов — "
                f"{source_added}"
            )

            time.sleep(1)

        except (
            urllib.error.URLError,
            TimeoutError,
            ET.ParseError,
            OSError,
            json.JSONDecodeError,
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

    candidates_data["last_updated"] = datetime.now(
        timezone.utc
    ).isoformat()

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
