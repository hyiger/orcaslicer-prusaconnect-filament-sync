# Prusa Connect Filament Sync for OrcaSlicer

Reads the filament Prusa Connect has for each tool of your printer, and selects the installed filament preset that matches it for that tool's filament slot: vendor, material and color, and for a material assigned in Prusa Connect, its product name. Requires OrcaSlicer 2.5.1 or later and a Prusa Account with your printer in Prusa Connect.

It is the OrcaSlicer counterpart of **Sync Filaments from Printer** reading Prusa Connect in [hyiger/PrusaSlicer](https://github.com/hyiger/PrusaSlicer), and matches presets as [PrusaLink Filament Sync](https://github.com/hyiger/orcaslicer-prusalink-filament-sync) does, which reads the printer itself over your network instead.

## Using it

1. Install `orca_prusaconnect_filament_sync_any.py` from OrcaSlicer's Plugins window. OrcaSlicer asks for permission to reach `connect.prusa3d.com` and `account.prusa3d.com`, and asks again after each update. If you refuse, the plugin does not load.
2. In Prusa Connect, open the printer's **Tools** page and pick each tool's material: brand, product and color.
3. Run **Sync filaments from Prusa Connect**. The first run asks you to sign in (see [Signing in](#signing-in)).

It shows the preset it picked for each filament slot. Press OK to select them. Tool 1 goes in slot 1, tool 2 in slot 2, and so on. When more than one preset fits a tool as well, it shows a list for that slot instead, with its best guess first. Pick one in each list and press Select.

When several printers in your account fit the printer profile, it asks which one to read. To skip that question, put the printer's name, as Prusa Connect shows it, in `printer` in the command's Config tab. Printers of the same name need a name of their own: rename one in Prusa Connect. The plugin also takes a printer's uuid there, which it does not show; Prusa Connect has it in the address of the printer's page.

**Sign out of Prusa Connect** forgets the sign-in. The next sync asks you to sign in again.

## Signing in

Prusa offers no tokens for other apps, so the plugin signs in as the Prusa Connect web app does, with its public OAuth client. You do this once for each install or update of the plugin: the plugin keeps the sign-in and renews it as you sync.

1. The dialog shows an address that starts with `view-source:https://account.prusa3d.com/o/authorize/`. Press **Copy**, paste it into the address bar of Chrome, Edge or Firefox, and press Return.
2. The browser shows a page of code. Its address starts with `view-source:https://connect.prusa3d.com/login/auth-callback?code=`. Copy that address and paste it into the dialog. The code alone works too.

If the page of code is Prusa Account's sign-in page instead, you are not signed in to Prusa Account in that browser: sign in at [account.prusa3d.com](https://account.prusa3d.com), then paste the address again.

Why `view-source:`: Prusa Account sends the browser to Prusa Connect's own sign-in page with the code. That page, finding a code it did not ask for, says "We could not sign you in" and replaces the address, so the code is gone from the address bar and from the history. In `view-source:` the browser follows Prusa Account to that page but shows its code without running it, so the address stays. Safari does not open `view-source:` addresses. There, open the address without `view-source:` with the Web Inspector's **Network** tab open and **Preserve Log** on, and copy the `auth-callback` request's address.

OrcaSlicer's own Prusa Connect connection cannot stand in for the sign-in: its API key only uploads G-code, and Prusa Connect refuses it for the printer list.

The address works once and for a few minutes. The plugin keeps the sign-in in `prusa_account.json` in its own folder, readable only by you, and renews it as you sync: an access token lasts 2 hours, and Prusa Account replaces the refresh token at each renewal. You sign in again only after a long break (the refresh token lasts about 30 days), after a sign-out, or after you update or reinstall the plugin, as OrcaSlicer replaces the plugin's folder, and the sign-in in it.

## How a preset is chosen

For each loaded tool, among the presets the slot's dropdown offers:

1. **Vendor and material must match.** Prusa Connect's brand is the vendor. Vendor names are compared without case or punctuation, so `3D Fuel` matches `3D-Fuel`, and `Prusament` matches `Prusa Polymers`. Materials are compared without case, and `FLEX`, `TPU`, `TPE` and `PEBA` count as one. A material the printer cut to 7 characters still matches (`PPS-CF1` matches `PPS-CF10`). Connect's material types follow Prusa's material database, so a type such as `PA11` does not match a preset of type `PA`. When no preset matches, the slot is left alone, and the list says so.
2. **Abrasive filament comes last on a nozzle that is not hardened**, when Connect says the nozzle is not: a preset that needs a hardened nozzle, or whose name or type says carbon or glass fiber. `Carbon Black` is a color, except from Kimya, whose Carbon line is carbon fiber filled: `Kimya PETG Carbon Black` is abrasive. A product Connect names as abrasive is noted even when its preset does not say so.
3. **Then the color.** The color name is the one of the printer's 16 colors that the product name holds (`Yellow` in `Pro PCTG, Translucent Yellow`, `Gray` for Grey), so a preset named for that color fits, whatever its own color value. A name with several (`PETG Black Silver Glitter`) goes by the one closest to Connect's color code. A product name with none of them goes by the color code alone, which is the material database's and needn't be your preset's. A preset named for another of the printer's colors is another color, unless the product name has it too. A copy you saved goes by its own name's color: `Spike PLA Blue`, saved from `Spike PLA Red`, is blue. A copy whose name has no color, such as `My Spike PLA`, goes by its parent's.
4. **Then the product name**, for a material assigned in Connect:
   - A product that names carbon or glass fiber, `Carbon` alone (`Kimya ABS Carbon`), or Kimya's `Carbon` (`Kimya PETG Carbon Black`), prefers presets that name it too, by name or type. One that does not prefers presets that do not.
   - Then the preset whose name is closest to the product's: the fewer words of the product it lacks, and the fewer words of another product it has. For `Pro PCTG, Translucent Yellow`, `Pro PCTG Daffodil Yellow` lacks `Translucent` and has `Daffodil`, so `Pro PCTG Translucent Yellow` wins. `PLA+` reads as `PLA Plus`. Numbers (`2.0`), and words after the color or after the name of the preset a copy was saved from (`Tuned`), do not count.
   - A closer preset replaces the slot's own only when it names no other product. Between presets that each name another product, such as `Fire Engine Red` and `Toolbox Red` for `Iron Red`, you pick.
   - Without a brand, the product name only orders the list: a word it shares with a preset, such as `Matte`, does not say whose filament it is.
5. **The slot's preset is kept** when it fits as well as the best one, even if you hid it since. It is judged with its unsaved edits, which are what the slot uses: a PLA preset edited to PETG is no PLA.
6. Among presets that fit as well, the order is: `Generic` first for a tool without a vendor, your own presets before a vendor's, the name that says the least beyond vendor, material, color and product, then the closest color.

A tool Connect has no assigned material for reports only what the printer sent: the material and, from firmware with Filament Slots, the vendor and color you set on the printer. It has no product name. A tool without a vendor, or with one of the printer's unnamed vendors `VENDOR1` to `VENDOR8`, goes by its material alone.

The list also notes a preset of another color picked for want of one in the tool's color, an abrasive preset passed over, a nozzle unlike the printer profile's, empty tools, tools beyond the project's filament slots, and tools it could not read.

## Choosing the printer

The plugin reads the printer named in `printer`, if set. Otherwise it reads the printer whose models, as Connect lists them (`COREONE_INDX8T`, `COREONE_INDX`), best fit the printer profile's model in OrcaSlicer (`Prusa CORE One INDX 8T`), so an 8-tool INDX beats a 4-tool one for an 8T profile, and a CORE One L beats a CORE One for a CORE One L profile. Connect lists an XL as `XLIS`, `XL2IS` or `XL5IS`, of which a `Prusa XL 5T` profile holds only `XL`, so among printers that fit as well, the one with as many tools as the profile has nozzles wins. An account with one printer reads it whatever the profile. When several fit as well, or none does, it asks.

Printers with an MMU are not read yet, as in the PrusaSlicer fork.

## Good to know

- **It is one way.** Nothing is sent to Prusa Connect, and the presets themselves are not changed: the plugin only selects them. It cannot make a preset for a color you have none of.
- **The slot's color is not changed.** OrcaSlicer's plugin API selects presets but cannot set a slot's color. When you pick a preset yourself, the slot takes the preset's default color, if it has one. A preset the plugin selects does not, so the slot keeps the color it had. Set it by hand if you want it to match.
- **If the Filament tab has unsaved changes**, OrcaSlicer asks about them before it switches presets, as when you pick one yourself. Cancel stops the command, and the slots changed before it stay changed.
- **The Prusa Connect API is not published.** The plugin uses what PrusaSlicer 3.0 reads, `GET /slicer/v2/printers`, as documented in the [Prusa Connect API wiki](https://github.com/hyiger/PrusaSlicer/wiki/Prusa-Connect-API). Prusa may change it without notice.

## Development

```bash
python3 -m venv .venv   # Python 3.12 or later, as the plugin's header asks
.venv/bin/pip install pytest pytest-cov
.venv/bin/python -m pytest
```

`pytest` fails below 100% line and branch coverage. Tests run against a stand-in for OrcaSlicer's `orca` module and a stand-in Prusa Account and Prusa Connect served over real HTTP, in `tests/conftest.py`. Prusa Account checks the PKCE verifier, takes a code once and replaces the refresh token at each use, and Prusa Connect answers only a current access token. The `orca` stand-in holds the rules found by testing the PrusaLink plugin in OrcaSlicer: a slot's selector offers only presets that are shown and compatible, or the slot's own, and `find_preset()` returns the edited copy of the selected preset.

## Publishing

Each GitHub release publishes `orca_prusaconnect_filament_sync_any.py` to the plugin's Orca Cloud listing, through `.github/workflows/publish-orcacloud.yml`, which follows [Orca Cloud's guide](https://cloud.orcaslicer.com/wiki/). The repository must first be connected to the listing, in its **Edit plugin** > **GitHub publishing**.

1. Raise `version` in the plugin's header and push.
2. Create a release whose tag is that version with a `v`, such as `v1.2.0`. Orca Cloud needs it higher than the listing's current version. The release notes become the changelog, cut to their first 4000 bytes.

The workflow stops if the tag is not the header's version.

## Testing in OrcaSlicer

The unit tests cannot reach the steps below, which need the running app and a real Prusa Account.

- [ ] Install the plugin. OrcaSlicer asks for permission to reach both Prusa hosts.
- [ ] Sync. The sign-in dialog opens. Copy the address into Chrome, copy the `view-source:` callback address it shows, paste it, and the sync goes on. `prusa_account.json` appears in the plugin's folder.
- [ ] Sync again. No sign-in is asked for.
- [ ] Save two copies of `3D-Fuel Pro PCTG`, named `3D-Fuel Pro PCTG Daffodil Yellow` and `3D-Fuel Pro PCTG Translucent Yellow`, as OrcaSlicer has neither. Assign `3D Fuel Pro PCTG, Translucent Yellow` to tool 1 in Connect, with `Daffodil Yellow` selected in slot 1. Sync selects `Translucent Yellow`, without a list.
- [ ] With two printers in the account, sync. It reads the one that fits the profile, or asks.
- [ ] Wait more than 2 hours and sync. It renews the sign-in without asking.
- [ ] Sign out, then sync. It asks you to sign in again.
- [ ] Paste the address from an earlier sign-in. It says so, and nothing changes.
- [ ] The sign-in and picker dialogs are readable in OrcaSlicer's dark mode, and Copy copies the address.
