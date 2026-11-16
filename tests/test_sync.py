"""Sync filaments from Prusa Connect: the tools Connect lists for a printer, against the installed presets."""
import json

import pytest

from conftest import NotificationLevel, PluginResult, connect_printer, connect_tool, preset

SYSTEM_NAME = "Generic PLA @System"
SYSTEM = "Generic PLA"


@pytest.fixture(autouse=True)
def signed_in(app):
    tokens = app.cloud.issue()
    app.account_file.write_text(json.dumps({key: tokens[key] for key in ("access_token", "refresh_token")}))


def tool(material="", brand="", product="", rgba="", **features):
    return connect_tool(material, brand, product, rgba, **features)


def printer(app, *tools, **details):
    """Give the account one printer, whose tools 1, 2 and so on are *tools*."""
    app.cloud.printers = [connect_printer(tools=list(tools), **details)]


def stock(app, *presets):
    app.filaments.add(*presets)


def slots(app, *names):
    app.slots[:] = names


def selected(app):
    return list(app.editor.selections)


def windows(app, kind):
    return [window for window in app.windows if window.data["kind"] == kind]


def picker_rows(app):
    [window] = windows(app, "picker")
    return window.data["rows"]


def labels(row):
    return [option["label"] for option in row["options"]]


def failed(result):
    assert result.status is PluginResult.Skipped
    return result.message


def succeeded(result):
    assert result.status is PluginResult.Success, result.message
    return result.message


YELLOWS = (
    preset("3D-Fuel Pro PCTG Daffodil Yellow", "3D-Fuel", "PCTG", "#FFD500"),
    preset("3D-Fuel Pro PCTG Translucent Yellow", "3D-Fuel", "PCTG", "#F5E663"),
)
TRANSLUCENT_YELLOW = tool("PCTG", "3D Fuel", "Pro PCTG, Translucent Yellow", "#F2D21BFF")


