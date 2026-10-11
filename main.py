import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CANDIDATES_FILE = ROOT / "data" / "candidates.json"
PUBLISHED_FILE = ROOT / "data" / "published.json"
MAX_POSTS_PER_RUN = 5
TELEGRAM_LIMIT = 3900


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def telegram_send(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError("Telegram не подтвердил отправку")
        return result["result"]["message_id"]
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram API вернул HTTP {error.code}: {details}") from error


def test_message(token, chat_id):
    telegram_send(
        token,
        chat_id,
        "🛰 RecompileRadar: проверка автоматизации!\n\nСвязь GitHub Actions с Telegram работает.",
    )
    print("Тестовое сообщение успешно отправлено в Telegram.")


def clean(value, limit=500):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value[:limit]


def eligible(candidate):
    # Публикуем перспективные и возможные проекты без ручного подтверждения.
    # Проекты с низкой релевантностью и недостатком признаков не публикуем.
    return (
        candidate.get("relevance_status") in ("promising", "possible")
        and bool(candidate.get("url"))
        and bool(candidate.get("title"))
    )


def link_lines(candidate):
    lines = []
    category = candidate.get("category")
    check = candidate.get("check") or {}
    builds = candidate.get("build_links") or []

    # Исключаем контрольные суммы и прочие файлы, которые не являются сборками.
    useful_builds = []
    seen = set()
    for build in builds:
        if not isinstance(build, dict):
            continue
        url = build.get("url")
        name = clean(build.get("name"), 120)
        kind = build.get("type", "")
        if not url or url in seen:
            continue
        if kind == "other_file" or re.search(r"\.(sha256|sha1|md5|sig|asc)$", name, re.I):
            continue
        seen.add(url)
        useful_builds.append((name or "Файл сборки", url))
        if len(useful_builds) >= 3:
            break

    if useful_builds:
        for name, url in useful_builds:
            lines.append(f"⬇️ Скачать ({name}): {url}")
    else:
        release_url = check.get("latest_release_url")
        if release_url:
            lines.append(f"📦 Релиз проекта: {release_url}")

    # Для браузерной категории не обещаем запуск, если не нашли конкретную страницу игры.
    if category == "browser":
        lines.append("🌐 Браузерная версия заявлена в описании; прямая ссылка на запуск автоматически не найдена.")
    elif not useful_builds:
        lines.append("📌 Готовая сборка автоматически не найдена; смотри исходный проект и его инструкции.")

    project_url = candidate.get("url")
    if project_url:
        lines.append(f"🔗 Исходный проект: {project_url}")

    return lines


def make_candidate_post(candidate):
    title = clean(candidate.get("title"), 180) or "Проект без названия"
    summary = clean(candidate.get("summary"), 600)
    category = candidate.get("category", "needs_review")
    platforms = candidate.get("platforms_guess") or []
    score = candidate.get("relevance_status")
    check = candidate.get("check") or {}
    release_name = clean(check.get("latest_release_name"), 120)

    status_labels = {
        "promising": "Перспективная находка",
        "possible": "Возможный порт / рекомпиляция",
    }
    category_labels = {
        "browser": "Браузер",
        "pc": "ПК",
        "android": "Android",
        "other": "Другая платформа",
        "needs_review": "Платформа уточняется",
    }

    lines = [f"🎮 {title}", f"🧭 {status_labels.get(score, 'Новая находка')}"]
    if platforms:
        lines.append("🖥 Платформы: " + ", ".join(platforms))
    elif category:
        lines.append("🖥 Категория: " + category_labels.get(category, category))
    if release_name:
        lines.append(f"📦 Последний найденный релиз: {release_name}")
    if summary:
        lines.append(f"\nℹ️ {summary}")
    lines.append("")
    lines.extend(link_lines(candidate))
    lines.append("\n⚠️ Статус и ссылки собраны автоматически; работоспособность игры не подтверждена вручную.")
    lines.append("🛰 RecompileRadar")
    return "\n".join(lines)


def make_digest(candidates):
    header = "🛰 RecompileRadar — новые находки\nАвтоматическая подборка портов, рекомпиляций и браузерных версий.\n"
    blocks = []
    used = []
    for candidate in candidates:
        block = make_candidate_post(candidate)
        separator = "\n\n━━━━━━━━━━━━━━\n\n"
        projected = len(header) + sum(len(x) for x in blocks) + len(separator) * len(blocks) + len(block)
        if projected > TELEGRAM_LIMIT:
            break
        blocks.append(block)
        used.append(candidate)
        if len(used) >= MAX_POSTS_PER_RUN:
            break
    if not blocks:
        return None, []
    return header + "\n\n━━━━━━━━━━━━━━\n\n".join(blocks), used


def publish_digest(token, chat_id):
    candidates_data = load_json(CANDIDATES_FILE, {"candidates": []})
    published_data = load_json(PUBLISHED_FILE, {"schema_version": 1, "published_ids": []})
    published_ids = set(published_data.get("published_ids", []))

    candidates = candidates_data.get("candidates", [])
    pending = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        url = candidate.get("url", "").strip().rstrip("/").lower()
        candidate_id = candidate.get("id") or url
        if not candidate_id or candidate_id in published_ids or not eligible(candidate):
            continue
        candidate["id"] = candidate_id
        pending.append(candidate)

    # Сначала наиболее релевантные и свежие находки.
    # Сначала новые записи, затем устойчиво сортируем по релевантности и оценке.
    pending.sort(key=lambda item: item.get("found_at", ""), reverse=True)
    pending.sort(key=lambda item: int(item.get("relevance_score") or 0), reverse=True)
    pending.sort(key=lambda item: 0 if item.get("relevance_status") == "promising" else 1)

    message, selected = make_digest(pending)
    if not selected:
        print("Нет новых подходящих проектов для публикации.")
        return

    telegram_send(token, chat_id, message)
    for candidate in selected:
        published_ids.add(candidate.get("id") or candidate.get("url", "").strip().rstrip("/").lower())
    published_data["schema_version"] = 1
    published_data["published_ids"] = sorted(published_ids)
    save_json(PUBLISHED_FILE, published_data)
    print(f"Опубликована подборка: {len(selected)} проектов.")
    for candidate in selected:
        print(f"  - {candidate.get('title', candidate.get('url'))}")


def main():
    task = os.environ.get("TASK", "")
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if task == "telegram_test":
        if not token or not chat_id:
            raise RuntimeError("Не настроены секреты Telegram")
        test_message(token, chat_id)
        return

    if task == "publish" or event == "schedule":
        if not token or not chat_id:
            raise RuntimeError("Не настроены секреты Telegram")
        publish_digest(token, chat_id)
        return

    print("Ручной запуск: выбери задачу collector или telegram_test.")


if __name__ == "__main__":
    main()
