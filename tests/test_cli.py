"""End-to-end: the CLI commands against a fake willhaben."""

import json
import os

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from immob import cli
from immob.filters import FilterProfile
from immob.notify import TelegramNotifier
from immob.sources.immoscout24 import ImmoScout24Source
from immob.sources.willhaben import WillhabenSource
from immob.storage import Storage
from tests.conftest import advert, detail_page, next_data_page, search_page
from tests.conftest import search_page as search_page_wh

ADS = [
    advert(
        1,
        HEADING="Wohnung Klagenfurt",
        PRICE="180000",
        POSTCODE="9020",
        NUMBER_OF_ROOMS="2",
        **{"ESTATE_SIZE/LIVING_AREA": "55"},
        SEO_URL="immobilien/d/wohnung-1/",
    ),
    advert(2, HEADING="Penthouse", PRICE="650000", POSTCODE="9220", SEO_URL="immobilien/d/p-2/"),
]
DETAILS = advert(1, FLOOR="2", CONSTRUCTION_YEAR="2001", BODY_DYN="Mit Tiefgaragenplatz.")


@pytest.fixture
def runner(monkeypatch, make_client, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/iad/immobilien/eigentumswohnung"):
            return httpx.Response(200, text=search_page(ADS))
        if request.url.path == "/iad/immobilien/d/wohnung-1/":
            return httpx.Response(200, text=detail_page(DETAILS))
        return httpx.Response(404)

    client = make_client(handler)
    monkeypatch.setattr(
        cli, "build_sources", lambda profile, _client: [(WillhabenSource(client), 3)]
    )
    # Tests check content, not layout: a wide console keeps tables from truncating.
    monkeypatch.setattr(cli, "console", Console(width=160))
    monkeypatch.setenv("IMMOB_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return CliRunner()


def test_crawl_search_show(runner):
    result = runner.invoke(cli.app, ["crawl", "--no-notify"])
    assert result.exit_code == 0, result.output
    assert "Seen 2 of 2 listings found: 2 new" in result.output
    assert "1 detail pages fetched" in result.output  # the penthouse is too expensive

    result = runner.invoke(cli.app, ["search"])
    assert result.exit_code == 0, result.output
    assert "1 listings matching" in result.output
    assert "€ 180.000" in result.output

    result = runner.invoke(cli.app, ["show", "2"])
    assert "rejected: price 650000 > 220000" in result.output


def test_crawl_without_telegram_config_fails_early(runner):
    result = runner.invoke(cli.app, ["crawl"])
    assert result.exit_code == 1
    assert "TELEGRAM_BOT_TOKEN" in result.output


def test_inspect_saved_page(runner, tmp_path):
    page = tmp_path / "page.html"
    page.write_text(detail_page(DETAILS), encoding="utf-8")
    result = runner.invoke(cli.app, ["inspect", str(page)])
    assert result.exit_code == 0, result.output
    assert "CONSTRUCTION_YEAR" in result.output


def test_inspect_finds_adverts_under_unexpected_keys(runner, tmp_path):
    page = tmp_path / "page.html"
    page.write_text(next_data_page({"listing": {"data": DETAILS}}), encoding="utf-8")
    result = runner.invoke(cli.app, ["inspect", str(page)])
    assert result.exit_code == 0, result.output
    assert "CONSTRUCTION_YEAR" in result.output
    assert "Path to adverts: props.pageProps.listing.data" in result.output


def test_inspect_without_adverts_shows_structure_and_saves_json(runner, tmp_path):
    page = tmp_path / "page.html"
    page.write_text(next_data_page({"results": {"total": 3, "hits": [{"x": 1}]}}), "utf-8")
    saved = tmp_path / "data.json"

    result = runner.invoke(cli.app, ["inspect", str(page), "--save-json", str(saved)])

    assert result.exit_code == 1
    assert "No advert-shaped objects found" in result.output
    assert "    results: {2 keys}" in result.output
    assert "      hits: [1 items]" in result.output
    assert json.loads(saved.read_text())["props"]["pageProps"]["results"]["total"] == 3


def test_show_text_prints_the_ad_text(runner):
    runner.invoke(cli.app, ["crawl", "--no-notify"])
    result = runner.invoke(cli.app, ["show", "1", "--text"])
    assert result.exit_code == 0, result.output
    assert "Mit Tiefgaragenplatz." in result.output


def test_build_source_uses_search_url_from_profile(make_client):
    url = "https://www.willhaben.at/iad/immobilien/eigentumswohnung/kaernten?sort=1"
    profile = FilterProfile.model_validate({"willhaben": {"search_url": url}})
    [(source, _)] = cli.build_sources(profile, make_client(lambda r: None))
    assert source.search_url == url


def _capturing_notifier(monkeypatch) -> list[dict]:
    """Make TelegramNotifier.from_env() return a notifier whose messages we record."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    def from_env(cls) -> TelegramNotifier:
        # A new notifier per call, like the real from_env: commands close theirs.
        return cls("t", "1", transport=httpx.MockTransport(handler), sleep=lambda _: None)

    monkeypatch.setattr(TelegramNotifier, "from_env", classmethod(from_env))
    return sent


def test_alert_sends_headline_and_piped_log(monkeypatch):
    sent = _capturing_notifier(monkeypatch)
    log = "crawl of willhaben stopped early: no <searchResult>\n" + "x" * 5000

    result = CliRunner().invoke(cli.app, ["alert", "immob failed on debian"], input=log)

    assert result.exit_code == 0, result.output
    text = sent[0]["text"]
    assert text.startswith("⚠️ <b>immob failed on debian</b>\n<pre>")
    assert len(text) < 4096  # Telegram's limit: only the end of a long log is kept
    assert text.endswith("x</pre>")


def test_alert_escapes_html(monkeypatch):
    sent = _capturing_notifier(monkeypatch)
    CliRunner().invoke(cli.app, ["alert", "a <b> & c"], input="<error>")
    assert sent[0]["text"] == "⚠️ <b>a &lt;b&gt; &amp; c</b>\n<pre>&lt;error&gt;</pre>"


def test_install_service_writes_units(tmp_path):
    data, units = tmp_path / "data", tmp_path / "units"
    result = CliRunner().invoke(
        cli.app,
        [
            "install-service",
            "--data-dir",
            str(data),
            "--unit-dir",
            str(units),
            "--calendar",
            "*-*-* 08:30",
        ],
    )
    assert result.exit_code == 0, result.output

    service = (units / "immob.service").read_text()
    assert f"WorkingDirectory={data}" in service
    assert "OnFailure=immob-failure.service" in service
    assert service.count("ExecStart=/") == 1 and service.rstrip().endswith("immob crawl")
    assert "OnCalendar=*-*-* 08:30" in (units / "immob.timer").read_text()
    failure = (units / "immob-failure.service").read_text()
    assert 'alert "immob failed on %H"' in failure
    for name in ("immob.service", "immob.timer", "immob-failure.service"):
        assert "${" not in (units / name).read_text()  # every placeholder filled in


def test_crawl_exits_with_error_when_search_fails(monkeypatch, make_client, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>a redesigned page</html>")

    client = make_client(handler)
    monkeypatch.setattr(
        cli, "build_sources", lambda profile, _client: [(WillhabenSource(client), 3)]
    )
    monkeypatch.setenv("IMMOB_DB", str(tmp_path / "test.db"))

    result = CliRunner().invoke(cli.app, ["crawl", "--no-notify"])

    assert result.exit_code == 1  # so systemd counts the run as failed and alerts
    assert "The search failed" in result.output


def test_notify_resend_sends_current_matches(runner, monkeypatch):
    runner.invoke(cli.app, ["crawl", "--no-notify", "--seed"])  # everything marked as seen
    sent = _capturing_notifier(monkeypatch)

    assert "Sent 0" in runner.invoke(cli.app, ["notify"]).output  # nothing pending
    result = runner.invoke(cli.app, ["notify", "--resend"])

    assert result.exit_code == 0, result.output
    assert "Sent 1 Telegram notifications." in result.output  # the flat that matches
    assert sent[0]["text"].startswith("🏠 <b>Current match</b>")


def test_notify_resend_asks_before_many_messages(runner, monkeypatch):
    monkeypatch.setattr(cli, "current_matches", lambda store, profile, max_age_days: [None] * 11)
    sent = _capturing_notifier(monkeypatch)

    result = runner.invoke(cli.app, ["notify", "--resend"], input="n\n")

    assert "Send 11 Telegram messages?" in result.output
    assert result.exit_code == 1  # aborted
    assert sent == []


def test_gone_lists_listings_that_left_the_search(runner):
    runner.invoke(cli.app, ["crawl", "--no-notify"])
    with Storage(os.environ["IMMOB_DB"]) as store:
        store.mark_gone("willhaben", seen_ids={"2"})  # listing 1 left the search

    result = runner.invoke(cli.app, ["gone"])

    assert result.exit_code == 0, result.output
    assert "1 listings gone in the last 30 days" in result.output
    assert "€ 180.000" in result.output
    assert "Gone" in runner.invoke(cli.app, ["show", "1"]).output


def test_crawl_reports_gone_after_complete_crawl(runner):
    result = runner.invoke(cli.app, ["crawl", "--no-notify"])
    assert "0 gone, 0 already known from another site." in result.output


def test_inspect_immoscout24_pages(tmp_path):
    from tests.test_immoscout24 import REAL_HIT, expose_page, search_page

    search = tmp_path / "search.html"
    search.write_text(search_page([REAL_HIT]), encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["inspect", str(search)])
    assert result.exit_code == 0, result.output
    assert "ImmoScout24 search: 1 of 1 hits" in result.output
    assert "€ 52.000" in result.output

    expose = tmp_path / "expose.html"
    expose.write_text(expose_page(), encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["inspect", str(expose)])
    assert result.exit_code == 0, result.output
    assert "hwb                 203.0" in result.output


def test_inspect_missing_file_explains():
    result = CliRunner().invoke(cli.app, ["inspect", "~/no such file.html"])
    assert result.exit_code == 1
    assert "File not found" in result.output


def test_crawl_reads_both_sites_and_links_duplicates(monkeypatch, make_client, tmp_path):
    from tests.test_immoscout24 import REAL_HIT, expose_page, search_page

    # The same flat as the willhaben listing 1 in ADS: same postcode, area, rooms, price.
    is24_hit = REAL_HIT | {
        "exposeId": "is1", "links": {}, "addressString": "9020 Klagenfurt",
        "primaryPrice": 181000, "primaryArea": 55, "numberOfRooms": 2,
    }  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/iad/immobilien/eigentumswohnung"):
            return httpx.Response(200, text=search_page_wh(ADS))
        if path == "/iad/immobilien/d/wohnung-1/":
            return httpx.Response(200, text=detail_page(DETAILS))
        if path == "/regional/kaernten":
            return httpx.Response(200, text=search_page([is24_hit]))
        if path.startswith("/expose/"):
            return httpx.Response(200, text=expose_page())
        return httpx.Response(404)

    client = make_client(handler)
    monkeypatch.setattr(
        cli,
        "build_sources",
        lambda profile, _client: [
            (WillhabenSource(client), 3),
            (
                ImmoScout24Source(
                    client, search_url="https://www.immobilienscout24.at/regional/kaernten"
                ),
                3,
            ),
        ],
    )
    monkeypatch.setattr(cli, "console", Console(width=160))
    monkeypatch.setenv("IMMOB_DB", str(tmp_path / "test.db"))

    result = CliRunner().invoke(cli.app, ["crawl", "--no-notify", "--no-details"])

    assert result.exit_code == 0, result.output
    assert "willhaben: Seen 2 of 2 listings found" in result.output
    assert "immoscout24: Seen 1 of 1 listings found" in result.output
    assert "1 already known from another site" in result.output


def test_build_sources_adds_immoscout24_when_configured(make_client):
    client = make_client(lambda r: None)
    assert [s.name for s, _ in cli.build_sources(FilterProfile(), client)] == ["willhaben"]
    url = "https://www.immobilienscout24.at/regional/kaernten/wohnung-kaufen"
    profile = FilterProfile.model_validate({"immoscout24": {"search_url": url, "max_pages": 2}})
    sources = cli.build_sources(profile, client)
    assert [(s.name, pages) for s, pages in sources] == [("willhaben", 3), ("immoscout24", 2)]
