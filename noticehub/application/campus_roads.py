"""把「这栋楼到那栋楼」的直线换成绕着楼走、尽量贴着路的折线。

为什么不直接沿 OSM 路网寻路：这份快照里 91 条路全在校区外圈与主路上，楼与楼之间一条人行道都没画。
硬按路走，立德楼到立行楼（直线 140 米）会绕成 1106 米——画出这样的线比直线更假。

所以换个做法：把快照里的楼块、水面当障碍，铺一张 8 像素（约 7 米）的格子，
A* 找一条不走楼里、并且尽量压在路上（路面上代价打六折）的线。
这样画出来的线既不会穿楼，也会自动拐到最近的那条路上，两头接不上路时就是一条直着走出楼门的短段。

坐标：楼位在平面图坐标，路网和楼块在快照像素坐标，两者差一个 8.9° 相似变换，
系数抄自 `campus-geo.json` 的 planToSnapshot。对外只收发平面图坐标。
"""

from __future__ import annotations

import heapq
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

WEB_STATIC = Path(__file__).resolve().parent.parent / "web_static"
SNAPSHOT_FILE = WEB_STATIC / "js" / "campus-osm.js"
GEO_FILE = WEB_STATIC / "js" / "campus-geo.json"

# 格子边长（快照像素）。12 像素 × 0.9 米 ≈ 11 米：比这更细，A* 在纯 Python 里跑一遍要十几秒。
CELL = 12.0
# 路面上打这个折：只打一点点。之前打到 0.275，A* 宁可绕出去两公里也要贴着主路走。
ROAD_COST = 0.85
# 草地/操场走着没毛病，但比硬化路面稍微贵一点，能绕开就绕开。
SOFT_COST = 1.1
# 离路多远算「在路上」（快照像素）。
ROAD_BAND = 9.0
# 折线化简容差（平面图像素，约 5 米）：拐点太多看着像抖，太少又看不出跟着路走了。
SIMPLIFY_PX = 6.0
# 绕成这么离谱就别画了，那条腿退回直线（超过直线的 2.2 倍）。
MAX_DETOUR = 2.2


def _read_js_object(path: Path, global_name: str) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    start = raw.index("{", raw.index(global_name))
    return json.loads(raw[start : raw.rindex("}") + 1])


@lru_cache(maxsize=1)
def _snapshot() -> dict[str, Any]:
    try:
        return _read_js_object(SNAPSHOT_FILE, "window.NH_CAMPUS_OSM")
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def _transform() -> dict[str, float]:
    try:
        raw = json.loads(GEO_FILE.read_text(encoding="utf-8"))
        table = raw.get("planToSnapshot") or {}
        return {key: float(table[key]) for key in ("a", "b", "tx", "ty")}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def to_snapshot(point: Sequence[float]) -> tuple[float, float]:
    """平面图坐标 -> 快照像素。没有标定就原样返回（画出来偏，但不会崩）。"""
    table = _transform()
    x, y = float(point[0]), float(point[1])
    if not table:
        return x, y
    a, b, tx, ty = table["a"], table["b"], table["tx"], table["ty"]
    return a * x - b * y + tx, b * x + a * y + ty


def to_plan(point: Sequence[float]) -> tuple[float, float]:
    """快照像素 -> 平面图坐标，to_snapshot 的逆。"""
    table = _transform()
    x, y = float(point[0]), float(point[1])
    if not table:
        return x, y
    a, b, tx, ty = table["a"], table["b"], table["tx"], table["ty"]
    scale = a * a + b * b
    dx, dy = x - tx, y - ty
    return (a * dx + b * dy) / scale, (-b * dx + a * dy) / scale


def _inside(poly: Sequence[Sequence[float]], x: float, y: float) -> bool:
    """射线法：点是不是在多边形里（楼块都是简单多边形）。"""
    hit = False
    count = len(poly)
    for index in range(count):
        ax, ay = poly[index][0], poly[index][1]
        bx, by = poly[(index + 1) % count][0], poly[(index + 1) % count][1]
        if (ay > y) != (by > y):
            cross = ax + (y - ay) * (bx - ax) / (by - ay)
            if x < cross:
                hit = not hit
    return hit


