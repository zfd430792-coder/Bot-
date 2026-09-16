#!/usr/bin/env bash
#
# Установка бота знакомств одной командой.
#
#   bash install.sh
#
# Скрипт проверит Python, создаст виртуальное окружение, поставит зависимости
# и запустит мастер настройки, который по шагам спросит токен и админа.
#
set -uo pipefail

REPO_URL="https://github.com/zfd430792-coder/Bot-.git"
PROJECT_DIR="Bot-"
MIN_PY_MINOR=10
LOG_FILE="$(mktemp -t dating-bot-install.XXXXXX.log)"

# Запуск через «curl … | bash» отдаёт скрипт на stdin — возвращаем ввод терминалу.
# Подсистему проверяем в подоболочке: без управляющего терминала exec убьёт скрипт.
if [ ! -t 0 ] && ( : < /dev/tty ) 2>/dev/null; then
    exec < /dev/tty
fi

# ─────────────────────────────── Оформление ─────────────────────────────────

if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; BLUE=$'\033[36m'; PINK=$'\033[35m'; OFF=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; PINK=""; OFF=""
fi

RULE="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
STEP_TOTAL=4

banner() {
    printf '\n%s%s%s\n' "$PINK" "$RULE" "$OFF"
    printf '  %s💞  БОТ ЗНАКОМСТВ%s %s· установка%s\n' "$BOLD" "$OFF" "$DIM" "$OFF"
    printf '%s%s%s\n\n' "$PINK" "$RULE" "$OFF"
}

step()  { printf '\n  %s[%s/%s]%s %s%s%s\n' "$PINK" "$1" "$STEP_TOTAL" "$OFF" "$BOLD" "$2" "$OFF"; }
ok()    { printf '      %s✔%s  %s\n' "$GREEN" "$OFF" "$1"; }
fail()  { printf '      %s✖%s  %s\n' "$RED" "$OFF" "$1"; }
warn()  { printf '      %s!%s  %s\n' "$YELLOW" "$OFF" "$1"; }
info()  { printf '      %s•%s  %s\n' "$BLUE" "$OFF" "$1"; }
hint()  { printf '         %s%s%s\n' "$DIM" "$1" "$OFF"; }

die() {
    printf '\n%s%s%s\n' "$RED" "$RULE" "$OFF"
    fail "$1"
    shift
    for line in "$@"; do hint "$line"; done
    printf '\n      %sПолный лог:%s %s\n\n' "$DIM" "$OFF" "$LOG_FILE"
    exit 1
}

