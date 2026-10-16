"""
Emby 反向代理媒体源展示路径测试
"""

import asyncio
import unittest
from typing import Any

from httpx import ASGITransport, AsyncClient, Request, Response

from embyreverseproxy import EmbyReverseProxy
from embyreverseproxy.proxy_app import (
    _mask_media_source_paths,
    _normalize_media_source_path_prefixes,
    _restore_media_source_path,
    create_app,
)


PREFIX = "http://moviepilot:3000/custom/redirect"
OTHER_PREFIX = "https://strm.example.com/open"
PREFIXES = _normalize_media_source_path_prefixes([PREFIX, OTHER_PREFIX])


class TestPluginConfiguration(unittest.TestCase):
    """
    插件配置表单测试
    """

    def test_lux_web_mode_is_opt_in(self) -> None:
        """
        验证 Lux 模式可在表单中开启且默认关闭
        """
        form, defaults = EmbyReverseProxy().get_form()
        switch_models = {
            field["props"]["model"]
            for row in form
            for column in row.get("content", [])
            for field in column.get("content", [])
            if field.get("component") == "VSwitch"
        }

        self.assertIn("lux_web_mode", switch_models)
        self.assertFalse(defaults["lux_web_mode"])


class TestMediaSourcePathMasking(unittest.IsolatedAsyncioTestCase):
    """
    媒体源展示路径遮罩测试
    """

    def test_normalizes_multiple_prefixes_longest_first(self) -> None:
        """
        验证多前缀规范化、去重与排序
        """
        prefixes = _normalize_media_source_path_prefixes(
            [
                f" {PREFIX}/ ",
                f"{PREFIX}/media",
                PREFIX,
                "not-a-url",
                "http://user:password@moviepilot:3000/private",
                f"{PREFIX}/%ZZ",
                f"{PREFIX}/%FF",
            ]
        )

        self.assertEqual(prefixes, [f"{PREFIX}/media", PREFIX])

    def test_restores_encoded_and_raw_paths(self) -> None:
        """
        验证原始路径与编码路径的还原
        """
        values = (
            (
                f"{PREFIX}/media/电影/A%20B.mp4?download=1",
                "/media/电影/A B.mp4",
            ),
            (
                f"{OTHER_PREFIX}/cloud/葉月まゆ/UMD-1015.mp4",
                "/cloud/葉月まゆ/UMD-1015.mp4",
            ),
            (
                f"{PREFIX}/media/A%2520B.mp4#details",
                "/media/A%20B.mp4",
            ),
        )

        for value, expected in values:
            with self.subTest(value=value):
                self.assertEqual(_restore_media_source_path(value, PREFIXES), expected)

    def test_uses_longest_prefix(self) -> None:
        """
        验证默认端口不影响最长路径前缀优先级
        """
        prefixes = _normalize_media_source_path_prefixes(
            [
                "http://moviepilot:80/custom",
                "http://moviepilot/custom/media",
            ]
        )

        actual = _restore_media_source_path(
            "http://moviepilot/custom/media/adults/A.mp4", prefixes
        )

        self.assertEqual(actual, "/adults/A.mp4")
        self.assertEqual(
            _restore_media_source_path(
                "http://moviepilot/custom/media?token=hidden#details", prefixes
            ),
            "/",
        )

    def test_prefers_decoded_file_name_query_parameter(self) -> None:
        """
        验证命中前缀时优先展示已解码的 file_name 参数
        """
        value = (
            f"{PREFIX}?pickcode=abc123&file_name="
            "%E5%BD%B1%E7%89%87%E5%90%8D+%282024%29.BluRay.2160p.mkv"
        )

        self.assertEqual(
            _restore_media_source_path(value, PREFIXES),
            "影片名 (2024).BluRay.2160p.mkv",
        )

    def test_leaves_unrelated_or_invalid_paths_unchanged(self) -> None:
        """
        验证无关或非法路径保持不变
        """
        values = (
            f"{PREFIX}2/media/A.mp4",
            "http://other:3000/custom/redirect/media/A.mp4",
            "https://moviepilot:3000/custom/redirect/media/A.mp4",
            "http://moviepilot:3001/custom/redirect/media/A.mp4",
            ("http://other:3000/custom/redirect?file_name=%E5%BD%B1%E7%89%87.mkv"),
            f"{PREFIX}/media/%FF.mp4",
            f"{PREFIX}/media/%ZZ.mp4",
            "/media/A.mp4",
        )

        for value in values:
            with self.subTest(value=value):
                self.assertEqual(_restore_media_source_path(value, PREFIXES), value)

        port_zero_prefixes = _normalize_media_source_path_prefixes(
            ["http://moviepilot:0/base"]
        )
        port_zero_url = "http://moviepilot/base/A.mp4"
        self.assertEqual(
            _restore_media_source_path(port_zero_url, port_zero_prefixes),
            port_zero_url,
        )

    def test_masks_only_nested_media_source_paths(self) -> None:
        """
        验证仅递归遮罩 MediaSources 内的 Path
        """
        payload = {
            "Path": "http://moviepilot:3000/keep/item.strm",
            "Items": [
                {
                    "MediaSources": [
                        {
                            "Path": f"{PREFIX}/media/A.mp4",
                            "DirectStreamUrl": "/videos/1/stream",
                            "SupportsDirectPlay": True,
                        },
                        {"Path": "http://example.com/keep.mp4"},
                    ]
                }
            ],
        }

        masked_count = _mask_media_source_paths(payload, PREFIXES)

        self.assertEqual(masked_count, 1)
        self.assertEqual(payload["Path"], "http://moviepilot:3000/keep/item.strm")
        source = payload["Items"][0]["MediaSources"][0]
        self.assertEqual(source["Path"], "/media/A.mp4")
        self.assertEqual(source["DirectStreamUrl"], "/videos/1/stream")
        self.assertTrue(source["SupportsDirectPlay"])
        self.assertEqual(
            payload["Items"][0]["MediaSources"][1]["Path"],
            "http://example.com/keep.mp4",
        )

    def test_empty_prefixes_disable_masking(self) -> None:
        """
        验证空前缀配置不修改媒体源路径
        """
        payload = {"MediaSources": [{"Path": f"{PREFIX}/media/A.mp4"}]}

        self.assertEqual(_mask_media_source_paths(payload, []), 0)
        self.assertEqual(payload["MediaSources"][0]["Path"], f"{PREFIX}/media/A.mp4")

    async def test_masks_requested_media_sources_on_any_get_route(self) -> None:
        """
        验证显式请求 MediaSources 的 GET 响应统一遮罩
        """
        app = create_app(
            "http://emby:8096",
            media_source_path_prefixes=[PREFIX],
        )

        class FakeClient:
            """
            模拟 Emby GET 客户端
            """

            @staticmethod
            def response(request: Request) -> Response:
                """
                构造模拟上游 JSON 响应
                """
                return Response(
                    200,
                    headers={"etag": '"upstream"'},
                    json={
                        "Items": [{"MediaSources": [{"Path": f"{PREFIX}/media/A.mp4"}]}]
                    },
                    request=request,
                )

            async def get(self, url: str, **kwargs: Any) -> Response:
                """
                模拟不跟随重定向的 GET 请求
                """
                if "/redirect" in url:
                    return Response(
                        302,
                        headers={"location": "http://cdn.example/video.mp4"},
                        request=Request("GET", url),
                    )
                if "/plain" in url:
                    return Response(
                        200,
                        text="plain response",
                        headers={"etag": '"plain"'},
                        request=Request("GET", url),
                    )
                return self.response(Request("GET", url))

            def build_request(self, method: str, url: str, **kwargs: Any) -> Request:
                """
                构造模拟上游请求
                """
                return Request(method, url, headers=kwargs.get("headers"))

            async def send(self, request: Request, **kwargs: Any) -> Response:
                """
                发送模拟上游请求
                """
                return self.response(request)

            async def request(self, method: str, url: str, **kwargs: Any) -> Response:
                """
                发送模拟单项详情请求
                """
                if "detail-redirect" in url:
                    return Response(
                        302,
                        headers={"location": "http://cdn.example/detail"},
                        request=Request(method, url),
                    )
                return self.response(Request(method, url))

        fake_client = FakeClient()
        app.state.http_client_follow = object()
        app.state.http_client_no_follow = fake_client

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://proxy") as client:
            params = {"Fields": "ProviderIds,MediaSources"}
            responses = (
                await client.get("/Items", params=params),
                await client.get("/emby/Shows/1/Episodes", params=params),
                await client.get("/emby/users/user-1/items", params=params),
                await client.get("/emby/users/user-1/items/1"),
            )
            passthrough = await client.get(
                "/unlisted.json", params={"Fields": "ProviderIds"}
            )
            redirect = await client.get("/redirect", params={"Fields": "MediaSources"})
            plain = await client.get("/plain", params={"Fields": "MediaSources"})
            detail_redirect = await client.get(
                "/emby/users/user-1/items/detail-redirect"
            )

        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["Items"][0]["MediaSources"][0]["Path"],
                "/media/A.mp4",
            )
            self.assertNotIn("etag", response.headers)
        self.assertEqual(
            passthrough.json()["Items"][0]["MediaSources"][0]["Path"],
            f"{PREFIX}/media/A.mp4",
        )
        self.assertEqual(redirect.status_code, 302)
        self.assertEqual(redirect.headers["location"], "http://cdn.example/video.mp4")
        self.assertEqual(plain.text, "plain response")
        self.assertEqual(plain.headers["etag"], '"plain"')
        self.assertEqual(detail_redirect.status_code, 302)
        self.assertEqual(
            detail_redirect.headers["location"], "http://cdn.example/detail"
        )

    async def test_masks_only_non_playback_playback_info(self) -> None:
        """
        验证仅展示请求遮罩且播放缓存保留原始 URL
        """
        app = create_app(
            "http://emby:8096",
            media_source_path_prefixes=[PREFIX],
            pin_rules=[("/media", PREFIX)],
        )
        strm_url = (
            f"{PREFIX}?pickcode=abc123&file_name="
            "%E5%BD%B1%E7%89%87%E5%90%8D+%282024%29.mkv"
        )

        class FakeClient:
            """
            模拟 Emby PlaybackInfo 客户端
            """

            async def request(self, method: str, url: str, **kwargs: Any) -> Response:
                """
                构造模拟 PlaybackInfo 响应
                """
                return Response(
                    200,
                    headers={"etag": '"playback"'},
                    json={
                        "MediaSources": [
                            {
                                "Id": "source-1",
                                "Path": strm_url,
                                "Protocol": "Http",
                                "IsRemote": True,
                                "Type": "Video",
                            },
                            {
                                "Id": "source-2",
                                "Path": "/media/B.mp4",
                                "Protocol": "File",
                                "IsRemote": False,
                                "Type": "Video",
                            },
                        ]
                    },
                    request=Request(method, url),
                )

        app.state.strm_source_cache = {}
        app.state.strm_source_lock = asyncio.Lock()
        app.state.http_client_follow = FakeClient()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://proxy") as client:
            display = await client.post(
                "/Items/1/PlaybackInfo",
                params={"IsPlayback": "false"},
                json={"DeviceProfile": {}},
            )
            playback = await client.post(
                "/Items/1/PlaybackInfo",
                params={"IsPlayback": "true"},
                json={"DeviceProfile": {}},
            )

        self.assertEqual(display.json()["MediaSources"][0]["Path"], "影片名 (2024).mkv")
        self.assertEqual(display.json()["MediaSources"][1]["Path"], "/media/B.mp4")
        self.assertNotIn("etag", display.headers)
        self.assertEqual(
            playback.json()["MediaSources"][0]["Path"],
            strm_url,
        )
        self.assertEqual(playback.json()["MediaSources"][1]["Path"], "/media/B.mp4")
        self.assertNotIn("etag", playback.headers)
        cached_sources = app.state.strm_source_cache["1"][0]
        self.assertEqual(cached_sources["source-1"], strm_url)
        self.assertEqual(cached_sources["source-2"], f"{PREFIX}/B.mp4")


