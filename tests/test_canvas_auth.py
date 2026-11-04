"""The Canvas proxy must not bypass ASP observer authorization or allow CSRF."""
import httpx

from canvas.backend.auth import COOKIE, CanvasAccess
from canvas.backend.main import build_app


async def test_local_bootstrap_sets_private_cookie_and_authorizes_status():
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as client:
        assert (await client.get("/status/snapshot")).status_code == 401
        assert (await client.post("/session/start", json={})).status_code == 401
        assert (await client.post("/agents/preview", json={"reference": "@development/code-reviewer"})).status_code == 401
        assert (await client.get("/session/configuration")).status_code == 401
        bootstrap = await client.get("/auth/bootstrap")
        assert bootstrap.status_code == 200
        assert "HttpOnly" in bootstrap.headers["set-cookie"]
        assert "SameSite=strict" in bootstrap.headers["set-cookie"]
        cookie_name = CanvasAccess.cookie_name({"host": "127.0.0.1:8765"})
        assert client.cookies.get(cookie_name)
        snapshot = await client.get("/status/snapshot")
        assert snapshot.status_code == 200 and not snapshot.json()["available"]
        assert client.cookies.get(cookie_name) not in snapshot.text
        rejected = await client.post("/session/start", json={}, headers={"Origin": "https://attacker.example"})
        assert rejected.status_code == 403
        assert (await client.get("/status/stream", headers={"Origin": "https://attacker.example"})).status_code == 403


async def test_rebound_hostname_cannot_obtain_bootstrap_credential():
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://attacker.example:8765") as client:
        assert (await client.get("/auth/bootstrap")).status_code == 401
        assert not any(cookie.name.startswith(COOKIE) for cookie in client.cookies.jar)
        assert (await client.get("/status/snapshot")).status_code == 401


async def test_two_local_canvas_ports_do_not_replace_each_others_credentials():
    first, second = build_app(), build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(first), base_url="http://127.0.0.1:8765") as one:
        await one.get("/auth/bootstrap")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(second), base_url="http://127.0.0.1:8766", cookies=one.cookies) as two:
            await two.get("/auth/bootstrap")
            one.cookies.update(two.cookies)
            assert (await one.get("/status/snapshot")).status_code == 200
            assert (await two.get("/status/snapshot")).status_code == 200
            assert len([cookie for cookie in one.cookies.jar if cookie.name.startswith(COOKIE)]) == 2


async def test_bearer_client_requires_the_server_credential():
    app = build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as client:
        assert (await client.get("/status/snapshot", headers={"Authorization": "Bearer wrong"})).status_code == 401
        result = await client.get("/status/snapshot", headers={"Authorization": "Bearer " + app.state.access.token})
        assert result.status_code == 200
