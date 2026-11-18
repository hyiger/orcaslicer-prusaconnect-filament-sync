"""Signing in to Prusa Account, keeping the sign-in, and reading the printer list from Prusa Connect."""
import io
import json
import socket
import sys
import urllib.error
import urllib.parse

import pytest

from conftest import NotificationLevel, PluginResult, connect_printer, connect_tool, preset


# OrcaSlicer's audit hook places a file write by its path, and asks the user about, then refuses, one it cannot
# place: an open() of a bare file descriptor, as os.fdopen() makes. An audit hook cannot be removed, so this one
# acts only while a test turns it on.
DESCRIPTORS = {"denied": False, "seen": []}


def deny_descriptor_writes(event, args):
    if DESCRIPTORS["denied"] and event == "open" and isinstance(args[0], int):
        DESCRIPTORS["seen"].append(args)
        raise PermissionError("OrcaSlicer asked for permission to write to a file descriptor")


sys.addaudithook(deny_descriptor_writes)


@pytest.fixture
def descriptor_writes_denied():
    DESCRIPTORS.update(denied=True, seen=[])
    yield DESCRIPTORS["seen"]
    DESCRIPTORS["denied"] = False


def kinds(app):
    return [window.data["kind"] for window in app.windows]


def ready(app):
    """A printer with Prusament PLA Galaxy Black in tool 1, and its preset installed."""
    app.cloud.printers = [connect_printer(tools=[connect_tool("PLA", "Prusament", "PLA Galaxy Black", "#3D3D3DFF")])]
    app.filaments.add(preset("Prusament PLA Galaxy Black", "Prusament", "PLA", "#3D3D3D"))


def sign_in(app, lifetime=None):
    """Store a sign-in, as an earlier sync would have."""
    tokens = app.cloud.issue(lifetime)
    app.account_file.write_text(json.dumps({key: tokens[key] for key in ("access_token", "refresh_token")}))
    return tokens


def stored(app):
    return json.loads(app.account_file.read_text())


def token_requests(app):
    return [request for request in app.cloud.requests if request[1] == "/o/token/"]


def failed(result):
    assert result.status is PluginResult.Skipped
    return result.message


def pasting(text):
    return lambda data: {"address": text}


def http_error(code, body=b""):
    return urllib.error.HTTPError("https://connect.prusa3d.com/slicer/v2/printers", code, "Error", {},
                                  io.BytesIO(body))


