#!/usr/bin/env bash
#
# Бот знакомств: установка и обновление одной командой.
#
#   curl -fsSL https://raw.githubusercontent.com/zfd430792-coder/Bot-/HEAD/install.sh | bash
#
# На чистом сервере Ubuntu/Debian скрипт сам ставит git, Python с venv и Redis,
# скачивает проект, ставит библиотеки, спрашивает токен и ID администратора и
# регистрирует бота службой systemd: он переживает закрытие SSH и сам
# поднимается после перезагрузки.
#
# Повторный запуск той же команды — обновление: свежий код с GitHub,
# доустановка библиотек и перезапуск службы. Настройки (.env) и база
# (data/bot.db) остаются как были.
#
#   … | bash -s -- --reconfigure    пройти мастер настройки заново
#
# Необязательные переменные окружения:
#   BOT_DIR      папка проекта (по умолчанию та, где бот уже стоит, иначе ~/Bot-)
#   BOT_SERVICE  имя службы systemd (dating-bot)
#   BOT_BRANCH   ветка репозитория (по умолчанию основная)
#   BOT_REPO     адрес git-репозитория
#
# Весь код разложен по функциям и запускается последней строкой: если
# загрузка через curl оборвётся, bash не выполнит обрывок скрипта.

# Через sh (dash) скрипт сломается на первой bash-конструкции — говорим прямо
if [ -z "${BASH_VERSION:-}" ]; then
    echo "Запустите через bash:  curl -fsSL <ссылка> | bash" >&2
    exit 1
fi

set -uo pipefail

REPO_URL="${BOT_REPO:-https://github.com/zfd430792-coder/Bot-.git}"
INSTALL_URL="https://raw.githubusercontent.com/zfd430792-coder/Bot-/HEAD/install.sh"
SERVICE="${BOT_SERVICE:-dating-bot}"
MIN_PY_MINOR=10
STEP_TOTAL=5

# Путь к самому скрипту есть только при запуске файлом (bash install.sh)
SELF="${BASH_SOURCE[0]:-}"

FORCE_CONFIG=0      # --reconfigure
IS_ROOT=0
CAN_ROOT=0          # root или рабочий sudo
SUDO_HINT=""        # «sudo » в подсказках для обычного пользователя
LOCAL_REDIS=1       # бот смотрит на Redis этой машины
PYTHON=""
PY_VER=""
VENV_PY=""
INSTALLED=()        # что поставили из пакетов в этот запуск
CHANGED=0           # код, библиотеки, настройки или служба поменялись
FIRST_CONFIG=0      # мастер настройки отработал в этот запуск
MODE=""             # service | manual
BOT_NAME=""
LOG_FILE=""
LOG_MARK=0          # с какой строки лога начался текущий шаг

# ─────────────────────────────── Оформление ─────────────────────────────────

RULE="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
# Кадры — элементами массива: срез строки ломает многобайтные символы
SPIN_FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)

setup_colors() {
    if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ] && [ -z "${NO_COLOR:-}" ]; then
        BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
        YELLOW=$'\033[33m'; BLUE=$'\033[36m'; PINK=$'\033[35m'; OFF=$'\033[0m'
    else
        BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; PINK=""; OFF=""
    fi
}

banner() {
    printf '\n%s%s%s\n' "$PINK" "$RULE" "$OFF"
    printf '  %s💞  БОТ ЗНАКОМСТВ%s %s· установка и обновление%s\n' "$BOLD" "$OFF" "$DIM" "$OFF"
    printf '%s%s%s\n\n' "$PINK" "$RULE" "$OFF"
}

step() {
    LOG_MARK=$(wc -l <"$LOG_FILE")
    printf '\n  %s[%s/%s]%s %s%s%s\n' "$PINK" "$1" "$STEP_TOTAL" "$OFF" "$BOLD" "$2" "$OFF"
}
ok()    { printf '      %s✔%s  %s\n' "$GREEN" "$OFF" "$1"; }
fail()  { printf '      %s✖%s  %s\n' "$RED" "$OFF" "$1"; }
warn()  { printf '      %s!%s  %s\n' "$YELLOW" "$OFF" "$1"; }
info()  { printf '      %s•%s  %s\n' "$BLUE" "$OFF" "$1"; }
hint()  { printf '         %s%s%s\n' "$DIM" "$1" "$OFF"; }
row()   { printf '  %s%s%s\n     %s%s%s\n\n' "$BOLD" "$1" "$OFF" "$BLUE" "$2" "$OFF"; }

