import httpx
import pytest

from onlybirds import ebird
from onlybirds.ebird import EBirdClient, EBirdRateLimited

EXHAUSTED = {"ratelimit": '"policy";r=0,"burst";r=25', "retry-after": "5"}
OK = {"ratelimit": '"policy";r=7,"burst";r=24'}


def _client(responses: list[httpx.Response], **kwargs) -> tuple[EBirdClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    c = EBirdClient(api_key="k", **kwargs)
    c._client = httpx.Client(base_url=ebird.BASE_URL, transport=httpx.MockTransport(handler))
    return c, seen


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(ebird.time, "sleep", sleeps.append)
    return sleeps


def test_exhausted_quota_raises_without_retrying():
    c, seen = _client([httpx.Response(429, headers=EXHAUSTED)])
    with pytest.raises(EBirdRateLimited):
        c._get("/x")
    assert len(seen) == 1
    assert c.hourly_remaining == 0


def test_wait_for_quota_polls_until_recovered(no_sleep):
    c, seen = _client(
        [httpx.Response(429, headers=EXHAUSTED)] * 4 + [httpx.Response(200, headers=OK, json=[1])],
        wait_for_quota=True,
    )
    assert c._get("/x") == [1]
    assert len(seen) == 5
    assert no_sleep == [ebird.QUOTA_POLL_S] * 4
    assert c.hourly_remaining == 7


def test_burst_429_honors_retry_after(no_sleep):
    burst = {"ratelimit": '"policy";r=300,"burst";r=0', "retry-after": "3"}
    c, _ = _client([httpx.Response(429, headers=burst), httpx.Response(200, headers=OK, json=[])])
    assert c._get("/x") == []
    assert no_sleep == [3.0]
