"""校园坐标上的开放路线与附近待办；距离为直线估算，不是步行路网导航。"""

from __future__ import annotations

import math
from itertools import permutations
from typing import Any

from noticehub.application.campus import load_pois, match_place
from noticehub.application.trips import WALK_M_PER_MIN, _distance, _order_stops


def _place(value: Any, pois: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("地点请填校园楼名或 POI 编号")
    value = value.strip()
    hits = [p for p in pois if value in [p.get('id'), p.get('name'), *(p.get('aliases') or [])]]
    if not hits:
        raise ValueError("找不到地点「%s」，请核对楼名或在 campus_poi.json 补充坐标" % value)
    if len(hits) != 1:
        raise ValueError("地点「%s」不唯一，请改用 POI 编号" % value)
    _check_coordinates(hits[0])
    return hits[0]


def _check_coordinates(poi: dict[str, Any]) -> None:
    try:
        valid = all(math.isfinite(float(poi[key])) and abs(float(poi[key])) <= 1e7 for key in ('x', 'y'))
    except (KeyError, ValueError, TypeError, OverflowError):
        valid = False
    if not valid:
        raise ValueError("「%s」的米制坐标无效，请检查 campus_poi.json" % poi.get('name', '地点'))


def _length(route: list[dict[str, Any]]) -> float:
    return sum(_distance(a, b) for a, b in zip(route, route[1:]))


def plan_route(points: list[str], start: str | None = None, *, pois: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """固定起点、不要求返回；未填 start 时以首点为起点，同一 POI 只访问一次。"""
    if not isinstance(points, list) or not 1 <= len(points) <= 64:
        raise ValueError("points 请填 1 到 64 个校园地点")
    # 默认目录必须是带坐标的那份（load_pois 才把 campus-geo.json 的 x/y 合进来）；
    # DEFAULT_POIS 只有名字和分区，拿它算距离只会抛「坐标无效」。
    catalog = load_pois(None) if pois is None else pois
    resolved = [_place(point, catalog) for point in points]
    origin = _place(start, catalog) if start is not None else resolved[0]
    stops = []
    seen = {origin['id']}
    for poi in resolved:
        if poi['id'] not in seen:
            stops.append(poi)
            seen.add(poi['id'])
    baseline = [origin, *stops]
    if len(points) <= 8:
        # 最多 8 个输入点：枚举全部访问顺序，固定起点的开放路线精确解。
        route = min(([origin, *order] for order in permutations(stops)), key=_length)
        algorithm = 'exact'
    else:
        # 超过 8 点：最近邻初始化 + 开放路径 2-opt，与输入顺序择短后改进，保证不会比原顺序更长。
        nearest = [origin, *(stop['place'] for stop in _order_stops([{'place': p} for p in stops], origin))]
        route = min((baseline, nearest), key=_length)
        while True:
            best = route
            best_length = _length(route)
            for left in range(1, len(route) - 1):
                for right in range(left + 1, len(route)):
                    candidate = route[:left] + route[left:right + 1][::-1] + route[right + 1:]
                    length = _length(candidate)
                    if length < best_length - 1e-9:
                        best, best_length = candidate, length
            if best is route:
                break
            route = best
        algorithm = 'nearest_neighbor_2opt'
    distance = _length(route)
    input_distance = _length(baseline)
    return {
        'order': [p['name'] for p in route],
        'poi_ids': [p['id'] for p in route],
        'legs': [dict(start=a['name'], end=b['name'], distance_m=round(_distance(a, b), 1),
                      walk_minutes=round(_distance(a, b) / WALK_M_PER_MIN, 2)) for a, b in zip(route, route[1:])],
        'distance_m': round(distance, 1),
        'walk_minutes': round(distance / WALK_M_PER_MIN, 2),
        'input_distance_m': round(input_distance, 1),
        'saved_m': round(max(0.0, input_distance - distance), 1),
        'algorithm': algorithm,
        # 步速只有一个来源（trips.WALK_M_PER_MIN）：同一段路，路线工具和行程面板不能报两个时间。
        'basis': '校园米制坐标直线估算，步速 %d 米/分钟；未计道路、进楼、办理时间及营业时间' % WALK_M_PER_MIN,
    }


def poi_near(place: str, radius_m: float, *, pois: list[dict[str, Any]] | None = None,
             tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """包含半径边界与所在楼；只列实际存在的未完待办，不推断营业或可办理。"""
    if isinstance(radius_m, bool) or not isinstance(radius_m, (int, float)):
        raise ValueError("radius_m 请填非负的有限米数")
    try:
        radius = float(radius_m)
    except OverflowError as error:
        raise ValueError("radius_m 请填非负的有限米数") from error
    if not math.isfinite(radius) or radius < 0:
        raise ValueError("radius_m 请填非负的有限米数")
    catalog = load_pois(None) if pois is None else pois
    origin = _place(place, catalog)
    nearby = []
    for poi in catalog:
        _check_coordinates(poi)
        distance = _distance(origin, poi)
        if distance <= radius:
            nearby.append((poi, distance))
    nearby.sort(key=lambda item: (item[1], item[0]['id']))
    distances = {p['id']: distance for p, distance in nearby}
    errands = []
    for task in tasks or []:
        if task.get('status') in {'done', 'dismissed', 'expired'} or task.get('done'):
            continue
        summary = str(task.get('summary') or '')
        poi = match_place(summary, catalog)
        if poi is not None and poi['id'] in distances:
            errands.append(dict(task_id=task['id'], summary=summary, place=poi['name'], distance_m=round(distances[poi['id']], 1)))
    errands.sort(key=lambda task: task['distance_m'])
    return {
        'place': origin['name'], 'radius_m': radius,
        'places': [dict(id=p['id'], name=p['name'], distance_m=round(distance, 1)) for p, distance in nearby],
        'tasks': errands,
        'basis': '校园米制坐标直线半径；附近待办未校验截止时间、营业时间和沿途绕行距离',
    }
