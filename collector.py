import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
GAMES_FILE = DATA_DIR / "games.json"
CANDIDATES_FILE = DATA_DIR / "candidates.json"

USER_AGENT = "RecompileRadar/0.3 (public project discovery)"
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


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def fetch_url(url, accept="application/json"):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def fetch_json(url):
    raw = fetch_url(url, "application/vnd.github+json")
    return json.loads(raw.decode("utf-8"))


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
    import xml.etree.ElementTree as ET

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
    data = fetch_json(
        "https://api.github.com/search/repositories?" + params
    )

    entries = []

    for repo in data.get("items", []):
        title = repo.get("full_name") or repo.get("name") or ""
        link = repo.get("html_url") or ""
        description = repo.get("description") or ""
        topics = repo.get("topics") or []

        summary = description
        if topics:
            summary = (
                f"{summary} Topics: {', '.join(topics)}"
            ).strip()

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
                "summary": clean_text(summary)[:1500],
                "published": published,
            })

    print(
        "GitHub API: всего совпадений по запросу — "
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


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def normalise_url(url):
    return (url or "").strip().rstrip("/")


def github_repo_path(url):
    match = re.match(
        r"https?://github\.com/([^/]+)/([^/#?]+)",
        url or "",
        re.IGNORECASE,
    )
    if not match:
        return None

    owner, repo = match.groups()
    if repo.lower().endswith(".git"):
        repo = repo[:-4]

    return f"{owner}/{repo}"


def check_github_project(candidate):
    """
    Проверяет только публичные метаданные GitHub.
    Не подтверждает, что игра запускается или работает.
    """
    repo_path = github_repo_path(candidate.get("url", ""))

    result = {
        "checked_at": now_iso(),
        "check_type": "github_metadata",
        "repository_accessible": None,
        "archived": None,
        "default_branch": None,
        "description": "",
        "latest_release_url": None,
        "latest_release_name": None,
        "release_assets": [],
        "verification": "unverified",
        "browser_playable": "unknown",
        "check_notes": [],
    }

    if not repo_path:
        result["check_notes"].append(
            "Не GitHub-ссылка: требуется отдельная проверка источника."
        )
        return result

    try:
        repo = fetch_json(
            "https://api.github.com/repos/" + repo_path
        )

        result["repository_accessible"] = True
        result["archived"] = bool(repo.get("archived"))
        result["default_branch"] = repo.get("default_branch")
        result["description"] = clean_text(
            repo.get("description") or ""
        )

        if repo.get("archived"):
            result["check_notes"].append(
                "Репозиторий архивирован; это не означает, "
                "что игра не работает."
            )

    except urllib.error.HTTPError as error:
        if error.code == 404:
            result["repository_accessible"] = False
            result["check_notes"].append(
                "GitHub вернул 404. Не считать проект удалённым "
                "без повторной проверки."
            )
            return result

        result["check_notes"].append(
            f"GitHub API вернул HTTP {error.code}; "
            "проверку нужно повторить."
        )
        return result

    except (urllib.error.URLError, TimeoutError, OSError) as error:
        result["check_notes"].append(
            f"Временная ошибка доступа: {error}"
        )
        return result

    except (json.JSONDecodeError, ValueError) as error:
        result["check_notes"].append(
            f"Не удалось разобрать ответ GitHub: {error}"
        )
        return result

    time.sleep(0.5)

    try:
        releases = fetch_json(
            "https://api.github.com/repos/"
            + repo_path
            + "/releases?per_page=10"
        )

        for release in releases:
            assets = release.get("assets") or []
            asset_info = []

            for asset in assets:
                download_url = asset.get("browser_download_url")
                if download_url:
                    asset_info.append({
                        "name": asset.get("name", ""),
                        "url": download_url,
                        "size": asset.get("size"),
                    })

            release_info = {
                "name": release.get("name") or release.get("tag_name"),
                "url": release.get("html_url"),
                "published_at": release.get("published_at"),
                "assets": asset_info,
                "draft": bool(release.get("draft")),
                "prerelease": bool(release.get("prerelease")),
            }

            # Сохраняем сведения о релизах и их файлах.
            result.setdefault("releases", []).append(release_info)

        public_releases = [
            release
            for release in result.get("releases", [])
            if not release["draft"]
        ]

        if public_releases:
            latest = public_releases[0]
            result["latest_release_url"] = latest.get("url")
            result["latest_release_name"] = latest.get("name")
            result["release_assets"] = latest.get("assets", [])

            if result["release_assets"]:
                result["check_notes"].append(
                    "Найдены файлы релиза. Их назначение и "
                    "работоспособность автоматически не подтверждены."
                )
            else:
                result["check_notes"].append(
                    "Найден релиз без прикреплённых файлов."
                )
        else:
            result["check_notes"].append(
                "Публичный релиз не найден."
            )

    except urllib.error.HTTPError as error:
        result["check_notes"].append(
            f"Не удалось проверить релизы (HTTP {error.code})."
        )

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        result["check_notes"].append(
            f"Не удалось проверить релизы: {error}"
        )

    # Не повышаем verification до verified автоматически.
    result["check_notes"].append(
        "Нужна ручная проверка: игра запускается ли, "
        "требуются ли оригинальные файлы и работает ли сборка."
    )

    return result


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
    candidates_data.setdefault("schema_version", 1)
    candidates_data.setdefault("candidates", [])

    # Не меняем и не удаляем старые записи.
    known = {
        normalise_url(item.get("url", ""))
        for item in candidates_data["candidates"]
        if item.get("url")
    }

    games_data = load_json(GAMES_FILE, {"games": []})
    known_games = set()

    for game in games_data.get("games", []):
        for field in ("source_url", "build_url"):
            url = normalise_url(game.get(field, ""))
            if url:
                known_games.add(url)

    added = 0
    errors = []

    # Этап 1. Сбор новых кандидатов.
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

                if (
                    not url
                    or url in known
                    or url in known_games
                    or not matches_keywords(entry)
                ):
                    continue

                candidate = {
                    "title": entry["title"],
                    "url": entry["url"],
                    "source_name": source["name"],
                    "published": entry.get("published", ""),
                    "summary": entry.get("summary", ""),
                    "status": "unverified",
                    "browser_playable": "unknown",
                    "found_at": now_iso(),
                    "check": None,
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

        except Exception as error:
            message = (
                f"{source['name']}: "
                f"{type(error).__name__}: {error}"
            )
            errors.append(message)
            print(f"ОШИБКА источника: {message}")

    # Этап 2. Предварительная проверка кандидатов GitHub.
    # Проверяем только записи без результата проверки или
    # с ошибкой/временной недоступностью в прошлой проверке.
    checked = 0
    candidates = candidates_data["candidates"]

    for candidate in candidates:
        if not github_repo_path(candidate.get("url", "")):
            continue

        previous = candidate.get("check") or {}
        previous_notes = " ".join(previous.get("check_notes", []))

        should_check = (
            candidate.get("check") is None
            or previous.get("repository_accessible") is False
            or "временная ошибка" in previous_notes.lower()
            or "нужно повторить" in previous_notes.lower()
        )

        if not should_check:
            continue

        candidate["check"] = check_github_project(candidate)
        checked += 1
        print(
            "Проверен GitHub-проект: "
            f"{candidate.get('title', candidate.get('url'))}"
        )
        time.sleep(0.5)

    candidates_data["last_updated"] = now_iso()
    candidates_data["candidates"].sort(
        key=lambda item: item.get("found_at", ""),
        reverse=True,
    )

    save_json(CANDIDATES_FILE, candidates_data)

    print(f"Новых кандидатов добавлено: {added}")
    print(f"Предварительно проверено проектов: {checked}")
    print(
        "Всего кандидатов в базе: "
        f"{len(candidates_data['candidates'])}"
    )
    print(f"Ошибок источников: {len(errors)}")
    print(
        "Проверенные игры автоматически не публиковались. "
        "Для публикации требуется отдельная подтверждённая запись "
        "в data/games.json."
    )


if __name__ == "__main__":
    main()
```