class TestSigningIn:
    def test_the_first_sync_signs_in_and_keeps_the_sign_in(self, app):
        ready(app)
        result = app.sync()
        assert result.status is PluginResult.Success
        assert kinds(app) == ["sign_in"]
        tokens = stored(app)
        assert set(tokens) == {"access_token", "refresh_token"}
        assert app.account_file.stat().st_mode & 0o777 == 0o600
        method, path, headers = app.cloud.requests[-1]
        assert (method, path) == ("GET", "/slicer/v2/printers")
        assert headers["Authorization"] == "Bearer " + tokens["access_token"]
        assert app.editor.selections == [(1, "Prusament PLA Galaxy Black")]

    def test_keeps_the_sign_in_where_orcaslicer_audits_writes_by_path(self, app, descriptor_writes_denied):
        """The first sign-in must be kept at once: Prusa Account has taken the code, which works once."""
        ready(app)
        assert app.sync().status is PluginResult.Success
        assert descriptor_writes_denied == []
        assert set(stored(app)) == {"access_token", "refresh_token"}
        assert app.account_file.stat().st_mode & 0o777 == 0o600

    def test_asks_prusa_account_for_a_code_with_pkce(self, app):
        ready(app)
        app.sync()
        [window] = app.windows
        assert window.title == "Sign in to Prusa Connect"
        assert window.data["reason"] == ""
        # view-source: keeps Prusa Connect's callback page from running, and replacing the address.
        assert window.data["url"].startswith("view-source:")
        url = urllib.parse.urlsplit(window.data["url"].removeprefix("view-source:"))
        query = dict(urllib.parse.parse_qsl(url.query))
        assert url.path == "/o/authorize/"
        assert query["client_id"] == app.cloud.CLIENT_ID
        assert query["redirect_uri"] == app.cloud.REDIRECT_URI
        assert (query["response_type"], query["scope"], query["code_challenge_method"]) == ("code", "basic_info", "S256")
        assert len(query["state"]) >= 16
        # The page names the address to paste.
        assert "view-source:https://connect.prusa3d.com/login/auth-callback?code=" in window.html

    def test_a_later_sync_uses_the_kept_sign_in(self, app):
        ready(app)
        sign_in(app)
        assert app.sync().status is PluginResult.Success
        assert kinds(app) == []
        assert token_requests(app) == []

    @pytest.mark.parametrize("text", [
        "view-source:{redirect}?code={code}&state={state}",
        "{redirect}?code={code}&state={state}",
        "  {redirect}?code={code}&state={state}#fragment  ",
        "?code={code}&state={state}",
        "code={code}",
        "{code}",
    ])
    def test_takes_the_address_its_query_or_the_code(self, app, text):
        ready(app)

        def answer(data):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(data["url"]).query))
            code = app.cloud.authorize(query["code_challenge"])
            return {"address": text.format(redirect=app.cloud.REDIRECT_URI, code=code, state=query["state"])}

        app.answers["sign_in"] = answer
        assert app.sync().status is PluginResult.Success

    @pytest.mark.parametrize("answer, message", [
        (lambda data: None, "You are not signed in to Prusa Connect, so nothing was changed."),
        (lambda data: {"address": 5}, "You are not signed in to Prusa Connect, so nothing was changed."),
        (lambda data: ["no"], "You are not signed in to Prusa Connect, so nothing was changed."),
        (pasting("   "), "No address was pasted, so you are not signed in, and nothing was changed."),
        (pasting("https://connect.prusa3d.com/login/auth-callback?code=abc&state=old"),
         "That address comes from an earlier sign-in. Run this again, and paste the address this sign-in leads to."),
        (pasting("https://connect.prusa3d.com/login/auth-callback?error=access_denied"),
         "Prusa Account did not sign you in: access_denied."),
        (pasting("not a code!"), "The pasted text holds no sign-in code. Paste the address the browser shows, "
                                 "which starts with view-source:https://connect.prusa3d.com/login/auth-callback?code="),
    ])
    def test_says_why_it_did_not_sign_in(self, app, answer, message):
        ready(app)
        app.answers["sign_in"] = answer
        assert failed(app.sync()) == message
        assert app.notifications[-1] == (NotificationLevel.WarningNotificationLevel, message)
        assert not app.account_file.exists()
        assert app.editor.selections == []

    def test_a_code_prusa_account_refuses(self, app):
        """A code issued for another sign-in fails the PKCE check."""
        ready(app)
        app.answers["sign_in"] = lambda data: {"address": app.cloud.authorize("another challenge")}
        assert failed(app.sync()) == ("Prusa Account did not accept the code (Invalid code or verifier.). A code "
                                      "works once, and for a few minutes: run this again and sign in.")

    def test_needs_a_refresh_token(self, app):
        ready(app)
        app.cloud.omit_refresh = True
        assert failed(app.sync()) == "Prusa Account's reply has no refresh token. Run this again."
        assert not app.account_file.exists()

    @pytest.mark.parametrize("reply", [{"token_type": "Bearer"}, {"access_token": ""}, ["access_token"]])
    def test_needs_an_access_token(self, app, reply):
        ready(app)
        app.cloud.fail["/o/token/"] = (200, reply)
        assert failed(app.sync()) == "Prusa Account's reply has no access token. Run this again."

    @pytest.mark.parametrize("status, body, text", [
        (500, b"Internal Server Error", "HTTP 500"),
        (503, json.dumps({"error": "temporarily_unavailable"}).encode(), "temporarily_unavailable"),
        (502, b"", "HTTP 502"),
    ])
    def test_reports_an_error_from_prusa_account(self, app, status, body, text):
        ready(app)
        app.cloud.fail["/o/token/"] = (status, body)
        assert failed(app.sync()) == f"Prusa Account answered with an error: {text}."

    def test_says_when_the_sign_in_cannot_be_kept(self, app, monkeypatch):
        ready(app)
        folder = app.account_file.parent / "missing"
        monkeypatch.setattr(app.plugin, "ACCOUNT_FILE", folder / "prusa_account.json")
        message = failed(app.sync())
        assert message.startswith(f"Could not keep the Prusa Account sign-in in {folder / 'prusa_account.json'}: ")
        assert message.endswith("You will be asked to sign in again.")

    def test_a_save_error_without_a_reason(self, app, monkeypatch):
        ready(app)

        def refuse(*args):
            raise OSError("disk on fire")

        monkeypatch.setattr(app.plugin.os, "replace", refuse)
        assert "sign-in in" in failed(app.sync()) and "disk on fire" in app.notifications[-1][1]

    @pytest.mark.parametrize("content", ["not json", "[]", '{"access_token": "x"}', '{"access_token": "", "refresh_token": "y"}'])
    def test_an_unreadable_sign_in_signs_in_again(self, app, content):
        ready(app)
        app.account_file.write_text(content)
        assert app.sync().status is PluginResult.Success
        assert kinds(app) == ["sign_in"]

    @pytest.mark.parametrize("timing", ["later", "unloaded"])
    def test_waits_for_the_dialog(self, app, timing):
        ready(app)
        app.orca.host.ui.timing = timing
        result = app.sync()
        assert (result.status is PluginResult.Success) == (timing == "later")


