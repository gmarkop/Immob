import httpx

SEARCH = "https://www.willhaben.at/iad/immobilien/eigentumswohnung/kaernten"


def test_params_merge_into_existing_query(make_client):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(200, text="ok")

    client = make_client(handler)
    client.get_text(f"{SEARCH}?PRICE_TO=220000&rows=30&page=4&sort=1", {"rows": 90, "page": 1})

    params = requested[0].params
    assert params["PRICE_TO"] == "220000"  # the URL's own filters survive
    assert params["sort"] == "1"
    assert (params["rows"], params["page"]) == ("90", "1")  # ours replace the URL's


def test_retries_after_429(make_client):
    responses = [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, text="ok")]
    client = make_client(lambda request: responses.pop(0))
    assert client.get_text(SEARCH) == "ok"
