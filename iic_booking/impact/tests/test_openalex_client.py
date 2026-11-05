import io
import json
import urllib.error
from unittest import mock

import pytest

from iic_booking.impact import openalex


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(body):
    return _Resp(json.dumps(body).encode())


def _http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://api.openalex.org/x", code, "Too Many Requests", headers, None)


def test_api_key_and_mailto_added_only_for_openalex(settings):
    settings.OPENALEX_API_KEY = "k-123"
    settings.OPENALEX_MAILTO = "ops@example.test"
    url = openalex.with_credentials("https://api.openalex.org/works?filter=doi:10.1/x&per-page=50")
    assert "api_key=k-123" in url and "mailto=ops%40example.test" in url and "per-page=50" in url
    assert openalex.with_credentials("https://pub.orcid.org/v3.0/x/works") == "https://pub.orcid.org/v3.0/x/works"


def test_no_key_leaves_url_without_api_key(settings):
    settings.OPENALEX_API_KEY = ""
    settings.OPENALEX_MAILTO = ""
    assert "api_key" not in openalex.with_credentials("https://api.openalex.org/authors?search=a")


def test_retries_throttled_request_then_succeeds(settings):
    settings.OPENALEX_API_KEY = "k-123"
    sleeps = []
    with mock.patch("urllib.request.urlopen", side_effect=[_http_error(429, "1"), _ok({"results": [1]})]) as op:
        body = openalex.get_json("https://api.openalex.org/works?x=1", user_agent="t", sleep=sleeps.append)
    assert body == {"results": [1]}
    assert sleeps == [1.0]
    assert "api_key=k-123" in op.call_args_list[0].args[0].full_url


def test_gives_up_without_leaking_key(settings):
    settings.OPENALEX_API_KEY = "secret-key"
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(429)):
        with pytest.raises(openalex.OpenAlexError) as err:
            openalex.get_json("https://api.openalex.org/works?x=1", user_agent="t", sleep=lambda s: None)
    assert err.value.status == 429
    assert "secret-key" not in str(err.value) and "openalex.org" not in str(err.value)


def test_client_error_is_not_retried(settings):
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(400)) as op:
        with pytest.raises(openalex.OpenAlexError):
            openalex.get_json("https://api.openalex.org/works?x=1", user_agent="t", sleep=lambda s: None)
    assert op.call_count == 1
