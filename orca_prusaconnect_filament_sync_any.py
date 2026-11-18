# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Prusa Connect Filament Sync"
# description = "Selects, for each filament slot, the installed filament preset that matches the vendor, material and color Prusa Connect has for that tool of your printer."
# author = "hyiger"
# version = "0.1.0"
# type = "script"
# ///
"""Prusa Connect Filament Sync -- selects the filament presets that match what Prusa Connect says a printer holds.

"Sync filaments from Prusa Connect" signs in to Prusa Account, reads the printer list that PrusaSlicer reads,
GET /slicer/v2/printers, and for each tool of the chosen printer selects, for that tool's filament slot, the
installed filament preset that fits it best: any preset the slot's dropdown offers, from a vendor, a bundle or the
user. A material assigned on the printer's Tools page in Prusa Connect carries its brand, product name and color,
and the product name tells apart presets of one color, such as Translucent Yellow and Daffodil Yellow. Where
several presets still fit as well, the user picks among them in an HTML dialog.

The sign-in is OAuth 2 with PKCE through the Prusa Connect web app's public client. Its redirect lands on Prusa
Connect, so the user pastes that address back. The tokens are kept in the plugin's folder and renewed as needed.
See https://github.com/hyiger/PrusaSlicer/wiki/Prusa-Connect-API.
"""
import base64
import dataclasses
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import orca

SYNC_NAME = "Sync filaments from Prusa Connect"
SIGN_OUT_NAME = "Sign out of Prusa Connect"
RUN_LOCK = threading.Lock()
# OrcaSlicer asks in the background and denies the attempt that made it ask.
PERMISSION_TEXT = "OrcaSlicer needs your permission to reach Prusa Connect. Allow it, then run this again."

CONNECT_URL = "https://connect.prusa3d.com"
ACCOUNT_URL = "https://account.prusa3d.com"
PRINTERS_PATH = "/slicer/v2/printers"
AUTHORIZE_PATH = "/o/authorize/"
VIEW_SOURCE = "view-source:"
TOKEN_PATH = "/o/token/"
# The Prusa Connect web app's public OAuth client. Its redirect is an ordinary page on Prusa Connect, which
# leaves the code in the browser's address bar and history, and ignores a code whose state it did not issue.
CLIENT_ID = "MRHTlZhZqkNrrQ6FUPtjyusAz8nc59ErHXP8XkS4"
REDIRECT_URI = "https://connect.prusa3d.com/login/auth-callback"
SCOPE = "basic_info"
# The sign-in, in the plugin's own folder, which OrcaSlicer lets a plugin write without asking.
ACCOUNT_FILE = Path(__file__).resolve().parent / "prusa_account.json"
TIMEOUT_SECONDS = 10
# An access token lasts 2 hours. One this close to its end is renewed first.
REFRESH_LEEWAY_SECONDS = 120
USER_AGENT = "PrusaConnectFilamentSync/0.1 (OrcaSlicer plugin)"
# Prusa Connect's messages are short; a long one is cut to fit a notification.
MESSAGE_LENGTH = 120

# Below this CIE76 difference, two colors are one to the printer's color names, as white and off-white are.
COLOR_TOLERANCE = 25
# How a preset's color fits a tool's, best first.
SAME_COLOR, NO_COLOR, OTHER_COLOR = range(3)
# The printer's preset colors as Buddy firmware spells them, with their values. A product name is read for
# these, so a preset named for one of them is that color, whatever its color value.
PALETTE = {
    "red": (0xFF, 0x00, 0x00), "green": (0x00, 0x80, 0x00), "blue": (0x00, 0x00, 0xFF),
    "cyan": (0x00, 0xFF, 0xFF), "magenta": (0xFF, 0x00, 0xFF), "yellow": (0xFF, 0xFF, 0x00),
    "black": (0x00, 0x00, 0x00), "white": (0xFF, 0xFF, 0xFF), "orange": (0xF8, 0x65, 0x1B),
    "brown": (0x8B, 0x45, 0x13), "gray": (0x80, 0x80, 0x80), "purple": (0x80, 0x00, 0x80),
    "pink": (0xFF, 0xC0, 0xCB), "silver": (0xC0, 0xC0, 0xC0), "natural": (0xF5, 0xF5, 0xDC),
    "gold": (0xFF, 0xD7, 0x00),
}
PRINTER_COLORS = frozenset(PALETTE)
# Filament DB gives a filament with no color this one, unless its name says it is gray.
BLANK_COLOR = (0x80, 0x80, 0x80)
FLEXIBLE_MATERIALS = frozenset({"flex", "peba", "tpe", "tpu"})
# The firmware keeps 7 characters of a material's name, so PPS-CF10 arrives as PPS-CF1.
MATERIAL_NAME_LENGTH = 7
# Prusa's slicers call Prusament's vendor Prusa Polymers.
VENDOR_ALIASES = {"prusapolymers": "prusament"}
PRUSAMENT_WORDS = frozenset({"prusament", "prusa", "polymers"})
# The firmware's user vendors and colors, which go by these until the user names them.
UNNAMED_ENTRY = re.compile(r"(vendor|color)[1-8]", re.IGNORECASE)
# Carbon or glass fiber, as in PA-CF, GF15, Carbon Fiber and PETG Carbon, but not Polycarbonate, nor the
# color Carbon Black.
ABRASIVE_WORDS = re.compile(r"""
    (?<![a-z]) (?: [cg]f\d* (?![a-z])
                 | carbon (?: [\W_]?fib | (?![a-z]) (?![\W_]*(?:black|gr[ae]y)(?![a-z])) )
                 | glass [\W_]?fib )
""", re.IGNORECASE | re.VERBOSE)
# Vendors whose Carbon is carbon fiber whatever color follows it, as in Kimya PETG Carbon Black.
CARBON_FIBER_VENDORS = frozenset({"kimya"})
HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?")
NUMBER_LIST = re.compile(r"[+-]?\d*\.?\d+%?(,[+-]?\d*\.?\d+%?)*")
# Connect numbers a printer's tools from 0. An MMU's slots have a dot, as in 0.0.
TOOL_KEY = re.compile(r"0|[1-9]\d{0,2}")
# The printer a preset is for, as in "Prusament PLA @CORE One HF 0.4" and "Prusament PLA @MK4S - Copy". Its
# words say nothing about the filament.
PRINTER_PART = re.compile(r"@[^-]*")
# OrcaSlicer's mark on a preset with unsaved edits.
MODIFIED_MARK = "* "

