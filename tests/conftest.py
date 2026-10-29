"""Shared test helpers: fake willhaben pages and a stub HTTP layer.

Tests never touch the network. httpx.MockTransport routes each request to a
Python function that returns a canned response.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from immob.http import PoliteClient


def advert(advert_id: str | int, **attrs: str | list[str]) -> dict[str, Any]:
    """A willhaben advert: advert(1, PRICE="189000") -> {"id": "1", "attributes": ...}."""
    data: dict[str, Any] = {
        "id": str(advert_id),
        "attributes": {
            "attribute": [
                {"name": name, "values": value if isinstance(value, list) else [value]}
                for name, value in attrs.items()
            ]
        },
    }
    if "HEADING" in attrs:  # willhaben repeats the heading as "description"
        data["description"] = attrs["HEADING"]
    return data


def next_data_page(page_props: dict[str, Any]) -> str:
    payload = {"props": {"pageProps": page_props}, "page": "/iad/[...seo]"}
    return (
        "<!DOCTYPE html><html><head><title>willhaben</title></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


def search_page(adverts: list[dict[str, Any]], rows_found: int | None = None) -> str:
    return next_data_page(
        {
            "searchResult": {
                "rowsFound": len(adverts) if rows_found is None else rows_found,
                "rowsReturned": len(adverts),
                "advertSummaryList": {"advertSummary": adverts},
            }
        }
    )


def detail_page(advert_data: dict[str, Any]) -> str:
    return next_data_page({"advertDetails": advert_data})


Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def make_client() -> Iterator[Callable[..., PoliteClient]]:
    """Build a PoliteClient whose requests go to `handler` and which never sleeps.

    robots.txt is served from `robots_txt`; None answers 404, meaning "no rules".
    """
    clients: list[PoliteClient] = []

    def factory(handler: Handler, robots_txt: str | None = None) -> PoliteClient:
        def route(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return (
                    httpx.Response(404)
                    if robots_txt is None
                    else httpx.Response(200, text=robots_txt)
                )
            return handler(request)

        client = PoliteClient(transport=httpx.MockTransport(route), sleep=lambda _: None, delay=0)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()