class TestLuxWebCompatibility(unittest.IsolatedAsyncioTestCase):
    """
    Lux 网页兼容模式测试
    """

    async def test_lux_mode_proxies_html_paths_events_and_session_cookies(self) -> None:
        """
        验证 Lux 模式保留路径大小写、网页、SSE 与会话 Cookie
        """
        app = create_app("http://lux:3000", lux_web_mode=True)

        class FakeClient:
            """
            模拟 Lux 上游 HTTP 客户端
            """

            def __init__(self) -> None:
                self.paths: list[str] = []
                self.cookies: list[str | None] = []

            def build_request(self, method: str, url: str, **kwargs: Any) -> Request:
                """
                构造并记录上游请求
                """
                request = Request(method, url, headers=kwargs.get("headers"))
                self.paths.append(request.url.path)
                self.cookies.append(request.headers.get("cookie"))
                return request

            async def send(self, request: Request, **kwargs: Any) -> Response:
                """
                返回模拟上游正文
                """
                if request.url.path == "/":
                    content = b"<html><head></head><body>Lux</body></html>"
                    content_type = "text/html; charset=utf-8"
                elif request.url.path == "/api/v1/events":
                    content = b'event: invalidate\ndata: {"scope":"home"}\n\n'
                    content_type = "text/event-stream"
                else:
                    content = b"ok"
                    content_type = "application/json"
                return Response(
                    200,
                    content=content,
                    headers={"content-type": content_type},
                    request=request,
                )

        fake_client = FakeClient()
        app.state.http_client_no_follow = fake_client

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://proxy") as client:
            root = await client.get("/")
            response = await client.get("/Users/AbC123/State.json")
            events = await client.get("/api/v1/events")
            session = await client.get(
                "/api/v1/auth/session",
                headers={"cookie": "lux_session=; lux_csrf="},
            )

        self.assertNotIn("[EmbyReverseProxy] crossOrigin", root.text)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(events.headers["content-type"], "text/event-stream")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(
            events.text,
            'event: invalidate\ndata: {"scope":"home"}\n\n',
        )
        self.assertEqual(
            fake_client.paths,
            [
                "/",
                "/Users/AbC123/State.json",
                "/api/v1/events",
                "/api/v1/auth/session",
            ],
        )
        self.assertEqual(fake_client.cookies[-1], "lux_session=; lux_csrf=")

    async def test_lux_login_preserves_multiple_set_cookie_headers(self) -> None:
        """
        验证 Lux 登录的多个 Set-Cookie 响应头分别转发
        """
        app = create_app("http://lux:3000", lux_web_mode=True)

        class FakeClient:
            """
            模拟返回多个 Set-Cookie 的 Lux 登录接口
            """

            def build_request(self, method: str, url: str, **kwargs: Any) -> Request:
                """
                构造上游请求
                """
                return Request(method, url, headers=kwargs.get("headers"))

            async def send(self, request: Request, **kwargs: Any) -> Response:
                """
                返回两个独立 Cookie 响应头
                """
                return Response(
                    200,
                    json={"ok": True},
                    headers=[
                        ("set-cookie", "lux_session=; HttpOnly; Path=/; SameSite=Lax"),
                        ("set-cookie", "lux_csrf=; Path=/; SameSite=Lax"),
                    ],
                    request=request,
                )

        app.state.http_client_no_follow = FakeClient()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://proxy") as client:
            response = await client.post("/api/v1/auth/login", json={})

        self.assertEqual(
            response.headers.get_list("set-cookie"),
            [
                "lux_session=; HttpOnly; Path=/; SameSite=Lax",
                "lux_csrf=; Path=/; SameSite=Lax",
            ],
        )

    async def test_default_mode_keeps_emby_html_injection(self) -> None:
        """
        验证未开启 Lux 模式时仍保留 Emby 页面兼容行为
        """
        app = create_app("http://emby:8096")

        class FakeClient:
            """
            模拟 Emby 上游 HTTP 客户端
            """

            def __init__(self) -> None:
                self.paths: list[str] = []

            def build_request(self, method: str, url: str, **kwargs: Any) -> Request:
                """
                构造上游请求
                """
                request = Request(method, url, headers=kwargs.get("headers"))
                self.paths.append(request.url.path)
                return request

            async def send(self, request: Request, **kwargs: Any) -> Response:
                """
                返回 Emby 静态资源
                """
                return Response(
                    200,
                    content=b"ok",
                    headers={"content-type": "application/json"},
                    request=request,
                )

            async def request(self, method: str, url: str, **kwargs: Any) -> Response:
                """
                返回 Emby 网页壳
                """
                return Response(
                    200,
                    text="<html><head></head><body>Emby</body></html>",
                    headers={"content-type": "text/html; charset=utf-8"},
                    request=Request(method, url, headers=kwargs.get("headers")),
                )

        fake_client = FakeClient()
        app.state.http_client_no_follow = fake_client
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://proxy") as client:
            html = await client.get("/")
            api = await client.get("/Users/AbC123/State.json")

        self.assertIn("[EmbyReverseProxy] crossOrigin", html.text)
        self.assertEqual(api.status_code, 200)
        self.assertEqual(fake_client.paths, ["/users/abc123/state.json"])


if __name__ == "__main__":
    unittest.main()
