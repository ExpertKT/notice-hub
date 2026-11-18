"""把「这栋楼到那栋楼」的直线换成绕着楼走、尽量贴着路的折线。

两条路都试，谁短用谁：

1. 路网图（`_road_graph`）：把快照里的路按顶点连成一张图，交点与贴得近的顶点处拆段加节点，
   Dijkstra 沿真路走。楼与楼之间没人行道时这条路绕远，所以只在「不比直线长太多」时才用。
2. 格子 A*（`_grid` + `_astar`）：楼块水面当障碍、路面打折，一定不穿楼，但走向是格子凑出来的。

每一段先算路网，绕行超过 `GRAPH_MAX_DETOUR` 倍直线就退回格子；格子也绕得离谱（`MAX_DETOUR`）
就退回直线。最后过一遍 Chaikin 平滑再简化，平滑若把点挤进楼里就删掉那个点。

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


# 路网这一档：直线长度的多少倍以内才肯用（比格子的 MAX_DETOUR 收得更紧，避免又绕成 1106 米）。
GRAPH_MAX_DETOUR = 1.9
# 两头接路时，从楼门走到最近路节点的短截最多多长（快照像素，约 117 米）。再远就不算「接得上路」。
MAX_STUB_PX = 130.0
# 顶点去重、以及一条路贴着另一条路顶点多近时拆段的阈值（快照像素）。
NODE_DEDUPE_PX = 2.5
# 路种权重：只给没标等级的路（service / pedestrian 等）加一点点价，别的都一样，不造大权重。
ROAD_KIND_FACTOR = {"primary": 1.0, "secondary": 1.0, "tertiary": 1.0}


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


def _segments_cross(p1: tuple[float, float], p2: tuple[float, float], q1: tuple[float, float], q2: tuple[float, float]) -> bool:
    """两线段是否真相交（共用端点不算）。"""

    def side(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2 = side(p1, p2, q1), side(p1, p2, q2)
    d3, d4 = side(q1, q2, p1), side(q1, q2, p2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _intersection_point(p1: tuple[float, float], p2: tuple[float, float], q1: tuple[float, float], q2: tuple[float, float]) -> tuple[float, float]:
    """两线段所在直线的交点（先过了 _segments_cross 才会调）。"""
    (x1, y1), (x2, y2), (x3, y3), (x4, y4) = p1, p2, q1, q2
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-12:
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
    a, b, c, d = x1 * y2 - y1 * x2, x3 * y4 - y3 * x4, x1 - x2, y1 - y2
    e, f = x3 - x4, y3 - y4
    return ((a * e - c * b) / denom, (a * f - d * b) / denom)


@lru_cache(maxsize=1)
def _road_graph() -> dict[str, Any]:
    """把快照里的路连成一张图：顶点为节点，交点/贴点处拆段，边权 = 长度 × 路种权重。

    拆段是不动点：新切出来的小段还要跟别的路再比一遍，否则两条路交叉处只切了先来的那条，
    图在路口断开，Dijkstra 只好绕着校区外圈走（实测 292 像素的直线走出 4861 像素）。
    用格子散列只比包围盒重叠的段对，避免 O(n²) 全比一遍。
    """
    data = _snapshot()
    ways = [list((r.get("pts") or [])) for r in data.get("roads") or []]
    nodes: list[tuple[float, float]] = []
    cells: dict[tuple[int, int], list[int]] = {}
    # 两条路首尾相接时坐标常差零点几像素（各自从 OSM 节点取整而来）。按精确坐标认节点就会
    # 认成两个点，图在接缝处断开，Dijkstra 只能绕着校外走。3 像素内算同一个节点。
    join = 3.0

    def node_id(pt: Sequence[float]) -> int:
        x, y = float(pt[0]), float(pt[1])
        gx, gy = int(x // join), int(y // join)
        best, best_d = -1, join * join
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for got in cells.get((gx + dx, gy + dy)) or ():
                    nx, ny = nodes[got]
                    d = (nx - x) ** 2 + (ny - y) ** 2
                    if d <= best_d:
                        best, best_d = got, d
        if best >= 0:
            return best
        nodes.append((x, y))
        got = len(nodes) - 1
        cells.setdefault((gx, gy), []).append(got)
        return got

    def factor_for(kind: Any) -> float:
        return ROAD_KIND_FACTOR.get(kind, 1.05)

    # 段 = (起点编号, 终点编号, 长度, 路种权重)。inf 长度表示这条段已被切走，作废。
    segs: list[list[float]] = []
    buckets: dict[tuple[int, int], list[int]] = {}
    seen: set[tuple[int, int]] = set()
    bucket = 48.0

    def seg_box(i: int) -> tuple[int, int, int, int]:
        a, b = nodes[int(segs[i][0])], nodes[int(segs[i][1])]
        return (
            int(min(a[0], b[0]) // bucket),
            int(min(a[1], b[1]) // bucket),
            int(max(a[0], b[0]) // bucket),
            int(max(a[1], b[1]) // bucket),
        )

    def enqueue(i: int) -> None:
        gx0, gy0, gx1, gy1 = seg_box(i)
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                buckets.setdefault((gx, gy), []).append(i)

    for road in data.get("roads") or []:
        kind_factor = factor_for(road.get("kind"))
        pts = road.get("pts") or []
        for k in range(len(pts) - 1):
            a, b = pts[k], pts[k + 1]
            length = math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
            if length < 1e-9:
                continue
            segs.append([node_id(a), node_id(b), length, kind_factor])
            enqueue(len(segs) - 1)

    pending = [(i, j) for cell in buckets.values() for pos, i in enumerate(cell) for j in cell[pos + 1 :]]
    # (i, j) 表示 i、j 两段还没比过；i == -1 表示刚切出来的段要跟所有包围盒重叠的老段重比一遍。
    while pending:
        i, j = pending.pop()
        if i >= 0:
            key = (i, j) if i < j else (j, i)
            if key in seen:
                continue
            seen.add(key)
        wi, wj = segs[i], segs[j]
        if wi[2] == float("inf") or wj[2] == float("inf"):
            continue
        ai, bi, cj, dj = int(wi[0]), int(wi[1]), int(wj[0]), int(wj[1])
        if len({ai, bi, cj, dj}) < 4:
            continue
        a, b, c, d = nodes[ai], nodes[bi], nodes[cj], nodes[dj]
        hit: tuple[float, float] | None = None
        if _segments_cross(a, b, c, d):
            hit = _intersection_point(a, b, c, d)
        else:
            for pt in (c, d):
                if seg_d(pt, a, b) <= NODE_DEDUPE_PX:
                    hit = (pt[0], pt[1])
                    break
            if hit is None:
                for pt in (a, b):
                    if seg_d(pt, c, d) <= NODE_DEDUPE_PX:
                        hit = (pt[0], pt[1])
                        break
        if hit is None:
            continue
        k = node_id(hit)
        if k in (ai, bi, cj, dj):
            continue
        for pos, anchor in ((i, a), (j, c)):
            head = math.hypot(hit[0] - anchor[0], hit[1] - anchor[1])
            end = int(segs[pos][1])
            tail = math.hypot(nodes[end][0] - hit[0], nodes[end][1] - hit[1])
            if head < 1e-9 or tail < 1e-9 or segs[pos][2] - head < 1e-9:
                continue
            start, _, _, kind = segs[pos]
            segs[pos][2] = float("inf")
            fresh: list[int] = []
            segs.append([int(start), k, head, kind])
            fresh.append(len(segs) - 1)
            enqueue(len(segs) - 1)
            segs.append([k, end, tail, kind])
            fresh.append(len(segs) - 1)
            enqueue(len(segs) - 1)
            for new in fresh:
                gx0, gy0, gx1, gy1 = seg_box(new)
                for gx in range(gx0, gx1 + 1):
                    for gy in range(gy0, gy1 + 1):
                        for other in buckets.get((gx, gy), ()):
                            if other not in fresh:
                                pending.append((new, other))

    # 排序会打乱节点编号，先重排再把段落映射到新编号上，否则边会指向错的坐标。
    order = sorted(range(len(nodes)), key=lambda i: nodes[i])
    remap = {old: new for new, old in enumerate(order)}
    nodes[:] = [nodes[i] for i in order]
    edges: dict[int, list[tuple[int, float]]] = {}
    for start, end, weight, kind in segs:
        if not math.isfinite(weight):
            continue
        cost = weight * kind
        u, v = remap[int(start)], remap[int(end)]
        edges.setdefault(u, []).append((v, cost))
        edges.setdefault(v, []).append((u, cost))
    return {"nodes": nodes, "edges": edges}


def seg_d(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> float:
    """点 p 到线段 ab 的距离。"""
    px, py = float(p[0]), float(p[1])
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _blocks() -> list[Sequence[Sequence[float]]]:
    """楼块与水面多边形（快照像素），供穿楼检查用。"""
    data = _snapshot()
    out: list[Sequence[Sequence[float]]] = []
    for key in ("buildings", "water"):
        for part in data.get(key) or []:
            pts = part.get("pts") or []
            if len(pts) >= 3:
                out.append(pts)
    return out


def _through_block(a: tuple[float, float], b: tuple[float, float], step: float = 6.0) -> bool:
    """短截会不会从楼里穿过去：每隔 step 像素采一个点测多边形包含。"""
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    blocks = _blocks()
    if not blocks:
        return False
    steps = max(1, int(length / step))
    for i in range(steps + 1):
        t = i / steps
        x = a[0] + (b[0] - a[0]) * t
        y = a[1] + (b[1] - a[1]) * t
        for poly in blocks:
            if _inside(poly, x, y):
                return True
    return False


def _nearest_node(point: tuple[float, float], edges: dict[int, list[tuple[int, float]]], nodes: list[tuple[float, float]]) -> int | None:
    """最近的图节点编号，没有节点就 None。"""
    best, best_d = None, float("inf")
    for nid, (x, y) in enumerate(nodes):
        d = (x - point[0]) ** 2 + (y - point[1]) ** 2
        if d < best_d:
            best, best_d = nid, d
    return best


def _dijkstra(start: int, goal: int, edges: dict[int, list[tuple[int, float]]]) -> list[int] | None:
    if start == goal:
        return [start]
    spent = {start: 0.0}
    came: dict[int, int] = {}
    queue = [(0.0, start)]
    while queue:
        total, node = heapq.heappop(queue)
        if node == goal:
            path = [node]
            while path[-1] != start:
                path.append(came[path[-1]])
            return list(reversed(path))
        if total > spent.get(node, float("inf")) + 1e-9:
            continue
        for nxt, weight in edges.get(node) or []:
            candidate = total + weight
            if candidate < spent.get(nxt, float("inf")) - 1e-9:
                spent[nxt] = candidate
                came[nxt] = node
                heapq.heappush(queue, (candidate, nxt))
    return None


def _graph_leg(here: tuple[float, float], there: tuple[float, float], crow: float) -> list[tuple[float, float]] | None:
    """沿路网走这一段：两端接最近路节点，短截短且不穿楼、总长不超过 crow×GRAPH_MAX_DETOUR 才用。"""
    graph = _road_graph()
    nodes = graph["nodes"]
    edges = graph["edges"]
    if not nodes:
        return None
    start = _nearest_node(here, edges, nodes)
    goal = _nearest_node(there, edges, nodes)
    if start is None or goal is None:
        return None
    stub_a = math.hypot(nodes[start][0] - here[0], nodes[start][1] - here[1])
    stub_b = math.hypot(nodes[goal][0] - there[0], nodes[goal][1] - there[1])
    if stub_a > MAX_STUB_PX or stub_b > MAX_STUB_PX:
        return None
    if _through_block(here, nodes[start]) or _through_block(nodes[goal], there):
        return None
    path = _dijkstra(start, goal, edges)
    if not path:
        return None
    total = stub_a + stub_b + sum(
        math.hypot(nodes[path[i + 1]][0] - nodes[path[i]][0], nodes[path[i + 1]][1] - nodes[path[i]][1])
        for i in range(len(path) - 1)
    )
    if crow > 1.0 and total > crow * GRAPH_MAX_DETOUR:
        return None
    out = [here, nodes[start]]
    out.extend(nodes[n] for n in path[1:])
    out.append(there)
    return out


def _chaikin(points: list[list[float]]) -> list[list[float]]:
    """一轮 Chaikin 切角，端点不动。"""
    if len(points) < 3:
        return list(points)
    out = [points[0]]
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        out.append([0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]])
        out.append([0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]])
    out.append(points[-1])
    return out


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
    # 不能裁到校区环：南边立行楼、立言楼就在环外（环 y 只到 1448.9，立言楼 1574.4）。
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
    # 逐行扫描把多边形盖住的格子挖掉：全校逐格做多边形包含要跑三秒，扫描线只碰多边形自己那几行。
    for poly in blocks:
        count = len(poly)
        top = max(0, int((min(p[1] for p in poly) - y0) / CELL))
        bottom = min(rows - 1, int((max(p[1] for p in poly) - y0) / CELL))
        for cy in range(top, bottom + 1):
            wy = y0 + cy * CELL
            xs: list[float] = []
            j = count - 1
            for i in range(count):
                ax, ay = poly[j]
                bx, by = poly[i]
                if (ay <= wy < by) or (by <= wy < ay):
                    xs.append(ax + (wy - ay) * (bx - ax) / (by - ay))
                j = i
            if len(xs) < 2:
                continue
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                cx0 = max(0, int((xs[k] - x0) / CELL))
                cx1 = min(cols - 1, int((xs[k + 1] - x0) / CELL))
                for cx in range(cx0, cx1 + 1):
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


def _free_near(grid: dict[str, Any], cx: int, cy: int, span: int = 40) -> tuple[int, int] | None:
    """落点正好压在楼里时（楼心取点就会这样），往外找到最近的可走格子。

    博雅居、图书馆这些楼的重心就在自己那栋楼的轮廓里，楼比 6 格（72 像素）大得多；
    只搜 6 格会一个可走格子都找不到，整段路线就退化成穿楼的直线。
    """
    if _walkable(grid, cx, cy):
        return cx, cy
    best: tuple[int, int] | None = None
    best_d = float("inf")
    for dx in range(-span, span + 1):
        for dy in range(-span, span + 1):
            distance = dx * dx + dy * dy
            if distance >= best_d:
                continue
            if _walkable(grid, cx + dx, cy + dy):
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
    cols, rows = grid["cols"], grid["rows"]
    goal_x, goal_y = goal
    # 搜索预算：单位是格子代价（1 格 ≈ 1.0），不是像素。以前这里拿像素差直接比，
    # 差了一个 CELL 的倍数，凡是绕过楼群/河道的路都被提前剪掉，最后退化成穿墙直线。
    budget = math.hypot(goal_x - start[0], goal_y - start[1]) * MAX_DETOUR * 3 + 1.0
    while queue:
        estimate, cell = heapq.heappop(queue)
        if cell == goal:
            path = [cell]
            while path[-1] != start:
                path.append(came[path[-1]])
            return list(reversed(path))
        if estimate > budget:
            continue
        if estimate > spent[cell] + math.hypot(goal_x - cell[0], goal_y - cell[1]) * ROAD_COST + 1e-9:
            continue
        cx, cy = cell
        base = spent[cell]
        here = cost[cx][cy]
        for dx, dy in steps:
            nx, ny = cx + dx, cy + dy
            if nx < 0 or ny < 0 or nx >= cols or ny >= rows:
                continue
            neighbour = cost[nx][ny]
            if neighbour is None:
                continue
            step = (here + neighbour) * 0.5
            total = base + step
            if total < spent.get((nx, ny), float("inf")) - 1e-9:
                spent[(nx, ny)] = total
                came[(nx, ny)] = cell
                heapq.heappush(queue, (total + math.hypot(goal_x - nx, goal_y - ny) * ROAD_COST, (nx, ny)))
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


def _polyline_length(points: Sequence[Sequence[float]]) -> float:
    return sum(
        math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
        for i in range(len(points) - 1)
    )


def _grid_leg(start: tuple[int, int], goal: tuple[int, int], grid: dict[str, Any]) -> list[tuple[float, float]] | None:
    """格子 A* 这一档，返回快照像素折线（不含起终点），走不通给 None。"""
    cells = _astar(grid, start, goal)
    if not cells or len(cells) < 2:
        return None
    return [(grid["x0"] + cx * CELL, grid["y0"] + cy * CELL) for cx, cy in cells]


@lru_cache(maxsize=1)
def _block_boxes() -> list[tuple[float, float, float, float]]:
    """楼块/水面包围盒，先粗筛再做逐点多边形包含，避免几百个平滑点每点都跑全量。"""
    return [
        (
            min(p[0] for p in poly), min(p[1] for p in poly),
            max(p[0] for p in poly), max(p[1] for p in poly),
        )
        for poly in _blocks()
    ]


def _blocked_cell(point: tuple[float, float]) -> bool:
    """这一点落在楼市/水面的格子上没有。

    判定和 A* 用同一张格子表，不再对多边形逐点做包含：水面多边形里有跨整个片区的环，
    逐点包含会把环围住的整片校园都算成水面，跟格子表给出的答案打架，路线会被无理由拒掉。
    """
    grid = _grid()
    if not grid:
        return False
    cx = int((point[0] - grid["x0"]) / CELL)
    cy = int((point[1] - grid["y0"]) / CELL)
    if cx < 0 or cy < 0 or cx >= grid["cols"] or cy >= grid["rows"]:
        return False
    return grid["cost"][cx][cy] is None


def _inside_any_building(point: tuple[float, float], blocks: Sequence[Sequence[Sequence[float]]]) -> bool:
    return _blocked_cell(point)


def _leg_hits_building(
    leg: Sequence[Sequence[float]],
    blocks: Sequence[Sequence[Sequence[float]]],
    step: float = 4.0,
    skip_head: float = 0.0,
    skip_tail: float = 0.0,
    tolerance: float = CELL,
) -> bool:
    """折线任一边每隔 step 像素采一点查楼，顶点干净但长边穿楼也能抓到。

    连续踩进楼里超过 tolerance 像素才算穿墙：网格是 12 像素一格，两格走十字时拐角上
    必然擦着多边形的边，一个采样点就算穿墙的话所有绕行路线全都收不下。
    skip_head / skip_tail：两头各让出这么多像素不查。博雅居这类点的坐标压在自家楼的重心，
    从楼心走到楼下最近的可走格子必然穿自家楼——那是没有门的数据，不该算穿墙。
    """
    total = _polyline_length(leg)
    walked = 0.0
    run = 0.0
    for i in range(len(leg) - 1):
        ax, ay = leg[i][0], leg[i][1]
        bx, by = leg[i + 1][0], leg[i + 1][1]
        span = math.hypot(bx - ax, by - ay)
        steps = max(1, int(span / step))
        for s in range(steps + 1):
            t = s / steps
            along = walked + span * t
            if along <= skip_head or along >= total - skip_tail:
                run = 0.0
                continue
            if _inside_any_building((ax + (bx - ax) * t, ay + (by - ay) * t), blocks):
                run += span / max(1, steps)
                if run > tolerance:
                    return True
            else:
                run = 0.0
        walked += span
    return False


def _stub_skips(leg: Sequence[Sequence[float]]) -> tuple[float, float]:
    """两头那一段直线（楼心到最近可走格子）各有多长，验证时整段让开。"""
    if len(leg) < 3:
        return 0.0, 0.0
    head = math.hypot(leg[1][0] - leg[0][0], leg[1][1] - leg[0][1]) + 4.0
    tail = math.hypot(leg[-1][0] - leg[-2][0], leg[-1][1] - leg[-2][1]) + 4.0
    return head, tail


def road_line(points: Sequence[Sequence[float]], simplify_px: float = SIMPLIFY_PX) -> list[list[float]]:
    """快照像素的一串点 -> 绕楼、贴路的折线（仍是快照像素）。

    入参与 `_blocks()`、`campus-geo.json` 的 `px`、前端 `toScreen` 是同一个空间。
    这里原来还做一次「平面图坐标 <-> 快照像素」的换算，可调用方（`agent_plan`、
    `trips`、测试）一律传快照像素：等于转了两遍，A* 找的是一对错位的点之间的路，
    折线端点对、中段从楼里穿过去。换算已删。

    每段先试路网图（沿真路走，只在绕行不超过直线 1.9 倍时用），不行再退格子 A*，
    再不行就那一段原样给直线：画图的那边不用为「有没有数据」分两套代码。
    """
    pts = [[float(p[0]), float(p[1])] for p in points if p is not None]
    if len(pts) < 2:
        return [[round(p[0], 1), round(p[1], 1)] for p in pts]
    grid = _grid()
    blocks = _blocks()
    merged: list[tuple[float, float]] = []
    for target in pts[1:]:
        # merged 里存的就是入口那种坐标（快照像素）；只往后追加，不回头再转。
        here = merged[-1] if merged else (pts[0][0], pts[0][1])
        there = (float(target[0]), float(target[1]))
        crow = math.hypot(there[0] - here[0], there[1] - here[1])
        if crow < 0.5:
            # 两头基本重合：任何一格绕行都会变成荒唐的远路，直接落到终点。
            merged.append(there)
            continue
        leg: list[tuple[float, float]] | None = None
        graph = _graph_leg(here, there, crow)
        if graph is not None:
            head, tail = _stub_skips(graph)
            if not _leg_hits_building(graph, blocks, skip_head=head, skip_tail=tail):
                leg = graph
        if leg is None and grid:
            start = _free_near(grid, int((here[0] - grid["x0"]) / CELL), int((here[1] - grid["y0"]) / CELL))
            goal = _free_near(grid, int((there[0] - grid["x0"]) / CELL), int((there[1] - grid["y0"]) / CELL))
            if start is not None and goal is not None:
                cells = _grid_leg(start, goal, grid)
                if cells is not None:
                    walk = [here] + cells + [there]
                    # 直线穿楼/穿水时只能绕：绕多远都得走这条路，宁可长一点也不画一条穿墙的线。
                    # 但绕出来的线自己也得是干净的——「直线不好」不是「随便收下」的理由。
                    straight_bad = _leg_hits_building([here, there], blocks)
                    head, tail = _stub_skips(walk)
                    if (
                        straight_bad or crow <= 1.0 or _polyline_length(walk) <= crow * MAX_DETOUR
                    ) and not _leg_hits_building(walk, blocks, skip_head=head, skip_tail=tail):
                        leg = walk
        merged.append(here)
        if leg is None:
            merged.append(there)
            continue
        merged.extend(leg[1:])
    merged[-1] = there
    # 平滑一下再简化：Chaikin 把路网的直角切成圆角，端点不动。
    smoothed = _chaikin([list(p) for p in merged])
    simplified = _simplify(smoothed, simplify_px)
    # 平滑可能把某个点挤进楼里，那种点直接删掉（端点保留）。
    out = [[round(simplified[0][0], 1), round(simplified[0][1], 1)]]
    for point in simplified[1:-1]:
        if _inside_any_building((point[0], point[1]), blocks):
            continue
        out.append([point[0], point[1]])
    out.append([simplified[-1][0], simplified[-1][1]])
    cleaned: list[list[float]] = [out[0]]
    for point in out[1:]:
        if math.hypot(point[0] - cleaned[-1][0], point[1] - cleaned[-1][1]) < 2.0:
            continue
        cleaned.append([round(point[0], 1), round(point[1], 1)])
    cleaned[-1] = [round(out[-1][0], 1), round(out[-1][1], 1)]
    # 圆角和简化会把拐角切进楼里/水面里（实测立德楼→立行楼切进 block 13 五个采样点）。
    # 走格子那条线本身是干净的，那就退回只简化、不切角的原折线：难看一点，但不穿墙。
    head, tail = _stub_skips(cleaned)
    if _leg_hits_building(cleaned, blocks, skip_head=head, skip_tail=tail):
        plain = _simplify([list(p) for p in merged], simplify_px)
        head, tail = _stub_skips(plain)
        if not _leg_hits_building([p for p in plain], blocks, skip_head=head, skip_tail=tail):
            cleaned = [[round(p[0], 1), round(p[1], 1)] for p in plain]
        else:
            head, tail = _stub_skips(merged)
            if not _leg_hits_building(merged, blocks, skip_head=head, skip_tail=tail):
                cleaned = []
                for point in merged:
                    got = [round(point[0], 1), round(point[1], 1)]
                    if not cleaned or math.hypot(got[0] - cleaned[-1][0], got[1] - cleaned[-1][1]) >= 2.0:
                        cleaned.append(got)
                cleaned[-1] = [round(pts[-1][0], 1), round(pts[-1][1], 1)]
    return cleaned


def road_leg(here: Sequence[float] | None, there: Sequence[float] | None) -> dict[str, Any]:
    """一段腿画成什么样，以及这一段是不是只能拿直线顶着。

    返回 {"path", "degraded", "reason"}：
    - 接上路网：path 是绕楼折线，degraded=False，reason="routed"；
    - 只画出两点且那条直线穿楼（或过水）：degraded=True，reason="straight"，
      界面据实标「这一段没接上路网，按直线估的距离」，不假装是路网；
    - 只画出两点但直线本来就走得通（两栋楼挨着、同一条路上）：不算降级；
    - 起点没有（第一站没有来处）：path=None，degraded=False，reason="no_leg"；
    - 坐标缺失或不是数：degraded=True，reason="missing_data"，也不抛给上层。
    """
    if here is None or there is None:
        return {"path": None, "degraded": False, "reason": "no_leg"}
    try:
        a = (float(here[0]), float(here[1]))
        b = (float(there[0]), float(there[1]))
        path = road_line([a, b])
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return {"path": None, "degraded": True, "reason": "missing_data"}
    straight = len(path) <= 2 and _straight_crosses_obstacle(a, b)
    return {"path": path, "degraded": straight, "reason": "straight" if straight else "routed"}


def _straight_crosses_obstacle(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """两端直连会不会穿楼/过水：穿就说明这条直线是画出来的示意，不是走法。

    只在中段采样：两头本来就落在自己楼里，采到端点必然算「穿楼」。
    """
    start, end = a, b
    span = math.hypot(end[0] - start[0], end[1] - start[1])
    steps = max(2, int(span / 6.0))
    for step in range(1, steps):
        ratio = step / float(steps)
        point = (start[0] + (end[0] - start[0]) * ratio, start[1] + (end[1] - start[1]) * ratio)
        if _blocked_cell(point):
            return True
    return False
