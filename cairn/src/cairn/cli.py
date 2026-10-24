import logging
from pathlib import Path

import click
import uvicorn

from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import auth, db

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@click.group()
def main():
    """Cairn - Fact-graph based collaborative exploration protocol."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option(
    "--auth-token",
    envvar=auth.TOKEN_ENV,
    default=None,
    help=(
        "Shared secret required by the console and the API. Defaults to the token stored in "
        "--auth-token-file, which is generated on first start."
    ),
)
@click.option(
    "--auth-token-file",
    envvar=auth.TOKEN_FILE_ENV,
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "File the auth token is read from, and written to when it has to be generated. "
        "Defaults to auth_token next to the database."
    ),
)
@click.option(
    "--no-auth",
    envvar=auth.NO_AUTH_ENV,
    is_flag=True,
    default=False,
    help="Disable authentication entirely. Only for isolated, fully trusted networks.",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(
    host: str,
    port: int,
    db_path: str,
    auth_token: str | None,
    auth_token_file: Path | None,
    no_auth: bool,
    log_level: str,
    access_log: bool,
):
    """Start the Cairn API server."""
    if no_auth and auth_token:
        raise click.UsageError("--no-auth cannot be combined with --auth-token")

    # Uvicorn only configures its own loggers; give the server's startup messages (where the
    # generated auth token is announced) somewhere to go.
    logging.basicConfig(level=log_level.upper(), format="%(levelname)s:     %(message)s")

    db.configure(Path(db_path))
    auth.configure(token=auth_token, token_file=auth_token_file, disabled=no_auth)
    if no_auth and host not in LOOPBACK_HOSTS:
        logging.getLogger("cairn.server").warning(
            "serving an UNAUTHENTICATED console on %s:%s; anyone who can reach it controls every project",
            host,
            port,
        )

    from cairn.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the Cairn dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    loop = DispatcherLoop(config_path)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
