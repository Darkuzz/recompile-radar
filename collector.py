import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
GAMES_FILE = DATA_DIR / "games.json"
CANDIDATES_FILE = DATA_DIR / "candidates.json"
REVIEW_QUEUE_FILE = DATA_DIR / "review_queue.json"

USER_AGENT = "RecompileRadar/0.4 (public project discovery)"
TIMEOUT = 20
MAX_ITEMS_PER_SOURCE = 30
MAX_CHECKS_PER_RUN = 8
CHECK_RETRY_DAYS = 7

# Основные поисковые запросы. Категория определяется отдельно:
# результат запроса не гарантирует, что проект относится к категории.
SOURCES = [
    {
        "name": "GamingOnLinux",
        "type": "rss",
        "url": "https://www.gamingonlinux.com/article_rss.php",
        "category_hint": "pc",
    },
    {
        "name": "GitHub: browser ports",
        "type": "github",
        "query": '"browser port" game',
        "category_hint": "browser",
    },
    {
        "name": "GitHub: browser playable",
        "type": "github",
        "query": '"play in browser" game',
        "category_hint": "browser",
    },
    {
        "name": "GitHub: WebAssembly games",
        "type": "github",
        "query": "game WebAssembly",
        "category_hint": "browser",
    },
    {
        "name": "GitHub: WebGL ports",
        "type": "github",
        "query": "game WebGL port",
        "category_hint": "browser",
    },
    {
        "name": "GitHub: static recompilation",
        "type": "github",
        "query": '"static recompilation" game',
        "category_hint": "pc",
    },
    {
        "name": "GitHub: source ports",
        "type": "github",
        "query": '"source port" game',
        "category_hint": "pc",
    },
    {
        "name": "GitHub: PC ports",
        "type": "github",
        "query": "game port Windows Linux",
        "category_hint": "pc",
    },
    {
        "name": "GitHub: Android ports",
        "type": "github",
        "query": "android game port",
        "category_hint": "android",
    },
    {
        "name": "GitHub: Android recompilation",
        "type": "github",
        "query": "android recompilation game",
        "category_hint": "android",
    },
]

# Сильные признаки нужного нам типа проекта.
# Общих слов вроде WebGL/Android недостаточно: они дают много шума.
STRONG_PROJECT_TERMS = (
    "recompil", "decompil", "source port", "source-port",
    "browser port", "web port of", "port of", "porting of",
    "static recompilation", "game preservation",
    "classic game", "reverse engineering", "reimplementation",
    "re-implementation", "byte-matching", "byte-exact",
    "bring-your-own-rom", "game port", "native port",
    "fan port", "fan-made port", "engine recreation",
    "game remake", "android port", "port to webgl",
    "port to browser", "port to html5", "port to wasm",
    "port to webassembly", "webgl port of", "browser version of",
    "web version of", "playable browser port",
)

SUPPORTING_TERMS = (
    "game", "gameplay", "rom", "original game", "game engine",
    "playable", "play in browser", "playable in browser",
    "webassembly", "wasm", "webgl", "html5",
    "windows", "linux", "android", "apk",
)

# Если в описании явно говорится о портфолио/шаблоне/инструменте,
# не считаем проект игровой рекомпиляцией только из-за WebGL/HTML5.
EXCLUDE_TERMS = (
    "portfolio", "ui/ux", "personal website", "personal portfolio",
    "landing page", "website template", "admin dashboard",
    "game ui design", "game benchmark", "benchmark portal",
    "browser benchmark", "chess", "world's worst airline",
    "destroy any website", "container day",
)

# Очень общие слова оставлены только для классификации платформы,
# но сами по себе не являются основанием добавить новый проект.
KEYWORDS = STRONG_PROJECT_TERMS


def contains_term(text, term):
    """Ищет целое слово/фразу, а не случайную подстроку (например, port в support)."""
    text = str(text or "").lower()
    term = str(term or "").lower().strip()
    if not term:
        return False
    # Эти основы намеренно распознают разные формы слов: recompilation, decompiled и т. п.
    if term in ("recompil", "decompil"):
        return re.search(r"\b" + re.escape(term) + r"[a-z]*\b", text) is not None
    return re.search(
        r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])",
        text,
    ) is not None

