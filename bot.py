#!/usr/bin/env python3
"""
Автопостинг в Telegram-канал.

Бот сам придумывает тему, пишет пост и публикует его по расписанию.
Запуск:
    python bot.py --check     проверить токен и доступ к каналу
    python bot.py --now       сгенерировать и опубликовать один пост прямо сейчас
    python bot.py --dry-run   сгенерировать пост и показать в консоли, НЕ публикуя
    python bot.py             запустить постоянный режим по расписанию
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import random
import re
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

import requests
from anthropic import Anthropic
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import config

BASE_DIR = Path(__file__).resolve().parent
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

log = logging.getLogger("autopost")


# --------------------------------------------------------------------------
# Инфраструктура
# --------------------------------------------------------------------------
def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def check_config() -> None:
    missing = [
        name
        for name in ("TELEGRAM_TOKEN", "CHANNEL_ID", "ANTHROPIC_API_KEY")
        if not getattr(config, name)
    ]
    if missing:
        raise SystemExit(
            "Не заполнено в .env: " + ", ".join(missing) + "\nСмотри README.md, шаг 2."
        )


def retry(fn, *, attempts: int = 4, base_delay: float = 4.0, what: str = "запрос"):
    """Повтор с нарастающей паузой — сеть и API иногда моргают."""
    last = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — нам важно не упасть насовсем
            last = exc
            if i == attempts:
                break
            delay = base_delay * (2 ** (i - 1)) + random.uniform(0, 2)
            log.warning("%s не удался (%s/%s): %s. Повтор через %.0f c",
                        what, i, attempts, exc, delay)
            time.sleep(delay)
    raise last  # type: ignore[misc]


# --------------------------------------------------------------------------
# История: чтобы бот не писал одно и то же по кругу
# --------------------------------------------------------------------------
def load_history() -> list[dict]:
    path = BASE_DIR / config.HISTORY_FILE
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("История повреждена (%s), начинаю с чистого листа", exc)
        return []


def save_history(entries: list[dict]) -> None:
    path = BASE_DIR / config.HISTORY_FILE
    path.write_text(
        json.dumps(entries[-500:], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def pick_rubric(history: list[dict]) -> str:
    """Берём рубрику, которая дольше всех не выходила."""
    used_recently = [e.get("rubric") for e in history[-len(config.RUBRICS):]]
    fresh = [r for r in config.RUBRICS if r not in used_recently]
    return random.choice(fresh or config.RUBRICS)


def too_similar(title: str, history: list[dict]) -> bool:
    norm = re.sub(r"\W+", " ", title.lower()).strip()
    for entry in history[-config.HISTORY_LOOKBACK:]:
        old = re.sub(r"\W+", " ", entry.get("title", "").lower()).strip()
        if old and SequenceMatcher(None, norm, old).ratio() > config.SIMILARITY_LIMIT:
            return True
    return False


# --------------------------------------------------------------------------
# Генерация текста
# --------------------------------------------------------------------------
def build_system_prompt() -> str:
    return f"""Ты — редактор Telegram-канала «{config.CHANNEL_NAME}».

Тема канала: {config.CHANNEL_TOPIC}
Аудитория: {config.AUDIENCE}
Тон: {config.TONE}
Язык постов: {config.LANGUAGE}

