"""Browser-Smoke-Tests mit Playwright (``pytest.mark.ui``).

Sie starten einen echten ``runserver`` gegen die Testdatenbank und prüfen die
Wege, die nur im Browser sichtbar sind: Login/Logout, Chart-Rendering samt
``plot.json``-Fetch, Filterweitergabe, Rollen-Sichtbarkeit und die
Asset-Auslieferung. Ohne ``STARCUBES_TEST_DSN`` oder ohne installierten
Playwright-Browser werden sie übersprungen.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "test-pass-123"
# Login and logout both redirect to the site root. Anchored to the root on
# purpose: a bare ``/$`` also matches ``/sources/`` and ``/login/``, so the
# wait returned before the POST it was meant to track had been answered.
ROOT_URL = re.compile(r"^https?://[^/]+/$")


@dataclass
class UiWorld:
    handle: object
    normal_name: str
    admin_name: str


@pytest.fixture(scope="session")
def ui_world(django_orm, meta_engine, pg_dsn):
    from tests.seed import create_user, drop_seeded, seed_datasource

    schema = "ui_" + uuid.uuid4().hex[:12]
    handle = seed_datasource(meta_engine, pg_dsn, schema)
    normal_name = "ui_n_" + uuid.uuid4().hex[:8]
    admin_name = "ui_a_" + uuid.uuid4().hex[:8]
    normal = create_user(normal_name, PASSWORD)
    admin = create_user(admin_name, PASSWORD, superuser=True)
    try:
        yield UiWorld(handle, normal_name, admin_name)
    finally:
        normal.delete()
        admin.delete()
        drop_seeded(meta_engine, handle)


@pytest.fixture(scope="session")
def ui_server(ui_world):
    """Startet ``runserver`` auf einem freien Port und wartet auf ``/login/``."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    log = tempfile.NamedTemporaryFile(
        prefix="starcubes-ui-", suffix=".log", delete=False, mode="w+")
    env = os.environ.copy()
    env.update({
        "STARCUBES_POSTGRES_DSN": os.environ["STARCUBES_TEST_DSN"],
        "STARCUBES_BOOTSTRAPPED": "1",
        "STARCUBES_DEBUG": "1",
        "STARCUBES_SECRET_KEY": "ui-test-secret",
        "PYTHONUNBUFFERED": "1",
    })
    proc = subprocess.Popen(
        [sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}",
         "--noreload"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(base + "/login/", timeout=1) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.2)
        else:
            proc.terminate()
            raise RuntimeError("UI-Server startete nicht innerhalb von 30 s")

        if proc.poll() is not None:
            log.flush()
            log.seek(0)
            raise RuntimeError(
                "UI-Server beendete sich sofort:\n" + log.read()[-4000:])
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.close()
        os.unlink(log.name)


@pytest.fixture(scope="session")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright ist nicht installiert")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - fehlender Browser ist ein Skip
            pytest.skip(f"Chromium startet nicht: {exc}")
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, ui_server):
    context = browser.new_context(base_url=ui_server)
    page = context.new_page()
    page._sc_errors = []  # type: ignore[attr-defined]
    page.on("pageerror", lambda exc: page._sc_errors.append(str(exc)))
    try:
        yield page
    finally:
        context.close()


def login(page, username: str, password: str = PASSWORD):
    page.goto("/login/")
    page.fill("#id_username", username)
    page.fill("#id_password", password)
    page.click("button[type=submit]")
    page.wait_for_url(ROOT_URL)


def _chart_url(world: UiWorld, **extra) -> str:
    h = world.handle
    params = {
        "name": h.name,
        "group_by": "host",
        "from": h.anchor.isoformat(),
        "to": (h.anchor + timedelta(minutes=1)).isoformat(),
        "interval": "60",
    }
    params.update(extra)
    return "/?" + urllib.parse.urlencode(params)


def _plot_totals(plot: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for slot in plot.get("data", {}).values():
        for key, value in slot.items():
            out[key] = out.get(key, 0) + int(value)
    return out


# ---------------------------------------------------------------------------

def test_login_und_logout(page, ui_world):
    login(page, ui_world.normal_name)
    page.goto("/sources/")
    assert ui_world.handle.name in page.content()

    page.evaluate("document.getElementById('logout-form').submit()")
    page.wait_for_url(ROOT_URL)
    page.goto("/sources/")
    assert "/login/" in page.url


def test_falsches_passwort_bleibt_auf_login(page, ui_world):
    page.goto("/login/")
    page.fill("#id_username", ui_world.normal_name)
    page.fill("#id_password", "falsch")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    assert "/login/" in page.url


def test_chart_rendert_und_holt_plot(page, ui_server, ui_world):
    url = ui_server + _chart_url(ui_world)
    with page.expect_response(
            lambda r: "/plot.json" in r.url and r.status == 200,
            timeout=15_000) as info:
        page.goto(url)
    plot = info.value.json()
    totals = _plot_totals(plot)
    assert totals.get("_1") == 3
    assert totals.get("_2") == 3

    canvas = page.locator("#chart")
    canvas.wait_for(state="visible")
    box = canvas.bounding_box()
    assert box and box["width"] > 100 and box["height"] > 100
    assert page._sc_errors == []


def test_filter_kommt_im_plot_an(page, ui_server, ui_world):
    url = ui_server + _chart_url(ui_world, filter="host=1")
    with page.expect_response(
            lambda r: "/plot.json" in r.url and r.status == 200,
            timeout=15_000) as info:
        page.goto(url)
    totals = _plot_totals(info.value.json())
    assert totals.get("_1") == 3
    assert "_2" not in totals


def test_rollen_sichtbarkeit_im_browser(browser, ui_server, ui_world):
    def open_as(username):
        ctx = browser.new_context(base_url=ui_server)
        page = ctx.new_page()
        try:
            login(page, username)
            page.goto(f"/sources/{ui_world.handle.id}/")
            return page.content(), page.locator("#delete-form").count()
        finally:
            ctx.close()

    normal_html, normal_delete = open_as(ui_world.normal_name)
    assert normal_delete == 0
    assert "Fact Columns (" not in normal_html
    assert "Alarms (" in normal_html

    admin_html, admin_delete = open_as(ui_world.admin_name)
    assert admin_delete == 1
    assert "Fact Columns (" in admin_html


def test_assets_und_traversal(page, ui_server):
    ok = page.request.get(ui_server + "/docs/img/chart-hero.png")
    assert ok.status == 200
    assert ok.headers["content-type"] == "image/png"

    bad = page.request.get(
        ui_server + "/presentation/%2e%2e/%2e%2e/etc/passwd")
    assert bad.status == 404

    missing = page.request.get(ui_server + "/docs/gibt-es-nicht.md")
    assert missing.status == 404
