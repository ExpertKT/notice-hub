"""R1 路由表契约：93 条 path 一条不丢、免鉴权边界不变、别名仍按 path 二次分派。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from qq_live_digest import routes, webapp, settings_api

WEBAPP_SRC = Path(webapp.__file__).read_text(encoding="utf-8")
# R2 把 settings 的三个 POST 处理函数搬去了 settings_api.py（同一 Handler 的 mixin）。
SETTINGS_API_SRC = Path(settings_api.__file__).read_text(encoding="utf-8")
HANDLER_SOURCES = (WEBAPP_SRC, SETTINGS_API_SRC)

# 改造前 do_GET / do_POST 的分派分支展开后的全部 path（审查基线 %TEMP%\r1-route-baseline.txt）。
BASELINE_GET = (
    '/',
    '/api/announcements',
    '/api/app/qr.png',
    '/api/backup',
    '/api/chaoxing/cookie/status',
    '/api/chaoxing/courses',
    '/api/chaoxing/status',
    '/api/community/counts',
    '/api/dingtalk/groups',
    '/api/dingtalk/status',
    '/api/donate/qr.png',
    '/api/firewall/status',
    '/api/groups/suggest',
    '/api/health',
    '/api/history/fetch/status',
    '/api/history/floor',
    '/api/hosting/status',
    '/api/inbox',
    '/api/inbox/classify/status',
    '/api/inbox/image',
    '/api/insights',
    '/api/lab/replies',
    '/api/lab/rules',
    '/api/lab/search',
    '/api/lab/status',
    '/api/meta',
    '/api/napcat/doctor',
    '/api/napcat/groups',
    '/api/napcat/install/status',
    '/api/napcat/qrcode',
    '/api/napcat/status',
    '/api/notices',
    '/api/onboarding',
    '/api/onboarding/next',
    '/api/onboarding/status',
    '/api/overview',
    '/api/pair/info',
    '/api/pair/pending',
    '/api/pair/status',
    '/api/qq/groups/status',
    '/api/selfcheck',
    '/api/settings',
    '/api/setup/status',
    '/api/sync/info',
    '/api/sync/qr.png',
    '/api/sync/test',
    '/api/tasks',
    '/api/tunnel/status',
    '/api/update',
    '/api/wechat/groups',
    '/api/wechat/status',
    '/calendar',
    '/calendar.ics',
    '/floatball',
    '/floatball.html',
    '/health',
    '/icon.svg',
    '/index.html',
    '/js/*',
    '/manifest.webmanifest',
    '/setup',
    '/sw.js',
)
BASELINE_POST = (
    '/api/chaoxing/auto_cookie',
    '/api/chaoxing/cookie/validate',
    '/api/dingtalk/scan',
    '/api/firewall/allow',
    '/api/history/fetch',
    '/api/history/fetch/cancel',
    '/api/hosting/start',
    '/api/hosting/stop',
    '/api/inbox/classify',
    '/api/inbox/delete',
    '/api/inbox/promote',
    '/api/lab/events',
    '/api/lab/replies/ack',
    '/api/lab/rules',
    '/api/lab/rules/test',
    '/api/napcat/autosetup',
    '/api/napcat/install',
    '/api/napcat/launch',
    '/api/napcat/restart',
    '/api/onboarding/accept',
    '/api/onboarding/run',
    '/api/pair/approve',
    '/api/pair/deny',
    '/api/pair/pending/clear',
    '/api/pair/public',
    '/api/pair/start',
    '/api/session/exchange',
    '/api/settings',
    '/api/settings/test-llm',
    '/api/settings/test-push',
    '/api/subscriptions',
    '/api/tasks/<id>',
    '/api/tasks/clear-finished',
    '/api/tasks/create',
    '/api/tasks/pin',
    '/api/tasks/urgent',
    '/api/tunnel/start',
    '/api/update/apply',
    '/api/update/download',
)

# 基线漏登、但改造前代码确实有的 POST：/api/dingtalk/scan 与 /api/wechat/scan 共用同一个分支。
BASELINE_MISSED = {"/api/wechat/scan"}

# 免鉴权 = 鉴权点之前的精确路径（GET 10 条 + /js/* 前缀 + POST 3 条）。
# /api/session/exchange 必须免鉴权：它本身就是「用令牌换 HttpOnly Cookie」的入口。
PUBLIC_GET = {
    "/health",
    "/api/health",
    "/manifest.webmanifest",
    "/icon.svg",
    "/sw.js",
    "/api/donate/qr.png",
    "/api/onboarding",
    "/api/onboarding/next",
    "/api/onboarding/status",
    "/api/pair/status",
}
PUBLIC_POST = {"/api/onboarding/accept", "/api/pair/start", "/api/session/exchange"}

ALIASES = (
    ("GET", "/health", "/api/health"),
    ("GET", "/floatball", "/floatball.html"),
    ("GET", "/", "/index.html"),
    ("POST", "/api/lab/replies/ack", "/api/lab/events"),
    ("POST", "/api/tunnel/start", "/api/pair/public"),
    ("POST", "/api/hosting/start", "/api/hosting/stop"),
)


def _table(method: str) -> dict:
    return routes.GET_ROUTES if method == "GET" else routes.POST_ROUTES


def _labels(method: str) -> set:
    """表项 + 模式项登记名，合起来就是「分派 path 清单」。"""
    table = _table(method)
    patterns = routes.GET_PATTERN_ROUTES if method == "GET" else routes.POST_PATTERN_ROUTES
    return set(table) | {label for *_rest, label in patterns}


def _handler_node(name: str) -> ast.FunctionDef:
    # R2 之后 settings 三个 POST 处理函数搬到 settings_api.py，所以两边都要找。
    for source in HANDLER_SOURCES:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
    raise AssertionError("webapp.py / settings_api.py 里都没有处理函数 %s" % name)


def _handler_src(name: str) -> str:
    for source in HANDLER_SOURCES:
        node = None
        for candidate in ast.walk(ast.parse(source)):
            if isinstance(candidate, ast.FunctionDef) and candidate.name == name:
                node = candidate
                break
        if node is not None:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError("没有处理函数 %s" % name)


class RoutesTableTests(unittest.TestCase):
    def test_no_baseline_path_is_lost(self) -> None:
        self.assertEqual(sorted(set(BASELINE_GET) - _labels("GET")), [], "GET 表里缺 path")
        self.assertEqual(sorted(set(BASELINE_POST) - _labels("POST")), [], "POST 表里缺 path")

    def test_only_documented_extra_post_route(self) -> None:
        self.assertEqual(_labels("POST") - set(BASELINE_POST), BASELINE_MISSED)
        self.assertEqual(_labels("GET") - set(BASELINE_GET), set())

    def test_counts(self) -> None:
        self.assertEqual(len(routes.GET_ROUTES), 61)
        self.assertEqual(len(routes.POST_ROUTES), 39)
        self.assertEqual(len(routes.GET_PATTERN_ROUTES), 1)   # /js/*
        self.assertEqual(len(routes.POST_PATTERN_ROUTES), 1)  # /api/tasks/<id>
        self.assertEqual(len(_labels("GET")), 62)  # 61 + /js/* 前缀
        self.assertEqual(len(_labels("POST")), 40)  # 39 + /api/tasks/<id>，另含基线漏登的 wechat/scan

    def test_every_registered_handler_exists(self) -> None:
        handlers = {name for name, _ in routes.GET_ROUTES.values()} | {
            name for name, _ in routes.POST_ROUTES.values()
        }
        for *_, handler, _public, _label in (
            *((p, h, pub, lab) for p, h, pub, lab in routes.GET_PATTERN_ROUTES),
            *((p, h, pub, lab) for p, h, pub, lab in routes.POST_PATTERN_ROUTES),
        ):
            handlers.add(handler)
        self.assertEqual(len(handlers), 95, "93 个普通 handler + 2 个前缀 handler")
        for name in handlers:
            _handler_node(name)

    def test_alias_groups_share_one_handler(self) -> None:
        for method, first, second in ALIASES:
            with self.subTest(pair=(first, second)):
                table = _table(method)
                self.assertEqual(table[first][0], table[second][0])

    def test_public_routes_are_exactly_the_pre_auth_ones(self) -> None:
        public_get = {path for path, (_, public) in routes.GET_ROUTES.items() if public}
        public_post = {path for path, (_, public) in routes.POST_ROUTES.items() if public}
        self.assertEqual(public_get, PUBLIC_GET)
        self.assertEqual(public_post, PUBLIC_POST)
        self.assertEqual(
            [(prefix, public, label) for prefix, _, public, label in routes.GET_PATTERN_ROUTES],
            [("/js/", True, "/js/*")],
        )

    def test_tasks_tail_is_a_pattern_entry(self) -> None:
        self.assertNotIn("/api/tasks/<id>", routes.POST_ROUTES)  # 不能按字面量登记
        self.assertEqual(
            [(prefix, public, label) for prefix, _, public, label in routes.POST_PATTERN_ROUTES],
            [("/api/tasks/", False, "/api/tasks/<id>")],
        )
        self.assertEqual(routes.match_pattern("POST", "/api/tasks/7"), ("_route_post_tasks", False))
        self.assertIsNone(routes.match_pattern("POST", "/api/tasks"))
        self.assertIsNone(routes.match_pattern("POST", "/api/meta"))

    def test_js_prefix_miss_falls_back_to_auth(self) -> None:
        """GET /js/ 未命中必须返回 False，让调用方继续走鉴权/404，不能回空响应。"""
        body = _handler_src("_route_get_js")
        self.assertIn("return True", body)
        self.assertIn("return False", body)


class DispatchStructureTests(unittest.TestCase):
    def test_dispatch_methods_have_no_hand_written_path_branches(self) -> None:
        for name in ("do_GET", "do_POST"):
            with self.subTest(method=name):
                leftovers = []
                for sub in ast.walk(_handler_node(name)):
                    if isinstance(sub, ast.If):
                        text = ast.unparse(sub.test)
                        if "path" in text and ("==" in text or " in " in text or "startswith" in text):
                            leftovers.append(text)
                self.assertEqual(leftovers, [], "dispatch 里又长回手写分支了")

    def test_dispatch_uses_tables_and_patterns(self) -> None:
        self.assertIn("routes.GET_ROUTES.get(path)", WEBAPP_SRC)
        self.assertIn("routes.POST_ROUTES.get(path)", WEBAPP_SRC)
        self.assertEqual(WEBAPP_SRC.count('routes.match_pattern("GET", path)'), 1)
        self.assertEqual(WEBAPP_SRC.count('routes.match_pattern("POST", path)'), 1)

    def test_auth_gate_sits_between_public_and_private_dispatch(self) -> None:
        for name in ("do_GET", "do_POST"):
            with self.subTest(method=name):
                body = _handler_src(name)
                public_at = body.index("entry[1]")
                auth_at = body.index("self._authorized()")
                private_at = body.index("getattr(self, entry[0])(path)", auth_at)
                self.assertLess(public_at, auth_at)
                self.assertLess(auth_at, private_at)


class AliasBehaviourTests(unittest.TestCase):
    def test_tunnel_confirm_gate_only_applies_to_the_new_path(self) -> None:
        body = _handler_src("_route_post_api_tunnel_start")
        self.assertIn('path == "/api/tunnel/start"', body)
        self.assertLess(body.index('path == "/api/tunnel/start"'), body.index("self._confirmed_host_action()"))

    def test_lab_alias_keeps_path_identity(self) -> None:
        self.assertIn('path == "/api/lab/replies/ack"', _handler_src("_route_post_api_lab_replies_ack"))

    def test_hosting_alias_keeps_path_identity(self) -> None:
        self.assertIn('path.endswith("/start")', _handler_src("_route_post_api_hosting_start"))


if __name__ == "__main__":
    unittest.main()