# The dialogs, sized in DIPs. A row's detail wraps at about PICKER_DETAIL_LENGTH characters, and the rows
# scroll when they still do not fit.
DIALOG_WIDTH = 680
PICKER_BASE_HEIGHT = 170
PICKER_ROW_HEIGHT = 62
PICKER_NOTE_HEIGHT = 18
PICKER_DETAIL_LENGTH = 55
PICKER_MAX_HEIGHT = 700
SIGN_IN_HEIGHT = 470
PRINTER_BASE_HEIGHT = 190
PRINTER_ROW_HEIGHT = 30
DIALOG_POLL_SECONDS = 0.25
PAGE_STYLE = """
/* OrcaSlicer injects its theme as --orca-* variables, and default styles for plain elements that may come
   after these rules, so the rules below name an id or class to win. */
:root { --fg: var(--orca-fg, #262e30); --bg: var(--orca-bg, #ffffff); --muted: var(--orca-muted, #6b6b6b);
        --line: var(--orca-border, #dbdbdb); --accent: var(--orca-accent, #009688); --on-accent: var(--orca-accent-fg, #ffffff); }
html:root { height: 100%; }
/* The buttons stay in view, and the rows above them scroll. */
html body { margin: 0; padding: 0; height: 100%; display: flex; flex-direction: column; color: var(--fg);
            background: var(--bg); font-size: 13px;
            font-family: var(--orca-font, -apple-system, "Segoe UI", system-ui, sans-serif); }
#content { flex: 1; min-height: 0; overflow-y: auto; padding: 18px 20px 0; }
#content p { margin: 0 0 12px; }
#rows { width: 100%; border-collapse: collapse; }
#rows td { padding: 9px 6px; border: 0; border-top: 1px solid var(--line); vertical-align: top; text-align: left; }
#rows td.slot { white-space: nowrap; font-weight: 600; }
#rows td.tool { width: 34%; }
.swatch { display: inline-block; flex: none; width: 12px; height: 12px; margin-right: 7px; vertical-align: -1px;
          border: 1px solid var(--line); border-radius: 3px; }
.choice { display: flex; align-items: center; }
.choice select { flex: 1; min-width: 0; font: inherit; }
.field { display: flex; gap: 8px; margin: 0 0 14px; }
.field input { flex: 1; min-width: 0; font: inherit; padding: 4px 6px; color: var(--fg); background: var(--bg);
               border: 1px solid var(--line); border-radius: 4px; }
label.printer { display: block; padding: 6px 0; border-top: 1px solid var(--line); }
.muted { color: var(--muted); font-size: 12px; margin-top: 4px; white-space: pre-line; }
#buttons { flex: none; display: flex; justify-content: flex-end; gap: 8px; padding: 14px 20px 18px; }
button { font: inherit; padding: 5px 18px; border-radius: 6px; border: 1px solid var(--line);
         background: transparent; color: var(--fg); cursor: pointer; }
button.primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
"""
PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><style>__STYLE__</style></head>
<body>
<div id="content">__BODY__</div>
<div id="buttons"><button id="cancel">Cancel</button><button id="ok" class="primary">__OK__</button></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const data = JSON.parse(document.getElementById("data").textContent);
let submit = () => window.orca.close();
__SCRIPT__
document.getElementById("ok").onclick = () => submit();
document.getElementById("cancel").onclick = () => window.orca.close();
document.addEventListener("keydown", event => {
  if (event.key === "Escape") window.orca.close();
  // A focused list or button handles its own Enter, so Enter on Cancel cancels.
  if (event.key === "Enter" && !["SELECT", "BUTTON"].includes(event.target.tagName)) submit();
});
</script></body></html>
"""
SIGN_IN_BODY = """
<p id="reason"></p>
<p>1. Copy this address, paste it into the address bar of Chrome, Edge or Firefox, and press Return.</p>
<div class="field"><input id="url" readonly><button id="copy">Copy</button></div>
<p>2. The browser shows a page of code. Copy its address, which starts with
<b>view-source:https://connect.prusa3d.com/login/auth-callback?code=</b>, and paste it here.</p>
<div class="field"><input id="address" placeholder="view-source:https://connect.prusa3d.com/login/auth-callback?code=..."></div>
<p class="muted">If the page of code is Prusa Account's sign-in page instead, sign in at account.prusa3d.com in
that browser, then paste the address again. The address starts with view-source: so that Prusa Connect's own page
does not run, as it would take the sign-in for itself. It works once, and for a few minutes.
The sign-in is kept in the plugin's folder and renewed as you sync, so you sign in again only after a month
without syncing, or after Sign out of Prusa Connect.</p>
"""
SIGN_IN_SCRIPT = """
const reason = document.getElementById("reason");
reason.textContent = data.reason;
reason.hidden = !data.reason;
const url = document.getElementById("url");
url.value = data.url;
document.getElementById("copy").onclick = () => {
  url.select();
  if (!document.execCommand("copy") && navigator.clipboard) navigator.clipboard.writeText(data.url);
};
const address = document.getElementById("address");
address.focus();
submit = () => window.orca.submit({address: address.value});
"""
PRINTER_BODY = """
<p id="intro"></p>
<div id="printers"></div>
<p class="muted" id="hint"></p>
"""
PRINTER_SCRIPT = """
document.getElementById("intro").textContent = data.intro;
document.getElementById("hint").textContent = data.hint;
const list = document.getElementById("printers");
data.printers.forEach((label, index) => {
  const line = document.createElement("label");
  line.className = "printer";
  const radio = document.createElement("input");
  radio.type = "radio";
  radio.name = "printer";
  radio.value = index;
  radio.checked = index === 0;
  line.append(radio, " ", label);
  list.append(line);
});
submit = () => window.orca.submit({index: Number(document.querySelector("input[name=printer]:checked").value)});
"""
PICKER_BODY = """
<p id="intro"></p>
<table id="rows"></table>
<div id="notes" class="muted"></div>
"""
PICKER_SCRIPT = """
function swatch(color) {
  const mark = document.createElement("span");
  mark.className = "swatch";
  mark.style.background = color || "transparent";
  return mark;
}
document.getElementById("intro").textContent = data.intro;
const selects = data.rows.map(row => {
  const line = document.getElementById("rows").insertRow();
  const slot = line.insertCell();
  slot.className = "slot";
  slot.textContent = "Slot " + row.slot;
  const tool = line.insertCell();
  tool.className = "tool";
  tool.append(swatch(row.color), row.tool);
  const cell = line.insertCell();
  const choice = document.createElement("div");
  choice.className = "choice";
  const mark = swatch(row.options[0].color);
  const select = document.createElement("select");
  row.options.forEach((option, index) => select.add(new Option(option.label, index)));
  select.onchange = () => { mark.style.background = row.options[select.value].color || "transparent"; };
  if (row.options.length > 1) {
    choice.append(mark, select);
  } else {
    choice.append(mark, row.options[0].label);
  }
  const detail = document.createElement("div");
  detail.className = "muted";
  detail.textContent = row.detail;
  cell.append(choice, detail);
  return select;
});
document.getElementById("notes").textContent = data.notes;
submit = () => window.orca.submit({picks: selects.map(select => Number(select.value))});
"""


class SyncError(Exception):
    """A failure to report to the user as it stands."""


class SignInRefused(Exception):
    """Prusa Account turned down a code or a refresh token."""


def page(body, script, ok, data):
    # "<" is escaped so that no text can end the script element.
    text = json.dumps(data).replace("<", "\\u003c")
    return (PAGE.replace("__STYLE__", PAGE_STYLE).replace("__BODY__", body).replace("__OK__", ok)
            .replace("__SCRIPT__", script).replace("__DATA__", text))


def show_dialog(html, title, height):
    """Show *html* as a modal dialog and wait for it. Return what it submitted, or None if it was closed."""
    answers, done = [], threading.Event()

    def submitted(data):
        answers.append(data)
        done.set()

    window = orca.host.ui.create_window(html, title, DIALOG_WIDTH, height, on_close=done.set,
                                        style=orca.host.ui.WINDOW_MODAL, on_submit=submitted)
    # OrcaSlicer calls back on its own thread. Unloading the plugin closes the dialog without a call,
    # so a closed window ends the wait too.
    while not done.wait(DIALOG_POLL_SECONDS):
        if not window.is_open():
            break
    return answers[0] if answers else None


def confirmed(question, title):
    """Ask *question* with OK and Cancel. OrcaSlicer shows it in a label, where a lone & marks a shortcut key
    and disappears, so names keep theirs."""
    return orca.host.ui.message(question.replace("&", "&&"), title, "ok_cancel", "question") == "ok"


def decode_value(value):
    """Turn a serialized config value into a list of its items."""
    if NUMBER_LIST.fullmatch(value):
        return value.split(",")
    if not value.startswith('"'):
        return [value]
    # Strings are double-quoted and C-escaped, and separated by semicolons.
    items, current, position = [], [], 1
    while position < len(value):
        character = value[position]
        if character == "\\":
            position += 1
            current.append({"n": "\n", "r": "\r", "t": "\t"}.get(value[position:position + 1], value[position:position + 1]))
        elif character == '"':
            items.append("".join(current))
            current = []
            position = value.find('"', position + 1)
            if position < 0:
                break
        else:
            current.append(character)
        position += 1
    return items


def unescaped(text):
    """Undo the C-style escaping that OrcaSlicer applies to the value of a text setting."""
    return re.sub(r"\\(.)", lambda match: {"n": "\n", "r": "\r"}.get(match[1], match[1]), text)


def display_name(preset):
    """Return the name OrcaSlicer shows, without the mark it puts on a preset with unsaved edits."""
    return preset.label().removeprefix(MODIFIED_MARK)


# Talking to Prusa Account and Prusa Connect


def fetch(url, service, form=None, token=None):
    """Return the decoded JSON reply to a GET of *url*, or to a POST of *form*. An HTTP error status raises
    urllib.error.HTTPError, and anything else that goes wrong SyncError."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except PermissionError as error:
        raise SyncError(PERMISSION_TEXT) from error
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError) as error:
        raise SyncError(f"Could not reach {service}. Check your internet connection.") from error
    # urllib wraps only the errors of sending the request: these come from the reply.
    except (http.client.HTTPException, ConnectionError) as error:
        raise SyncError(f"{service} closed the connection before it answered. Run this again.") from error
    except ValueError as error:
        raise SyncError(f"{service} sent a reply that is not JSON.") from error