class TestRenewing:
    def test_renews_an_access_token_that_runs_out_soon(self, app):
        ready(app)
        old = sign_in(app, lifetime=60)
        assert app.sync().status is PluginResult.Success
        [(method, path, _)] = token_requests(app)
        tokens = stored(app)
        assert tokens["refresh_token"] != old["refresh_token"]
        assert tokens["access_token"] != old["access_token"]
        # Prusa Account replaced the refresh token, so the old one is spent.
        assert old["refresh_token"] not in app.cloud.refresh_tokens

    @pytest.mark.parametrize("token", ["no-dots", "a.!!!.c", "a.W10.c", "a.eyJleHAiOiAiMSJ9.c"])
    def test_renews_an_access_token_it_cannot_read(self, app, token):
        """No payload, not base64, a list, or an exp that is not a number."""
        ready(app)
        tokens = app.cloud.issue()
        app.account_file.write_text(json.dumps({"access_token": token, "refresh_token": tokens["refresh_token"]}))
        assert app.sync().status is PluginResult.Success
        assert len(token_requests(app)) == 1

    def test_keeps_the_refresh_token_when_prusa_account_sends_none(self, app):
        ready(app)
        old = sign_in(app, lifetime=0)
        app.cloud.omit_refresh = True
        assert app.sync().status is PluginResult.Success
        assert stored(app)["refresh_token"] == old["refresh_token"]

    def test_renews_a_token_prusa_connect_turns_down(self, app):
        ready(app)
        old = sign_in(app)
        app.cloud.expire()
        assert app.sync().status is PluginResult.Success
        assert len(token_requests(app)) == 1
        assert stored(app)["access_token"] != old["access_token"]

    def test_signs_in_again_when_the_refresh_token_is_refused(self, app):
        ready(app)
        old = sign_in(app, lifetime=0)
        app.cloud.refresh_tokens.discard(old["refresh_token"])
        assert app.sync().status is PluginResult.Success
        [window] = app.windows
        assert window.data["reason"] == "Your Prusa Account sign-in has expired. Sign in again."
        assert stored(app)["refresh_token"] != old["refresh_token"]

    def test_a_sign_in_prusa_connect_keeps_turning_down(self, app):
        ready(app)
        sign_in(app)
        app.cloud.fail["/slicer/v2/printers"] = (401, {"message": "Unauthorized"})
        assert failed(app.sync()) == ("Prusa Connect did not accept your Prusa Account sign-in. Run Sign out of "
                                      "Prusa Connect, then run this again to sign in.")
        assert len(token_requests(app)) == 1

    def test_an_error_after_renewing(self, app, monkeypatch):
        ready(app)
        sign_in(app)
        replies = iter([http_error(401), http_error(500, b'{"message": "Boom"}')])

        def get_printers(tokens):
            raise next(replies)

        monkeypatch.setattr(app.plugin.Account, "get_printers", staticmethod(get_printers))
        assert failed(app.sync()) == "Prusa Connect answered with an error: Boom."