usage() {
    cat <<EOF
Установка и обновление бота знакомств.

  curl -fsSL $INSTALL_URL | bash
  curl -fsSL $INSTALL_URL | bash -s -- --reconfigure

  --reconfigure   заново пройти мастер настройки (токен, админы, Redis)
  --help          эта справка
EOF
}

# ──────────────────────────────── Лог ───────────────────────────────────────

# Лог лежит не в /tmp: тот чистится при перезагрузке, и причина сбоя
# пропадает вместе с ним
open_log() {
    if [ "$(id -u)" -eq 0 ]; then
        LOG_FILE="/var/log/dating-bot-install.log"
    else
        LOG_FILE="$HOME/dating-bot-install.log"
    fi
    { : >>"$LOG_FILE"; } 2>/dev/null || LOG_FILE="$(mktemp)"
    printf '\n===== %s · install.sh %s =====\n' "$(date '+%F %T')" "$*" >>"$LOG_FILE"
    LOG_MARK=$(wc -l <"$LOG_FILE")
}

# Хвост лога текущего шага — причина сбоя видна сразу, без поиска по файлам.
# Только этого шага: вывод прошлых команд к сбою отношения не имеет.
show_log_tail() {
    local tail_text
    tail_text=$(tail -n "+$((LOG_MARK + 1))" "$LOG_FILE" 2>/dev/null | tail -n 15)
    [ -n "$tail_text" ] || return 0
    printf '\n      %sЧто именно пошло не так:%s\n' "$DIM" "$OFF"
    printf '%s\n' "$tail_text" | sed 's/^/        /'
}

die() {
    printf '\n%s%s%s\n' "$RED" "$RULE" "$OFF"
    fail "$1"
    shift
    local line
    for line in "$@"; do hint "$line"; done
    show_log_tail
    printf '\n      %sПолный лог:%s %s\n\n' "$DIM" "$OFF" "$LOG_FILE"
    exit 1
}

# Долгая команда: вывод уходит в лог, на экране крутилка.
# Код возврата — честный код самой команды.
spin() {
    local message="$1"; shift
    printf '\n$ %s\n' "$*" >>"$LOG_FILE"
    "$@" </dev/null >>"$LOG_FILE" 2>&1 &
    local pid=$! i=0
    if [ -t 1 ]; then
        while kill -0 "$pid" 2>/dev/null; do
            printf '\r      %s%s%s  %s' "$PINK" "${SPIN_FRAMES[i++ % 10]}" "$OFF" "$message"
            sleep 0.1
        done
        printf '\r\033[K'
    else
        printf '      …  %s\n' "$message"
    fi
    wait "$pid"
}

# ─────────────────────────────── Окружение ──────────────────────────────────