def reply_text(error):
    """What an error reply says: Prusa Connect's message, or Prusa Account's OAuth error, or the status."""
    try:
        body = json.loads(error.read() or b"null")
    except ValueError:
        body = None
    text = ""
    if isinstance(body, dict):
        for key in ("message", "error_description", "error"):
            if isinstance(body.get(key), str) and body[key].strip():
                text = body[key].strip()
                break
    if not text:
        return f"HTTP {error.code}"
    return text if len(text) <= MESSAGE_LENGTH else text[:MESSAGE_LENGTH - 3] + "..."


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def token_expiry(token):
    """The exp claim of a JWT, in seconds since the epoch, or 0 when it cannot be read, which renews it."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return 0
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    return expiry if type(expiry) in (int, float) else 0


def token_reply(reply):
    """Check a token reply from Prusa Account, and return it."""
    if not (isinstance(reply, dict) and isinstance(reply.get("access_token"), str) and reply["access_token"]):
        raise SyncError("Prusa Account's reply has no access token. Run this again.")
    return reply


def request_tokens(form):
    """POST *form* to Prusa Account's token endpoint. A code or refresh token it turns down raises SignInRefused."""
    try:
        return token_reply(fetch(ACCOUNT_URL + TOKEN_PATH, "Prusa Account", form=form))
    except urllib.error.HTTPError as error:
        if error.code in (400, 401):
            raise SignInRefused(reply_text(error)) from error
        raise SyncError(f"Prusa Account answered with an error: {reply_text(error)}.") from error


def code_from(text, state):
    """Return the authorization code in what the user pasted: the address Prusa Account sent the browser to,
    with or without view-source:, its query, or the code alone."""
    text = text.strip().split("#", 1)[0]
    if not text:
        raise SyncError("No address was pasted, so you are not signed in, and nothing was changed.")
    params = urllib.parse.parse_qs(text.split("?", 1)[-1])
    if "code" in params:
        if params.get("state", [state])[0] != state:
            raise SyncError("That address comes from an earlier sign-in. Run this again, and paste the address "
                            "this sign-in leads to.")
        return params["code"][0]
    if "error" in params:
        raise SyncError(f"Prusa Account did not sign you in: {params['error'][0]}.")
    if re.fullmatch(r"[A-Za-z0-9._~-]+", text):
        return text
    raise SyncError("The pasted text holds no sign-in code. Paste the address the browser shows, which starts "
                    f"with {VIEW_SOURCE}{REDIRECT_URI}?code=")


def private_file(path, flags):
    """Open *path* as a file only its owner can read. Opened by path, never by descriptor: OrcaSlicer's audit
    hook cannot place os.fdopen()'s descriptor inside the plugin's folder, so it asks the user, and refuses
    the attempt that made it ask, after Prusa Account has taken the code or replaced the refresh token."""
    return os.open(path, flags, 0o600)


