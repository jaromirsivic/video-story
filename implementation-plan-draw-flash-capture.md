# Video Story – implementační plán: blikající rámeček kreslení, červené kolečko myši, Collapse/Expand all

Stav: **implementováno 13. 11. 2026** (návrh v3, schválený). Unit testy prochází (`tests/unit`, 758 testů), ruff čistý. Ruční kontrola na dvou monitorech zbývá (rámeček, barvy, pohlcení kliknutí během zelené animace, menu „⋯“). Vychází z nahrávky `20261113-001346` (celá, 0:00–5:19.5) a z odpovědí na otázky v chatu 13. 11. 2026.

## 1. Cíl

Tři samostatné úpravy:

1. **Blikající rámeček kreslicí vrstvy.** Rámeček po obvodu každé obrazovky má tloušťku **max(20 px, 1 % delšího rozměru obrazovky)** v pixelech obrazovky. Pro 2560×1600 je to 26 px, pro 1920×1080 je to 20 px.
   - **Zapnutí** (Ctrl+Space / Draw): 250 ms jasně červená, 250 ms tmavě červená, 250 ms jasně červená, 250 ms tmavě červená, pak rámeček zmizí. Kreslit jde hned od začátku animace.
   - **Vypnutí** (Ctrl+Space, Draw, Esc, křížek na Drawing tools): obrazce se hned smažou a vrstva se hned přepne ze Sketch zpět na obrazovky. Pak 250 ms jasně zelená, 250 ms tmavě zelená a vrstva zmizí. Během těch 0,5 s vrstva dál pohlcuje kliknutí, takže do aplikací pod ní nic neprojde a kreslit nejde. Aplikace jsou znovu dostupné, až vrstva zmizí.
   - Rámeček **je vidět ve snímcích**, ale **není to obrazec**: neobjeví se v `drawings` ani `drawing_now` v sidecarech, nedostane `id` a v timeline nebude žádný řádek `DRAW`, `REMOVED` ani jiný. `SKILL.md` řekne, že barevný okraj obrazovky ve snímcích není kresba, jen zvýraznění pro uživatele, že začal nebo skončil kreslení.
2. **Editor – myš jen červeně.** Kolečko a šipky směru na ikoně myši budou červené jako stisknutá tlačítka. Žlutá z ikony zmizí úplně.
3. **Editor – tlačítko „…“ vedle New Folder.** Otevře menu se dvěma položkami: **Collapse all** zabalí všechny složky, **Expand all** je všechny rozbalí.

**Zamítnuto:** volba „Capture Video Story windows while drawing“, tedy vynechání oken Video Story a Drawing tools ze snímků. Na Windows by to šlo (ověřeno s `mss` přes `WDA_EXCLUDEFROMCAPTURE`) a na macOS pravděpodobně taky. Na Linuxu X11 to ale jednoduše nejde. Okna se proto dál zaznamenávají jako dnes a nastavení nepřibude.

## 2. Kroky

### Krok 1 – blikající rámeček (`ui/overlay.py`, `ui/control_window.py`)

- Konstanty:
  - `FLASH_MIN_PX = 20` a `FLASH_SCREEN_RATIO = 0.01`. Tloušťka se počítá v pixelech obrazovky jako `max(20, round(0.01 * max(šířka, výška)))` a převádí se na logické pixely přes `devicePixelRatio`.
  - `FLASH_STEP_MS = 250`.
  - Barvy: jasně červená `#FF0000`, tmavě červená `#8B0000`, jasně zelená `#00E000`, tmavě zelená `#006400`.
  - Sekvence `OPEN_FLASH = (jasná, tmavá, jasná, tmavá)` a `CLOSE_FLASH = (jasná, tmavá)`.
- `DrawingOverlay`: jeden `QTimer` (single-shot, 250 ms) a stav `_flash: QColor | None`. `OverlayWindow.paintEvent` kreslí rámeček **nad** vším ostatním (snímek, Sketch, obrazce), uvnitř okraje okna, nezávisle na zoomu. Kreslí se přímo v `paintEvent`, mimo seznam obrazců (`_shapes`, `_history`), takže se do `drawings`, `drawing_now`, Undo, gumy ani timeline nedostane. Guma ho nezasáhne.
- **Zapnutí:** `show()` spustí `OPEN_FLASH`. Kreslení funguje hned, rámeček jen překresluje okraj.
- **Vypnutí:** nová metoda `leave()`. Všechny cesty povedou přes ni: tlačítko Draw / Ctrl+Space (`_toggle_overlay(False)`), Esc, křížek (`_user_close`) a Alt+F4.
  1. Hned: `clear()` (obrazce pryč, v timeline `REMOVED` v čase stisku), `_set_sketch_state(False)`, zrušit zoom, skrýt toolbar, vypnout zkratky, `_visible = False` (sidecary od té chvíle hlásí, že se nekreslí).
  2. Stav `_leaving = True`: okna vrstvy zůstávají a pohlcují myš, ale stisk tlačítka nic nekreslí. Esc se ignoruje.
  3. Spustí se `CLOSE_FLASH`. Po jeho skončení se zavolá `hide()` (dnešní kód včetně překreslení prázdného okna před skrytím).