tty_available() { ( : </dev/tty ) 2>/dev/null; }
has_systemd()   { [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; }
is_project()    { [ -f "$1/main.py" ] && [ -f "$1/requirements.txt" ] && [ -d "$1/app" ]; }

as_root() {
    if [ "$IS_ROOT" = 1 ]; then "$@"; else sudo "$@"; fi
}

check_privileges() {
    if [ "$(id -u)" -eq 0 ]; then
        IS_ROOT=1; CAN_ROOT=1
        return
    fi
    SUDO_HINT="sudo "
    command -v sudo >/dev/null 2>&1 || return 0
    if sudo -n true 2>/dev/null; then
        CAN_ROOT=1
    elif tty_available; then
        info "Для пакетов и службы нужны права администратора — sudo спросит пароль."
        sudo -v </dev/tty && CAN_ROOT=1
    fi
}

need_root() {
    [ "$CAN_ROOT" = 1 ] && return 0
    die "Нужно поставить системные пакеты, а прав администратора нет." \
        "Запустите установку от root или попросите администратора выполнить:" \
        "sudo $1"
}

# Где стоит (или будет стоять) бот
resolve_bot_dir() {
    if [ -n "${BOT_DIR:-}" ]; then
        case "$BOT_DIR" in /*) ;; *) BOT_DIR="$PWD/$BOT_DIR" ;; esac
        return
    fi
    local dir
    if [ "$(basename -- "$SELF")" = "install.sh" ] && [ -f "$SELF" ]; then
        dir=$(cd "$(dirname -- "$SELF")" && pwd)
        if is_project "$dir"; then BOT_DIR="$dir"; return; fi
    fi
    if is_project "$PWD"; then BOT_DIR="$PWD"; return; fi
    if has_systemd; then
        dir=$(systemctl show -p WorkingDirectory --value "$SERVICE" 2>/dev/null)
        if [ -n "$dir" ]; then BOT_DIR="$dir"; return; fi
    fi
    BOT_DIR="$HOME/Bot-"
}

# Значение из .env — по тем же правилам, что в app/config.py:
# первое вхождение, кавычки по краям снимаются
env_value() {
    [ -f "$BOT_DIR/.env" ] || return 1
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$BOT_DIR/.env" \
        | head -n 1 | tr -d '\r' \
        | sed -e 's/[[:space:]]*$//' -e "s/^[\"']//" -e "s/[\"']\$//"
}

env_configured() {
    local token admins
    token=$(env_value BOT_TOKEN) || return 1
    admins=$(env_value ADMIN_IDS) || return 1
    [[ "$token" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]] && [[ "$admins" =~ [0-9] ]]
}

# Локальным Redis установщик управляет, только если бот смотрит на
# localhost:6379 без пароля. Чужой или запароленный Redis — забота хозяина.
detect_local_redis() {
    local url rest host port
    url=$(env_value REDIS_URL 2>/dev/null)
    url="${url:-redis://localhost:6379/0}"
    rest="${url#*://}"
    rest="${rest%%/*}"
    LOCAL_REDIS=0
    case "$rest" in *@*) return ;; esac
    host="${rest%%:*}"
    port="${rest##*:}"
    [ "$port" = "$rest" ] && port=6379
    case "$host" in
        localhost|127.0.0.1) [ "$port" = 6379 ] && LOCAL_REDIS=1 ;;
    esac
}

# PING прямо из bash — redis-cli может и не стоять
redis_ping() {
    (
        exec 3<>/dev/tcp/127.0.0.1/6379 || exit 1
        printf 'PING\r\n' >&3
        IFS= read -r -t 3 reply <&3 || exit 1
        [ "${reply%$'\r'}" = "+PONG" ]
    ) 2>/dev/null
}

find_python() {
    local candidate version
    # Системный python3 первым: для него в репозитории точно есть venv
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        version=$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) \
            || continue
        case "$version" in
            3.*) [ "${version#3.}" -ge "$MIN_PY_MINOR" ] 2>/dev/null || continue ;;
            *) continue ;;
        esac
        PYTHON="$candidate"
        PY_VER="$version"
        return 0
    done
    return 1
}

# ───────────────────────────── 1. Система ───────────────────────────────────

pkg_installed() {
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

# Без вопросов: ни debconf, ни needrestart не должны ждать ответа,
# а занятый автообновлениями dpkg — повод подождать, а не упасть
apt_get() {
    as_root env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 NEEDRESTART_MODE=a \
        apt-get -y -q -o DPkg::Lock::Timeout=300 \
        -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$@"
}

APT_UPDATED=0
apt_install() {
    need_root "apt install -y $*"
    if [ "$APT_UPDATED" = 0 ]; then
        spin "Обновляю список пакетов…" apt_get update \
            || warn "Список пакетов обновился с ошибками — пробую ставить как есть"
        APT_UPDATED=1
    fi
    spin "Ставлю $*…" apt_get install --no-install-recommends "$@"
}

ensure_redis() {
    if redis_ping; then
        ok "Redis отвечает"
        return
    fi
    need_root "systemctl enable --now redis-server"
    if has_systemd; then
        spin "Запускаю Redis…" as_root systemctl enable --now redis-server
    else
        # Контейнер или WSL без systemd: поднимаем вручную, без автозапуска
        local dir=/var/lib/redis
        [ -d "$dir" ] || dir=/tmp
        spin "Запускаю Redis…" as_root redis-server --daemonize yes \
            --bind 127.0.0.1 --port 6379 --dir "$dir"
    fi
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        redis_ping && break
        sleep 1
    done
    redis_ping || die "Redis установлен, но не отвечает." \
        "Состояние службы: ${SUDO_HINT}systemctl status redis-server"
    if has_systemd; then
        ok "Redis запущен и включён в автозагрузку"
    else
        ok "Redis запущен (без systemd сам после перезагрузки не поднимется)"
    fi
}

# Не Debian/Ubuntu: ставить не умеем, но проверяем и говорим, чего не хватает
check_prerequisites() {
    local missing=()
    command -v git >/dev/null 2>&1 || missing+=("git")
    find_python || missing+=("Python 3.${MIN_PY_MINOR}+")
    if [ ${#missing[@]} -gt 0 ]; then
        die "Не хватает: ${missing[*]}." \
            "Сам ставить пакеты установщик умеет на Ubuntu и Debian." \
            "Поставьте недостающее и запустите команду ещё раз."
    fi
    ok "git и Python ${PY_VER} на месте"
    if [ "$LOCAL_REDIS" = 1 ]; then
        if redis_ping; then
            ok "Redis отвечает"
        else
            warn "Redis не отвечает — мастер настройки предложит, как быть"
        fi
    fi
}

step_system() {
    step 1 "Системные пакеты"

    if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg-query >/dev/null 2>&1; then
        check_prerequisites
        return
    fi

    local want=(git ca-certificates python3) missing=() pkg
    [ "$LOCAL_REDIS" = 1 ] && want+=(redis-server)
    for pkg in "${want[@]}"; do
        pkg_installed "$pkg" || missing+=("$pkg")
    done
    if [ ${#missing[@]} -gt 0 ]; then
        apt_install "${missing[@]}" || die "Не удалось поставить: ${missing[*]}." \
            "Частые причины — нет интернета или кончилось место: df -h /"
        INSTALLED+=("${missing[@]}")
    fi

    find_python || die "Нужен Python 3.${MIN_PY_MINOR} или новее, а в системе $(python3 --version 2>&1)." \
        "Подходят Ubuntu 22.04+ и Debian 12+."

    # venv в Debian/Ubuntu — отдельный пакет. Без него окружение создаётся
    # наполовину, без pip: именно на этом падала установка на чистом сервере.
    local venv_pkg="python${PY_VER}-venv"
    apt-cache show "$venv_pkg" >/dev/null 2>&1 || venv_pkg="python3-venv"
    if ! pkg_installed "$venv_pkg"; then
        apt_install "$venv_pkg" || die "Не удалось поставить $venv_pkg."
        INSTALLED+=("$venv_pkg")
    fi

    if [ ${#INSTALLED[@]} -gt 0 ]; then
        ok "Поставлено: ${INSTALLED[*]}"
    else
        ok "git и Python ${PY_VER} с venv уже есть"
    fi

    if [ "$LOCAL_REDIS" = 1 ]; then
        ensure_redis
    fi
}

# ─────────────────────────────── 2. Код ─────────────────────────────────────

# safe.directory: git не откажется работать, если папка принадлежит
# другому пользователю (установка через sudo)
git_in() { git -C "$BOT_DIR" -c safe.directory="$BOT_DIR" "$@"; }

clone_repo() {
    local args=(clone --quiet)
    [ -n "${BOT_BRANCH:-}" ] && args+=(--branch "$BOT_BRANCH")
    mkdir -p "$(dirname -- "$BOT_DIR")" 2>/dev/null
    spin "Скачиваю проект…" git "${args[@]}" "$REPO_URL" "$BOT_DIR" \
        || die "Не удалось скачать проект." \
               "Проверьте доступ к GitHub: curl -I https://github.com"
    CHANGED=1
    ok "Проект скачан: $BOT_DIR ($(git_in rev-parse --short HEAD))"
}

update_repo() {
    local before after current target
    before=$(git_in rev-parse --short HEAD 2>/dev/null)

    spin "Проверяю обновления…" git_in fetch --prune origin \
        || die "Не удалось связаться с репозиторием." \
               "Проверьте доступ к GitHub: curl -I https://github.com"
    git_in remote set-head origin --auto >>"$LOG_FILE" 2>&1

    # Остаёмся на своей ветке, пока она есть на GitHub; иначе — основная
    current=$(git_in symbolic-ref --quiet --short HEAD 2>/dev/null)
    if [ -n "${BOT_BRANCH:-}" ]; then
        target="$BOT_BRANCH"
    elif [ -n "$current" ] && git_in show-ref --verify --quiet "refs/remotes/origin/$current"; then
        target="$current"
    else
        target=$(git_in symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
        target="${target#origin/}"
    fi
    if [ -z "$target" ] || ! git_in show-ref --verify --quiet "refs/remotes/origin/$target"; then
        die "Не нашёл в репозитории ветку ${target:-по умолчанию}."
    fi

    # Правки, сделанные прямо на сервере, не теряем, а откладываем в сторону
    if [ -n "$(git_in status --porcelain --untracked-files=no 2>/dev/null)" ]; then
        local label
        label="install.sh $(date '+%F %H:%M')"
        git_in -c user.name=install.sh -c user.email=install@localhost \
            stash push --quiet -m "$label" >>"$LOG_FILE" 2>&1 \
            || die "Не удалось отложить локальные правки в коде."
        warn "Правки в коде на сервере отложены в git stash («$label»)"
        hint "Вернуть их: cd $BOT_DIR && git stash pop"
    fi
    if [ -n "$before" ] && ! git_in merge-base --is-ancestor HEAD "origin/$target" 2>/dev/null; then
        local backup
        backup="backup-$(date +%Y%m%d-%H%M%S)"
        git_in branch "$backup" >>"$LOG_FILE" 2>&1
        warn "Локальные коммиты сохранены в ветке $backup"
    fi

    git_in checkout --quiet -B "$target" "origin/$target" >>"$LOG_FILE" 2>&1 \
        || die "Не удалось переключиться на свежую версию."
    git_in branch --quiet --set-upstream-to="origin/$target" >>"$LOG_FILE" 2>&1

    after=$(git_in rev-parse --short HEAD)
    if [ "$before" = "$after" ]; then
        ok "Уже последняя версия ($after)"
        return
    fi
    CHANGED=1
    ok "Обновлено: ${before:-?} → $after"
    if [ -n "$before" ]; then
        git_in log --no-merges --format='%s' "$before..$after" 2>/dev/null | head -n 8 \
            | while IFS= read -r subject; do hint "• $subject"; done
    fi
}

step_source() {
    step 2 "Код бота"
    if [ -d "$BOT_DIR/.git" ]; then
        update_repo
    elif is_project "$BOT_DIR"; then
        warn "Проект в $BOT_DIR без git — обновлять нечем, ставлю как есть"
    elif [ -e "$BOT_DIR" ] && [ -n "$(ls -A "$BOT_DIR" 2>/dev/null)" ]; then
        die "Папка $BOT_DIR уже занята чем-то другим." \
            "Укажите другую:  curl -fsSL $INSTALL_URL | BOT_DIR=/путь/к/папке bash"
    else
        clone_repo
    fi
}

# ───────────────────────── 3. Python-окружение ──────────────────────────────

venv_python() {
    if [ -x "$BOT_DIR/.venv/Scripts/python.exe" ] && [ ! -x "$BOT_DIR/.venv/bin/python" ]; then
        echo "$BOT_DIR/.venv/Scripts/python.exe"      # Windows / Git Bash
    else
        echo "$BOT_DIR/.venv/bin/python"
    fi
}

venv_ok()     { "$VENV_PY" -m pip --version >/dev/null 2>&1; }
imports_ok()  { "$VENV_PY" -c 'import aiogram, aiosqlite, redis, PIL' >>"$LOG_FILE" 2>&1; }
pip_install() { "$VENV_PY" -m pip install --disable-pip-version-check -r "$BOT_DIR/requirements.txt"; }

file_hash() {
    "$VENV_PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$1"
}

install_requirements() {
    local stamp="$BOT_DIR/.venv/.requirements.sha256" want have
    want=$(file_hash "$BOT_DIR/requirements.txt" 2>/dev/null)
    have=$(cat "$stamp" 2>/dev/null)
    if [ -n "$want" ] && [ "$want" = "$have" ] && imports_ok; then
        ok "Библиотеки на месте"
        return
    fi

    if ! spin "Ставлю библиотеки (aiogram, Pillow, redis)…" pip_install; then
        # Готовых сборок Pillow нет только на редких платформах — там нужен компилятор
        if command -v apt-get >/dev/null 2>&1 && [ "$CAN_ROOT" = 1 ]; then
            { apt_install build-essential "python${PY_VER}-dev" libjpeg-dev zlib1g-dev \
                && spin "Повторяю установку библиотек…" pip_install; } \
                || die "Не удалось установить библиотеки."
        else
            die "Не удалось установить библиотеки." \
                "Ошибки сети — проверьте доступ: curl -I https://pypi.org" \
                "Ошибки сборки — нужны build-essential python3-dev libjpeg-dev zlib1g-dev"
        fi
    fi
    imports_ok || die "Библиотеки установились, но не импортируются." \
        "Пересоздайте окружение: rm -rf $BOT_DIR/.venv — и запустите установку снова."
    printf '%s\n' "$want" >"$stamp"
    CHANGED=1
    ok "Библиотеки установлены"
}

step_python() {
    step 3 "Python-окружение"
    VENV_PY=$(venv_python)

    if [ -d "$BOT_DIR/.venv" ] && ! venv_ok; then
        warn "Окружение .venv повреждено — создаю заново"
        rm -rf "$BOT_DIR/.venv"
    fi

    if [ -d "$BOT_DIR/.venv" ]; then
        ok "Окружение на месте: $("$VENV_PY" --version 2>&1)"
    else
        [ -n "$PYTHON" ] || find_python || die "Не найден Python 3.${MIN_PY_MINOR}+."
        spin "Создаю виртуальное окружение…" "$PYTHON" -m venv "$BOT_DIR/.venv"
        VENV_PY=$(venv_python)
        if ! venv_ok; then
            rm -rf "$BOT_DIR/.venv"
            die "Не удалось создать виртуальное окружение с pip." \
                "На Debian/Ubuntu не хватает пакета: apt install -y python${PY_VER}-venv"
        fi
        spin "Обновляю pip…" "$VENV_PY" -m pip install --quiet --upgrade pip \
            || warn "pip не обновился — продолжаю с тем, что есть"
        ok "Окружение создано: $("$VENV_PY" --version 2>&1)"
    fi

    install_requirements
}

# ───────────────────────────── 4. Настройка ─────────────────────────────────

step_config() {
    step 4 "Настройка"
    if [ "$FORCE_CONFIG" = 0 ] && env_configured; then
        ok "Токен и администраторы уже заданы ($BOT_DIR/.env)"
        return
    fi

    # При «curl | bash» stdin занят самим скриптом — мастер читает с терминала
    local input=""
    if [ -t 0 ]; then
        input="stdin"
    elif tty_available; then
        input="tty"
    else
        die "Нужно ввести токен бота, а терминала нет." \
            "Запустите ту же команду в обычном SSH-сеансе — мастер спросит всё по шагам."
    fi

    # Работающий бот сам забирает сообщения — мастер не поймал бы ваш ID
    local stopped=0
    if has_systemd && [ "$CAN_ROOT" = 1 ] && systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        as_root systemctl stop "$SERVICE" >>"$LOG_FILE" 2>&1 && stopped=1
        info "Бот остановлен на время настройки"
    fi

    info "Мастер спросит токен бота и ваш Telegram ID."
    printf '\n'
    if [ "$input" = "tty" ]; then
        (cd "$BOT_DIR" && DATING_BOT_FROM_INSTALLER=1 "$VENV_PY" setup.py </dev/tty)
    else
        (cd "$BOT_DIR" && DATING_BOT_FROM_INSTALLER=1 "$VENV_PY" setup.py)
    fi
    local code=$?

    if [ "$code" -ne 0 ]; then
        printf '\n'
        warn "Настройка не завершена."
        if [ "$stopped" = 1 ]; then
            as_root systemctl start "$SERVICE" >>"$LOG_FILE" 2>&1
            hint "Бот снова запущен с прежними настройками."
        fi
        hint "Продолжить: запустите ту же команду ещё раз."
        printf '\n'
        exit "$code"
    fi
    env_configured || die "Мастер не записал токен и ID администратора." \
        "Проверьте файл $BOT_DIR/.env"
    CHANGED=1
    FIRST_CONFIG=1
}

# ────────────────────────────── 5. Запуск ───────────────────────────────────

unit_path() { echo "/etc/systemd/system/$SERVICE.service"; }

# Код 0 — файл службы пришлось создать или поменять
write_unit() {
    local owner exec_py tmp
    owner=$(stat -c %U "$BOT_DIR" 2>/dev/null || echo root)
    exec_py="$VENV_PY"
    case "$exec_py" in *" "*) exec_py="\"$exec_py\"" ;; esac
    tmp=$(mktemp)
    {
        echo "# Создано install.sh бота знакомств и перезаписывается при обновлении."
        echo "# Свои настройки добавляйте через: systemctl edit $SERVICE"
        echo "[Unit]"
        echo "Description=Telegram dating bot"
        if [ "$LOCAL_REDIS" = 1 ]; then
            echo "After=network-online.target redis-server.service"
            echo "Wants=network-online.target redis-server.service"
        else
            echo "After=network-online.target"
            echo "Wants=network-online.target"
        fi
        echo
        echo "[Service]"
        echo "Type=simple"
        if [ "$owner" != "root" ]; then echo "User=$owner"; fi
        echo "WorkingDirectory=$BOT_DIR"
        echo "ExecStart=$exec_py main.py"
        echo "Environment=PYTHONUNBUFFERED=1"
        echo "Restart=always"
        echo "RestartSec=5"
        echo
        echo "[Install]"
        echo "WantedBy=multi-user.target"
    } >"$tmp"
    if cmp -s "$tmp" "$(unit_path)"; then
        rm -f "$tmp"
        return 1
    fi
    as_root install -m 644 "$tmp" "$(unit_path)" || die "Не удалось записать $(unit_path)"
    rm -f "$tmp"
    return 0
}

# .env правили руками после запуска бота — значит, нужен перезапуск
env_changed_since_start() {
    local started
    started=$(systemctl show -p ActiveEnterTimestamp --value "$SERVICE" 2>/dev/null)
    [ -n "$started" ] || return 0
    started=$(date -d "$started" +%s 2>/dev/null) || return 0
    [ "$(stat -c %Y "$BOT_DIR/.env" 2>/dev/null || echo 0)" -gt "$started" ]
}

# Журнал службы с момента перезапуска — только новый запуск, без остановки старого
service_log() {
    as_root journalctl -u "$SERVICE" --since "@$1" -o cat --no-pager 2>/dev/null \
        | sed -n '/^Started /,$p'
}

bot_name_from() {
    printf '%s\n' "$1" | sed -n 's/.*Run polling for bot @\([A-Za-z0-9_]*\).*/\1/p' | tail -n 1
}

explain_failure() {
    case "$1" in
        *TelegramUnauthorizedError*|*Unauthorized*)
            hint "Telegram не принял токен — возможно, его отозвали в @BotFather."
            hint "Задать новый: curl -fsSL $INSTALL_URL | bash -s -- --reconfigure" ;;
        *"Redis недоступен"*)
            hint "Не отвечает Redis: ${SUDO_HINT}systemctl status redis-server" ;;
        *"BOT_TOKEN не задан"*|*"ADMIN_IDS не заданы"*)
            hint "В .env не хватает токена или админов."
            hint "Настроить: curl -fsSL $INSTALL_URL | bash -s -- --reconfigure" ;;
        *ModuleNotFoundError*|*ImportError*)
            hint "Не хватает библиотеки: rm -rf $BOT_DIR/.venv — и запустите установку снова." ;;
        *TelegramNetworkError*|*ClientConnectorError*|*"Cannot connect to host"*)
            hint "Нет связи с Telegram: curl -I https://api.telegram.org" ;;
    esac
}