class Account:
    """The Prusa Account sign-in, kept in a file. Prusa Account replaces the refresh token at each use, so the
    new one is saved before the access token is used."""

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            tokens = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(tokens, dict) and all(isinstance(tokens.get(key), str) and tokens[key]
                                            for key in ("access_token", "refresh_token")):
            return tokens
        return None

    def save(self, tokens):
        stored = {key: tokens[key] for key in ("access_token", "refresh_token")}
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(temporary, "w", encoding="utf-8", opener=private_file) as file:
                json.dump(stored, file)
            os.replace(temporary, self.path)
        except OSError as error:
            raise SyncError(f"Could not keep the Prusa Account sign-in in {self.path}: {error.strerror or error}. "
                            "You will be asked to sign in again.") from error
        return stored

    def clear(self):
        """Forget the sign-in. Return whether there was one."""
        existed = self.path.exists()
        self.path.unlink(missing_ok=True)
        return existed

    def tokens(self):
        """Return a sign-in whose access token is good for a while, signing in or renewing it as needed."""
        tokens = self.load()
        if tokens is None:
            return self.sign_in()
        if token_expiry(tokens["access_token"]) - REFRESH_LEEWAY_SECONDS <= time.time():
            return self.refresh(tokens)
        return tokens

    def refresh(self, tokens):
        try:
            renewed = request_tokens({"grant_type": "refresh_token", "client_id": CLIENT_ID,
                                      "refresh_token": tokens["refresh_token"]})
        except SignInRefused:
            # Expired, or used elsewhere: the user signs in again.
            self.clear()
            return self.sign_in("Your Prusa Account sign-in has expired. Sign in again.")
        if not (isinstance(renewed.get("refresh_token"), str) and renewed["refresh_token"]):
            renewed["refresh_token"] = tokens["refresh_token"]
        return self.save(renewed)

    def sign_in(self, reason=""):
        """Sign in with PKCE: the user signs in in a browser and pastes back the address it lands on."""
        verifier = b64url(secrets.token_bytes(64))
        state = secrets.token_urlsafe(16)
        url = ACCOUNT_URL + AUTHORIZE_PATH + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "scope": SCOPE,
            "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
            "state": state,
        })
        # Shown as view-source: so that the browser stops on Prusa Connect's callback page without running it:
        # the page would replace the address, code and all, as it cannot use a code it did not ask for.
        answer = show_dialog(page(SIGN_IN_BODY, SIGN_IN_SCRIPT, "Sign in",
                                  {"kind": "sign_in", "url": VIEW_SOURCE + url, "reason": reason}),
                             "Sign in to Prusa Connect", SIGN_IN_HEIGHT)
        address = answer.get("address") if isinstance(answer, dict) else None
        if not isinstance(address, str):
            raise SyncError("You are not signed in to Prusa Connect, so nothing was changed.")
        code = code_from(address, state)
        try:
            tokens = request_tokens({"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID,
                                     "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
        except SignInRefused as error:
            raise SyncError(f"Prusa Account did not accept the code ({error}). A code works once, and for a few "
                            "minutes: run this again and sign in.") from error
        if not (isinstance(tokens.get("refresh_token"), str) and tokens["refresh_token"]):
            raise SyncError("Prusa Account's reply has no refresh token. Run this again.")
        return self.save(tokens)

    def printers(self):
        """Read the account's printers from Prusa Connect. A token Connect turns down is renewed once."""
        tokens = self.tokens()
        try:
            return self.get_printers(tokens)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise SyncError(f"Prusa Connect answered with an error: {reply_text(error)}.") from error
        try:
            return self.get_printers(self.refresh(tokens))
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise SyncError(f"Prusa Connect did not accept your Prusa Account sign-in. Run {SIGN_OUT_NAME}, "
                                "then run this again to sign in.") from error
            raise SyncError(f"Prusa Connect answered with an error: {reply_text(error)}.") from error

    @staticmethod
    def get_printers(tokens):
        return parse_printers(fetch(CONNECT_URL + PRINTERS_PATH, "Prusa Connect", token=tokens["access_token"]))


# Reading Prusa Connect's printer list


@dataclasses.dataclass
class Tool:
    """What Prusa Connect has for one tool, numbered from 1 like the filament slots. An empty tool has no
    material, and *hardened* is None where Connect does not say. *product* is the name of a material assigned
    in Connect, such as "Pro PCTG, Translucent Yellow"."""
    number: int
    material: str
    vendor: str
    color: str
    color_hex: tuple | None
    nozzle: float | None
    hardened: bool | None
    product: str = ""

    def describe(self):
        return " ".join(part for part in (self.vendor, self.material) if part)

    def text(self):
        """The tool as the user knows it: its brand and product, or its vendor, material and color."""
        if self.product:
            return " ".join(part for part in (self.vendor, self.product) if part)
        return " ".join(part for part in (self.vendor, self.material, self.color) if part)


@dataclasses.dataclass
class ConnectPrinter:
    uuid: str
    name: str
    state: str
    model: str
    models: list
    tools: dict
    unreadable: list
    feeder: bool
    tool_count: int = 0

    def label(self):
        details = ", ".join(part for part in (self.model, self.state.capitalize()) if part)
        return f"{self.name or self.uuid} ({details})" if details else self.name or self.uuid


def parse_color(text):
    """Return the (r, g, b) of a #RRGGBB or #RRGGBBAA color, or None."""
    match = HEX_COLOR.fullmatch(text.strip())
    return tuple(bytes.fromhex(match[1])) if match else None


def text_at(node, *path):
    """The stripped string at a path of keys, or "" when a step is missing or of another type."""
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node.strip() if isinstance(node, str) else ""


def word_list(text):
    """The words of *text* in order, folded for comparison, with grey spelled gray."""
    return ["gray" if word == "grey" else word for word in re.findall(r"[0-9a-z]+", text.casefold())]


def words(text):
    return set(word_list(text))


def product_words(text):
    """The words of a product's name, where PLA+ is PLA Plus."""
    return word_list(text.replace("+", " plus "))


def palette_color(product, rgb):
    """The printer's name for a material's color: the one of its colors the product name names, or, for a
    product name that names none, the color that is exactly *rgb*, as the printer itself would name it."""
    named = list(dict.fromkeys(word for word in word_list(product) if word in PALETTE))
    if len(named) == 1 or (named and rgb is None):
        return named[-1].capitalize()
    if named:
        # "PETG Black Silver Glitter": the one closest to the product's color
        return min(named, key=lambda name: math.dist(PALETTE[name], rgb)).capitalize()
    return next((name.capitalize() for name, value in PALETTE.items() if value == rgb), "")


def read_tool(number, entry):
    """Return the tool Connect lists as *entry*, or None if the entry cannot be read."""
    if not isinstance(entry, dict):
        return None
    features = entry.get("features")
    features = features if isinstance(features, dict) else {}
    nozzle = features.get("nozzle_diameter")
    hardened = features.get("nozzle_hardened")
    tool = Tool(number, "", "", "", None, nozzle if type(nozzle) in (int, float) else None,
                hardened if isinstance(hardened, bool) else None)
    # An empty tool has no material package, or a null one.
    package = entry.get("material_package_instance")
    if not isinstance(package, dict):
        return tool
    material = package.get("package")
    material = material.get("material") if isinstance(material, dict) else None
    tool.material = text_at(material, "type") or text_at(material, "abbreviation")
    if not tool.material:
        return None
    brand = material.get("brand")
    vendor = brand.strip() if isinstance(brand, str) else text_at(brand, "name")
    tool.vendor = "" if UNNAMED_ENTRY.fullmatch(vendor) else vendor
    tool.product = text_at(material, "name")
    tool.color_hex = parse_color(text_at(material, "primary_color", "color_rgba"))
    tool.color = palette_color(tool.product, tool.color_hex)
    return tool


def read_printer(entry):
    """Return the printer Connect lists as *entry*, or None if it has no uuid."""
    uuid = text_at(entry, "uuid")
    if not uuid:
        return None
    # The printer models it can print as, most specific first: COREONE_INDX8T before COREONE_INDX.
    legacy = entry.get("legacy_printer_model")
    if isinstance(legacy, list):
        legacy = legacy[0] if legacy else ""
    supported = entry.get("supported_printer_models")
    models = [model.strip() for model in [legacy, *(supported if isinstance(supported, list) else []),
                                          entry.get("model")] if isinstance(model, str) and model.strip()]
    tools, unreadable, feeder = {}, [], False
    listed = entry.get("tools")
    for key, value in (listed.items() if isinstance(listed, dict) else ()):
        if "." in key:
            feeder = True
        elif not TOOL_KEY.fullmatch(key) or (tool := read_tool(int(key) + 1, value)) is None:
            unreadable.append(key)
        else:
            tools[tool.number] = tool
    count = entry.get("tool_count")
    tool_count = count if type(count) is int else len(tools) + len(unreadable)
    return ConnectPrinter(uuid, text_at(entry, "name"), text_at(entry, "connect_state"), text_at(entry, "model"),
                          models, tools, unreadable, feeder, tool_count)


def parse_printers(body):
    """Read the reply to GET /slicer/v2/printers: {"printers": [...]}, or that object alone in a list."""
    if isinstance(body, list) and len(body) == 1:
        body = body[0]
    listed = body.get("printers") if isinstance(body, dict) else None
    if not isinstance(listed, list):
        raise SyncError("Prusa Connect's reply does not list your printers.")
    return [printer for printer in (read_printer(entry) for entry in listed if isinstance(entry, dict)) if printer]


def fold(text):
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def model_fit(printer, printer_model):
    """How well a Connect printer fits the printer profile's model, such as "Prusa CORE One INDX 8T": the
    length of the longest model of the printer that the profile's names, so COREONE_INDX8T beats
    COREONE_INDX, and COREONEL beats COREONE for a CORE One L. 0 when none fits."""
    profile = fold(printer_model)
    return max((len(key) for key in map(fold, printer.models) if key and key in profile), default=0)


def choose_printer(printers, wanted, printer_model, tool_count=0):
    """Return the Connect printer to read: the one named in the Config tab, else the one that fits the printer
    profile best, its model, then its *tool_count* tools, else the one the user picks."""
    names = ", ".join(printer.name or printer.uuid for printer in printers)
    if not printers:
        raise SyncError("Your Prusa Connect account has no printers. Add your printer in Prusa Connect.")
    if wanted := wanted.strip():
        found = [printer for printer in printers if wanted.casefold() in (printer.uuid.casefold(), printer.name.casefold())]
        if not found:
            raise SyncError(f'No printer in your Prusa Connect account is named "{wanted}". It has {names}. '
                            f"Change printer in the Config tab of {SYNC_NAME}.")
        if len(found) == 1:
            return found[0]
        candidates, intro = found, f'Your Prusa Connect account has several printers named "{wanted}".'
    else:
        best = max(model_fit(printer, printer_model) for printer in printers)
        candidates = [printer for printer in printers if model_fit(printer, printer_model) == best]
        if best and len(candidates) > 1:
            # Connect's models of an XL, XLIS and XL5IS, share only XL with "Prusa XL 5T": its tools tell them apart.
            candidates = [printer for printer in candidates if printer.tool_count == tool_count] or candidates
        if len(candidates) == 1:
            return candidates[0]
        intro = ("Several printers in your Prusa Connect account fit this printer profile." if best else
                 f"None of the printers in your Prusa Connect account fits the printer profile's model, "
                 f"{printer_model or 'which it does not name'}.")
    hint = (f"To skip this question, put the printer's name in printer, in the Config tab of {SYNC_NAME}. "
            "Printers of the same name need their uuid there instead.")
    data = {"kind": "printer", "intro": intro + " Which one's filaments do you want?", "hint": hint,
            "printers": [printer.label() for printer in candidates]}
    answer = show_dialog(page(PRINTER_BODY, PRINTER_SCRIPT, "Read", data), SYNC_NAME,
                         PRINTER_BASE_HEIGHT + PRINTER_ROW_HEIGHT * len(candidates))
    index = answer.get("index") if isinstance(answer, dict) else None
    if not (type(index) is int and 0 <= index < len(candidates)):
        raise SyncError("Nothing was changed.")
    return candidates[index]


# Matching presets to tools


def lab(rgb):
    """Convert an sRGB color to CIELAB, under D65."""
    def linear(channel):
        channel /= 255
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    r, g, b = (linear(channel) for channel in rgb)
    x = f((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047)
    y = f(0.2126 * r + 0.7152 * g + 0.0722 * b)
    z = f((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    return 116 * y - 16, 500 * (x - y), 200 * (y - z)


def color_difference(first, second):
    """The CIE76 difference between two colors."""
    return math.dist(lab(first), lab(second))


def vendor_key(vendor):
    """Fold a vendor's name, so that 3D-Fuel is 3D Fuel and eSun is Esun."""
    folded = fold(vendor)
    return VENDOR_ALIASES.get(folded, folded)


def material_key(material):
    folded = material.strip().casefold()
    return "flex" if folded in FLEXIBLE_MATERIALS else folded


def same_material(reported, material):
    """Tell whether Connect's material is *material*. PETG and PET stay apart."""
    reported, material = material_key(reported), material_key(material)
    return reported == material or (len(reported) == MATERIAL_NAME_LENGTH and material.startswith(reported))


def names_fiber(text, vendor):
    """Whether *text* names carbon or glass fiber, as PA-CF, GF15, Carbon Fiber and ABS Carbon do. Carbon Black
    is a color, except from a vendor whose Carbon line is carbon fiber filled, as Kimya's PETG Carbon Black is."""
    return bool(ABRASIVE_WORDS.search(text)) or (
        vendor_key(vendor) in CARBON_FIBER_VENDORS and "carbon" in word_list(text))


@dataclasses.dataclass
class Candidate:
    """A filament preset, as far as matching it to a tool goes. *named* holds the words its color and kind are
    read from, *title* its shown name without the printer it is for, *parent* the name of the preset it was
    saved from, and *system* tells a vendor's preset from one the user made or installed."""
    name: str
    label: str
    vendor: str
    material: str
    color: tuple | None
    abrasive: bool
    named: frozenset
    system: bool = False
    title: str = ""
    parent: str = ""
    fiber: bool = False


IDENTITY_KEYS = ("default_filament_colour", "filament_type", "filament_vendor", "required_nozzle_HRC")


def candidate(name, label, values, parent="", system=False):
    """Describe the preset *name* from *values*, which maps a setting to its list of values.

    A user preset is also read by the name of the preset it inherits from, *parent*, as the user may have
    named it for something else, such as My Tuned PLA.
    """
    def first(key):
        return (values.get(key) or [""])[0].strip()

    material = first("filament_type")
    label_words, parent_words = (PRINTER_PART.sub(" ", text) for text in (label, parent))
    own, inherited = words(label_words), words(parent_words)
    if own & PRINTER_COLORS:
        # Spike PLA Blue, saved from Spike PLA Red, is blue.
        inherited -= PRINTER_COLORS
    named = frozenset(own | inherited)
    color = parse_color(first("default_filament_colour"))
    if color == BLANK_COLOR and "gray" not in named:
        color = None
    # Carbon or glass fiber by name or type. The INDX profiles also treat a filament that needs a hardened
    # nozzle as abrasive.
    fiber = any(names_fiber(text, first("filament_vendor")) for text in (label_words, parent_words, material))
    abrasive = fiber or first("required_nozzle_HRC") not in ("", "0")
    return Candidate(name, label, first("filament_vendor"), material, color, abrasive, named, system,
                     label_words, parent_words, fiber)


def preset_candidate(preset, label=None):
    values = {key: decode_value(preset.config_value(key) or "") for key in IDENTITY_KEYS}
    parent = unescaped(preset.config_value("inherits") or "")
    return candidate(preset.name, label or display_name(preset), values, parent, preset.is_system)


def installed_presets(filaments):
    """The filament presets that a slot's dropdown offers: those shown and compatible with the printer.

    Where two show the same name, as a vendor's preset for two nozzles may, each is labelled by its own name.
    """
    offered = [preset for preset in (filaments.preset(index) for index in range(filaments.size()))
               if preset.is_visible and preset.is_compatible and not preset.is_default]
    labels = [display_name(preset) for preset in offered]
    return [preset_candidate(preset, label if labels.count(label) == 1 else preset.name)
            for preset, label in zip(offered, labels)]


def tool_product(tool):
    """The words of the tool's product name beyond its vendor and material, in order: "Pro PCTG, Translucent
    Yellow" of 3D Fuel PCTG is pro, translucent, yellow."""
    plain = words(tool.vendor) | words(tool.material)
    return [word for word in product_words(tool.product) if word not in plain]


def color_fit(tool, preset):
    """Return SAME_COLOR, NO_COLOR or OTHER_COLOR for the color of *preset* against the tool's.

    A name says more than a color value, which a preset may leave at a default. A tool without a color counts
    against no preset. The tool's color name is one of the printer's colors, or none.
    """
    if tool.color_hex is None and not tool.color:
        return SAME_COLOR
    named, label = words(tool.color), preset.named
    if named and named <= label:
        # One of the printer's own colors, such as White, also covers close shades named otherwise, such as Ivory.
        return SAME_COLOR
    # A tool named for one of the printer's colors is not another of them, unless its product names that one
    # too, as Black Silver Glitter does Silver. Without a name, as for a color the printer's list lacks, such
    # as Teal, the color value decides.
    if named and label & (PRINTER_COLORS - named - set(tool_product(tool))):
        return OTHER_COLOR
    if preset.color is None or tool.color_hex is None:
        return NO_COLOR
    return SAME_COLOR if color_difference(tool.color_hex, preset.color) <= COLOR_TOLERANCE else OTHER_COLOR


def fit(tool, preset):
    """What tells presets apart for any tool: abrasive filament on a nozzle that is not hardened, then color."""
    return preset.abrasive and tool.hardened is False, color_fit(tool, preset)


def missing_words(tool, preset):
    """Words of the tool's product name that a preset lacks in its name, vendor and material: Pro PCTG
    Daffodil Yellow lacks Translucent of Pro PCTG, Translucent Yellow."""
    has = set(product_words(preset.title)) | set(product_words(preset.vendor)) | set(product_words(preset.material))
    return sum(1 for word in tool_product(tool) if word not in has and not word.isdigit())


def foreign_words(tool, preset):
    """The words of another product than the tool's in a preset's name: Daffodil for Pro PCTG, Translucent
    Yellow, and High Speed for PLA Gravity Grey. Words after its color, or after the name of the preset it was
    saved from, name the user's copy, such as Tuned, rather than a product. A number is a version or a share
    of a fill, as in PLA+ 2.0 and Magnetite 40%."""
    product = tool_product(tool)
    title = product_words(preset.title)
    ends = {product[-1], *word_list(tool.color)} if product else set(word_list(tool.color))
    parent = product_words(preset.parent)
    cut = next((index for index in range(len(title) - 1, -1, -1) if title[index] in ends), None)
    if cut is not None:
        title = title[:cut]
    elif not preset.system and parent and title[:len(parent)] == parent:
        title = title[:len(parent)]
    known = words(tool.vendor) | words(tool.material) | words(tool.color) | set(product) | words(preset.vendor)
    if vendor_key(preset.vendor) == "prusament":
        known |= PRUSAMENT_WORDS
    return sum(1 for word in title if word not in known and not word.isdigit())


def tool_fiber(tool):
    """Whether the tool's product names carbon or glass fiber, or carbon alone, as Kimya ABS Carbon does."""
    return names_fiber(tool.product, tool.vendor)


def product_fit(tool, preset):
    """(fiber, product): whether the preset disagrees with the product about fiber, and how far its name is
    from the product's, as the product's words it lacks and the words of another product it has. (0, 0)
    without a product name."""
    if not tool.product:
        return 0, 0
    return tool_fiber(tool) != preset.fiber, missing_words(tool, preset) + foreign_words(tool, preset)


def beats(tool, first, second):
    """Whether *first* fits the tool better than *second*, of as good a fit, by what only a product name from
    Prusa Connect tells: the fiber, then the product, when *first* is closer and names no other product.
    Between presets that each name another product, as Fire Engine Red and Toolbox Red do for Iron Red, fewer
    words is no better fit. Without a brand, a word that a product shares with a preset, such as Matte, does not
    say whose filament it is."""
    if not (tool.product and tool.vendor):
        return False
    (first_fiber, first_far), (second_fiber, second_far) = product_fit(tool, first), product_fit(tool, second)
    if first_fiber != second_fiber:
        return first_fiber < second_fiber
    return first_far < second_far and foreign_words(tool, first) == 0


def preference(tool, preset):
    """Order presets that fit equally: the product's fiber and words, Generic first for a tool without a
    vendor, then the user's own presets before a vendor's, then the name that says the least beyond vendor,
    material, color and product (PLA White before PLA Silk White), then the closest color."""
    plain = words(preset.vendor) | words(preset.material) | words(tool.color) | set(tool_product(tool))
    distance = (color_difference(tool.color_hex, preset.color)
                if tool.color_hex is not None and preset.color is not None else math.inf)
    return (*fit(tool, preset), *product_fit(tool, preset), not tool.vendor and vendor_key(preset.vendor) != "generic",
            preset.system, len(preset.named - plain), distance, preset.label.casefold())


def matches(tool, preset):
    """Vendor and material must match. A tool without a vendor goes by its material alone."""
    return same_material(tool.material, preset.material) and (
        not tool.vendor or vendor_key(tool.vendor) == vendor_key(preset.vendor))


def options(tool, presets, current):
    """Return (presets, kept, abrasive): the presets that fit the tool as well as the best one, the default
    first, and the abrasive preset of the tool's color that a nozzle which is not hardened passed over.

    Prusa Connect reports vendor, material and color, and for a material assigned in Connect its product name.
    It cannot tell a preset for one nozzle from another of the same product, and without a product name not
    plain from matte, silk or carbon fiber filament: the user picks among presets that fit as well. The slot's
    own preset is the default when it fits as well, since the user chose it.
    """
    # The slot's preset is judged as edited, since the slot uses its edits, in place of the saved copy the
    # dropdown lists. A slot may also hold a preset the dropdown no longer offers, such as a hidden one.
    pool = [preset for preset in presets if current is None or preset.name != current.name]
    ranked = [preset for preset in [*pool, *([current] if current is not None else [])] if matches(tool, preset)]
    ranked.sort(key=lambda preset: preference(tool, preset))
    tier = [preset for preset in ranked if fit(tool, preset) == fit(tool, ranked[0])]
    best = [preset for preset in tier if not any(beats(tool, other, preset) for other in tier)]
    kept = current is not None and any(preset.name == current.name for preset in best)
    if kept:
        best = [current, *(preset for preset in best if preset.name != current.name)]
    color = color_fit(tool, best[0]) if best else OTHER_COLOR
    abrasive = next((preset for preset in ranked if fit(tool, preset)[0]
                     and color_fit(tool, preset) < color and color_fit(tool, preset) <= SAME_COLOR), None)
    return best, kept, abrasive


@dataclasses.dataclass
class Step:
    """What one loaded tool's slot can get, the default first. A slot with no option is left alone."""
    tool: Tool
    was: str
    options: list
    kept: bool
    abrasive: Candidate | None = None

    @property
    def preset(self):
        return self.options[0] if self.options else None


def numbered(noun, numbers):
    joined = ", ".join(map(str, numbers[:-1])) + " and " + str(numbers[-1]) if len(numbers) > 1 else str(numbers[0])
    return f"{noun}{'s' if len(numbers) > 1 else ''} {joined}"


def css_color(rgb):
    return "#%02X%02X%02X" % rgb if rgb else ""


def step_notes(step, nozzles):
    """What to know about the slot's preset. Every option fits the tool as well, so they share these."""
    tool = step.tool
    abrasive, color = fit(tool, step.preset)
    notes = []
    tool_color = tool.color or css_color(tool.color_hex)
    if step.abrasive is not None:
        notes.append(f"{step.abrasive.label} is {tool_color}, but abrasive, and the nozzle is not hardened")
    elif color > SAME_COLOR:
        notes.append(f"no installed {tool.describe()} preset is {tool_color}")
    if abrasive:
        notes.append("it is abrasive, and the nozzle is not hardened")
    elif step.abrasive is None and tool.hardened is False and tool_fiber(tool):
        # A preset that does not name the fiber is no less abrasive in use.
        notes.append(f"{tool.product} is abrasive, and the nozzle is not hardened")
    if not tool.vendor and not step.kept:
        notes.append("Prusa Connect names no vendor: assign the filament on the printer's Tools page in Prusa Connect")
    profile_nozzle = nozzles[min(tool.number, len(nozzles)) - 1] if nozzles else None
    if tool.nozzle is not None and profile_nozzle is not None and abs(tool.nozzle - profile_nozzle) > 0.001:
        notes.append(f"the tool has a {tool.nozzle:g} mm nozzle, and the printer profile {profile_nozzle:g} mm")
    return notes


def step_line(step, nozzles):
    tool = step.tool
    if step.preset is None:
        return f"Slot {tool.number}: left alone, as no installed preset is {tool.describe()}"
    notes = step_notes(step, nozzles)
    text = (f"Slot {tool.number}: keeps {step.preset.label}" if step.kept
            else f"Slot {tool.number}: {step.preset.label}, in place of {step.was}")
    return text + (f" ({'; '.join(notes)})" if notes else "")


def pick(steps, nozzles, notes, printer):
    """Ask which preset each slot gets, where several fit as well. Return [(step, preset)], or None when the
    user cancels. *notes* says what happens to the tools that get no preset."""
    shown = [step for step in steps if step.options]
    rows = [{
        "slot": step.tool.number,
        "tool": step.tool.text(),
        "color": css_color(step.tool.color_hex),
        "detail": "; ".join([f"Now {step.was}", *step_notes(step, nozzles)]),
        "options": [{"label": preset.label, "color": css_color(preset.color)} for preset in step.options],
    } for step in shown]
    intro = (f"For some tools of {printer.name or printer.uuid}, more than one installed preset fits what Prusa "
             "Connect reports as well. Pick what each slot holds.")
    lines = len(notes) + sum(len(row["detail"]) // PICKER_DETAIL_LENGTH for row in rows)
    height = min(PICKER_MAX_HEIGHT, PICKER_BASE_HEIGHT + PICKER_ROW_HEIGHT * len(rows) + PICKER_NOTE_HEIGHT * lines)
    data = {"kind": "picker", "intro": intro, "rows": rows, "notes": "\n".join(notes)}
    answer = show_dialog(page(PICKER_BODY, PICKER_SCRIPT, "Select", data), SYNC_NAME, height)
    picks = answer.get("picks") if isinstance(answer, dict) else None
    if not (isinstance(picks, list) and len(picks) == len(shown) and all(
            type(index) is int and 0 <= index < len(step.options) for index, step in zip(picks, shown))):
        return None
    return [(step, step.options[index]) for index, step in zip(picks, shown)]


def sync(config):
    """Select, for each loaded tool of the printer in Prusa Connect, the installed preset that fits it best, or
    the one the user picks where several fit as well."""
    bundle = orca.host.preset_bundle()
    printers = Account(ACCOUNT_FILE).printers()
    printer_model = unescaped(bundle.full_config_value("printer_model") or "")
    nozzles = [float(item) for item in (bundle.full_config_value("nozzle_diameter") or "").split(",") if item]
    printer = choose_printer(printers, str(config.get("printer") or ""), printer_model, len(nozzles))
    name = printer.name or printer.uuid
    if printer.feeder:
        raise SyncError(f"{name} has an MMU, whose slots this plugin does not read yet. Nothing was changed.")
    if not printer.tools and not printer.unreadable:
        raise SyncError(f"Prusa Connect has no tools of {name} yet. Turn the printer on and connect it, "
                        "then run this again.")

    presets = installed_presets(bundle.filaments)
    slot_names = list(bundle.current_filament_preset_names())
    # A slot may hold a preset the dropdown no longer offers, such as a hidden one, and it can keep it.
    current = [preset_candidate(preset) if (preset := bundle.filaments.find_preset(slot)) is not None else None
               for slot in slot_names]
    tools = [tool for _, tool in sorted(printer.tools.items())]
    loaded = [tool for tool in tools if tool.material]
    steps = [Step(tool, current[tool.number - 1].label if current[tool.number - 1] else slot_names[tool.number - 1],
                  *options(tool, presets, current[tool.number - 1]))
             for tool in loaded if tool.number <= len(slot_names)]

    lines = [step_line(step, nozzles) for step in steps]
    others = []
    if empty := [tool.number for tool in tools if not tool.material]:
        others.append(f"Empty in Prusa Connect, so left alone: {numbered('tool', empty)}")
    if beyond := [tool.number for tool in loaded if tool.number > len(slot_names)]:
        slots = f"{len(slot_names)} filament slot{'s' if len(slot_names) != 1 else ''}"
        others.append(f"Not in this project, which has {slots}: {numbered('tool', beyond)}")
    if printer.unreadable:
        others.append("Could not read what Prusa Connect sent for tools " + ", ".join(
            str(int(key) + 1) if TOOL_KEY.fullmatch(key) else f'"{key}"' for key in printer.unreadable))

    # Where several presets fit a tool as well, the user picks; that dialog also confirms the rest.
    asked = any(len(step.options) > 1 for step in steps)
    if asked:
        picked = pick(steps, nozzles, [line for step, line in zip(steps, lines) if not step.options] + others, printer)
        if picked is None:
            raise SyncError("Nothing was changed.")
    else:
        picked = [(step, step.preset) for step in steps if step.options]
    changes = [(step, preset) for step, preset in picked if preset.name != slot_names[step.tool.number - 1]]
    lines += others
    if not changes:
        return f"Nothing to change for {name}.\n\n" + "\n".join(lines)
    question = f"Select these filament presets to match {name} in Prusa Connect?\n\n" + "\n".join(lines)
    if not asked and not confirmed(question, SYNC_NAME):
        raise SyncError("Nothing was changed.")

    selected, refused = [], []
    for step, preset in changes:
        try:
            orca.host.preset.filament.begin_update(slot_number=step.tool.number, preset_name=preset.name).commit()
        except ValueError as error:
            # Such as a mixed-color slot, which has no preset of its own.
            refused.append(f"OrcaSlicer would not select {preset.label} for slot {step.tool.number}: {error}")
        except RuntimeError as error:
            # The user cancelled the prompt about unsaved edits, so the rest is left as it is.
            done = f" Slots changed before it: {', '.join(map(str, selected))}." if selected else ""
            raise SyncError(f"Stopped at slot {step.tool.number}: {error}.{done}") from error
        else:
            selected.append(step.tool.number)
    if not selected:
        raise SyncError("\n".join([*refused, "Nothing was changed."]))
    return "\n".join([f"Selected filament presets for {numbered('slot', selected)} to match {name}.", *refused])


def sign_out(_config):
    if Account(ACCOUNT_FILE).clear():
        return "Signed out of Prusa Connect. The next sync asks you to sign in."
    return "You were not signed in to Prusa Connect."


def run_command(command, name, config):
    """Run a command and show its outcome, since a script's return value is easy to miss."""
    levels = orca.host.ui.NotificationLevel
    locked = RUN_LOCK.acquire(blocking=False)
    try:
        if not locked:
            raise SyncError("Prusa Connect Filament Sync is still running.")
        message = command(config)
    except SyncError as error:
        message = str(error)
    except Exception as error:
        # OrcaSlicer disables a plugin whose script raises, which would remove the command from the
        # Plugins window over something as passing as a preset it refused to select.
        message = f"{name} failed: {error}"
    else:
        orca.host.ui.push_notification(levels.RegularNotificationLevel, message)
        return orca.ExecutionResult.success(message)
    finally:
        if locked:
            RUN_LOCK.release()
    orca.host.ui.push_notification(levels.WarningNotificationLevel, message)
    return orca.ExecutionResult.skipped(message)


class SyncFromPrusaConnect(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return SYNC_NAME

    def get_default_config(self):
        return {"printer": ""}

    def execute(self):
        return run_command(sync, SYNC_NAME, json.loads(self.get_config()))


class SignOutOfPrusaConnect(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return SIGN_OUT_NAME

    def execute(self):
        return run_command(sign_out, SIGN_OUT_NAME, {})


@orca.plugin
class PrusaConnectFilamentSync(orca.base):
    def register_capabilities(self):
        orca.request_permissions(network=[CONNECT_URL, ACCOUNT_URL])
        orca.register_capability(SyncFromPrusaConnect)
        orca.register_capability(SignOutOfPrusaConnect)
