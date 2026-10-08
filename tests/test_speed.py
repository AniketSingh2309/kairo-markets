"""Speed features: AMFI disk cache, screener stale-while-revalidate, warm-up guard."""

from __future__ import annotations

import datetime as dt
import time

import httpx

from tests.test_funds import fake_amfi
from tests.test_platform_api import env  # noqa: F401 - shared fixture
from tools.providers.amfi_provider import AmfiProvider


def counting(calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return fake_amfi(request)
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://test")


def test_amfi_data_survives_a_restart_via_disk(tmp_path):
    first_calls: list[str] = []
    first = AmfiProvider(client=counting(first_calls), cache_dir=tmp_path)
    past = dt.date.today() - dt.timedelta(days=400)
    day, navs = first.data.snapshot(past)
    catalog = first.data.catalog()
    code = next(iter(catalog))
    first.data.history(code)
    assert first_calls

    second_calls: list[str] = []
    second = AmfiProvider(client=counting(second_calls), cache_dir=tmp_path)  # "after a restart"
    assert second.data.snapshot(past) == (day, navs)
    assert set(second.data.catalog()) == set(catalog)
    assert second.data.history(code) == first.data.history(code)
    assert second_calls == []                                      # nothing re-downloaded


def test_screener_keeps_last_results_while_refreshing(env):  # noqa: F811
    import api.insights as ins

    client, _, _ = env
    ins._JOBS.clear(); ins._READY.clear()
    deadline = time.time() + 15
    while (body := client.get("/screener").json())["status"] != "ready" and time.time() < deadline:
        time.sleep(0.1)
    rows = body["rows"]
    assert rows
    again = client.get("/screener?refresh=true").json()
    assert again["status"] in ("refreshing", "ready") and again["rows"] == rows  # never an empty table mid-refresh


def test_warm_up_is_off_under_tests():
    from api import warmup

    assert warmup.enabled() is False
