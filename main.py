
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GAMES_FILE = ROOT / "data" / "games.json"
PUBLISHED_FILE = ROOT / "data" / "published.json"


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
        raise RuntimeError(
            f"Telegram API вернул HTTP {error.code}: {details}"
        ) from error


def test_message(token, chat_id):
    message = (
        "🛰 RecompileRadar: проверка автоматизации!\n\n"
        "Связь GitHub Actions с Telegram работает."
    )
    telegram_send(token, chat_id, message)
    print("Тестовое сообщение успешно отправлено в Telegram.")


def make_message(game):
    title = game.get("title", "Без названия")
    year = game.get("original_release_year")
    platforms = game.get("platforms", [])
    status = game.get("status", "unknown")
    browser_playable = game.get("browser_playable", False)
    technologies = game.get("technologies", [])
    build_url = game.get("build_url")
    source_url = game.get("source_url")
    announcement_url = game.get("announcement_url")
    notes = game.get("notes", "")

    status_names = {
        "released": "Релиз",
        "playable": "Можно играть",
        "demo": "Демоверсия",
    }

    lines = [f"🎮 {title}"]

    if year:
        lines.append(f"🗓 Оригинальная игра: {year}")

    if platforms:
        lines.append("🖥 Платформы: " + ", ".join(platforms))

    lines.append("📌 Статус: " + status_names.get(status, status))

    if browser_playable is True:
        lines.append("🌐 Запускается прямо в браузере")

    if technologies:
        lines.append("⚙️ Технологии: " + ", ".join(technologies))

    if notes:
        lines.append(f"\nℹ️ {notes}")

    if build_url:
        lines.append(f"\n▶️ Играть / скачать: {build_url}")

    if source_url:
        lines.append(f"🔗 Проект: {source_url}")

    if announcement_url:
        lines.append(f"📰 Источник новости: {announcement_url}")

    lines.append("\n🛰 RecompileRadar")

    return "\n".join(lines)


def publish_one(token, chat_id):
    games_data = load_json(GAMES_FILE, {"games": []})
    published_data = load_json(
        PUBLISHED_FILE,
        {"schema_version": 1, "published_ids": []},
    )

    published_ids = set(published_data.get("published_ids", []))

    for game in games_data.get("games", []):
        game_id = game.get("id")

        if not game_id or game_id in published_ids:
            continue

        if game.get("verification") != "verified":
            continue

        if game.get("status") not in ("released", "playable", "demo"):
            continue

        if not game.get("build_url") and not (
            game.get("browser_playable") is True
        ):
            continue

        message = make_message(game)
        telegram_send(token, chat_id, message)

        published_ids.add(game_id)
        published_data["schema_version"] = 1
        published_data["published_ids"] = sorted(published_ids)
        save_json(PUBLISHED_FILE, published_data)

        print(f"Опубликован проект: {game.get('title', game_id)}")
        return

    print("Нет новых проверенных проектов для публикации.")


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

    if event == "schedule":
        if not token or not chat_id:
            raise RuntimeError("Не настроены секреты Telegram")
        publish_one(token, chat_id)
        return

    print("Ручной запуск: выбери задачу collector или telegram_test.")


if __name__ == "__main__":
    main()
