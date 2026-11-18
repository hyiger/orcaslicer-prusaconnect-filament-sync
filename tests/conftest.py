"""Stands in for the `orca` module, and for Prusa Account and Prusa Connect.

The stand-ins hold the rules that real testing found, because a stand-in that accepts anything would pass a
plugin that OrcaSlicer or Prusa rejects. A slot's selector offers only presets that are shown and compatible, or
the slot's own, and a selected preset's name finds the edited copy. Prusa Account checks the PKCE verifier, uses
a code once and replaces the refresh token at each use; Prusa Connect wants a current access token.
"""
import base64
import enum
import hashlib
import importlib
import itertools
import json
import sys
import threading
import time
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MODULE = "orca_prusaconnect_filament_sync_any"


class PluginResult(enum.Enum):
    Success = 0
    Skipped = 1


class ExecutionResult:
    def __init__(self, status, message):
        self.status = status
        self.message = message

    @staticmethod
    def success(message=""):
        return ExecutionResult(PluginResult.Success, message)

    @staticmethod
    def skipped(message=""):
        return ExecutionResult(PluginResult.Skipped, message)


class NotificationLevel(enum.Enum):
    RegularNotificationLevel = 0
    WarningNotificationLevel = 1


def serialized(value):
    """Format a value the way Preset.config_value() returns it: a list of strings is quoted where it needs it."""
    return f'"{value}"' if " " in value or "\n" in value else value


class FakePreset:
    def __init__(self, name, values=None, alias="", is_system=False, is_visible=True, is_compatible=True,
                 is_default=False, is_dirty=False):
        self.name = name
        self.alias = alias
        self.is_system = is_system
        self.is_visible = is_visible
        self.is_compatible = is_compatible
        self.is_default = is_default
        self.is_dirty = is_dirty
        self.values = dict(values or {})

    def config_value(self, key):
        return self.values.get(key)

    def label(self):
        return ("* " if self.is_dirty else "") + (self.alias or self.name)


def preset(name, vendor, material, color=None, hrc=None, parent=None, **flags):
    """A filament preset. A list of strings serializes quoted where needed, and inherits, a single string, never."""
    values = {"filament_vendor": serialized(vendor), "filament_type": serialized(material)}
    if color is not None:
        values["default_filament_colour"] = color
    if hrc is not None:
        values["required_nozzle_HRC"] = hrc
    if parent is not None:
        values["inherits"] = parent
    return FakePreset(name, values, **flags)


class FakeFilaments:
    """PresetCollection: the default preset, then the vendors' presets, then the user's, in order."""

    def __init__(self):
        default = FakePreset("Default Filament", {"filament_type": "PLA"}, is_default=True, is_visible=False)
        system = preset("Generic PLA @System", "Generic", "PLA", alias="Generic PLA", is_system=True)
        self.presets = {default.name: default, system.name: system}
        self.selected = system
        self.edited = system

    def add(self, *presets):
        for item in presets:
            self.presets[item.name] = item

    def size(self):
        return len(self.presets)

    def preset(self, index):
        return list(self.presets.values())[index]

    def find_preset(self, name):
        # The host's lookup hands back the edited copy for the selected preset.
        return self.edited if name == self.selected.name else self.presets.get(name)

    def select(self, name):
        self.selected = self.edited = self.presets[name]


