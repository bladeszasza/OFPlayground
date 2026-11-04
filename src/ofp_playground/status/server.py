"""Separately runnable ASP monitor; the conversation runner is not its owner."""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
from pathlib import Path

import click

from ofp_playground.status.models import Credential
from ofp_playground.status.store import StatusStore

DEFAULT_DATA_DIR = Path.home() / ".ofp-playground" / "status"


def load_local_credentials(data_dir: Path) -> dict[str, str]:
    """Create a private local credential file once; never print its contents."""
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = data_dir / "credentials.json"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({key: secrets.token_urlsafe(32) for key in
                       ("operatorToken", "observerToken", "supervisorToken", "guardrailToken")}, stream)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("status credentials must be accessible only to their owner (chmod 600)")
    data = json.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"operatorToken", "observerToken", "supervisorToken", "guardrailToken"} or any(not isinstance(v, str) or len(v) < 32 for v in data.values()):
        raise ValueError("invalid local status credential configuration")
    return data


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8787, type=click.IntRange(1, 65535), show_default=True)
@click.option("--data-dir", type=click.Path(path_type=Path), default=DEFAULT_DATA_DIR, show_default=True)
@click.option("--retention", default=10000, type=click.IntRange(1, 100000))
@click.option("--cert", type=click.Path(exists=True, path_type=Path))
@click.option("--key", type=click.Path(exists=True, path_type=Path))
def main(host: str, port: int, data_dir: Path, retention: int, cert: Path | None, key: Path | None):
    """Run the independent ASP service. Ctrl+C closes the service and journal."""
    if bool(cert) != bool(key):
        raise click.ClickException("TLS requires both --cert and --key")
    if not is_loopback(host) and not cert:
        raise click.ClickException("Non-local status service requires TLS (--cert and --key)")
    try:
        import uvicorn

        from ofp_playground.status.api import build_status_app
    except ImportError as exc:
        raise click.ClickException("Install status dependencies: pip install -e '.[status]'") from exc
    try:
        config = load_local_credentials(data_dir)
    except (ValueError, OSError) as exc:
        raise click.ClickException("Cannot load private status credentials; check --data-dir permissions/configuration") from exc
    store = StatusStore(data_dir / "journal.sqlite", retention=retention)
    app = build_status_app(store, {
        config["operatorToken"]: Credential("local-operator", "operator", frozenset({"*"}), observe=True),
        config["observerToken"]: Credential("local-observer", "peer", frozenset({"*"}), observe=True, ingest=False),
        config["supervisorToken"]: Credential("local-supervisor", "supervisor", frozenset({"*"})),
        config["guardrailToken"]: Credential("local-guardrail", "guardrail", frozenset({"*"})),
    })
    scheme = "https" if cert else "http"
    click.echo(f"ASP service listening at {scheme}://{host}:{port}; credentials: {data_dir / 'credentials.json'}")
    try:
        server = uvicorn.Server(uvicorn.Config(
            app, host=host, port=port, access_log=False,
            ssl_certfile=str(cert) if cert else None, ssl_keyfile=str(key) if key else None,
            log_level="warning", timeout_graceful_shutdown=5))

        def request_shutdown() -> None:
            for queue in tuple(store.subscribers):
                store._disconnect(queue)
            server.should_exit = True

        app.state.shutdown = request_shutdown
        server.run()
    finally:
        store.close()


if __name__ == "__main__":
    main()