class TestProductName:
    def test_tells_presets_of_one_color_apart(self, app):
        stock(app, *YELLOWS)
        slots(app, "3D-Fuel Pro PCTG Daffodil Yellow")
        printer(app, TRANSLUCENT_YELLOW)
        message = succeeded(app.sync())
        assert windows(app, "picker") == []
        assert selected(app) == [(1, "3D-Fuel Pro PCTG Translucent Yellow")]
        assert app.dialogs == ["Select these filament presets to match Core One INDX in Prusa Connect?\n\n"
                               "Slot 1: 3D-Fuel Pro PCTG Translucent Yellow, in place of 3D-Fuel Pro PCTG Daffodil Yellow"]
        assert message == "Selected filament presets for slot 1 to match Core One INDX."
        assert app.notifications[-1] == (NotificationLevel.RegularNotificationLevel, message)

    def test_keeps_the_choice_between_presets_that_each_name_another_product(self, app):
        """Fire Engine Red and Toolbox Red are both another product than Iron Red."""
        stock(app, preset("3D-Fuel Pro PCTG Fire Engine Red", "3D-Fuel", "PCTG", "#CE2029"),
              preset("3D-Fuel Pro PCTG Toolbox Red", "3D-Fuel", "PCTG", "#B22222"))
        slots(app, "3D-Fuel Pro PCTG Fire Engine Red")
        printer(app, tool("PCTG", "3D Fuel", "Pro PCTG, Iron Red", "#8B1A1AFF"))
        message = succeeded(app.sync())
        [row] = picker_rows(app)
        assert labels(row) == ["3D-Fuel Pro PCTG Fire Engine Red", "3D-Fuel Pro PCTG Toolbox Red"]
        assert row["tool"] == "3D Fuel Pro PCTG, Iron Red"
        assert row["color"] == "#8B1A1A"
        assert selected(app) == []
        assert message.startswith("Nothing to change for Core One INDX.\n\nSlot 1: keeps 3D-Fuel Pro PCTG Fire Engine Red")

    def test_words_after_the_color_are_the_users(self, app):
        """A user's Tuned copy is no other product, and comes before the vendor's preset it copies."""
        stock(app, preset("3D-Fuel Pro PCTG Translucent Yellow", "3D-Fuel", "PCTG", "#F5E663", is_system=True),
              preset("3D-Fuel Pro PCTG Translucent Yellow Tuned", "3D-Fuel", "PCTG", "#F5E663"),
              preset("My Pro PCTG", "3D-Fuel", "PCTG", "#F5E663", parent="3D-Fuel Pro PCTG Translucent Yellow"),
              preset("3D-Fuel Pro PCTG Daffodil Yellow", "3D-Fuel", "PCTG", "#FFD500"))
        printer(app, TRANSLUCENT_YELLOW)
        succeeded(app.sync())
        [row] = picker_rows(app)
        assert labels(row) == ["3D-Fuel Pro PCTG Translucent Yellow Tuned", "3D-Fuel Pro PCTG Translucent Yellow"]

    @pytest.mark.parametrize("name, parent, system, foreign", [
        ("3D-Fuel Pro PCTG Translucent Mine", "3D-Fuel Pro PCTG Translucent @CORE One", False, 0),
        ("3D-Fuel Pro PCTG Translucent Mine", "", False, 1),
        ("3D-Fuel Pro PCTG Translucent Mine", "3D-Fuel Pro PCTG Translucent", True, 1),
        ("3D-Fuel Pro PCTG Translucent Mine", "Other PCTG", False, 1),
        ("3D-Fuel Pro PCTG Daffodil Yellow Mine", "", False, 1),
    ])
    def test_words_after_the_parents_name_are_the_users(self, plugin, name, parent, system, foreign):
        tool = plugin.Tool(1, "PCTG", "3D Fuel", "Yellow", None, None, None, "Pro PCTG, Translucent Yellow")
        values = {"filament_vendor": ["3D-Fuel"], "filament_type": ["PCTG"]}
        assert plugin.foreign_words(tool, plugin.candidate(name, name, values, parent, system)) == foreign

    def test_names_no_fiber_the_product_does_not(self, app):
        stock(app, preset("Prusament PETG Jet Black", "Prusament", "PETG", "#000000"),
              preset("Prusament PETG Carbon Fiber Black", "Prusament", "PETG", "#1A1A1A", hrc="40"))
        printer(app, tool("PETG", "Prusament", "PETG Carbon Fiber Black", "#1A1A1AFF", hardened=True))
        succeeded(app.sync())
        assert selected(app) == [(1, "Prusament PETG Carbon Fiber Black")]

    def test_fiber_comes_before_the_words(self, app):
        """A preset that agrees about fiber wins even when its name is farther from the product's."""
        stock(app, preset("Prusament PETG Black", "Prusament", "PETG", "#000000"),
              preset("Prusament PETG-CF Extra Black", "Prusament", "PETG", "#000000"))
        printer(app, tool("PETG", "Prusament", "PETG Black", "#000000FF", hardened=True))
        slots(app, "Prusament PETG-CF Extra Black")
        succeeded(app.sync())
        assert selected(app) == [(1, "Prusament PETG Black")]

    def test_carbon_alone_names_a_fiber_in_a_product(self, app):
        stock(app, preset("Kimya ABS Carbon", "Kimya", "ABS", "#000000"),
              preset("Kimya ABS Black", "Kimya", "ABS", "#000000"))
        printer(app, tool("ABS", "Kimya", "ABS Carbon Black", "#000000FF", hardened=True))
        succeeded(app.sync())
        assert selected(app) == [(1, "Kimya ABS Carbon")]

    def test_without_a_brand_the_product_only_orders(self, app):
        stock(app, preset("Overture PLA White", "Overture", "PLA", "#FFFFFF"),
              preset("Overture PLA Matte White", "Overture", "PLA", "#F5F5F5"))
        printer(app, tool("PLA", "", "PLA Matte White", "#FFFFFFFF"))
        succeeded(app.sync())
        [row] = picker_rows(app)
        assert labels(row) == ["Overture PLA Matte White", "Overture PLA White"]
        assert "Prusa Connect names no vendor" in row["detail"]
        assert selected(app) == [(1, "Overture PLA Matte White")]

    def test_a_color_the_product_names_too_is_not_another(self, app):
        stock(app, preset("Spike PETG Black Silver Glitter", "Spike", "PETG", "#202020"),
              preset("Spike PETG Silver", "Spike", "PETG", "#C0C0C0"))
        printer(app, tool("PETG", "Spike", "PETG Black Silver Glitter", "#202020FF"))
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PETG Black Silver Glitter")]

    def test_plus_reads_as_plus(self, app):
        stock(app, preset("eSun PLA+ Black", "eSun", "PLA", "#000000"),
              preset("eSun PLA Matte Black", "eSun", "PLA", "#000000"))
        printer(app, tool("PLA", "eSUN", "PLA Plus Black", "#000000FF"))
        succeeded(app.sync())
        assert selected(app) == [(1, "eSun PLA+ Black")]

    def test_numbers_and_prusas_names_are_no_other_product(self, app):
        stock(app, preset("Prusa Polymers PLA 2.0 Galaxy Black", "Prusa Polymers", "PLA", "#3D3D3D"),
              preset("Prusament PLA Blend Galaxy Black", "Prusament", "PLA", "#3D3D3D"))
        printer(app, tool("PLA", "Prusament", "PLA Galaxy Black", "#3D3D3DFF"))
        succeeded(app.sync())
        assert selected(app) == [(1, "Prusa Polymers PLA 2.0 Galaxy Black")]