Правила поста:
1. Объём — {config.MIN_CHARS}–{config.MAX_CHARS} знаков. Это Telegram, а не блог: короткие абзацы по 1–3 предложения, между ними пустая строка.
2. Первая строка обязана цеплять: конкретика, вопрос или неожиданный факт. Никаких «Привет, друзья!» и «Сегодня мы поговорим о».
3. В посте — одна мысль, доведённая до конца. Не пытайся впихнуть всё.
4. Обязательно что-то полезное: приём, объяснение, чек-лист, вывод. Читатель должен уйти с чем-то в руках.
5. Финал — короткий вывод или вопрос читателю. Без призывов «подписывайся» и «ставь лайк».
6. Не используй markdown-разметку (**, ##, ---). Только чистый текст, эмодзи и списки через «•» или «—».
7. Ограничения: {config.FORBIDDEN}

Отвечай ТОЛЬКО валидным JSON, без пояснений и без ``` :
{{"title": "заголовок до 60 знаков", "text": "тело поста", "hashtags": ["тег1", "тег2", "тег3"]}}"""


def build_user_prompt(rubric: str, history: list[dict]) -> str:
    recent = [e["title"] for e in history[-config.HISTORY_LOOKBACK:] if e.get("title")]
    recent_block = (
        "Эти темы уже выходили — не повторяй их и не пиши про то же самое другими словами:\n"
        + "\n".join(f"— {t}" for t in recent)
        if recent
        else "Это первый пост канала."
    )
    return (
        f"Напиши новый пост для рубрики:\n{rubric}\n\n"
        f"{recent_block}\n\n"
        f"Сегодня {datetime.now(config.TIMEZONE):%d.%m.%Y}. "
        "Выбери свежий, узкий и конкретный угол — не общий обзор темы."
    )


def extract_json(raw: str) -> dict:
    """Модель иногда добавляет обёртку — достаём JSON аккуратно."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"В ответе нет JSON: {raw[:200]}")
    return json.loads(cleaned[start : end + 1])


def generate_post(client: Anthropic, rubric: str, history: list[dict]) -> dict:
    """Возвращает {'title','text','hashtags'}. До 3 попыток на брак/повтор."""
    for attempt in range(1, 4):
        def call():
            return client.messages.create(
                model=config.MODEL,
                max_tokens=config.MAX_TOKENS,
                system=build_system_prompt(),
                messages=[{"role": "user", "content": build_user_prompt(rubric, history)}],
                # temperature намеренно не задаём: claude-sonnet-5 и новее
                # отклоняют нестандартные параметры сэмплирования
            )

        resp = retry(call, what="запрос к Claude")
        raw = "".join(b.text for b in resp.content if b.type == "text")

        try:
            post = extract_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("Попытка %s: не разобрал ответ (%s)", attempt, exc)
            continue

        title = str(post.get("title", "")).strip()
        text = str(post.get("text", "")).strip()

        if not title or len(text) < config.MIN_CHARS:
            log.warning("Попытка %s: пост слишком короткий (%s знаков)", attempt, len(text))
            continue
        if too_similar(title, history):
            log.warning("Попытка %s: тема «%s» повторяет прошлую", attempt, title)
            continue

        hashtags = [str(h) for h in post.get("hashtags", [])][:4]
        log.info("Готов пост: «%s» (%s знаков)", title, len(text))
        return {"title": title, "text": text, "hashtags": hashtags, "rubric": rubric}

    raise RuntimeError("Не получилось сгенерировать нормальный пост за 3 попытки")


# --------------------------------------------------------------------------
# Оформление и отправка
# --------------------------------------------------------------------------
def clean_hashtag(tag: str) -> str:
    tag = re.sub(r"[^\w]", "", tag.lstrip("#"), flags=re.UNICODE)
    return f"#{tag}" if tag else ""


def render_html(post: dict) -> str:
    """Собираем HTML для Telegram, экранируя всё, что пришло от модели."""
    title = html_lib.escape(post["title"], quote=False)
    body = html_lib.escape(post["text"], quote=False)

    limit = config.MAX_CHARS - len(title) - 120
    if len(body) > limit:
        cut = body[:limit]
        body = cut[: cut.rfind("\n\n")] if "\n\n" in cut else cut.rsplit(" ", 1)[0] + "…"

    parts = [f"<b>{title}</b>", "", body]

    if config.ADD_HASHTAGS and post.get("hashtags"):
        tags = " ".join(filter(None, (clean_hashtag(t) for t in post["hashtags"])))
        if tags:
            parts += ["", html_lib.escape(tags, quote=False)]

    result = "\n".join(parts) + config.SIGNATURE
    return result[:4096]


def telegram_call(method: str, payload: dict) -> dict:
    def call():
        r = requests.post(
            TELEGRAM_API.format(token=config.TELEGRAM_TOKEN, method=method),
            json=payload,
            timeout=30,
        )
        if r.status_code == 429:  # флуд-контроль: ждём ровно столько, сколько просят
            wait = r.json().get("parameters", {}).get("retry_after", 30)
            log.warning("Telegram просит подождать %s c", wait)
            time.sleep(wait + 1)
            raise RuntimeError("flood control")
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram: {data.get('description')}")
        return data["result"]

    return retry(call, what=f"Telegram {method}")


def send_post(html_text: str) -> int:
    result = telegram_call(
        "sendMessage",
        {
            "chat_id": config.CHANNEL_ID,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )
    return result["message_id"]


# --------------------------------------------------------------------------
# Сценарии запуска
# --------------------------------------------------------------------------
def run_once(dry_run: bool = False) -> None:
    history = load_history()
    rubric = pick_rubric(history)
    log.info("Рубрика: %s", rubric.split(" — ")[0])

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    try:
        post = generate_post(client, rubric, history)
        html_text = render_html(post)
    except Exception as exc:  # noqa: BLE001
        log.error("Пост не создан: %s", exc)
        return

    if dry_run:
        print("\n" + "=" * 60)
        print(re.sub(r"</?b>", "", html_text))
        print("=" * 60 + f"\n({len(html_text)} знаков, не опубликовано)\n")
        return

    try:
        message_id = send_post(html_text)
    except Exception as exc:  # noqa: BLE001
        log.error("Не отправилось: %s", exc)
        history.append({
            "ts": datetime.now(config.TIMEZONE).isoformat(timespec="seconds"),
            "rubric": rubric, "title": post["title"], "sent": False, "error": str(exc),
        })
        save_history(history)
        return

    log.info("Опубликовано, message_id=%s", message_id)
    history.append({
        "ts": datetime.now(config.TIMEZONE).isoformat(timespec="seconds"),
        "rubric": rubric, "title": post["title"], "sent": True, "message_id": message_id,
    })
    save_history(history)


def do_check() -> None:
    me = telegram_call("getMe", {})
    log.info("Бот: @%s", me["username"])
    chat = telegram_call("getChat", {"chat_id": config.CHANNEL_ID})
    log.info("Канал: %s (id=%s)", chat.get("title"), chat.get("id"))

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    client.messages.create(
        model=config.MODEL, max_tokens=16,
        messages=[{"role": "user", "content": "ответь одним словом: ок"}],
    )
    log.info("Claude API отвечает, модель %s", config.MODEL)
    log.info("Всё в порядке. Можно запускать: python bot.py")


def run_scheduler() -> None:
    scheduler = BlockingScheduler(timezone=config.TIMEZONE)
    for slot in config.POST_TIMES:
        hour, minute = map(int, slot.split(":"))
        scheduler.add_job(
            run_once,
            CronTrigger(hour=hour, minute=minute, timezone=config.TIMEZONE,
                        jitter=config.JITTER_MINUTES * 60),
            id=f"post_{slot}",
            misfire_grace_time=900,   # проспали из-за перезагрузки — постим в течение 15 мин
            coalesce=True,            # накопившиеся пропуски не выстреливают пачкой
            max_instances=1,
        )
    log.info("Расписание: %s (%s), разброс ±%s мин",
             ", ".join(config.POST_TIMES), config.TIMEZONE, config.JITTER_MINUTES)
    log.info("Работаю. Остановить — Ctrl+C")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен")


def main() -> None:
    parser = argparse.ArgumentParser(description="Автопостинг в Telegram-канал")
    parser.add_argument("--now", action="store_true", help="опубликовать один пост сразу")
    parser.add_argument("--dry-run", action="store_true", help="показать пост, не публикуя")
    parser.add_argument("--check", action="store_true", help="проверить доступы")
    args = parser.parse_args()

    setup_logging()
    check_config()

    if args.check:
        do_check()
    elif args.dry_run:
        run_once(dry_run=True)
    elif args.now:
        run_once()
    else:
        run_scheduler()


if __name__ == "__main__":
    main()