@lru_cache(maxsize=1)
def _grid() -> dict[str, Any]:
    """可走图：格子代价表。楼里和水面是 None，路上打折，草地加价。"""
    data = _snapshot()
    roads = [road.get("pts") or [] for road in data.get("roads") or []]
    blocks: list[Sequence[Sequence[float]]] = []
    for key in ("buildings", "water"):
        for part in data.get(key) or []:
            pts = part.get("pts") or []
            if len(pts) >= 3:
                blocks.append(pts)
    xs = [p[0] for road in roads for p in road if len(p) >= 2]
    ys = [p[1] for road in roads for p in road if len(p) >= 2]
    for poly in blocks:
        xs.extend(p[0] for p in poly)
        ys.extend(p[1] for p in poly)
    # 绿地和水面有画到校外的（河道一直伸出去），包围盒别被它们撑大：格子多一倍，A* 就慢一倍。
    edge = data.get("campus") or []
    if edge and len(edge[0]) >= 3:
        xs = [p[0] for p in edge[0]] + xs[:1]
        ys = [p[1] for p in edge[0]] + ys[:1]
    if not xs or not ys:
        return {}
    x0, x1 = min(xs) - CELL, max(xs) + CELL
    y0, y1 = min(ys) - CELL, max(ys) + CELL
    cols = int((x1 - x0) / CELL) + 1
    rows = int((y1 - y0) / CELL) + 1

    road_cells: set[tuple[int, int]] = set()
    band = int(ROAD_BAND / CELL) + 1
    for pts in roads:
        for ax, ay in pts:
            cx = int((ax - x0) / CELL)
            cy = int((ay - y0) / CELL)
            for dx in range(-band, band + 1):
                for dy in range(-band, band + 1):
                    if dx * dx + dy * dy <= band * band:
                        road_cells.add((cx + dx, cy + dy))

    soft_cells: set[tuple[int, int]] = set()
    for part in data.get("green") or []:
        box = part.get("pts") or []
        for px, py in box:
            soft_cells.add((int((px - x0) / CELL), int((py - y0) / CELL)))

    cost: list[list[float | None]] = [[1.0 for _ in range(rows)] for _ in range(cols)]
    # 只测多边形自己包围盒里的格子：全校铺一遍逐格点做多边形包含，纯 Python 要跑十几秒。
    for poly in blocks:
        px = [p[0] for p in poly]
        py = [p[1] for p in poly]
        cx0 = max(0, int((min(px) - x0) / CELL))
        cx1 = min(cols - 1, int((max(px) - x0) / CELL) + 1)
        cy0 = max(0, int((min(py) - y0) / CELL))
        cy1 = min(rows - 1, int((max(py) - y0) / CELL) + 1)
        for cx in range(cx0, cx1 + 1):
            wx = x0 + cx * CELL
            for cy in range(cy0, cy1 + 1):
                if cost[cx][cy] is not None and _inside(poly, wx, y0 + cy * CELL):
                    cost[cx][cy] = None
    for (cx, cy) in road_cells:
        if 0 <= cx < cols and 0 <= cy < rows and cost[cx][cy] is not None:
            cost[cx][cy] = ROAD_COST
    for (cx, cy) in soft_cells:
        if 0 <= cx < cols and 0 <= cy < rows and cost[cx][cy] == 1.0:
            cost[cx][cy] = SOFT_COST
    return {"x0": x0, "y0": y0, "cols": cols, "rows": rows, "cost": cost}


def _walkable(grid: dict[str, Any], cx: int, cy: int) -> bool:
    if cx < 0 or cy < 0 or cx >= grid["cols"] or cy >= grid["rows"]:
        return False
    return grid["cost"][cx][cy] is not None


def _free_near(grid: dict[str, Any], cx: int, cy: int, span: int = 6) -> tuple[int, int] | None:
    """落点正好压在楼里时（楼心取点就会这样），往四周挪到最近的可走格子。"""
    if _walkable(grid, cx, cy):
        return cx, cy
    best = None
    best_d = float("inf")
    for dx in range(-span, span + 1):
        for dy in range(-span, span + 1):
            if _walkable(grid, cx + dx, cy + dy):
                distance = dx * dx + dy * dy
                if distance < best_d:
                    best, best_d = (cx + dx, cy + dy), distance
    return best


