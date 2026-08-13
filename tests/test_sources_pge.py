"""The portal client, against a faked transport.

Nothing here touches the network or a credential. What is worth pinning is the
protocol shape -- the portal is Salesforce Aura, not REST, and every call is one
POST naming an Apex class and method -- and the handling of the ways it fails,
since a session that has quietly expired returns a login page with status 200.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("httpx")

from nem_rates.errors import ConfigError
from nem_rates.sources.pge import (
    APEX_ACTION,
    ENDPOINTS,
    PgeSession,
    PgeSettings,
    PortalError,
    _export_config,
    _unzip_csv,
    parse_green_button,
    time_interval,
)

CSV = """TYPE,DATE,START TIME,END TIME,IMPORT (kWh),EXPORT (kWh)
Electric usage,2026-01-01,00:00,00:59,1.5,0.0
Electric usage,2026-01-01,01:00,01:59,2.5,0.0
"""


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200, content_type: str = "application/json"):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self) -> Any:
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """Stands in for httpx.Client, recording what was sent."""

    def __init__(self, login_page: str = "", post: FakeResponse | None = None) -> None:
        self.login_page = login_page or '{"fwuid":"ABC123","token":"' + "t" * 30 + '"}'
        self.post_response = post
        self.posts: list[dict[str, Any]] = []
        self.cookies: dict[str, str] = {}

    def get(self, path: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse(self.login_page, content_type="text/html")

    def post(self, path: str, **kwargs: Any) -> FakeResponse:
        self.posts.append({"path": path, **kwargs})
        assert self.post_response is not None
        return self.post_response

    def close(self) -> None:
        pass


def session_with(client: FakeClient, tmp_path: Path) -> PgeSession:
    settings = PgeSettings(username="u", password="p", cookie_path=tmp_path / "cookies.json")
    session = PgeSession(settings)
    session._client = client  # type: ignore[assignment]
    return session


class TestSettings:
    def test_credentials_come_from_the_environment(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("PGE_USERNAME", "someone@example.invalid")
        monkeypatch.setenv("PGE_PASSWORD", "hunter2")
        settings = PgeSettings.load(dotenv_path=tmp_path / "absent.env")
        assert settings.username == "someone@example.invalid"

    def test_a_missing_credential_says_where_to_put_it(self, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("PGE_USERNAME", raising=False)
        monkeypatch.delenv("PGE_PASSWORD", raising=False)
        with pytest.raises(ConfigError, match="PGE_USERNAME"):
            PgeSettings.load(dotenv_path=tmp_path / "absent.env")

    def test_the_password_stays_out_of_the_repr(self) -> None:
        # dataclass frames are rendered in tracebacks, so a password in the
        # repr ends up in any bug report that includes one.
        settings = PgeSettings(username="u", password="s3cret")
        assert "s3cret" not in repr(settings)


class TestProtocol:
    def test_the_framework_id_is_read_fresh_from_the_portal(self, tmp_path: Path) -> None:
        # fwuid changes on each Salesforce release; a stored one fails in a way
        # that looks like a broken endpoint rather than a stale constant.
        session = session_with(FakeClient(), tmp_path)
        fwuid, token = session.bootstrap()
        assert fwuid == "ABC123"
        assert len(token) >= 20

    def test_a_login_page_without_a_bootstrap_is_named_as_such(self, tmp_path: Path) -> None:
        session = session_with(FakeClient(login_page="<html>maintenance</html>"), tmp_path)
        with pytest.raises(PortalError, match="not the Aura bootstrap"):
            session.bootstrap()

    def test_an_apex_call_names_a_class_and_method(self, tmp_path: Path) -> None:
        client = FakeClient(
            post=FakeResponse({"actions": [{"state": "SUCCESS", "returnValue": {"ok": 1}}]})
        )
        session = session_with(client, tmp_path)
        assert session.apex("bill_pdf", {"billidfrombillhistory": "abc"}) == {"ok": 1}

        sent = client.posts[0]["data"]
        message = json.loads(sent["message"])
        action = message["actions"][0]
        assert action["descriptor"] == APEX_ACTION
        assert action["params"]["classname"] == "MyAcct_DownloadBillPdf"
        assert action["params"]["method"] == "httpCalloutDownloadBill"
        assert action["params"]["params"] == {"billidfrombillhistory": "abc"}
        # The context and CSRF token travel with every call.
        assert json.loads(sent["aura.context"])["fwuid"] == "ABC123"
        assert sent["aura.token"]

    def test_html_where_json_was_expected_is_explained(self, tmp_path: Path) -> None:
        # The shape of an expired session: status 200, but a login page.
        client = FakeClient(post=FakeResponse("<html>sign in</html>", content_type="text/html"))
        session = session_with(client, tmp_path)
        with pytest.raises(PortalError, match="expected JSON"):
            session.apex("session_check")

    def test_an_apex_error_is_surfaced(self, tmp_path: Path) -> None:
        client = FakeClient(
            post=FakeResponse(
                {"actions": [{"state": "ERROR", "error": [{"message": "no such bill"}]}]}
            )
        )
        session = session_with(client, tmp_path)
        with pytest.raises(PortalError, match="no such bill"):
            session.apex("bill_pdf", {"billidfrombillhistory": "nope"})

    def test_an_unconfirmed_endpoint_says_so_when_it_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An endpoint inferred rather than observed is not a bug, but a failure
        # against one should point at itself first. Every shipped endpoint has
        # since been confirmed, so this uses a made-up one.
        from nem_rates.sources import pge

        monkeypatch.setattr(
            pge, "ENDPOINTS", {**pge.ENDPOINTS, "guess": pge.Endpoint("guess", "C", "m")}
        )
        client = FakeClient(post=FakeResponse("<html>", content_type="text/html"))
        session = session_with(client, tmp_path)
        with pytest.raises(PortalError, match="never been confirmed"):
            session.apex("guess")

    def test_an_unknown_endpoint_is_refused(self, tmp_path: Path) -> None:
        session = session_with(FakeClient(), tmp_path)
        with pytest.raises(PortalError, match="unknown endpoint"):
            session.apex("teleport")

    def test_being_a_guest_is_not_being_signed_in(self, tmp_path: Path) -> None:
        # The probe is isGuestUserCheck, which answers the opposite question and
        # succeeds for an anonymous visitor: {"returnValue": true} means "yes, a
        # guest". Reading success as "signed in" skips login, and the first real
        # request then fails with a 401 far from the cause.
        guest = FakeClient(
            post=FakeResponse(
                {"actions": [{"state": "SUCCESS", "returnValue": {"returnValue": True}}]}
            )
        )
        assert session_with(guest, tmp_path).signed_in() is False

        member = FakeClient(
            post=FakeResponse(
                {"actions": [{"state": "SUCCESS", "returnValue": {"returnValue": False}}]}
            )
        )
        assert session_with(member, tmp_path).signed_in() is True

        dead = FakeClient(post=FakeResponse("<html>", content_type="text/html"))
        assert session_with(dead, tmp_path).signed_in() is False

    def test_an_unrecognised_answer_counts_as_not_signed_in(self, tmp_path: Path) -> None:
        # Signing in again is cheap; assuming a live session is not.
        odd = FakeClient(post=FakeResponse({"actions": [{"state": "SUCCESS", "returnValue": 1}]}))
        assert session_with(odd, tmp_path).signed_in() is False

    def test_cookies_sharing_a_name_across_domains_survive(self, tmp_path: Path) -> None:
        # dict(client.cookies) raises CookieConflict on these, and the portal
        # sets several -- renderCtx among them.
        import httpx

        session = PgeSession(
            PgeSettings(username="u", password="p", cookie_path=tmp_path / "c.json")
        )
        client = httpx.Client()
        client.cookies.set("renderCtx", "a", domain="myaccount.pge.com", path="/")
        client.cookies.set("renderCtx", "b", domain="www.pge.com", path="/")
        session._client = client  # type: ignore[assignment]
        session._save_cookies()
        assert (tmp_path / "c.json").is_file()

        reopened = PgeSession(
            PgeSettings(username="u", password="p", cookie_path=tmp_path / "c.json")
        )
        reopened._client = httpx.Client()  # type: ignore[assignment]
        reopened._load_cookies()
        assert len(list(reopened._client.cookies.jar)) == 2  # type: ignore[union-attr]

    def test_an_unreadable_cookie_cache_is_discarded_not_raised(self, tmp_path: Path) -> None:
        import httpx

        path = tmp_path / "c.json"
        path.write_text('{"old": "format"}', encoding="utf-8")
        session = PgeSession(PgeSettings(username="u", password="p", cookie_path=path))
        session._client = httpx.Client()  # type: ignore[assignment]
        session._load_cookies()
        assert list(session._client.cookies.jar) == []  # type: ignore[union-attr]


class TestEndpointRegistry:
    def test_every_endpoint_records_whether_it_was_observed(self) -> None:
        # The registry is the memory of what was actually seen, so a future
        # reader can tell a confirmed action from an educated guess.
        assert all(endpoint.captured for endpoint in ENDPOINTS.values())

    def test_the_login_controller_is_the_utilitys_own(self) -> None:
        # Worth pinning, because the obvious guess is wrong: this is not
        # Salesforce's stock LightningLoginFormController, and a client written
        # against that one fails looking like bad credentials.
        login = ENDPOINTS["login"]
        assert login.classname == "MyAcct_customLoginLWCController"
        assert "not Salesforce's stock" in login.note


class TestGreenButton:
    def test_downloaded_text_parses_exactly_like_a_file(self, tmp_path: Path) -> None:
        # The whole reason the download is a separate concern from the format:
        # there must be exactly one parser, and this is what pins that.
        from nem_rates.sources import read_green_button

        path = tmp_path / "gb.csv"
        path.write_text(CSV, encoding="utf-8")
        assert parse_green_button(CSV) == read_green_button(path)

    def test_the_interval_carries_the_offset_in_force_at_each_end(self) -> None:
        # The December cycle starts in PST and, in other years, can end in PDT.
        # The portal sends the offset that applied on each end rather than one
        # for the whole span, so this is built from zoned datetimes.
        interval = time_interval(date(2025, 12, 30), date(2026, 1, 29))
        assert interval == "2025-12-30T00:00:00-08:00/2026-01-29T23:59:59-08:00"

        across = time_interval(date(2026, 3, 3), date(2026, 3, 31))
        start, end = across.split("/")
        assert start.endswith("-08:00") and end.endswith("-07:00")

    def test_the_export_asks_for_the_window_and_account_it_was_given(self) -> None:
        config = _export_config("A/B", "urn:acct", "CSV")
        assert config["timeInterval"] == "A/B"
        assert config["urns"] == ["urn:acct"]
        assert config["format"] == "CSV"
        assert config["utilityCode"] == "pge"

    def test_a_zipped_export_yields_its_csv(self) -> None:
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("pge_electric_usage_interval_data_1_2026.csv", CSV)
        assert _unzip_csv(buffer.getvalue()) == CSV

    def test_a_bare_csv_is_accepted_too(self) -> None:
        # The response shape is the portal's business; either is usable.
        assert _unzip_csv(CSV.encode("utf-8")) == CSV

    def test_an_archive_without_a_csv_is_named_as_such(self) -> None:
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "nope")
        with pytest.raises(PortalError, match="no CSV"):
            _unzip_csv(buffer.getvalue())