- **Ctrl+Space během zavírací animace:** animace se zruší a vrstva se hned znovu zapne s červenou animací. Obrazce už jsou pryč.
- **Stop nahrávání (`_close_overlay`) a zavření okna:** bez animace, okamžitě jako dnes.

Ověření (`tests/unit/test_overlay.py`, čas přes `qtbot.wait`):

- Po `show()` jde postupně jasná, tmavá, jasná, tmavá a pak `None`. Kreslení během animace přidá tvar.
- Tloušťka: 2560×1600 → 26, 1920×1080 → 20, 1280×720 → 20.
- Během animace jsou `drawings()` a `drawing_now()` beze změny (prázdné, nebo jen skutečné obrazce). Timeline nahrávky s kreslením nemá žádný řádek pro rámeček.
- `leave()`: obrazce jsou pryč hned, `sketch` je False hned, `background()` vrací None hned. Stisk myši během animace nic nepřidá. Okna jsou viditelná ještě 0,5 s a pak skrytá.
- `show()` během `leave()` animaci zruší a začne červenou.
- `_close_overlay` skryje vrstvu bez čekání.
- Ručně: rámeček na obou monitorech, barvy a tloušťka, během zelené animace se do aplikací pod vrstvou nedá kliknout, hned po ní ano.

### Krok 2 – kolečko myši červeně (`editor/mouse_activity.py`, `editor/mouse_details_dialog.py`, `editor/mouse_view.py`)

- `mouse_activity.py`: `WHEEL_FILL` a `YELLOW_IDS` zmizí. Všechny rozsvícené prvky (`RED_IDS | WHEEL_IDS`) se plní `PRESSED_FILL` (`#e53935`). Docstring modulu (tabulka „yellow“) se upraví.
- `mouse_details_dialog.py` (okno Mouse details, šipky kolečka): styl `turning` dostane červené pozadí a bílý text jako `held`. Úprava docstringu.
- `mouse_view.py:69`: tooltip „a pressed button and a turning wheel and its direction are red“.

Ověření: `tests/unit/test_editor_mouse_view.py` / `test_editor_mouse_details.py`. SVG pro kolečko nahoru obsahuje `#e53935` u `button-3` a `scroll-up` a žádné `#ffe600`. Ve stylu šipky není žlutá. Testy, které dnes kontrolují `WHEEL_FILL`, se přepíšou.

### Krok 3 – „…“ s Collapse all / Expand all (`editor/window.py`, `editor/recordings_model.py`, `core/folders.py`)

- `window.py` (řádek s titulkem „Recordings“): vpravo od `new_folder_button` nový `QToolButton` `folders_menu_button`. Má text „⋯“, stejnou velikost a `autoRaise`, `setPopupMode(InstantPopup)` a tooltip „Folders“. Menu má položky **Collapse all** a **Expand all**. Položka, která nic nezmění, bude zašedlá (vše už zabalené / rozbalené).
- `core/folders.py`: `set_all_collapsed(recording_folder, names, collapsed)` zapíše `folders.json` **jednou** (ne jednou za každou složku).
- `recordings_model.py`: `set_all_collapsed(collapsed) -> bool` aktualizuje všechny `FolderHeader` a vyšle jeden `dataChanged`. Pak se volá `list_view.apply_collapsed()`.
- Vybraná nahrávka ve složce, která se zabalí: chování jako dnes při ručním zabalení jedné složky (řádek se jen skryje, náhled zůstane).

Ověření: `tests/unit/test_editor_folders.py`. Tři složky, Collapse all → všechny `collapsed` a v `folders.json` všechny tři. Expand all → žádná. Řádky nahrávek jsou skryté / viditelné. Položky menu jsou správně zašedlé.

### Krok 4 – dokumentace a testy

- `video-story-spec.md`:
  - §4 Nahrávací okno: rámeček, zavírání, pohlcení kliknutí,
  - §8 Editor: červené kolečko, menu „…“.
- `skills/video-story/SKILL.md` (u kreslení): barevný okraj obrazovky ve snímcích není kresba, jen zvýraznění pro uživatele. Červený je v první sekundě po zapnutí kreslení, zelený v 0,5 s po vypnutí. V timeline není.
- Docstring `ui/overlay.py` (zapnutí a vypnutí, rámeček).
- `uv run pytest tests/unit` celé, `ruff`.

## 3. Rizika a předpoklady

- Rámeček je ve snímcích. Obrazec, který uživatel nakreslí těsně u okraje obrazovky, může být v první sekundě rámečkem částečně překrytý. V timeline je ale celý, takže agent o něj nepřijde.
- Kliknutí během zavíracích 0,5 s se ztratí (vrstva je pohltí). Tak je to zadané.