def _astar(grid: dict[str, Any], start: tuple[int, int], goal: tuple[int, int]) -> list[tuple[int, int]] | None:
    cost = grid["cost"]

    def weight(cell: tuple[int, int]) -> float:
        return cost[cell[0]][cell[1]]

    def heuristic(cell: tuple[int, int]) -> float:
        return math.hypot(goal[0] - cell[0], goal[1] - cell[1]) * ROAD_COST

    queue: list[tuple[float, tuple[int, int]]] = [(0.0, start)]
    came: dict[tuple[int, int], tuple[int, int]] = {}
    spent: dict[tuple[int, int], float] = {start: 0.0}
    steps = ((1, 0), (-1, 0), (0, 1), (0, -1))
    while queue:
        estimate, cell = heapq.heappop(queue)
        if cell == goal:
            path = [cell]
            while path[-1] != start:
                path.append(came[path[-1]])
            return list(reversed(path))
        if estimate > spent.get(cell, float("inf")) + heuristic(cell) + 1e-9:
            continue
        for dx, dy in steps:
            neighbour = (cell[0] + dx, cell[1] + dy)
            if not _walkable(grid, *neighbour):
                continue
            step = math.hypot(dx, dy) * (weight(cell) + weight(neighbour)) / 2
            total = spent[cell] + step
            if total < spent.get(neighbour, float("inf")) - 1e-9:
                spent[neighbour] = total
                came[neighbour] = cell
                heapq.heappush(queue, (total + heuristic(neighbour), neighbour))
    return None


def _simplify(points: list[list[float]], tolerance: float) -> list[list[float]]:
    """Douglas-Peucker：把一条弯弯的格子线压成几个拐点。"""
    if len(points) < 3:
        return points
    first, last = points[0], points[-1]
    dx, dy = last[0] - first[0], last[1] - first[1]
    span = math.hypot(dx, dy)
    worst = 0.0
    where = 0
    for index in range(1, len(points) - 1):
        px, py = points[index]
        if span < 1e-6:
            distance = math.hypot(px - first[0], py - first[1])
        else:
            distance = abs(dy * (px - first[0]) - dx * (py - first[1])) / span
        if distance > worst:
            worst, where = distance, index
    if worst <= tolerance:
        return [first, last]
    left = _simplify(points[: where + 1], tolerance)
    right = _simplify(points[where:], tolerance)
    return left[:-1] + right


def road_line(points: Sequence[Sequence[float]], simplify_px: float = SIMPLIFY_PX) -> list[list[float]]:
    """平面图坐标的一串点 -> 绕楼、贴路的折线（仍是平面图坐标）。

    没有路网快照、或者某一段格子图里根本走不通，就那一段原样给直线：
    画图的那边不用为「有没有数据」分两套代码。
    """
    pts = [[float(p[0]), float(p[1])] for p in points if p is not None]
    if len(pts) < 2:
        return pts
    grid = _grid()
    if not grid:
        return pts
    out: list[list[float]] = [pts[0]]
    for target in pts[1:]:
        here = to_snapshot(out[-1])
        there = to_snapshot(target)
        start = _free_near(grid, int((here[0] - grid["x0"]) / CELL), int((here[1] - grid["y0"]) / CELL))
        goal = _free_near(grid, int((there[0] - grid["x0"]) / CELL), int((there[1] - grid["y0"]) / CELL))
        if start is None or goal is None:
            out.append(target)
            continue
        cells = _astar(grid, start, goal)
        if not cells or len(cells) < 2:
            out.append(target)
            continue
        walk = [
            list(to_plan((grid["x0"] + cx * CELL, grid["y0"] + cy * CELL)))
            for cx, cy in cells
        ]
        detour = sum(
            math.hypot(walk[i + 1][0] - walk[i][0], walk[i + 1][1] - walk[i][1])
            for i in range(len(walk) - 1)
        )
        crow = math.hypot(target[0] - out[-1][0], target[1] - out[-1][1])
        if crow > 1.0 and detour > crow * MAX_DETOUR:
            out.append(target)
            continue
        for point in _simplify(walk, simplify_px)[1:]:
            out.append([round(point[0], 1), round(point[1], 1)])
        out.append(target)
    # 挨得太近的点删掉，折线密到看不出走向。
    cleaned: list[list[float]] = []
    for point in out:
        if cleaned and math.hypot(point[0] - cleaned[-1][0], point[1] - cleaned[-1][1]) < 2.0:
            continue
        cleaned.append(point)
    return cleaned
