"""Собственная капча «найди фигуры».

Почему не готовые решения:
* текстовые капчи (искажённые буквы/цифры) распознаются OCR почти со 100%;
* «нажми на кнопку» и «посчитай 2+2» обходятся скриптом за одну строку, потому
  что ответ виден прямо в callback_data;
* готовые библиотеки давно в датасетах решателей.

Что сделано здесь:
1. Ответ существует только на картинке — в сообщении и в кнопках его нет.
2. Номера клеток нарисованы **вектором** (семисегментные цифры), поэтому
   капча не зависит от шрифтов в системе и не похожа ни на один датасет.
3. Номера **перемешаны**: кнопка «7» — это не седьмая клетка, а та, на которой
   нарисована семёрка. Кнопки всегда одни и те же (1–15), их порядок ничего
   не подсказывает.
4. Нужно выбрать **все** подходящие клетки и ни одной лишней: перебор даёт
   шанс ~1/455 (при 3 верных из 15), «выбрать всё» не работает.
5. Учитывается время: мгновенный ответ — признак скрипта, долгий — протухание.
6. Каждая неудача даёт полностью новое задание, после N попыток — блокировка.
"""
from __future__ import annotations

import io
import math
import random
import time
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter

# ── Палитра и фигуры ────────────────────────────────────────────────────────

COLORS: dict[str, tuple[str, tuple[int, int, int]]] = {
    "red":     ("красные",      (223, 60, 60)),
    "blue":    ("синие",        (47, 111, 228)),
    "green":   ("зелёные",      (47, 168, 79)),
    "yellow":  ("жёлтые",       (232, 185, 46)),
    "purple":  ("фиолетовые",   (140, 75, 216)),
    "cyan":    ("бирюзовые",    (23, 191, 191)),
}

SHAPES: dict[str, str] = {
    "circle":   "круги",
    "square":   "квадраты",
    "triangle": "треугольники",
    "star":     "звёзды",
    "hexagon":  "шестиугольники",
    "cross":    "кресты",
    "heart":    "сердца",
}

# Пары, которые на небольшой картинке путает живой человек. В одном задании
# они не встречаются: иначе капча проверяет не «человек ли ты», а зрение.
CONFUSABLE_COLORS = (
    frozenset({"green", "cyan"}),     # зелёный и бирюзовый
    frozenset({"blue", "cyan"}),
    frozenset({"blue", "purple"}),
)
CONFUSABLE_SHAPES = (
    frozenset({"circle", "hexagon"}), # шестиугольник легко принять за круг
)


def _clashing(value: str, pairs: tuple[frozenset[str], ...]) -> set[str]:
    return {other for pair in pairs if value in pair for other in pair if other != value}


GRID_COLS, GRID_ROWS = 5, 3
CELLS = GRID_COLS * GRID_ROWS
CELL = 120
GAP = 4
MARGIN = 10

# ── Семисегментные цифры (без шрифтов) ──────────────────────────────────────

SEGMENTS = {
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
    "5": "afgcd", "6": "afgecd", "7": "abc", "8": "abcdefg", "9": "abcdfg",
}


def _digit_lines(ch: str, x: float, y: float, w: float, h: float) -> list[tuple]:
    mid = y + h / 2
    coords = {
        "a": ((x, y), (x + w, y)),
        "b": ((x + w, y), (x + w, mid)),
        "c": ((x + w, mid), (x + w, y + h)),
        "d": ((x, y + h), (x + w, y + h)),
        "e": ((x, mid), (x, y + h)),
        "f": ((x, y), (x, mid)),
        "g": ((x, mid), (x + w, mid)),
    }
    return [coords[s] for s in SEGMENTS.get(ch, "")]


def _draw_number(draw: ImageDraw.ImageDraw, number: int, x: float, y: float,
                 size: float, color: tuple[int, int, int, int], width: int = 3) -> None:
    """Рисует число семисегментными штрихами — работает на любой системе."""
    digits = str(number)
    dw, dh = size * 0.55, size
    for i, ch in enumerate(digits):
        ox = x + i * (dw + size * 0.28)
        for (x1, y1), (x2, y2) in _digit_lines(ch, ox, y, dw, dh):
            draw.line([(x1, y1), (x2, y2)], fill=color, width=width)


# ── Геометрия фигур ─────────────────────────────────────────────────────────

