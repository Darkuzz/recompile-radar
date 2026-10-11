
import os
import json
import urllib.request
import urllib.error


def main():
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        print("Автоматический запуск: публикации пока отключены.")
        return

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    message = (
        "🛰 RecompileRadar: проверка автоматизации!\n\n"
        "Связь GitHub Actions с Telegram настроена. "
        "Скоро здесь появятся подборки рекомпиляций "
        "и портов игр для ПК и Android."
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))

        if not result.get("ok"):
            raise RuntimeError("Telegram не подтвердил отправку")

        print("Тестовое сообщение успешно отправлено в Telegram.")

    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Telegram API вернул HTTP {error.code}: {details}"
        ) from error


if __name__ == "__main__":
    main()