class FakeFilamentEditor:
    """orca.host.preset.filament, which selects a preset for a 1-based slot through the slot's selector."""

    def __init__(self, filaments, slots):
        self.filaments = filaments
        self.slots = slots
        self.selections = []
        # Slots whose switch the user cancels, at the prompt about unsaved edits.
        self.cancelled_slots = set()
        self.mixed_color_slots = set()

    def begin_update(self, slot_number, preset_name=""):
        if not self.slots:
            # The project's one slot, which holds the selected preset.
            self.slots.append(self.filaments.selected.name)
        if not 1 <= slot_number <= len(self.slots):
            raise ValueError(f"Filament slot {slot_number} is out of range")
        if slot_number in self.mixed_color_slots:
            raise ValueError(f"Filament slot {slot_number} is a mixed-color virtual slot")
        target = self.filaments.presets.get(preset_name)
        offered = target is not None and target.is_visible and target.is_compatible
        if not (offered or preset_name == self.slots[slot_number - 1]):
            raise ValueError(f'Filament preset "{preset_name}" is not offered by the filament preset selector '
                             f"for filament slot {slot_number}")
        if slot_number in self.cancelled_slots:
            raise RuntimeError(f'Switching the edited filament preset to "{preset_name}" was canceled')
        self.filaments.select(preset_name)
        self.slots[slot_number - 1] = preset_name
        self.selections.append((slot_number, preset_name))
        return SimpleNamespace(commit=lambda: None)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeCloud:
    """Prusa Account's token endpoint and Prusa Connect's printer list, on one local server.

    Account issues a code for a PKCE challenge, takes it once with the matching verifier, and replaces the
    refresh token at each use. Connect answers GET /slicer/v2/printers to a current access token.
    """

    CLIENT_ID = "MRHTlZhZqkNrrQ6FUPtjyusAz8nc59ErHXP8XkS4"
    REDIRECT_URI = "https://connect.prusa3d.com/login/auth-callback"

    def __init__(self):
        self.printers = []
        self.codes = {}
        self.refresh_tokens = set()
        self.access_tokens = set()
        self.counter = itertools.count(1)
        self.lifetime = 7200
        # Overrides: a status and body for one path, a reply that is not JSON, a dropped connection.
        self.fail = {}
        self.drop = set()
        self.omit_refresh = False
        self.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def authorize(self, challenge):
        """What Prusa Account does once the user signs in: a code bound to the PKCE challenge."""
        code = f"code{next(self.counter)}"
        self.codes[code] = challenge
        return code

    def jwt(self, lifetime=None):
        claims = {"exp": int(time.time()) + (self.lifetime if lifetime is None else lifetime),
                  "jti": next(self.counter), "type": "access"}
        return ".".join([b64url(b'{"alg":"RS256"}'), b64url(json.dumps(claims).encode()), "signature"])

    def issue(self, lifetime=None):
        access, refresh = self.jwt(lifetime), f"refresh{next(self.counter)}"
        self.access_tokens.add(access)
        self.refresh_tokens.add(refresh)
        reply = {"access_token": access, "token_type": "Bearer", "expires_in": 7200, "scope": "basic_info",
                 "refresh_token": refresh}
        if self.omit_refresh:
            del reply["refresh_token"]
        return reply

    def token(self, form):
        if form.get("client_id") != self.CLIENT_ID:
            return 401, {"error": "invalid_client"}
        if form.get("grant_type") == "authorization_code":
            challenge = self.codes.pop(form.get("code"), None)
            verifier = form.get("code_verifier", "")
            if (challenge is None or form.get("redirect_uri") != self.REDIRECT_URI
                    or b64url(hashlib.sha256(verifier.encode()).digest()) != challenge):
                return 400, {"error": "invalid_grant", "error_description": "Invalid code or verifier."}
            return 200, self.issue()
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") not in self.refresh_tokens:
                return 400, {"error": "invalid_grant"}
            self.refresh_tokens.discard(form["refresh_token"])
            return 200, self.issue()
        return 400, {"error": "unsupported_grant_type"}

    def expire(self):
        """Prusa Connect stops taking the access tokens issued so far, as when they run out."""
        self.access_tokens.clear()

    def handler(self):
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data):
                body = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def answer(self, path, status, data):
                cloud.requests.append((self.command, path, dict(self.headers)))
                if path in cloud.drop:
                    return  # closes the connection without a reply
                if path in cloud.fail:
                    self.reply(*cloud.fail[path])
                else:
                    self.reply(status, data)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode()))
                if self.path != "/o/token/":
                    return self.answer(self.path, 404, {"message": "Not found"})
                self.answer(self.path, *cloud.token(form))

            def do_GET(self):
                if self.path != "/slicer/v2/printers":
                    return self.answer(self.path, 404, {"message": "Not found"})
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if token not in cloud.access_tokens:
                    return self.answer(self.path, 401, {"message": "Unauthorized", "code": "UNAUTHORIZED"})
                self.answer(self.path, 200, {"printers": cloud.printers})

        return Handler


def connect_tool(material="", brand="", product="", rgba="", nozzle=0.4, hardened=False, high_flow=True):
    """A tool as /slicer/v2/printers lists it. Without a material it is empty."""
    tool = {"class": "FFF", "type": "PRINT_HEAD",
            "features": {"nozzle_diameter": nozzle, "nozzle_high_flow": high_flow, "nozzle_hardened": hardened},
            "telemetry": {"active": False}, "material_package_instance": None}
    if material:
        material_data = {"class": "FFF", "type": material, "abbreviation": material}
        if brand:
            material_data["brand"] = {"name": brand}
        if product:
            material_data["name"] = product
        if rgba:
            material_data["primary_color"] = {"color_rgba": rgba}
        tool["material_package_instance"] = {"class": "FFF", "is_from_nfc": False, "dynamic_data": {},
                                             "package": {"class": "FFF", "material": material_data}}
    return tool


def connect_printer(name="Core One INDX", tools=(), uuid=None, legacy="COREONE_INDX8T", model="COREONE_INDX",
                    supported=None, state="IDLE"):
    """A printer as /slicer/v2/printers lists it. Its tools are numbered from "0"; None leaves one out."""
    return {
        "uuid": uuid or f"uuid-{name.casefold().replace(' ', '-')}", "name": name, "connect_state": state,
        "model": model, "base_model": model, "tool_count": len(tools), "legacy_printer_model": legacy,
        "supported_printer_models": supported if supported is not None else [legacy],
        "tools": {str(index): tool for index, tool in enumerate(tools) if tool is not None},
    }