BROWSER_TERMS = (
    "browser", "webassembly", "webgl", "html5",
    "play in browser", "playable in browser",
    "browser port", "web port", "web player",
    "web version", "web build", "wasm",
)

ANDROID_TERMS = (
    "android", "android port", "android apk",
    "arm64-v8a", "android build",
)

PC_TERMS = (
    "windows", "win64", "linux", "pc port",
    "desktop", "native pc", "windows build",
    "linux build",
)

OTHER_PLATFORM_TERMS = (
    "nintendo switch", "switch port", "playstation vita",
    "ps vita", "ps2 port", "xbox port", "ios port",
    "3ds port", "wii port",
)

BUILD_EXTENSIONS = (
    ".apk", ".exe", ".msi", ".msix", ".zip",
    ".7z", ".tar.gz", ".tgz", ".dmg", ".deb",
    ".appimage", ".html", ".wasm", ".ipa",
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_datetime(value):
    if not value:
        return None

    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


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


def child_text(element, names):
    for child in element.iter():
        if child.tag.rsplit("}", 1)[-1].lower() in names and child.text:
            value = clean_text(child.text)
            if value:
                return value
    return ""


def entry_link(entry):
    for child in entry:
        if child.tag.rsplit("}", 1)[-1].lower() == "link":
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
        if element.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
            continue

        title = child_text(element, {"title"})
        link = entry_link(element)
        summary = child_text(
            element, {"description", "summary", "content", "encoded"}
        )
        published = child_text(
            element, {"pubdate", "published", "updated", "date"}
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

        if title and link:
            entries.append({
                "title": title,
                "url": link,
                "summary": clean_text(summary)[:1500],
                "published": (
                    repo.get("pushed_at")
                    or repo.get("updated_at")
                    or repo.get("created_at")
                    or ""
                ),
            })

    print(
        "GitHub API: совпадений по запросу — "
        f"{data.get('total_count', len(entries))}"
    )
    return entries


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


def relevance_assessment(item):
    """Оценивает релевантность; не подтверждает запуск или готовность игры."""
    title = str(item.get("title", ""))
    summary = str(item.get("summary", ""))
    check = item.get("check") or {}
    description = str(check.get("description", ""))
    source_name = str(item.get("source_name", ""))
    category = str(item.get("category", ""))

    project_text = " ".join([title, summary, description]).lower()
    context_text = " ".join([project_text, source_name, category]).lower()
    # Нормализуем пунктуацию для поиска фраз, но сохраняем исходный текст
    # для более гибких проверок формулировок вроде "port ... to WebGL".
    normalized = re.sub(r"[^a-z0-9+#]+", " ", project_text)

    strong_hits = [term for term in STRONG_PROJECT_TERMS if contains_term(project_text, term)]
    explicit_port_patterns = (
        r"\bport(?:ed|ing)?\s+(?:or|of|the|an?)\b.{0,100}\b(?:game|webgl|webassembly|wasm|browser|html5)\b",
        r"\bport(?:ed|ing)?\b.{0,100}\bto\s+(?:webgl|webassembly|wasm|the browser|browser|html5)\b",
        r"\b(?:browser|webgl|webassembly|wasm|html5)\s+port\b",
        r"\b(?:recompil(?:e|ation|ed)|decompil(?:e|ation|ed))\b",
    )
    explicit_port = any(re.search(pattern, normalized) for pattern in explicit_port_patterns)
    if explicit_port and "explicit port/recompilation wording" not in strong_hits:
        strong_hits.append("explicit port/recompilation wording")

    support_hits = [term for term in SUPPORTING_TERMS if contains_term(project_text, term)]
    exclude_hits = [term for term in EXCLUDE_TERMS if contains_term(context_text, term)]

    score = 0
    reasons = []
    if strong_hits:
        # Явная формулировка о переносе важнее случайных слов вроде portfolio.
        score += min(10, len(strong_hits) * 3)
        reasons.append("Есть прямые признаки порта/рекомпиляции: " + ", ".join(strong_hits[:5]))
    if support_hits:
        score += min(3, len(support_hits))
        reasons.append("Есть игровые/платформенные признаки: " + ", ".join(support_hits[:5]))

    source_port_hint = any(contains_term(source_name, term) for term in (
        "browser ports", "webgl ports", "source ports", "pc ports",
        "android ports", "static recompilation", "android recompilation",
    ))
    game_context = any(contains_term(project_text, term) for term in (
        "game", "gameplay", "original game", "rom", "recompil",
        "decompil", "source port", "game port", "webgl port",
    ))
    if source_port_hint and game_context and not strong_hits:
        score += 2
        reasons.append("Источник специализируется на портах; требуется проверить сам проект")

    # Веб-технология сама по себе не доказывает наличие порта.
    generic_web_only = (
        any(contains_term(project_text, term) for term in ("webgl", "webassembly", "wasm", "html5"))
        and not strong_hits
    )

    # Иногда описание явно говорит, что игра запускается в браузере,
    # но не использует слово port. Оставляем такие проекты на ручную проверку.
    browser_game_context = (
        contains_term(project_text, "game")
        and any(phrase in normalized for phrase in (
            "in the browser", "play in browser", "playable in browser",
            "browser version", "browser game",
        ))
        and any(contains_term(project_text, term) for term in ("webassembly", "wasm", "webgl", "html5", "browser"))
    )
    if browser_game_context and not strong_hits:
        score += 2
        reasons.append("Описание указывает на игру в браузере; требуется ручная проверка способа переноса")
    if generic_web_only:
        reasons.append("Есть веб-технологии, но прямое указание на порт/рекомпиляцию не найдено")

    if exclude_hits and not strong_hits:
        score -= 8
        reasons.append("Найдены признаки постороннего проекта: " + ", ".join(exclude_hits[:5]))
    elif exclude_hits and strong_hits:
        score -= 1
        reasons.append("Есть признаки постороннего контекста, но также прямое указание на порт — нужна ручная проверка")

    # Портфолио/шахматные приложения и подобные проекты не должны проходить
    # только потому, что в описании есть слова game, WebGL или WebAssembly.
    if exclude_hits and not strong_hits:
        status = "likely_irrelevant"
    elif strong_hits and score >= 5:
        status = "promising"
    elif strong_hits or (source_port_hint and game_context) or browser_game_context:
        status = "possible"
    elif score <= -4:
        status = "likely_irrelevant"
    else:
        status = "needs_review"

    return {
        "relevance_score": score,
        "relevance_status": status,
        "relevance_reasons": reasons or ["Недостаточно признаков для уверенной оценки; сохранено для проверки"],
    }


def matches_keywords(item):
    assessment = relevance_assessment(item)
    return assessment["relevance_status"] in ("promising", "possible")


def classify_candidate(candidate):
    """
    Предварительная классификация по описанию, названию,
    ссылкам и найденным файлам. Это не подтверждение запуска.
    """
    title = candidate.get("title", "")
    summary = candidate.get("summary", "")
    url = candidate.get("url", "")
    check = candidate.get("check") or {}

    release_assets = check.get("release_assets", [])
    releases = check.get("releases", [])

    asset_text = " ".join(
        asset.get("name", "")
        for asset in release_assets
        if isinstance(asset, dict)
    )

    text = " ".join([
        title,
        summary,
        url,
        check.get("description", ""),
        asset_text,
    ]).lower()

    browser_hits = [term for term in BROWSER_TERMS if term in text]
    android_hits = [term for term in ANDROID_TERMS if term in text]
    pc_hits = [term for term in PC_TERMS if term in text]
    other_hits = [term for term in OTHER_PLATFORM_TERMS if term in text]

    platforms = []

    if browser_hits:
        platforms.append("Web")
    if android_hits:
        platforms.append("Android")
    if pc_hits:
        platforms.append("PC")

    # Не отправляем сторонние платформы в основные категории
    # только на основании их упоминания. Фиксируем их отдельно.
    if not platforms and other_hits:
        category = "other"
    elif "Web" in platforms:
        category = "browser"
    elif "PC" in platforms:
        category = "pc"
    elif "Android" in platforms:
        category = "android"
    elif other_hits:
        category = "other"
    else:
        category = "needs_review"

    # Ссылки на релизы и сборки собираем без утверждения,
    # что соответствующий файл точно работает.
    build_links = []
    for release in releases:
        if not isinstance(release, dict):
            continue

        for asset in release.get("assets", []):
            if not isinstance(asset, dict):
                continue

            asset_url = asset.get("url", "")
            asset_name = asset.get("name", "")
            if asset_url:
                build_links.append({
                    "name": asset_name,
                    "url": asset_url,
                    "type": file_type(asset_name),
                })

    for asset in release_assets:
        if not isinstance(asset, dict):
            continue

        asset_url = asset.get("url", "")
        asset_name = asset.get("name", "")
        if asset_url:
            build_links.append({
                "name": asset_name,
                "url": asset_url,
                "type": file_type(asset_name),
            })

    unique_builds = []
    seen_build_urls = set()
    for item in build_links:
        asset_url = item["url"]
        if asset_url not in seen_build_urls:
            unique_builds.append(item)
            seen_build_urls.add(asset_url)

    # Браузерный запуск остаётся unknown, пока не проверена
    # реальная страница запуска.
    browser_playable = "unknown"

    notes = []
    if browser_hits:
        notes.append(
            "Есть признаки веб-версии; запуск непосредственно "
            "в браузере пока не подтверждён."
        )
    if android_hits:
        notes.append("В описании обнаружены признаки Android-поддержки.")
    if pc_hits:
        notes.append("В описании обнаружены признаки PC-поддержки.")
    if other_hits:
        notes.append(
            "Обнаружены упоминания других платформ; "
            "нужно уточнить основную платформу проекта."
        )
    if unique_builds:
        notes.append(
            "Найдены ссылки на файлы релизов; работоспособность "
            "и назначение файлов не подтверждены."
        )

    return {
        "category": category,
        "platforms_guess": platforms,
        "category_evidence": {
            "browser_terms": browser_hits,
            "android_terms": android_hits,
            "pc_terms": pc_hits,
            "other_platform_terms": other_hits,
        },
        "browser_playable": browser_playable,
        "build_links": unique_builds,
        "classification_notes": notes,
    }


def file_type(name):
    lower = (name or "").lower()

    if lower.endswith(".apk"):
        return "android_apk"
    if lower.endswith((".exe", ".msi", ".msix")):
        return "windows_installer_or_executable"
    if lower.endswith((".zip", ".7z", ".tar.gz", ".tgz")):
        return "archive"
    if lower.endswith((".dmg", ".appimage", ".deb")):
        return "desktop_package"
    if lower.endswith(".html"):
        return "html_file"
    if lower.endswith(".wasm"):
        return "webassembly_file"
    if lower.endswith(".ipa"):
        return "ios_package"
    return "other_file"


def should_check(candidate):
    """
    Ограничиваем число запросов к GitHub API.
    Временные ошибки повторяем не чаще одного раза в неделю.
    """
    previous = candidate.get("check")
    if not previous:
        return True

    notes = " ".join(previous.get("check_notes", [])).lower()
    retry_markers = (
        "http 403",
        "http 429",
        "временная ошибка",
        "не удалось проверить релизы",
        "не удалось разобрать ответ github",
        "проверку нужно повторить",
    )

    if any(marker in notes for marker in retry_markers):
        checked_at = parse_datetime(previous.get("checked_at"))
        if checked_at is None:
            return True
        return datetime.now(timezone.utc) - checked_at >= timedelta(
            days=CHECK_RETRY_DAYS
        )

    # Обычные успешные проверки не повторяем каждый запуск.
    return False


def check_github_project(candidate):
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
        "releases": [],
        "verification": "unverified",
        "browser_playable": "unknown",
        "check_notes": [],
    }

    if not repo_path:
        result["check_notes"].append(
            "Не GitHub-ссылка: требуется отдельная проверка."
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
                "Репозиторий архивирован; это не доказывает, "
                "что игра не работает."
            )

    except urllib.error.HTTPError as error:
        if error.code == 404:
            result["repository_accessible"] = False
            result["check_notes"].append(
                "GitHub вернул 404. Проект не удаляем; "
                "проверку нужно повторить."
            )
        else:
            result["check_notes"].append(
                f"GitHub API вернул HTTP {error.code}; "
                "проверку нужно повторить."
            )
        return result

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        result["check_notes"].append(
            f"Временная ошибка доступа: {error}"
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
            if release.get("draft"):
                continue

            assets = []
            for asset in release.get("assets", []):
                asset_url = asset.get("browser_download_url")
                if asset_url:
                    assets.append({
                        "name": asset.get("name", ""),
                        "url": asset_url,
                        "size": asset.get("size"),
                    })

            result["releases"].append({
                "name": release.get("name") or release.get("tag_name"),
                "url": release.get("html_url"),
                "published_at": release.get("published_at"),
                "assets": assets,
                "draft": bool(release.get("draft")),
                "prerelease": bool(release.get("prerelease")),
            })

        if result["releases"]:
            latest = result["releases"][0]
            result["latest_release_url"] = latest.get("url")
            result["latest_release_name"] = latest.get("name")
            result["release_assets"] = latest.get("assets", [])

            if result["release_assets"]:
                result["check_notes"].append(
                    "Найдены файлы релиза; их работоспособность "
                    "автоматически не подтверждена."
                )
            else:
                result["check_notes"].append(
                    "Найден релиз без прикреплённых файлов."
                )
        else:
            result["check_notes"].append(
                "Публичные релизы не найдены."
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

    result["check_notes"].append(
        "Нужна отдельная проверка запуска, совместимости "
        "и необходимости оригинальных игровых файлов."
    )

    return result



def stable_candidate_id(candidate):
    """Стабильный ID на основе URL, чтобы не плодить дубликаты."""
    url = normalise_url(candidate.get("url", "")).lower()
    if not url:
        url = candidate.get("title", "").strip().lower()
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def review_priority(candidate):
    """
    Приоритет отражает порядок ручной проверки, а не качество/готовность игры.
    Нерелевантные совпадения сохраняются, но уходят в конец очереди.
    """
    assessment = relevance_assessment(candidate)
    relevance = assessment["relevance_status"]

    if relevance == "likely_irrelevant":
        return "low"

    category = candidate.get("category", "needs_review")
    build_links = candidate.get("build_links") or []
    check = candidate.get("check") or {}
    release_assets = check.get("release_assets") or []
    has_build = bool(build_links or release_assets)

    if relevance == "promising" and category == "browser":
        return "high"
    if relevance == "promising" and has_build:
        return "high"
    if relevance == "promising":
        return "medium"
    if relevance == "possible" and has_build:
        return "medium"
    return "low"


def sync_review_queue(candidates, queue_file):
    """
    Добавляет кандидатов в review_queue.json и обновляет сведения,
    сохраняя ручные поля и состояние проверки существующих записей.
    Не меняет candidates.json и не подтверждает игры автоматически.
    """
    queue_data = load_json(
        queue_file,
        {
            "schema_version": 1,
            "last_updated": None,
            "items": [],
        },
    )

    queue_data.setdefault("schema_version", 1)
    queue_data.setdefault("items", [])

    existing_items = {}
    for item in queue_data["items"]:
        if not isinstance(item, dict):
            continue

        item_id = item.get("id")
        if not item_id:
            item_id = stable_candidate_id(item)
            item["id"] = item_id

        # Если в старой очереди случайно есть дубликаты, оставляем
        # первую запись и не затираем её ручные поля.
        existing_items.setdefault(item_id, item)

    added = 0
    updated = 0
    seen_ids = set()
    now = now_iso()

    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate.get("url"):
            continue

        item_id = stable_candidate_id(candidate)
        seen_ids.add(item_id)

        current = existing_items.get(item_id)
        if current is None:
            current = {
                "id": item_id,
                "review_status": "pending",
                "manual_notes": "",
                "added_to_queue_at": now,
            }
            existing_items[item_id] = current
            added += 1
        else:
            updated += 1

        # Обновляем только поля, формируемые сборщиком.
        # review_status, manual_notes и added_to_queue_at сохраняются.
        current.update({
            "title": candidate.get("title", ""),
            "url": candidate.get("url", ""),
            "source_name": candidate.get("source_name", ""),
            "published": candidate.get("published", ""),
            "summary": candidate.get("summary", ""),
            "category": candidate.get("category", "needs_review"),
            "platforms_guess": candidate.get("platforms_guess", []),
            "category_evidence": candidate.get("category_evidence", {}),
            "priority": review_priority(candidate),
            **relevance_assessment(candidate),
            "status": candidate.get("status", "unverified"),
            "verification": "unverified",
            "browser_playable": "unknown",
            "build_links": candidate.get("build_links", []),
            "check": candidate.get("check"),
            "classification_notes": candidate.get(
                "classification_notes", []
            ),
            "found_at": candidate.get("found_at"),
            "last_seen_in_candidates_at": now,
        })

        # Поле статуса проверки не должно исчезать, если запись уже
        # была просмотрена вручную.
        current.setdefault("review_status", "pending")
        current.setdefault("manual_notes", "")
        current.setdefault("added_to_queue_at", now)

        # Не удаляем сомнительные записи из базы. Явно нерелевантные
        # помечаем filtered, но не перезаписываем ручное решение пользователя.
        if (
            current.get("relevance_status") == "likely_irrelevant"
            and current.get("review_status") == "pending"
            and not current.get("manual_notes", "").strip()
        ):
            current["review_status"] = "filtered"
            current["review_status_origin"] = "auto"
        elif (
            current.get("relevance_status") != "likely_irrelevant"
            and current.get("review_status") == "filtered"
            and current.get("review_status_origin") == "auto"
        ):
            current["review_status"] = "pending"
            current.pop("review_status_origin", None)

    # Не удаляем элементы, которых больше нет в candidates.json:
    # они могли быть вручную отмечены или временно отсутствовать.
    queue_data["items"] = sorted(
        existing_items.values(),
        key=lambda item: (
            {"high": 0, "medium": 1, "low": 2}.get(
                item.get("priority", "low"), 2
            ),
            item.get("title", "").lower(),
        ),
    )
    queue_data["last_updated"] = now
    save_json(queue_file, queue_data)

    return added, updated, len(queue_data["items"])


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

    games_data = load_json(GAMES_FILE, {"games": []})

    known_urls = {
        normalise_url(item.get("url", ""))
        for item in candidates_data["candidates"]
        if item.get("url")
    }

    known_game_urls = set()
    for game in games_data.get("games", []):
        for field in ("source_url", "build_url"):
            url = normalise_url(game.get(field, ""))
            if url:
                known_game_urls.add(url)

    added = 0
    errors = []

    # Этап 1: собираем новые записи.
    for source in SOURCES:
        try:
            if source["type"] == "rss":
                raw = fetch_url(
                    source["url"],
                    "application/rss+xml, application/atom+xml, "
                    "application/xml, text/xml",
                )
                entries = parse_feed(raw)
            else:
                entries = fetch_github_repositories(source["query"])

            print(
                f"{source['name']}: получено записей — {len(entries)}"
            )

            source_added = 0

            for entry in entries:
                url = normalise_url(entry.get("url", ""))

                if (
                    not url
                    or url in known_urls
                    or url in known_game_urls
                    or not matches_keywords(entry)
                ):
                    continue

                candidate = {
                    "title": entry.get("title", ""),
                    "url": entry.get("url", ""),
                    "source_name": source["name"],
                    "published": entry.get("published", ""),
                    "summary": entry.get("summary", ""),
                    "status": "unverified",
                    "browser_playable": "unknown",
                    "found_at": now_iso(),
                    "check": None,
                }

                candidate.update(classify_candidate(candidate))
                candidate.update(relevance_assessment(candidate))

                candidates_data["candidates"].append(candidate)
                known_urls.add(url)
                added += 1
                source_added += 1

            print(
                f"{source['name']}: новых кандидатов — {source_added}"
            )
            time.sleep(1)

        except Exception as error:
            message = (
                f"{source['name']}: "
                f"{type(error).__name__}: {error}"
            )
            errors.append(message)
            print(f"ОШИБКА источника: {message}")

    # Этап 2: обогащаем все записи локальной классификацией.
    # Это не вызывает сетевых запросов и не удаляет старые поля.
    candidates = candidates_data["candidates"]

    for candidate in candidates:
        classification = classify_candidate(candidate)
        for key, value in classification.items():
            candidate[key] = value
        candidate.update(relevance_assessment(candidate))

    # Этап 3: ограниченная проверка GitHub.
    checked = 0

    for candidate in candidates:
        if checked >= MAX_CHECKS_PER_RUN:
            break

        if not github_repo_path(candidate.get("url", "")):
            continue

        if not should_check(candidate):
            continue

        candidate["check"] = check_github_project(candidate)
        checked += 1

        # Обновляем категорию с учётом описания и релизов.
        classification = classify_candidate(candidate)
        for key, value in classification.items():
            candidate[key] = value
        candidate.update(relevance_assessment(candidate))

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

    # Этап 4: синхронизируем очередь проверки, сохраняя ручные отметки.
    queue_added, queue_updated, queue_total = sync_review_queue(
        candidates, REVIEW_QUEUE_FILE
    )

    # Итоговая статистика.
    counts = {
        "browser": 0,
        "pc": 0,
        "android": 0,
        "other": 0,
        "needs_review": 0,
    }

    for candidate in candidates:
        category = candidate.get("category", "needs_review")
        if category not in counts:
            category = "needs_review"
        counts[category] += 1

    print(f"Новых кандидатов добавлено: {added}")
    print(f"Проверено GitHub-проектов в этом запуске: {checked}")
    print(f"Всего кандидатов в базе: {len(candidates)}")
    print("Распределение по категориям:")
    print(f"  Браузер: {counts['browser']}")
    print(f"  ПК: {counts['pc']}")
    print(f"  Android: {counts['android']}")
    print(f"  Другие платформы: {counts['other']}")
    print(f"  Требуют уточнения: {counts['needs_review']}")
    print(f"Ошибок источников: {len(errors)}")
    relevance_counts = {
        "promising": 0,
        "possible": 0,
        "needs_review": 0,
        "likely_irrelevant": 0,
    }
    for candidate in candidates:
        status = candidate.get("relevance_status", "needs_review")
        relevance_counts[status if status in relevance_counts else "needs_review"] += 1

    print("Оценка тематической релевантности:")
    print(f"  Перспективные: {relevance_counts['promising']}")
    print(f"  Возможные: {relevance_counts['possible']}")
    print(f"  Нужна проверка: {relevance_counts['needs_review']}")
    print(f"  Вероятно нерелевантные (сохранены): {relevance_counts['likely_irrelevant']}")
    print(f"Новых записей добавлено в очередь проверки: {queue_added}")
    print(f"Записей очереди обновлено: {queue_updated}")
    print(f"Всего записей в review_queue.json: {queue_total}")
    print(
        "Важно: category — предварительная классификация. "
        "verification остаётся unverified до отдельного подтверждения."
    )


if __name__ == "__main__":
    main()