def _polygon(shape: str, cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    def ring(n: int, start: float = -math.pi / 2, radius: float | None = None):
        rad = r if radius is None else radius
        return [
            (cx + rad * math.cos(start + 2 * math.pi * i / n),
             cy + rad * math.sin(start + 2 * math.pi * i / n))
            for i in range(n)
        ]

    if shape == "circle":
        return ring(40)
    if shape == "square":
        return ring(4, start=-math.pi / 4, radius=r * 1.05)
    if shape == "triangle":
        return ring(3)
    if shape == "hexagon":
        return ring(6)
    if shape == "star":
        pts = []
        for i in range(10):
            rad = r if i % 2 == 0 else r * 0.45
            ang = -math.pi / 2 + math.pi * i / 5
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        return pts
    if shape == "cross":
        t = r * 0.36
        return [
            (cx - t, cy - r), (cx + t, cy - r), (cx + t, cy - t), (cx + r, cy - t),
            (cx + r, cy + t), (cx + t, cy + t), (cx + t, cy + r), (cx - t, cy + r),
            (cx - t, cy + t), (cx - r, cy + t), (cx - r, cy - t), (cx - t, cy - t),
        ]
    if shape == "heart":
        pts = []
        for i in range(44):
            t = 2 * math.pi * i / 44
            hx = 16 * math.sin(t) ** 3
            hy = -(13 * math.cos(t) - 5 * math.cos(2 * t)
                   - 2 * math.cos(3 * t) - math.cos(4 * t))
            pts.append((cx + hx * r / 17, cy + hy * r / 16))
        return pts
    return ring(40)


# Максимальный поворот для каждой фигуры (радианы). Свободно крутить нельзя:
# квадрат на 45° превращается в ромб, а сердце вверх ногами не читается.
MAX_ROTATION = {
    "circle": math.pi,
    "square": math.radians(18),
    "triangle": math.radians(28),
    "star": math.radians(36),
    "hexagon": math.radians(30),
    "cross": math.radians(20),
    "heart": math.radians(16),
}


def _rotate(points: list[tuple[float, float]], cx: float, cy: float,
            angle: float) -> list[tuple[float, float]]:
    ca, sa = math.cos(angle), math.sin(angle)
    return [
        (cx + (px - cx) * ca - (py - cy) * sa, cy + (px - cx) * sa + (py - cy) * ca)
        for px, py in points
    ]


def _jitter(points: list[tuple[float, float]], amount: float) -> list[tuple[float, float]]:
    """Лёгкая «дрожь» контура — ломает шаблонное сравнение картинок."""
    return [(px + random.uniform(-amount, amount), py + random.uniform(-amount, amount))
            for px, py in points]


# ── Задание ─────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class Challenge:
    task: str
    correct: list[int]            # номера клеток (те, что нарисованы), а не позиции
    image: bytes
    created_at: float = field(default_factory=time.monotonic)


def _background(width: int, height: int) -> Image.Image:
    """Мягкий случайный градиент + шум, чтобы не было двух одинаковых картинок."""
    base = random.choice([
        ((248, 249, 252), (226, 232, 246)),
        ((252, 247, 243), (240, 231, 222)),
        ((244, 250, 246), (223, 240, 231)),
        ((250, 246, 252), (235, 226, 246)),
    ])
    (r1, g1, b1), (r2, g2, b2) = base
    img = Image.new("RGB", (width, height), base[0])
    draw = ImageDraw.Draw(img)
    for y in range(height):
        k = y / max(1, height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=(int(r1 + (r2 - r1) * k), int(g1 + (g2 - g1) * k), int(b1 + (b2 - b1) * k)),
        )
    # Точечный шум
    for _ in range(width * height // 260):
        x, y = random.randrange(width), random.randrange(height)
        shade = random.randint(150, 215)
        draw.point((x, y), fill=(shade, shade, shade))
    return img


def _overlay_noise(img: Image.Image) -> Image.Image:
    """Полупрозрачные кривые поверх всей картинки — мешают сегментации."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    w, h = img.size
    for _ in range(random.randint(5, 8)):
        pts = [(random.randrange(w), random.randrange(h)) for _ in range(4)]
        color = (random.randrange(60, 200), random.randrange(60, 200),
                 random.randrange(60, 200), random.randint(28, 55))
        draw.line(pts, fill=color, width=random.randint(2, 5), joint="curve")
    for _ in range(random.randint(14, 24)):
        x, y = random.randrange(w), random.randrange(h)
        rad = random.randint(3, 11)
        draw.ellipse(
            [x - rad, y - rad, x + rad, y + rad],
            outline=(random.randrange(80, 190),) * 3 + (random.randint(30, 60),),
            width=1,
        )
    return Image.alpha_composite(img.convert("RGBA"), layer)


def _warp(img: Image.Image) -> Image.Image:
    """Слабая деформация по общей сетке узлов.

    Смещения считаются один раз для каждого узла и переиспользуются соседними
    ячейками — иначе на швах появляются разрывы и фигуры «ломаются».
    """
    w, h = img.size
    nx, ny = 6, 4
    step_x, step_y = w / nx, h / ny
    jitter = 3.0

    # Узлы на границе не двигаем, чтобы не появились пустые края
    nodes: dict[tuple[int, int], tuple[float, float]] = {}
    for gy in range(ny + 1):
        for gx in range(nx + 1):
            edge = gx in (0, nx) or gy in (0, ny)
            dx = 0.0 if edge else random.uniform(-jitter, jitter)
            dy = 0.0 if edge else random.uniform(-jitter, jitter)
            nodes[(gx, gy)] = (gx * step_x + dx, gy * step_y + dy)

    mesh = []
    for gy in range(ny):
        for gx in range(nx):
            box = (int(gx * step_x), int(gy * step_y),
                   int((gx + 1) * step_x), int((gy + 1) * step_y))
            nw, sw = nodes[(gx, gy)], nodes[(gx, gy + 1)]
            se, ne = nodes[(gx + 1, gy + 1)], nodes[(gx + 1, gy)]
            mesh.append((box, (*nw, *sw, *se, *ne)))
    try:
        return img.transform(img.size, Image.MESH, mesh, Image.BILINEAR)
    except Exception:  # деформация — приятный бонус, но не критичный
        return img


def _pick_cells() -> tuple[str, str, list[tuple[str, str]], int]:
    """Возвращает (цель-фигура, цель-цвет, содержимое клеток, сколько верных)."""
    target_shape = random.choice(list(SHAPES))
    target_color = random.choice(list(COLORS))
    correct_count = random.randint(2, 4)

    cells: list[tuple[str, str]] = [(target_shape, target_color)] * correct_count

    # Ловушки: та же фигура другого цвета и тот же цвет другой фигурой —
    # невнимательный человек ошибётся, а простой классификатор тем более.
    banned_colors = _clashing(target_color, CONFUSABLE_COLORS) | {target_color}
    banned_shapes = _clashing(target_shape, CONFUSABLE_SHAPES) | {target_shape}
    other_colors = [c for c in COLORS if c not in banned_colors]
    other_shapes = [s for s in SHAPES if s not in banned_shapes]
    for _ in range(random.randint(2, 3)):
        cells.append((target_shape, random.choice(other_colors)))
    for _ in range(random.randint(2, 3)):
        cells.append((random.choice(other_shapes), target_color))
    while len(cells) < CELLS:
        cells.append((random.choice(other_shapes), random.choice(other_colors)))

    cells = cells[:CELLS]
    random.shuffle(cells)
    return target_shape, target_color, cells, correct_count


def generate() -> Challenge:
    """Создаёт новое задание. Вызывать в отдельном потоке (asyncio.to_thread)."""
    target_shape, target_color, cells, _ = _pick_cells()

    width = MARGIN * 2 + GRID_COLS * CELL + (GRID_COLS - 1) * GAP
    height = MARGIN * 2 + GRID_ROWS * CELL + (GRID_ROWS - 1) * GAP
    img = _background(width, height)
    draw = ImageDraw.Draw(img, "RGBA")

    # Номера клеток перемешаны — позиция кнопки ничего не подсказывает
    labels = list(range(1, CELLS + 1))
    random.shuffle(labels)

    correct: list[int] = []
    for idx, (shape, color_key) in enumerate(cells):
        col, row = idx % GRID_COLS, idx // GRID_COLS
        x0 = MARGIN + col * (CELL + GAP)
        y0 = MARGIN + row * (CELL + GAP)
        label = labels[idx]

        # Подложка клетки со случайным оттенком
        tint = random.randint(238, 252)
        draw.rounded_rectangle(
            [x0, y0, x0 + CELL, y0 + CELL], radius=14,
            fill=(tint, tint, min(255, tint + random.randint(0, 4)), 205),
            outline=(200, 206, 218, 180), width=1,
        )

        rgb = COLORS[color_key][1]
        cx = x0 + CELL / 2 + random.uniform(-7, 7)
        cy = y0 + CELL / 2 + random.uniform(-5, 9)
        radius = random.uniform(CELL * 0.27, CELL * 0.34)
        pts = _polygon(shape, cx, cy, radius)
        limit = MAX_ROTATION.get(shape, math.radians(20))
        pts = _rotate(pts, cx, cy, random.uniform(-limit, limit))
        pts = _jitter(pts, 1.6)
        draw.polygon(pts, fill=rgb + (random.randint(215, 245),))
        draw.line(pts + [pts[0]], fill=tuple(max(0, c - 45) for c in rgb) + (200,), width=2)

        # Номер клетки — вектором, на светлой плашке
        draw.rounded_rectangle([x0 + 5, y0 + 5, x0 + 43, y0 + 33], radius=8,
                               fill=(255, 255, 255, 215))
        _draw_number(draw, label, x0 + 12, y0 + 10, 18,
                     (40, 46, 60, 255), width=random.choice([3, 3, 4]))

        if (shape, color_key) == (target_shape, target_color):
            correct.append(label)

    img = _overlay_noise(img)
    img = _warp(img)
    img = img.filter(ImageFilter.SMOOTH)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)

    task = f"<b>все {COLORS[target_color][0]} {SHAPES[target_shape]}</b>"
    return Challenge(task=task, correct=sorted(correct), image=buf.getvalue())


def check(correct: list[int], selected: list[int]) -> bool:
    return sorted(set(selected)) == sorted(set(correct))
