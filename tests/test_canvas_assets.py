"""A checked-in Canvas build must serve every asset referenced by its HTML."""
import re

from httpx import ASGITransport, AsyncClient

from canvas.backend.main import build_app


async def test_bundled_canvas_assets_load():
    async with AsyncClient(transport=ASGITransport(app=build_app(authenticate=False)), base_url="http://test") as client:
        page = await client.get("/")
        assert page.status_code == 200
        assets = re.findall(r'(?:src|href)="(/[^\"]+)"', page.text)
        assert any(asset.endswith(".js") for asset in assets)
        for asset in assets:
            response = await client.get(asset)
            assert response.status_code == 200, asset
            assert len(response.content) > 0, asset
            assert "text/html" not in response.headers["content-type"], asset


async def test_trace_preview_without_a_session():
    async with AsyncClient(transport=ASGITransport(app=build_app(authenticate=False)), base_url="http://test") as client:
        response = await client.get("/trace/live")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