wait_for_start() {
    local since="$1" log="" i=0 started=0 grace=-1
    [ -t 1 ] || printf '      …  Запускаю бота…\n'
    # До минуты: первый старт после загрузки сервера бывает небыстрым
    while [ "$i" -lt 120 ]; do
        if [ -t 1 ]; then
            printf '\r      %s%s%s  Запускаю бота…' "$PINK" "${SPIN_FRAMES[i % 10]}" "$OFF"
        fi
        sleep 0.5
        i=$((i + 1))
        log=$(service_log "$since")
        case "$log" in
            *"Start polling"*) started=1; break ;;
            *"Main process exited"*|*"Failed with result"*) break ;;
            *"Traceback (most recent call last)"*)
                # Трейсбек до старта — почти наверняка падение, даже если
                # процесс не вышел. Три секунды ждём: вдруг всё же поднимется.
                if [ "$grace" -lt 0 ]; then
                    grace=6
                elif [ "$grace" -eq 0 ]; then
                    break
                else
                    grace=$((grace - 1))
                fi
                ;;
        esac
    done
    if [ -t 1 ]; then printf '\r\033[K'; fi

    if [ "$started" = 1 ]; then
        BOT_NAME=$(bot_name_from "$log")
        ok "Бот запущен${BOT_NAME:+: @$BOT_NAME}"
        # Вторая копия с тем же токеном проявляется только на первых запросах
        sleep 3
        case "$(service_log "$since")" in
            *TelegramConflictError*|*"terminated by other getUpdates"*)
                warn "Этим токеном пользуется ещё одна копия бота — на компьютере или другом сервере."
                hint "Остановите её, иначе копии будут отбирать друг у друга сообщения."
                ;;
        esac
        return 0
    fi

    printf '\n%s%s%s\n' "$RED" "$RULE" "$OFF"
    if [ "$i" -ge 120 ]; then
        fail "Бот не подтвердил запуск за минуту."
    else
        fail "Бот не запустился."
    fi
    explain_failure "$log"
    # Строки с отступом — кадры трейсбека; суть в неотступленных: логи бота,
    # последняя строка исключения и сообщения systemd
    printf '\n      %sПоследние строки журнала:%s\n' "$DIM" "$OFF"
    printf '%s\n' "$log" | grep -v '^[[:space:]]' | grep -v '^$' | tail -n 15 | sed 's/^/        /'
    printf '\n      %sЖурнал целиком:%s %sjournalctl -u %s -n 100 --no-pager\n\n' \
        "$DIM" "$OFF" "$SUDO_HINT" "$SERVICE"
    exit 1
}

