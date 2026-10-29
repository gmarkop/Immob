"""End-to-end: the CLI commands against a fake willhaben."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from immob import cli
from immob.sources.willhaben import WillhabenSource
from tests.conftest import advert, detail_page, next_data_page, search_page

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
    monkeypatch.setattr(cli, "build_source", lambda profile, _client: WillhabenSource(client))
    monkeypatch.setenv("IMMOB_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return CliRunner()


def test_crawl_search_show(runner):
    result = runner.invoke(cli.app, ["crawl", "--no-notify"])
    assert result.exit_code == 0, result.output
    assert "Seen 2 listings: 2 new" in result.output
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