class TestReachingPrusaConnect:
    @pytest.mark.parametrize("body, text", [
        ({"message": "Forbidden for this team", "code": "FORBIDDEN"}, "Forbidden for this team"),
        ({"message": "x" * 200}, "x" * 117 + "..."),
        ({"message": "  ", "code": "FORBIDDEN"}, "HTTP 403"),
        ({"message": 5}, "HTTP 403"),
    ])
    def test_reports_prusa_connects_message(self, app, body, text):
        ready(app)
        sign_in(app)
        app.cloud.fail["/slicer/v2/printers"] = (403, body)
        assert failed(app.sync()) == f"Prusa Connect answered with an error: {text}."

    def test_a_reply_that_is_not_json(self, app):
        ready(app)
        sign_in(app)
        app.cloud.fail["/slicer/v2/printers"] = (200, b"<html>")
        assert failed(app.sync()) == "Prusa Connect sent a reply that is not JSON."

    def test_a_dropped_connection(self, app):
        ready(app)
        sign_in(app)
        app.cloud.drop.add("/slicer/v2/printers")
        assert failed(app.sync()) == "Prusa Connect closed the connection before it answered. Run this again."

    def test_an_address_it_cannot_reach(self, app, monkeypatch):
        ready(app)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        monkeypatch.setattr(app.plugin, "ACCOUNT_URL", f"http://127.0.0.1:{port}")
        assert failed(app.sync()) == "Could not reach Prusa Account. Check your internet connection."

    def test_a_denied_permission(self, app, monkeypatch):
        ready(app)

        def deny(*args, **kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(app.plugin.urllib.request, "urlopen", deny)
        assert failed(app.sync()) == ("OrcaSlicer needs your permission to reach Prusa Connect. Allow it, then run "
                                      "this again.")

    def test_identifies_itself(self, app):
        ready(app)
        app.sync()
        for _, _, headers in app.cloud.requests:
            assert headers["User-Agent"].startswith("PrusaConnectFilamentSync/")


class TestSigningOut:
    def test_forgets_the_sign_in(self, app):
        sign_in(app)
        result = app.sign_out()
        assert result.status is PluginResult.Success
        assert result.message == "Signed out of Prusa Connect. The next sync asks you to sign in."
        assert not app.account_file.exists()
        assert app.notifications[-1] == (NotificationLevel.RegularNotificationLevel, result.message)

    def test_when_not_signed_in(self, app):
        assert app.sign_out().message == "You were not signed in to Prusa Connect."

    def test_the_next_sync_signs_in(self, app):
        ready(app)
        sign_in(app)
        app.sign_out()
        app.sync()
        assert kinds(app) == ["sign_in"]


class TestPlugin:
    def test_registers_its_commands_and_asks_for_both_services(self, orca, plugin):
        plugin.PrusaConnectFilamentSync().register_capabilities()
        assert orca.registered == [plugin.SyncFromPrusaConnect, plugin.SignOutOfPrusaConnect]
        assert orca.permissions == [{"network": ["https://connect.prusa3d.com", "https://account.prusa3d.com"]}]
        assert plugin.SyncFromPrusaConnect().get_name() == "Sync filaments from Prusa Connect"
        assert plugin.SignOutOfPrusaConnect().get_name() == "Sign out of Prusa Connect"
        assert plugin.SyncFromPrusaConnect().get_default_config() == {"printer": ""}

    def test_keeps_the_sign_in_next_to_itself(self, plugin):
        assert plugin.ACCOUNT_FILE.name == "prusa_account.json"
        assert plugin.ACCOUNT_FILE.parent == plugin.Path(plugin.__file__).resolve().parent

    def test_one_run_at_a_time(self, app):
        app.plugin.RUN_LOCK.acquire()
        try:
            assert failed(app.sync()) == "Prusa Connect Filament Sync is still running."
        finally:
            app.plugin.RUN_LOCK.release()

    def test_an_unexpected_error_does_not_raise(self, app, monkeypatch):
        def broken(path):
            raise KeyError("boom")

        monkeypatch.setattr(app.plugin, "Account", broken)
        assert failed(app.sync()) == "Sync filaments from Prusa Connect failed: 'boom'"