step_service() {
    step 5 "Запуск"
    if ! has_systemd || [ "$CAN_ROOT" != 1 ]; then
        MODE="manual"
        if has_systemd; then
            warn "Без прав администратора бота не сделать службой."
        else
            warn "В системе нет systemd — бота не сделать службой."
        fi
        hint "Запускать вручную: cd $BOT_DIR && .venv/bin/python main.py"
        return
    fi
    MODE="service"

    if write_unit; then
        CHANGED=1
        ok "Служба $SERVICE: автозапуск и перезапуск при сбоях"
    fi
    as_root systemctl daemon-reload >>"$LOG_FILE" 2>&1
    as_root systemctl enable --quiet "$SERVICE" >>"$LOG_FILE" 2>&1 \
        || warn "Не удалось включить автозапуск: systemctl enable $SERVICE"

    if [ "$CHANGED" = 0 ] && systemctl is-active --quiet "$SERVICE" && ! env_changed_since_start; then
        BOT_NAME=$(bot_name_from "$(as_root journalctl -u "$SERVICE" -o cat -n 500 --no-pager 2>/dev/null)")
        ok "Бот уже работает на последней версии — перезапуск не нужен"
        return
    fi

    local since
    since=$(date +%s)
    as_root systemctl restart "$SERVICE" >>"$LOG_FILE" 2>&1
    wait_for_start "$since"
}