class TestColor:
    def test_reads_the_color_name_from_the_product(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"),
              preset("Spike PLA Blue", "Spike", "PLA", "#0000FF"))
        # Connect's color code for the product needn't be the preset's.
        printer(app, tool("PLA", "Spike", "PLA Navy Blue", "#1B2A4AFF"))
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PLA Blue")]

    def test_a_color_without_a_name_goes_by_its_value(self, app):
        stock(app, preset("Spike PLA Teal", "Spike", "PLA", "#008080"),
              preset("Spike PLA Green", "Spike", "PLA", "#00FF00"))
        printer(app, tool("PLA", "Spike", "PLA Teal", "#008080FF"))
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PLA Teal")]

    def test_a_printer_color_without_a_product(self, app):
        """A tool Connect knows only from the printer: its type and color, which has the printer's name."""
        stock(app, preset("Polymaker PETG Cyan", "Polymaker", "PETG", "#00B0F0"),
              preset("Generic PETG", "Generic", "PETG", is_system=True))
        printer(app, tool("PETG", rgba="#00FFFF"))
        succeeded(app.sync())
        assert selected(app) == [(1, "Polymaker PETG Cyan")]

    def test_a_color_name_without_a_value(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"), preset("Spike PLA", "Spike", "PLA"))
        printer(app, tool("PLA", "Spike", "PLA Red"))
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PLA Red")]

    def test_a_tool_without_a_color_counts_against_no_preset(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        printer(app, tool("PLA", "Spike", "PLA"))
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PLA Red")]

    def test_filament_dbs_blank_gray_is_no_color(self, app):
        stock(app, preset("Spike PLA", "Spike", "PLA", "#808080"), preset("Spike PLA Mint", "Spike", "PLA", "#98FF98"))
        printer(app, tool("PLA", "Spike", "PLA Gray", "#808080FF"))
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PLA")]
        assert app.dialogs[0].endswith("Slot 1: Spike PLA, in place of Generic PLA (no installed Spike PLA preset "
                                       "is Gray)")

    def test_notes_the_closest_color(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        printer(app, tool("PLA", "Spike", "PLA Teal", "#008080FF"))
        succeeded(app.sync())
        assert app.dialogs == ["Select these filament presets to match Core One INDX in Prusa Connect?\n\n"
                               "Slot 1: Spike PLA Red, in place of Generic PLA (no installed Spike PLA preset is #008080)"]

    @pytest.mark.parametrize("product, rgba, color", [
        ("PLA Galaxy Black", "#3D3D3DFF", "Black"),
        ("PLA Light Grey", "", "Gray"),
        ("PETG Black Silver Glitter", "#B0B0B0FF", "Silver"),
        ("PETG Black Silver Glitter", "", "Silver"),
        ("PLA Teal", "#00FFFFFF", "Cyan"),
        ("PLA Teal", "#008080FF", ""),
        ("", "#F8651B", "Orange"),
    ])
    def test_palette_names(self, plugin, product, rgba, color):
        assert plugin.palette_color(product, plugin.parse_color(rgba)) == color


class TestAbrasive:
    def test_an_abrasive_preset_comes_last_on_a_nozzle_that_is_not_hardened(self, app):
        stock(app, preset("Prusament PETG Carbon Fiber Black", "Prusament", "PETG", "#000000"),
              preset("Prusament PETG Orange", "Prusament", "PETG", "#F8651B"))
        printer(app, tool("PETG", "Prusament", "PETG Black", "#000000FF", hardened=False))
        succeeded(app.sync())
        assert selected(app) == [(1, "Prusament PETG Orange")]
        assert app.dialogs[0].endswith("(Prusament PETG Carbon Fiber Black is Black, but abrasive, and the nozzle "
                                       "is not hardened)")

    def test_notes_an_abrasive_only_match(self, app):
        stock(app, preset("Prusament PETG Carbon Fiber Black", "Prusament", "PETG", "#000000"))
        printer(app, tool("PETG", "Prusament", "PETG Carbon Fiber Black", "#000000FF", hardened=False))
        succeeded(app.sync())
        assert app.dialogs[0].endswith("(it is abrasive, and the nozzle is not hardened)")

    def test_needs_no_hardened_nozzle_when_connect_does_not_say(self, app):
        entry = tool("PETG", "Prusament", "PETG Black", "#000000FF")
        del entry["features"]["nozzle_hardened"]
        stock(app, preset("Prusament PETG Carbon Fiber Black", "Prusament", "PETG", "#000000"),
              preset("Prusament PETG Orange", "Prusament", "PETG", "#F8651B"))
        printer(app, entry)
        succeeded(app.sync())
        assert selected(app) == [(1, "Prusament PETG Carbon Fiber Black")]


class TestTools:
    def test_tool_n_is_slot_n(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"), preset("Spike PLA Blue", "Spike", "PLA", "#0000FF"))
        slots(app, SYSTEM_NAME, SYSTEM_NAME, SYSTEM_NAME)
        printer(app, tool("PLA", "Spike", "PLA Red", "#FF0000FF"), None, tool("PLA", "Spike", "PLA Blue", "#0000FFFF"))
        assert succeeded(app.sync()) == "Selected filament presets for slots 1 and 3 to match Core One INDX."
        assert selected(app) == [(1, "Spike PLA Red"), (3, "Spike PLA Blue")]

    def test_reports_empty_tools_tools_beyond_the_project_and_unreadable_ones(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        slots(app, SYSTEM_NAME, SYSTEM_NAME)
        entry = connect_printer()
        entry["tools"] = {"0": tool("PLA", "Spike", "PLA Red", "#FF0000FF"), "1": tool(), "2": "broken",
                          "3": {"material_package_instance": {"package": {"material": {"name": "Mystery"}}}},
                          "x": tool("PLA"), "007": tool("PLA"), "5": tool("PLA", "Spike", "PLA Red", "#FF0000FF"),
                          "6": tool("PLA", "Spike", "PLA Red")}
        app.cloud.printers = [entry]
        succeeded(app.sync())
        assert app.dialogs == ["Select these filament presets to match Core One INDX in Prusa Connect?\n\n"
                               "Slot 1: Spike PLA Red, in place of Generic PLA\n"
                               "Empty in Prusa Connect, so left alone: tool 2\n"
                               "Not in this project, which has 2 filament slots: tools 6 and 7\n"
                               'Could not read what Prusa Connect sent for tools 3, 4, "x", "007"']

    def test_one_slot_in_the_project(self, app):
        printer(app, tool("PLA", "Spike", "PLA Red", "#FF0000FF"), tool("PLA", "Spike", "PLA Red", "#FF0000FF"))
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        succeeded(app.sync())
        assert "Not in this project, which has 1 filament slot: tool 2" in app.dialogs[0]

    def test_a_tool_with_no_preset_is_left_alone(self, app):
        printer(app, tool("ASA", "Spike", "ASA Red", "#FF0000FF"))
        message = succeeded(app.sync())
        assert message == ("Nothing to change for Core One INDX.\n\n"
                           "Slot 1: left alone, as no installed preset is Spike ASA")

    def test_an_mmu_is_not_read_yet(self, app):
        entry = connect_printer(name="MK4", legacy="MK4MMU3", tools=[tool("PLA")])
        entry["tools"]["0.0"] = tool("PLA")
        app.cloud.printers = [entry]
        assert failed(app.sync()) == "MK4 has an MMU, whose slots this plugin does not read yet. Nothing was changed."

    @pytest.mark.parametrize("tools", [{}, None, []])
    def test_a_printer_with_no_tools_yet(self, app, tools):
        entry = connect_printer()
        entry["tools"] = tools
        app.cloud.printers = [entry]
        assert failed(app.sync()) == ("Prusa Connect has no tools of Core One INDX yet. Turn the printer on and "
                                      "connect it, then run this again.")

    def test_notes_a_nozzle_unlike_the_profiles(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        slots(app, SYSTEM_NAME, SYSTEM_NAME)
        app.state.config["nozzle_diameter"] = "0.4,0.6"
        printer(app, tool("PLA", "Spike", "PLA Red", "#FF0000FF", nozzle=0.6),
                tool("PLA", "Spike", "PLA Red", "#FF0000FF", nozzle=0.6))
        succeeded(app.sync())
        assert app.dialogs[0].splitlines()[2:] == [
            "Slot 1: Spike PLA Red, in place of Generic PLA (the tool has a 0.6 mm nozzle, and the printer profile 0.4 mm)",
            "Slot 2: Spike PLA Red, in place of Generic PLA",
        ]

    def test_without_nozzles_in_the_profile(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        app.state.config["nozzle_diameter"] = None
        printer(app, tool("PLA", "Spike", "PLA Red", "#FF0000FF", nozzle=0.6))
        succeeded(app.sync())
        assert "nozzle" not in app.dialogs[0]


class TestReadingConnect:
    def test_takes_the_list_alone_in_an_array(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        app.cloud.fail["/slicer/v2/printers"] = (200, [{"printers": [connect_printer(tools=[
            tool("PLA", "Spike", "PLA Red", "#FF0000FF")])]}])
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PLA Red")]

    @pytest.mark.parametrize("body", [{}, {"printers": {}}, [], [{"printers": []}, {}], "printers"])
    def test_a_reply_without_a_list(self, app, body):
        app.cloud.fail["/slicer/v2/printers"] = (200, body)
        assert failed(app.sync()) == "Prusa Connect's reply does not list your printers."

    def test_skips_printers_it_cannot_read(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        good = connect_printer(tools=[tool("PLA", "Spike", "PLA Red", "#FF0000FF")])
        app.cloud.fail["/slicer/v2/printers"] = (200, {"printers": ["broken", {"name": "No uuid"}, {"uuid": " "}, good]})
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PLA Red")]

    def test_no_printers(self, app):
        app.cloud.printers = []
        assert failed(app.sync()) == "Your Prusa Connect account has no printers. Add your printer in Prusa Connect."

    @pytest.mark.parametrize("brand", ["Spike", {"name": " Spike "}])
    def test_a_brand_as_a_name_or_an_object(self, app, brand):
        entry = tool("PLA", product="PLA Red", rgba="#FF0000FF")
        entry["material_package_instance"]["package"]["material"]["brand"] = brand
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"), preset("Other PLA Red", "Other", "PLA", "#FF0000"))
        printer(app, entry)
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PLA Red")]

    def test_an_unnamed_vendor_is_no_vendor(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        printer(app, tool("PLA", "VENDOR3", "", "#FF0000FF"))
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PLA Red")]
        assert "Prusa Connect names no vendor" in app.dialogs[0]

    def test_the_abbreviation_when_there_is_no_type(self, app):
        entry = tool("PETG", "Spike", "PETG Red", "#FF0000FF")
        del entry["material_package_instance"]["package"]["material"]["type"]
        stock(app, preset("Spike PETG Red", "Spike", "PETG", "#FF0000"))
        printer(app, entry)
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PETG Red")]

    def test_features_it_cannot_read(self, app):
        entry = tool("PLA", "Spike", "PLA Red", "nonsense")
        entry["features"] = {"nozzle_diameter": True, "nozzle_hardened": "yes"}
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        printer(app, entry)
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PLA Red")]
        entry["features"] = "none"
        app.cloud.printers[0]["tools"]["0"] = entry
        slots(app, SYSTEM_NAME)
        succeeded(app.sync())


INDX_8T = dict(name="INDX 8", legacy="COREONE_INDX8T", model="COREONE_INDX", supported=["COREONE_INDX8T"])
INDX_4T = dict(name="INDX 4", legacy="COREONE_INDX4T", model="COREONE_INDX", supported=["COREONE_INDX4T"])
MK4S = dict(name="MK4S", legacy="MK4S", model="MK4S", supported=["MK4S", "MK4"])


def printers(app, *details):
    app.cloud.printers = [connect_printer(tools=[tool("PLA", "Spike", "PLA Red", "#FF0000FF")], **d) for d in details]
    stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))


class TestChoosingThePrinter:
    @pytest.mark.parametrize("model, name", [
        ("Prusa CORE One INDX 8T", "INDX 8"),
        ("Prusa CORE One INDX 4T", "INDX 4"),
        ("Prusa MK4S HF", "MK4S"),
    ])
    def test_reads_the_printer_that_fits_the_profile(self, app, model, name):
        printers(app, INDX_4T, INDX_8T, MK4S)
        app.state.config["printer_model"] = model
        assert succeeded(app.sync()).endswith(f"to match {name}.")
        assert windows(app, "printer") == []

    def test_asks_when_several_fit(self, app):
        printers(app, INDX_8T, dict(INDX_8T, name="INDX 8 B", uuid="b", state="PRINTING"))
        app.answers["printer"] = lambda data: {"index": 1}
        assert succeeded(app.sync()).endswith("to match INDX 8 B.")
        [window] = windows(app, "printer")
        assert window.data["intro"] == ("Several printers in your Prusa Connect account fit this printer profile. "
                                        "Which one's filaments do you want?")
        assert window.data["printers"] == ["INDX 8 (COREONE_INDX, Idle)", "INDX 8 B (COREONE_INDX, Printing)"]
        assert "Config tab of Sync filaments from Prusa Connect" in window.data["hint"]

    @pytest.mark.parametrize("model, shown", [("Prusa MINI", "Prusa MINI"), (None, "which it does not name")])
    def test_asks_when_none_fits(self, app, model, shown):
        printers(app, INDX_8T, MK4S)
        app.state.config["printer_model"] = model
        succeeded(app.sync())
        [window] = windows(app, "printer")
        assert window.data["intro"].startswith("None of the printers in your Prusa Connect account fits the printer "
                                               f"profile's model, {shown}.")

    def test_reads_an_only_printer_that_does_not_fit(self, app):
        printers(app, MK4S)
        succeeded(app.sync())
        assert windows(app, "printer") == []

    @pytest.mark.parametrize("answer", [None, {"index": 5}, {"index": "0"}, {"index": True}, []])
    def test_nothing_changes_when_the_question_is_cancelled(self, app, answer):
        printers(app, INDX_8T, dict(INDX_8T, name="Other", uuid="b"))
        app.answers["printer"] = lambda data: answer
        assert failed(app.sync()) == "Nothing was changed."
        assert selected(app) == []

    @pytest.mark.parametrize("wanted", ["mk4s", " uuid-mk4s "])
    def test_reads_the_printer_named_in_the_config_tab(self, app, wanted):
        printers(app, INDX_8T, MK4S)
        app.config["printer"] = wanted
        assert succeeded(app.sync()).endswith("to match MK4S.")

    def test_a_printer_name_it_cannot_find(self, app):
        printers(app, INDX_8T, MK4S)
        app.config["printer"] = "Nope"
        assert failed(app.sync()) == ('No printer in your Prusa Connect account is named "Nope". It has INDX 8, MK4S. '
                                      "Change printer in the Config tab of Sync filaments from Prusa Connect.")

    def test_asks_among_printers_of_the_same_name(self, app):
        printers(app, MK4S, dict(MK4S, uuid="second"), INDX_8T)
        app.config["printer"] = "MK4S"
        succeeded(app.sync())
        [window] = windows(app, "printer")
        assert window.data["intro"].startswith('Your Prusa Connect account has several printers named "MK4S".')
        assert len(window.data["printers"]) == 2

    def test_labels_a_printer_by_what_it_has(self, plugin):
        bare = plugin.ConnectPrinter("u1", "", "", "", [], {}, [], False)
        assert bare.label() == "u1"
        assert plugin.ConnectPrinter("u1", "Mine", "", "", [], {}, [], False).label() == "Mine"

    def test_reads_a_legacy_model_given_as_a_list(self, app):
        printers(app, dict(INDX_4T, legacy=["COREONE_INDX4T"]), dict(INDX_8T, legacy=["COREONE_INDX8T"], supported="x"))
        app.cloud.printers.append(connect_printer(name="Empty", legacy=[], supported=[], model=""))
        app.state.config["printer_model"] = "Prusa CORE One INDX 4T"
        assert succeeded(app.sync()).endswith("to match INDX 4.")

    def test_uses_the_uuid_for_a_printer_without_a_name(self, app):
        printers(app, dict(MK4S, name="", uuid="the-uuid"))
        assert succeeded(app.sync()).endswith("to match the-uuid.")


class TestSelecting:
    def red(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        printer(app, tool("PLA", "Spike", "PLA Red", "#FF0000FF"))

    def test_cancelling_the_confirmation_changes_nothing(self, app):
        self.red(app)
        app.orca.host.ui.answer = "cancel"
        assert failed(app.sync()) == "Nothing was changed."
        assert selected(app) == []

    def test_escapes_ampersands_in_the_question(self, app):
        stock(app, preset("Spike PLA Red & Co", "Spike", "PLA", "#FF0000"))
        printer(app, tool("PLA", "Spike", "PLA Red", "#FF0000FF"))
        succeeded(app.sync())
        assert "Spike PLA Red && Co" in app.dialogs[0]

    def test_keeps_the_slots_preset(self, app):
        self.red(app)
        slots(app, "Spike PLA Red")
        assert succeeded(app.sync()) == "Nothing to change for Core One INDX.\n\nSlot 1: keeps Spike PLA Red"
        assert app.dialogs == []

    def test_keeps_a_hidden_preset(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000", is_visible=False),
              preset("Spike PLA Scarlet Red", "Spike", "PLA", "#FF2400"))
        slots(app, "Spike PLA Red")
        # No product name, so Connect cannot tell the two apart.
        printer(app, tool("PLA", "Spike", "", "#FF0000FF"))
        succeeded(app.sync())
        [row] = picker_rows(app)
        assert labels(row) == ["Spike PLA Red", "Spike PLA Scarlet Red"]
        assert selected(app) == []

    def test_reads_a_slot_whose_preset_is_gone(self, app):
        self.red(app)
        slots(app, "Removed preset")
        succeeded(app.sync())
        assert app.dialogs[0].endswith("Slot 1: Spike PLA Red, in place of Removed preset")

    def test_names_presets_that_show_the_same_name_by_their_own(self, app):
        stock(app, preset("Spike PLA Red @0.4", "Spike", "PLA", "#FF0000", alias="Spike PLA Red"),
              preset("Spike PLA Red @0.6", "Spike", "PLA", "#FF0000", alias="Spike PLA Red", is_dirty=True))
        printer(app, tool("PLA", "Spike", "PLA Red", "#FF0000FF"))
        succeeded(app.sync())
        [row] = picker_rows(app)
        assert sorted(labels(row)) == ["Spike PLA Red @0.4", "Spike PLA Red @0.6"]

    def test_leaves_out_presets_the_dropdown_does_not_offer(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000", is_compatible=False))
        printer(app, tool("PLA", "Spike", "PLA Red", "#FF0000FF"))
        assert "left alone" in succeeded(app.sync())

    def test_the_user_picks_another(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"), preset("Spike PLA Matte Red", "Spike", "PLA", "#FF0000"))
        slots(app, SYSTEM_NAME, SYSTEM_NAME)
        printer(app, tool("PLA", "Spike", "", "#FF0000FF"), tool("PLA", "Spike", "", "#FF0000FF"))
        app.answers["picker"] = lambda data: {"picks": [1, 0]}
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike PLA Matte Red"), (2, "Spike PLA Red")]
        [window] = windows(app, "picker")
        assert window.data["intro"].startswith("For some tools of Core One INDX, more than one installed preset fits")
        assert app.dialogs == []

    @pytest.mark.parametrize("answer", [None, {"picks": [5]}, {"picks": [0, 0]}, {"picks": "0"}, ["0"]])
    def test_cancelling_the_picker_changes_nothing(self, app, answer):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"), preset("Spike PLA Matte Red", "Spike", "PLA", "#FF0000"))
        printer(app, tool("PLA", "Spike", "", "#FF0000FF"))
        app.answers["picker"] = lambda data: answer
        assert failed(app.sync()) == "Nothing was changed."

    def test_the_picker_lists_the_notes_and_escapes_its_data(self, app):
        stock(app, preset("Spike PLA Red <b>", "Spike", "PLA", "#FF0000"), preset("Spike PLA Matte Red", "Spike", "PLA", "#FF0000"))
        slots(app, SYSTEM_NAME, SYSTEM_NAME)
        printer(app, tool("PLA", "Spike", "", "#FF0000FF"), tool("ASA", "Spike", "", "#FF0000FF"), tool())
        succeeded(app.sync())
        [window] = windows(app, "picker")
        assert window.data["notes"] == ("Slot 2: left alone, as no installed preset is Spike ASA\n"
                                        "Empty in Prusa Connect, so left alone: tool 3")
        assert "Red <b>" not in window.html and "Red \\u003cb>" in window.html

    def test_the_picker_fits_its_rows(self, app):
        many = [preset(f"Spike PLA Red {n}", "Spike", "PLA", "#FF0000") for n in range(3)]
        stock(app, *many)
        slots(app, *[SYSTEM_NAME] * 9)
        printer(app, *[tool("PLA", "Spike", "", "#FF0000FF")] * 9)
        succeeded(app.sync())
        [window] = windows(app, "picker")
        assert window.size == (680, 700)

    def test_a_cancelled_switch_stops(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        slots(app, SYSTEM_NAME, SYSTEM_NAME)
        printer(app, tool("PLA", "Spike", "", "#FF0000FF"), tool("PLA", "Spike", "", "#FF0000FF"))
        app.editor.cancelled_slots.add(2)
        assert failed(app.sync()) == ('Stopped at slot 2: Switching the edited filament preset to "Spike PLA Red" was '
                                      "canceled. Slots changed before it: 1.")

    def test_a_cancelled_first_switch(self, app):
        self.red(app)
        app.editor.cancelled_slots.add(1)
        assert failed(app.sync()) == ('Stopped at slot 1: Switching the edited filament preset to "Spike PLA Red" was '
                                      "canceled.")

    def test_reports_a_slot_orcaslicer_refuses(self, app):
        stock(app, preset("Spike PLA Red", "Spike", "PLA", "#FF0000"))
        slots(app, SYSTEM_NAME, SYSTEM_NAME)
        printer(app, tool("PLA", "Spike", "", "#FF0000FF"), tool("PLA", "Spike", "", "#FF0000FF"))
        app.editor.mixed_color_slots.add(2)
        assert succeeded(app.sync()) == (
            "Selected filament presets for slot 1 to match Core One INDX.\n"
            "OrcaSlicer would not select Spike PLA Red for slot 2: Filament slot 2 is a mixed-color virtual slot")

    def test_when_orcaslicer_refuses_every_slot(self, app):
        self.red(app)
        slots(app, SYSTEM_NAME)
        app.editor.mixed_color_slots.add(1)
        assert failed(app.sync()) == ("OrcaSlicer would not select Spike PLA Red for slot 1: Filament slot 1 is a "
                                      "mixed-color virtual slot\nNothing was changed.")


class TestReadingPresets:
    @pytest.mark.parametrize("value, items", [
        ("0.4,0.6", ["0.4", "0.6"]),
        ("Spike", ["Spike"]),
        ('"3D Fuel"', ["3D Fuel"]),
        ('"a\\"b";"c\\nd"', ['a"b', "c\nd"]),
        ('"open', ["open"][:0]),
        ('"x\\t"', ["x\t"]),
    ])
    def test_decodes_config_values(self, plugin, value, items):
        assert plugin.decode_value(value) == items

    def test_unescapes_text_settings(self, plugin):
        assert plugin.unescaped('a\\nb\\"c\\\\d') == 'a\nb"c\\d'

    def test_reads_an_escaped_printer_model(self, app):
        printers(app, INDX_8T, MK4S)
        app.state.config["printer_model"] = "Prusa \\\"MK4S\\\" HF"
        assert succeeded(app.sync()).endswith("to match MK4S.")

    def test_a_flexible_material_and_a_cut_one(self, app):
        stock(app, preset("Spike TPU Red", "Spike", "TPU", "#FF0000"),
              preset("Spike PPS-CF10 Black", "Spike", "PPS-CF10", "#000000"))
        slots(app, SYSTEM_NAME, SYSTEM_NAME)
        printer(app, tool("FLEX", "Spike", "", "#FF0000FF"), tool("PPS-CF1", "Spike", "", "#000000FF", hardened=True))
        succeeded(app.sync())
        assert selected(app) == [(1, "Spike TPU Red"), (2, "Spike PPS-CF10 Black")]