def fake_orca():
    module = types.ModuleType("orca")
    module.ExecutionResult = ExecutionResult
    module.script = types.SimpleNamespace(ScriptPluginCapabilityBase=type("ScriptBase", (), {
        "get_config": lambda self: json.dumps(getattr(self, "config", {})),
    }))
    module.base = object
    module.plugin = lambda cls: cls
    module.registered = []
    module.permissions = []
    module.register_capability = module.registered.append
    module.request_permissions = lambda **kinds: module.permissions.append(kinds)
    module.host = types.SimpleNamespace(
        ui=types.SimpleNamespace(NotificationLevel=NotificationLevel, notifications=[], dialogs=[], answer="ok"),
        preset=types.SimpleNamespace(),
    )
    module.host.ui.push_notification = lambda level, text: module.host.ui.notifications.append((level, text))

    def message(text, title, buttons, icon):
        module.host.ui.dialogs.append(text)
        return module.host.ui.answer

    module.host.ui.message = message
    module.host.ui.WINDOW_MODAL = 1
    # What each dialog showed, and how the user answers it, by its kind. An answer of None closes the dialog.
    # The answer is given at once, "later" while the plugin waits, or never because the plugin was "unloaded".
    ui = module.host.ui
    ui.windows = []
    ui.answers = {"picker": lambda data: {"picks": [0] * len(data["rows"])},
                  "printer": lambda data: {"index": 0}}
    ui.timing = "now"

    def create_window(html, title, width, height, on_close=None, style=0, on_submit=None):
        assert style == ui.WINDOW_MODAL
        data = json.loads(html.split('<script id="data" type="application/json">')[1].split("</script>")[0])
        ui.windows.append(SimpleNamespace(html=html, data=data, title=title, size=(width, height)))

        def answer():
            result = ui.answers[data["kind"]](data)
            if result is None:
                on_close()
            else:
                on_submit(result)

        if ui.timing == "now":
            answer()
        state = {"asked": False}

        def is_open():
            if ui.timing == "later" and not state["asked"]:
                state["asked"] = True
                answer()
                return True
            return ui.timing != "unloaded"

        return SimpleNamespace(is_open=is_open)

    ui.create_window = create_window
    return module


@pytest.fixture
def orca(monkeypatch):
    module = fake_orca()
    monkeypatch.setitem(sys.modules, "orca", module)
    return module


@pytest.fixture
def plugin(orca):
    sys.modules.pop(MODULE, None)
    yield importlib.import_module(MODULE)
    sys.modules.pop(MODULE, None)


@pytest.fixture
def cloud():
    server = FakeCloud()
    yield server
    server.server.shutdown()


def signs_in(cloud):
    """The user, who opens the view-source: address in the browser, signs in, and pastes the address it shows."""
    def answer(data):
        assert data["url"].startswith("view-source:")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(data["url"].removeprefix("view-source:")).query)
        code = cloud.authorize(query["code_challenge"][0])
        return {"address": f"view-source:{cloud.REDIRECT_URI}?code={code}&state={query['state'][0]}"}
    return answer


@pytest.fixture
def app(orca, plugin, cloud, monkeypatch, tmp_path):
    """A running OrcaSlicer with the plugin loaded, an 8-tool CORE One INDX profile and a 0.4 mm nozzle.

    The user is not signed in yet, and signs in when asked. Leaving `slots` empty gives the project one filament
    slot, which holds the selected preset.
    """
    monkeypatch.setattr(plugin, "CONNECT_URL", cloud.url)
    monkeypatch.setattr(plugin, "ACCOUNT_URL", cloud.url)
    monkeypatch.setattr(plugin, "ACCOUNT_FILE", tmp_path / "prusa_account.json")
    monkeypatch.setattr(plugin, "DIALOG_POLL_SECONDS", 0.01)
    orca.host.ui.answers["sign_in"] = signs_in(cloud)
    filaments = FakeFilaments()
    slots = []
    state = SimpleNamespace(config={"nozzle_diameter": "0.4", "printer_model": "Prusa CORE One INDX 8T"})
    bundle = SimpleNamespace(
        filaments=filaments,
        full_config_value=lambda key: state.config.get(key),
        current_filament_preset_names=lambda: list(slots) or [filaments.selected.name],
    )
    orca.host.preset_bundle = lambda: bundle
    orca.host.preset.filament = FakeFilamentEditor(filaments, slots)
    command = plugin.SyncFromPrusaConnect()
    command.config = {}
    sign_out = plugin.SignOutOfPrusaConnect()
    return SimpleNamespace(
        filaments=filaments, state=state, plugin=plugin, orca=orca, cloud=cloud, slots=slots,
        editor=orca.host.preset.filament, sync=command.execute, sign_out=sign_out.execute, config=command.config,
        notifications=orca.host.ui.notifications, dialogs=orca.host.ui.dialogs, windows=orca.host.ui.windows,
        answers=orca.host.ui.answers, account_file=tmp_path / "prusa_account.json",
    )