# Крутилка на время долгих шагов
spin() {
    local message="$1"; shift
    local frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' i=0
    "$@" >>"$LOG_FILE" 2>&1 &
    local pid=$!
    if [ -t 1 ]; then
        while kill -0 "$pid" 2>/dev/null; do
            i=$(( (i + 1) % ${#frames} ))
            printf '\r      %s%s%s  %s' "$PINK" "${frames:$i:1}" "$OFF" "$message"
            sleep 0.1
        done
        printf '\r%*s\r' $(( ${#message} + 12 )) ''
    else
        printf '      …  %s\n' "$message"
        wait "$pid"
    fi
    wait "$pid"
}

# ──────────────────────────────── Шаги ──────────────────────────────────────

banner

# 1. Исходники
step 1 "Исходники"
if [ -f "requirements.txt" ] && [ -f "main.py" ]; then
    ok "Проект уже здесь: $(pwd)"
else
    command -v git >/dev/null 2>&1 || die "Не найден git." \
        "Установите его: sudo apt install git"
    if [ -d "$PROJECT_DIR" ]; then
        cd "$PROJECT_DIR" || die "Не могу войти в папку $PROJECT_DIR"
        spin "Обновляю проект…" git pull --ff-only
        ok "Проект обновлён: $(pwd)"
    else
        spin "Скачиваю проект…" git clone --depth 1 "$REPO_URL" "$PROJECT_DIR"
        cd "$PROJECT_DIR" || die "Не удалось скачать проект" \
            "Проверьте доступ к $REPO_URL"
        ok "Проект скачан: $(pwd)"
    fi
fi

# 2. Python
step 2 "Python"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        minor=$("$candidate" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)
        major=$("$candidate" -c 'import sys; print(sys.version_info.major)' 2>/dev/null || echo 0)
        if [ "$major" = "3" ] && [ "$minor" -ge "$MIN_PY_MINOR" ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done
[ -n "$PYTHON" ] || die "Нужен Python 3.${MIN_PY_MINOR} или новее." \
    "Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip" \
    "macOS:         brew install python"
ok "$("$PYTHON" --version)"

# 3. Окружение и зависимости
step 3 "Зависимости"
if [ -d ".venv" ]; then
    ok "Виртуальное окружение уже есть"
else
    spin "Создаю виртуальное окружение…" "$PYTHON" -m venv .venv
    [ -d ".venv" ] || die "Не удалось создать виртуальное окружение." \
        "Скорее всего не хватает пакета venv:" \
        "sudo apt install python3-venv"
    ok "Виртуальное окружение создано"
fi

VENV_PY=".venv/bin/python"
[ -x "$VENV_PY" ] || VENV_PY=".venv/Scripts/python.exe"   # Windows / Git Bash
[ -x "$VENV_PY" ] || die "Внутри .venv нет интерпретатора." \
    "Удалите папку .venv и запустите установку заново."

spin "Обновляю pip…" "$VENV_PY" -m pip install --upgrade pip
spin "Ставлю библиотеки (aiogram, Pillow, redis)…" \
    "$VENV_PY" -m pip install -r requirements.txt
"$VENV_PY" -c "import aiogram, PIL, redis, aiosqlite" >>"$LOG_FILE" 2>&1 \
    || die "Библиотеки установились не полностью." \
           "Посмотрите лог — обычно не хватает компилятора или доступа в сеть."
ok "Библиотеки на месте"

# 4. Настройка
step 4 "Настройка"
info "Сейчас мастер спросит токен бота и ваш ID."
printf '\n'
DATING_BOT_FROM_INSTALLER=1 "$VENV_PY" setup.py
SETUP_CODE=$?

if [ "$SETUP_CODE" -ne 0 ]; then
    printf '\n'
    warn "Мастер настройки не завершён."
    hint "Запустить его снова: $VENV_PY setup.py"
    printf '\n'
    exit "$SETUP_CODE"
fi

# ─────────────────────────────── Готово ─────────────────────────────────────

printf '%s%s%s\n' "$GREEN" "$RULE" "$OFF"
printf '  %s✔  Всё готово%s\n' "$BOLD" "$OFF"
printf '%s%s%s\n\n' "$GREEN" "$RULE" "$OFF"

printf '  %sЗапустить сейчас:%s\n' "$BOLD" "$OFF"
printf '     %s%s main.py%s\n\n' "$BLUE" "$VENV_PY" "$OFF"
printf '  %sПроверить, что всё работает:%s\n' "$BOLD" "$OFF"
printf '     %s%s tests/smoke_test.py%s\n\n' "$BLUE" "$VENV_PY" "$OFF"
printf '  %sЗапуск как служба (чтобы работал всегда):%s\n' "$BOLD" "$OFF"
printf '     %sсм. раздел «Запуск на сервере» в README.md%s\n\n' "$DIM" "$OFF"

# Предлагаем запуск только человеку за терминалом: в автоматическом прогоне
# (CI, «| bash» без tty) молча стартовать бота нельзя.
if [ -t 0 ]; then
    printf '  %sЗапустить бота прямо сейчас? (Y/n)%s ' "$BOLD" "$OFF"
    if read -r answer; then
        case "${answer:-y}" in
            [YyДд]*|"")
                printf '\n  %sЗапускаю. Остановить — Ctrl+C%s\n\n' "$DIM" "$OFF"
                exec "$VENV_PY" main.py
                ;;
        esac
    fi
    printf '\n  Хорошо. Команда для запуска выше 👆\n\n'
fi