# ─────────────────────────────── Готово ─────────────────────────────────────

finish() {
    printf '\n%s%s%s\n' "$GREEN" "$RULE" "$OFF"
    if [ "$MODE" = "service" ]; then
        printf '  %s✔  Бот работает%s%s\n' "$BOLD" "$OFF" "${BOT_NAME:+ · @$BOT_NAME}"
    else
        printf '  %s✔  Установка завершена%s\n' "$BOLD" "$OFF"
    fi
    printf '%s%s%s\n\n' "$GREEN" "$RULE" "$OFF"

    if [ "$MODE" = "service" ]; then
        row "Обновить бота — та же команда:" "curl -fsSL $INSTALL_URL | bash"
        row "Журнал бота:" "${SUDO_HINT}journalctl -u $SERVICE -f"
        row "Перезапустить / остановить:" \
            "${SUDO_HINT}systemctl restart $SERVICE   ·   ${SUDO_HINT}systemctl stop $SERVICE"
        row "Сменить токен или админов:" "curl -fsSL $INSTALL_URL | bash -s -- --reconfigure"
    else
        row "Запустить бота:" "cd $BOT_DIR && .venv/bin/python main.py"
    fi
    if [ "$FIRST_CONFIG" = 1 ] && [ -n "$BOT_NAME" ]; then
        printf '  Откройте %s@%s%s в Telegram и нажмите %s/start%s 🎉\n\n' \
            "$BOLD" "$BOT_NAME" "$OFF" "$BOLD" "$OFF"
    fi
    printf '  %sЛог установки: %s%s\n\n' "$DIM" "$LOG_FILE" "$OFF"
}

main() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            --reconfigure|--config) FORCE_CONFIG=1 ;;
            -h|--help) usage; return 0 ;;
            *) echo "Неизвестный параметр: $arg" >&2; usage >&2; return 2 ;;
        esac
    done

    setup_colors
    open_log "$@"
    banner
    check_privileges
    resolve_bot_dir
    detect_local_redis
    info "Папка бота: $BOT_DIR"

    step_system
    step_source
    step_python
    step_config
    step_service
    finish
}

main "$@"; exit $?
