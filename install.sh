#!/usr/bin/env bash
#
# Установка бота на сервер. Запускать из папки с репозиторием:
#
#   ./install.sh              обычная установка, спросит ключи и про автозапуск
#   ./install.sh --service    сразу поставить systemd-юнит, ничего не спрашивая
#   ./install.sh --no-service только окружение и ключи, без автозапуска
#
# Скрипт идемпотентный: можно запускать повторно, ничего не сломается.
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
ENV_FILE="$APP_DIR/.env"
SERVICE_NAME="tgbot"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"

WANT_SERVICE="ask"
for arg in "$@"; do
    case "$arg" in
        --service)    WANT_SERVICE="yes" ;;
        --no-service) WANT_SERVICE="no" ;;
        -h|--help)    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
        *)            echo "Неизвестный аргумент: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
say "Проверяю Python"
# --------------------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die \
    "python3 не найден. Debian/Ubuntu: sudo apt update && sudo apt install -y python3 python3-venv"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die \
    "нужен Python 3.9 или новее, а стоит $(python3 --version 2>&1)"
ok "$(python3 --version 2>&1)"

# --------------------------------------------------------------------------
say "Виртуальное окружение и зависимости"
# --------------------------------------------------------------------------
if [ ! -x "$PY" ]; then
    python3 -m venv "$VENV" 2>/dev/null || die \
        "не удалось создать venv. Debian/Ubuntu: sudo apt install -y python3-venv"
    ok "создано $VENV"
else
    ok "уже есть $VENV"
fi

"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -r "$APP_DIR/requirements.txt" || die "не установились зависимости"
ok "зависимости установлены"

# На минимальных серверах не бывает системной базы часовых поясов,
# и ZoneInfo из config.py падает. Дотягиваем tzdata в venv.
if ! "$PY" -c 'import config' >/dev/null 2>&1; then
    "$PIP" install --quiet tzdata || true
    "$PY" -c 'import config' >/dev/null 2>&1 || die \
        "config.py не импортируется, подробности: $PY -c 'import config'"
    ok "доустановлен tzdata (часовые пояса)"
fi

# --------------------------------------------------------------------------
say "Ключи (.env)"
# --------------------------------------------------------------------------
if [ -f "$ENV_FILE" ]; then
    ok ".env уже есть, не трогаю — правится через: nano $ENV_FILE"
elif [ ! -t 0 ]; then
    cp "$APP_DIR/.env.example" "$ENV_FILE"
    warn "неинтерактивный запуск: создал .env из шаблона, впиши ключи сам"
else
    echo "    Вставь три значения (см. README, шаг 2). Ввод не сохраняется в историю."
    echo
    read -r -p "    TELEGRAM_TOKEN      : " V_TOKEN
    read -r -p "    CHANNEL_ID (@имя или ссылка t.me/...): " V_CHANNEL
    read -r -p "    ANTHROPIC_API_KEY   : " V_KEY
    ( umask 077; cat > "$ENV_FILE" <<EOF
TELEGRAM_TOKEN=${V_TOKEN}
CHANNEL_ID=${V_CHANNEL}
ANTHROPIC_API_KEY=${V_KEY}
EOF
    )
    unset V_TOKEN V_CHANNEL V_KEY
    ok "создан .env"
fi
chmod 600 "$ENV_FILE"
ok "права на .env: 600 (читает только владелец)"

# --------------------------------------------------------------------------
say "Проверка доступов"
# --------------------------------------------------------------------------
if grep -qE 'AAxxxx|sk-ant-xxxx|@moy_kanal' "$ENV_FILE"; then
    warn "в .env остались значения-заглушки — проверку пропускаю"
    warn "впиши настоящие ключи в $ENV_FILE и запусти ./install.sh ещё раз"
    SKIP_SERVICE=1
else
    if "$PY" "$APP_DIR/bot.py" --check; then
        ok "токен, канал, права на публикацию и Claude API — в порядке"
    else
        die "проверка не прошла. Почини по сообщению выше и запусти ./install.sh снова"
    fi
fi

# --------------------------------------------------------------------------
say "Автозапуск (systemd)"
# --------------------------------------------------------------------------
if [ "${SKIP_SERVICE:-0}" = "1" ]; then
    warn "пока не настроены ключи — автозапуск ставить рано, пропускаю"
    exit 0
fi

if [ "$WANT_SERVICE" = "ask" ]; then
    if [ -t 0 ]; then
        read -r -p "    Поставить автозапуск, чтобы бот работал 24/7? [Y/n] " ANSWER
        case "${ANSWER:-y}" in [Nn]*) WANT_SERVICE="no" ;; *) WANT_SERVICE="yes" ;; esac
    else
        WANT_SERVICE="no"
    fi
fi

if [ "$WANT_SERVICE" = "no" ]; then
    warn "пропущено. Запускать вручную: $PY $APP_DIR/bot.py"
    exit 0
fi

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    die "нужны права root, а sudo нет. Запусти от root: su - ; ./install.sh --service"
fi

# Под каким пользователем крутить сервис: не root, если скрипт звали через sudo
SERVICE_USER="${SUDO_USER:-$(id -un)}"

$SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Telegram autopost bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${PY} ${APP_DIR}/bot.py
Restart=always
RestartSec=30

# бот пишет bot.log и history.json рядом с собой — доступ нужен только туда
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=${APP_DIR}

[Install]
WantedBy=multi-user.target
EOF
ok "записан $UNIT (пользователь: ${SERVICE_USER})"

# история и лог должны принадлежать пользователю сервиса
$SUDO chown -R "${SERVICE_USER}" "$APP_DIR" 2>/dev/null || true

$SUDO systemctl daemon-reload
$SUDO systemctl enable --quiet "$SERVICE_NAME"
$SUDO systemctl restart "$SERVICE_NAME"
sleep 2

if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "сервис запущен и поднимется сам после перезагрузки"
else
    die "сервис не поднялся. Смотри: sudo journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

cat <<EOF

$(printf '\033[1mГотово.\033[0m') Бот работает по расписанию из config.py.

  sudo systemctl status $SERVICE_NAME      состояние
  sudo journalctl -u $SERVICE_NAME -f      живой лог
  sudo systemctl restart $SERVICE_NAME     перечитать config.py после правок
  sudo systemctl stop $SERVICE_NAME        остановить
  $PY $APP_DIR/bot.py --dry-run            показать пост, не публикуя

EOF
