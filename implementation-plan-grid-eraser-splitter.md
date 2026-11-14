# Video Story – implementační plán: guma maže celé skupiny, mřížka s Ctrl, štětec se Shiftem, oddělovače v editoru

Stav: **implementováno 12. 11. 2026** (2. verze plánu, schválená s odpověďmi na otázky v chatu). Vychází z nahrávky `20261112-223534` (celá, 0:00–5:49.7). Unit testy prochází (`tests/unit`, 744 testů), ruční kontrola na dvou monitorech zbývá (krok 8).

## 1. Cíl

Čtyři samostatné úpravy:

1. **Guma maže celou skupinu.** Když guma zasáhne jednu čáru ze spojených čar (kreslených se Shiftem), smaže se celá skupina, i když leží na víc monitorech. Čára nebo šipka přes víc monitorů se už dnes maže celá a funguje správně. Tu jen sjednotíme pod stejné pravidlo.
2. **Mřížka s Ctrl.** Při kreslení a drženém **Ctrl** se body, kde uživatel kliká, zarovnávají na mřížku. Rozteč je **nastavitelná v Settings, 8–128 px, výchozí 16 px**. Na Sketch plátně se mřížka při drženém Ctrl i zobrazí světle šedou `#E2E2E2`. Na obrazovce se nezobrazuje, ale zarovnává se stejně. Timeline zapíše, kdy se zarovnávalo.
3. **Štětec: Shift i během tahu.** Stisk Shiftu kdykoli během tahu štětcem hned spojí konec tahu se začátkem (uzavřený tvar). Dnes to jde jen se Shiftem drženým už před stiskem tlačítka.
4. **Oddělovače v editoru.** Mezi náhledem obrazovky a střední oblastí (ovládání přehrávání, myš) přibude tenká šedá čára, stejná jako ta dnešní. Dnešní posuvný oddělovač nad řádkem In/Out budou **dvě čáry nad sebou s mezerou 4 px**: uživatel tak pozná, že se dá táhnout. Horní jednoduchá čára táhnout nepůjde.

| Nástroj | S Ctrl (zarovnání na mřížku) |
|---|---|
| Obdélník `R`, elipsa `E` | oba rohy (u elipsy rohy ohraničujícího obdélníku) na bodech mřížky; se Shiftem čtverec / kruh se stranou v násobcích rozteče |
| Čára `L` | začátek i konec |
| Spojené čáry (`Ctrl+Shift`) | každý lomový bod a konec |
| Šipka (levé `A` i pravé tlačítko) | konec i hrot; **ne** body, kde se šipka láme na hraně monitoru |
| Štětec `B`, guma `U` | nic, kreslí a mažou volně |

## 2. Jak se kresby dnes zapisují pro AI (odpověď na dotaz)

Zapisují se do dvou míst. Každý snímek má sidecar (`frames_info/<čas>_<screen>.json`, každých 500 ms). Příkaz `timeline` z nich skládá textovou časovou osu pro agenta.

| Co | Sidecar snímku | Timeline |
|---|---|---|
| **Štětec, dokud se kreslí** | `drawing_now`: nástroj, barva, obrazovka a `started_ms`, bez geometrie. Snímky už tah ukazují. | Zatím nic. |
| **Štětec dokončený** (puštěné tlačítko) | Tvar se objeví v `drawings` s `id`, body, `started_ms` a `finished_ms`. | Řádek `DRAW #id brush ... (pressed X, released Y; frames before …, after …)` v čase puštění. Uzavřený tvar má `(closed shape, Shift)`. |
| **Elipsa, obdélník** | Stejně jako štětec: během tažení `drawing_now`, po puštění tvar v `drawings`. | Jeden řádek `DRAW` v čase puštění. |
| **Spojené čáry (Shift)** | **Každý segment se uloží hned**, když se kliknutím ukončí. Má `group` (společný pro celou lomenou čáru) a `segment` (pořadí). Rozkreslený segment je v `drawing_now`. | Pro každý segment vlastní řádek `DRAW … (connected lines group g, segment n)` v čase kliknutí, prostřídaný s tím, co uživatel říká. Agent tedy vidí, jak lomená čára vzniká. |
| **Sketch zapnutý / vypnutý** | `background`: `screen` / `sketch` v každém sidecaru a u každého tvaru. | Řádky `SKETCH on: …` a `SKETCH off (the screens again; sketching since …)`. Tvary na plátně mají `(on the sketch canvas)`. |

Tohle všechno už funguje a chování odpovídá tomu, co popisujete. Chybí jen jedna věc: v timeline není vidět tvar, který se začal kreslit, ale nedokončil se. Například když nahrávka skončí nebo crop končí uprostřed tahu, nebo když uživatel kreslení zruší klávesou Esc. Doplní to krok 5.

## 3. Kroky

### Krok 1 – guma maže celou skupinu (`src/video_story/ui/overlay.py`)

Dnes: `OverlayWindow._erase_to` smaže zasažený tvar a přes `DrawingOverlay._take_split_pieces` i ostatní kusy **téhož rozděleného** tvaru (`split`). Spojené čáry mají společné jen `group`, takže guma smaže jen zasažený segment.

Změna:

- `_erase_to`: u zasaženého tvaru s `group is not None` odebrat **všechny tvary s tímto `group`** v tomto okně (zapsat do `self._erased` s indexy) i ve všech ostatních oknech (nová `DrawingOverlay._take_group_pieces(group, origin)`, obdoba `_take_split_pieces`; `OverlayWindow._take_split` zobecnit na `_take_matching(predicate)`).
- `group` mají spojené čáry i kusy rozdělené čáry nebo šipky (`_commit_span` ho přiděluje i při rozdělení), takže jedno pravidlo pokryje oba případy. Tvar se `split` a bez `group` (nemělo by nastat) dál řeší podle `split`.
- Undo: celý zásah je dál jeden krok (`_Action(erased=...)`) a Ctrl+Z vrátí celou skupinu se stejnými `id` a v původním pořadí. Ověřit, že `restore_shapes` vrací víc tvarů z jednoho okna na správné indexy, i když se odebíraly postupně.
- Timeline už umí `REMOVED #3 #4 #5` v jednom řádku, tam se nic nemění.

Ověření (`tests/unit/test_overlay_shift.py`): 3 spojené čáry, guma přes prostřední → všechny pryč, Undo → všechny zpět ve stejném pořadí. Spojené čáry přes dva monitory → zásah na jednom monitoru smaže kusy na obou. Šipka přes 3 monitory → zásah do prostředního kusu smaže všechny tři (regrese). Samostatná čára a jiná skupina zůstanou.

### Krok 2 – štětec: Shift i během tahu (`src/video_story/ui/overlay.py`)

Dnes: `mousePressEvent` nastaví `shift=True` u štětce jen tehdy, když je Shift dole už při stisku. `_on_shift_changed` pak puštěním Shiftu tvar dokončí. Stisk Shiftu během tahu nedělá nic.

Změna:

- `_on_shift_changed(held=True)` u rozkresleného štětce (`tool is BRUSH`, `shift` dosud `False`): nastavit `shape.shift = True` a `update()`. `paint_shape` u `closed` tvaru kreslí cestu uzavřenou (`closeSubpath`), takže se konec se začátkem spojí hned, ještě v pohybu.
- Pravidla dokončení zůstanou stejná jako u tvaru začatého se Shiftem. Puštění Shiftu dokončí uzavřený tvar. Puštění tlačítka ho dokončí taky, uzavřený. Uživatel tedy může začít kreslit volně, rozmyslet si to a Shiftem tah uzavřít.
- Zápis: tvar má `closed: true` jako dnes a timeline píše `(closed shape, Shift)`. Doplníme `closed_ms` (čas, kdy byl Shift stisknut), když se tah uzavřel až během kreslení. Timeline pak napíše `(closed shape, Shift from <ms>)`, aby agent věděl, od kdy jde o „tuhle oblast“.
- Tooltip `B` v `TOOLS`: „freehand line; Shift (before or while drawing) closes the shape“. Upravit docstring modulu (odstavec o Shiftu).

Ověření (`test_overlay_shift.py`, falešný `_shift_source`): tah bez Shiftu → Shift dolů uprostřed → `closed` je `True` a obraz okna (render do `QImage`) má čáru mezi koncem a začátkem. Puštění Shiftu tvar dokončí. Varianta bez puštění Shiftu: puštění tlačítka tvar dokončí uzavřený. Tah začatý se Shiftem se chová jako dřív (regrese). Shift u obdélníku a elipsy beze změny.

### Krok 3 – oddělovače v editoru (`src/video_story/editor/window.py`, `src/video_story/editor/preview.py`)

- Výběr barvy ze `SeparatorHandle.paintEvent` přesunout do funkce `separator_color(palette)`: na světlém pozadí `Dark`, na tmavém `Window.lighter(220)`. Dnešní čára zůstane stejná a nová bude mít ve světlém i tmavém vzhledu stejnou barvu.
- `SeparatorHandle.paintEvent`: dvě 1px čáry symetricky kolem středu, **4 px od sebe**. `SeparatorSplitter` používá jen `right_splitter` (svislý), svislou variantu ale nakreslit taky, pro úplnost. `right_splitter.setHandleWidth(9)`, aby se obě čáry vešly i s okrajem.
- `PreviewPanel` (`preview.py`, `layout` kolem ř. 775): mezi `self.view` a mřížku `controls` vložit nový `SeparatorLine(QWidget)` s pevnou výškou asi 3 px. V `paintEvent` nakreslí jednu 1px čáru barvou `separator_color(self.palette())`, takže se při přepnutí tématu přebarví. Nemá kurzor pro tažení a nereaguje na myš.
- `QFrame.HLine` nepoužít: jeho vzhled závisí na stylu a v tmavém tématu by se lišil od handle.

Ověření: unit test (`tests/unit/test_editor_preview.py` / `test_editor.py`), že `PreviewPanel` obsahuje oddělovač mezi náhledem a ovládáním, že `right_splitter.handleWidth()` stačí na dvě čáry a že vykreslení handle do `QImage` dá dva řádky barvy oddělovače 4 px od sebe. Ručně screenshot editoru ve světlém i tmavém tématu: horní čára netáhne, spodní ano.

### Krok 4 – nastavení rozteče mřížky (`core/config.py`, `ui/settings_dialog.py`, `ui/control_window.py`)

- `Config.drawing_grid_px: int = 16`, konstanty `GRID_MIN_PX = 8`, `GRID_MAX_PX = 128`. `load_config` hodnotu ověří přes `_require_int` s rozsahem 8–128 (chybějící klíč = 16, takže starý `config.json` dál funguje). Klíč půjde měnit i přes CLI `config set` a MCP `config`, protože ty berou pole z `Config`.
- `SettingsDialog`: řádek **Drawing Grid** se `QSpinBox` (8–128, krok 1, přípona „ px“, tooltip „Ctrl while drawing snaps to this grid; shown on the Sketch canvas“), uložení v `_collect` vedle `auto_stop_minutes`.
- Overlay: `DrawingOverlay.set_grid_size(px)`. Volá ho ovládací okno (`control_window.py`) při startu a po uložení Settings, stejně jako ostatní hodnoty z configu. Změna platí od dalšího kreslení. Už nakreslené tvary se nemění.

Ověření: `tests/unit/test_config.py` (výchozí 16, 8 a 128 projdou, 7 a 129 a text vyhodí chybu), test dialogu (spinbox má hodnotu z configu a po uložení ji zapíše), test, že `set_grid_size` změní zarovnání (`snap_to_grid` s roztečí 32).

### Krok 5 – Ctrl a mřížka (`src/video_story/ui/overlay.py`)

**Stav Ctrl.** Stejně jako u Shiftu: `DrawingOverlay._ctrl`, `_note_ctrl(held)` a `ctrl_held`. Ctrl se čte z událostí myši (`event.modifiers()`), z `keyPress/keyReleaseEvent` (`Key_Control`) a pollingem přes `_shift_source`, který se přejmenuje na `_modifier_source` (dnes `QGuiApplication.queryKeyboardModifiers`). Timer `_update_shift_timer` poběží i tehdy, když je zapnutý Sketch a overlay je vidět. Jinak by se mřížka po stisku Ctrl bez pohybu myši a bez fokusu overlaye neukázala. Změna Ctrl zavolá `update()` všech oken (kvůli mřížce) a přepočítá konec taženého tvaru, takže zarovnání naskočí, i když se myš nehne.

**Zarovnání.** Rozteč `self._grid_px` (z kroku 4) v canvas souřadnicích okna, tedy v logických px nezvětšené obrazovky. Čistá funkce `snap_to_grid(p: QPointF, step: int) -> QPointF` zaokrouhlí x a y na nejbližší násobek rozteče. Ctrl se čte vždy v okamžiku daného bodu: začátek při stisku, konec při pohybu a puštění. Ctrl tedy jde stisknout i pustit uprostřed tažení.

- Obdélník, elipsa: `mousePressEvent` zarovná začátek, `_shaped_end` zarovná konec. Se Shiftem nejdřív `constrain_square` a pak se strana zaokrouhlí na násobek rozteče od zarovnaného začátku. Zarovnat x a y zvlášť by čtverec rozbilo, proto nová funkce `snap_square(start, end, step)`.
- Čára, spojené čáry, šipka: span drží globální souřadnice (`_begin_span`, `_move_span`, `_chain_press`). Nová `DrawingOverlay._snap_global(window, global_pos)` převede bod globální → `mapFromGlobal` → `to_canvas` → `snap_to_grid` → `to_view` → `mapToGlobal`, vždy v okně pod kurzorem. Volá se ve `_begin_span` (začátek), `_move_span` (konec, i čekající konec spojených čar) a `_chain_press` (lomový bod). Body zlomu na hranách monitorů dál počítá `clip_segment` ze zarovnaných konců, takže se nezarovnávají, přesně podle zadání.
- Štětec a guma zarovnání nevolají.

**Vykreslení mřížky.** V `OverlayWindow.paintEvent` hned po bílém pozadí Sketche a před tvary: když platí `controller.sketch and controller.ctrl_held`, nakreslit svislé a vodorovné 1px čáry (kosmetické pero, `GRID_COLOR = #E2E2E2`) na násobcích rozteče ve `visible_canvas_rect()`, převedené přes `to_view`. Při zoomu se mřížka zvětšuje s obsahem (buňka má na displeji rozteč × zoom), takže čáry mřížky sedí s body zarovnání. Bez Sketche se mřížka nekreslí. Při malé rozteči a zoomu pod 100 % to nenastane, protože zoom pod 100 % nejde.

**Zápis pro AI.**

- Sidecar snímku: nové pole `grid` = rozteč v px, dokud je v kreslicím režimu držený Ctrl, jinak `null`. Recorder ho čte stejně jako `background` (nová `DrawingOverlay.grid()` vedle `background()`).
- Tvar: pole `grid: <px>`, když byl aspoň jeden jeho bod zarovnaný. Timeline k řádku `DRAW` připíše `(snapped to the 16 px grid, Ctrl)`.
- Timeline (`host/events.py`, obdoba `_screen_events` pro Sketch): řádky `GRID on: Ctrl held, points snap to a 16 px grid (visible on the sketch canvas)` a `GRID off (snapping since <ms>)`. Aby Ctrl ze zkratek (Ctrl+Z, Ctrl+Space…) nezaplavil timeline, vynechá se interval kratší než 1 s, ve kterém se nedokončil žádný zarovnaný tvar. Doplnit legendu `TIMELINE_LEGEND` a `counts` (`grid`).

**Nápověda.** Tooltipy R, E, L, A doplnit o „Ctrl: snap to the grid“. Upravit docstring modulu (odstavec o Ctrl vedle odstavce o Shiftu).

Ověření (nový `tests/unit/test_overlay_grid.py`, falešný `_modifier_source` jako u Shiftu):

- `snap_to_grid`, `snap_square` s roztečí 16 i 8 a 128 (záporné směry tažení, hodnoty přesně mezi body).
- Obdélník a elipsa s Ctrl → oba rohy na násobcích rozteče. Ctrl stisknutý až během tažení → zarovná se jen konec.
- Čára a spojené čáry (Ctrl+Shift) → všechny vrcholy na mřížce.
- Šipka přes dva monitory s Ctrl → konec a hrot zarovnané, bod zlomu na hraně ne.
- Štětec a guma s Ctrl → body beze změny.
- Render okna do `QImage`: Sketch + Ctrl → pixel na čáře mřížky má `GRID_COLOR`, mimo ni je bílý. Bez Ctrl nebo bez Sketche → mřížka nikde.
- Polling timer běží se zapnutým Sketchem a zastaví se po jeho vypnutí nebo po skrytí overlaye.
- `tests/unit/test_events.py`: sidecary s `grid` → řádky `GRID on/off`. Krátký Ctrl bez tvaru se vynechá. `DRAW` se zarovnaným tvarem má poznámku.

Ručně: nahrávka se Sketchem, držet Ctrl → mřížka se ukáže a po puštění zmizí. Zkusit obdélník, kruh, lomenou čáru a šipku přes oba monitory, pak totéž na obrazovce bez Sketche (mřížka neviditelná, tvary zarovnané). Rozteč změnit v Settings na 32 a zopakovat. Nakonec `timeline` nahrávky: `GRID on/off` a poznámky u `DRAW`.

### Krok 6 – nedokončený tvar v timeline (`host/events.py`)

Když má poslední sidecar v rozsahu cropu `drawing_now` a tvar se do konce rozsahu nedokončil, nebo když `drawing_now` zmizí bez nového tvaru v `drawings` (Esc, zavření kreslení), timeline přidá řádek `DRAWING <tool> <color> <screen> (pressed <ms>, not finished: cut by the crop / cancelled)`. U dokončených tvarů se nic nepřidává, protože čas začátku už je v `DRAW … (pressed X, …)`.

Ověření: `test_events.py` se sidecary, kde `drawing_now` trvá do konce rozsahu, a se sidecary, kde zmizí bez tvaru.

### Krok 7 – dokumentace

- `video-story-spec.md`: kap. 6 Kresby (skupiny a guma, Shift u štětce během tahu, Ctrl a mřížka, `grid` v sidecaru a u tvaru), kapitola editoru (oddělovače), kapitola configu (`drawing_grid_px`).
- `skills/video-story/SKILL.md`: odstavec o kreslení (Ctrl = mřížka, viditelná na Sketchi i ve snímcích), `REMOVED` u skupin, `GRID on/off`, `DRAWING … not finished`, `closed shape, Shift from`.
- `README.md` / nápověda Settings: nové pole Drawing Grid.
- Tento plán: stav změnit na „implementováno“ a doplnit datum.

### Krok 8 – celkové ověření

`uv run pytest tests/unit` a `ruff`, pak ruční kontrola podle kroků 1–6 na dvou monitorech (2560×1600 + 1920×1080) a jedna krátká nahrávka, ve které se vyzkouší vše najednou, s kontrolou výstupu `timeline`.

## 4. Rizika

- **Ctrl a zkratky.** Ctrl je i v `Ctrl+Z`, `Ctrl+Alt+C`, `Ctrl+Space`/`Ctrl+F1` a `Ctrl+Alt+K`/`Ctrl+Alt+S`. Při drženém Ctrl kvůli mřížce stačí stisknout Z a udělá se Undo, Space a kreslení skončí. Je to očekávané chování zkratek, jen to může překvapit. Krátké stisky Ctrl ze zkratek timeline vynechá (krok 5).
- **Polling při Sketchi.** Timer běží po celou dobu Sketche (30 ms, levné `queryKeyboardModifiers`). Na Windows vrací stav i bez fokusu. Na Linuxu X11 a macOS to Qt může vracet jen pro aktivní aplikaci, tam se mřížka v nejhorším ukáže až s pohybem myši.
- **DPI.** Rozteč je v logických px. Při škálování 150 % je 16 px ve snímku 24 fyzických px a timeline uvádí geometrii v pixelech snímku. Poznámka u `GRID on` proto uvede rozteč v pixelech snímku dané obrazovky, ne v logických px.
- **Mazání skupiny může smazat víc, než uživatel čeká** (dlouhá lomená čára kvůli jednomu segmentu). Undo to vrátí celé a je to tak zadané.
- **Štětec uzavřený omylem.** Shift stisknutý během tahu kvůli něčemu jinému tah uzavře. Puštění Shiftu ho pak hned dokončí. Undo ho odebere celý.
- **Výška náhledu** se o pár px zmenší (širší handle a horní čára). Uložený stav splitteru (`splitter/right`) zůstane platný.

## 5. Předpoklady

- Mřížka začíná v levém horním rohu každé obrazovky (canvas 0,0), ne v bodě prvního kliknutí.
- Při zoomu je mřížka v souřadnicích skutečné obrazovky: rozteč na obrazovce, na displeji rozteč × zoom.
- Mřížka je jen v režimu Sketch a jen dokud je Ctrl držený, i mimo kreslení tvaru. Je vidět i ve snímcích nahrávky.
- Pravé tlačítko (šipka) se zarovnává také. Shift na něj dál nemá vliv.
- Guma se nezarovnává, Ctrl na ni nemá vliv.
- Štětec uzavřený Shiftem během tahu se dokončí puštěním Shiftu, stejně jako dnes tah začatý se Shiftem.
- Změna rozteče v Settings platí od dalšího kreslení a nevyžaduje restart.

## 6. Rozhodnuto (odpovědi z chatu)

1. Šipka přes víc monitorů se maže celá a funguje správně. Krok 1 přidává jen spojené čáry.
2. Barva mřížky `#E2E2E2` (tmavší varianta).
3. Do timeline se zapíše, kdy je Ctrl držený a zarovnává se (`GRID on/off`), a u tvaru, že byl zarovnaný.
4. Mezera mezi spodními čarami 4 px.

5. Štětec uzavřený Shiftem během tahu se chová stejně jako tah začatý se Shiftem: puštění Shiftu ho dokončí.
6. Rozteč mřížky v Settings: libovolné celé číslo 8–128.

## 7. Poznámky k implementaci

- Nový modul `src/video_story/editor/separators.py` (`SeparatorHandle`, `SeparatorSplitter`, `SeparatorLine`, `splitter_line_color`); handle má šířku 9 px.
- Shift a Ctrl se pollují po celou dobu, kdy je kreslení zobrazené (ne jen během tažení), aby se mřížka ukázala i bez pohybu myši a sidecar `grid` byl přesný.
- Zarovnání zaokrouhluje polovinu nahoru (`floor(x + 0.5)`), ne bankéřsky.
- `frames` výstup (`FrameRef.to_dict`) nese i `grid`.
- Testy: `tests/unit/test_overlay_grid.py`, `tests/unit/test_editor_separators.py`, rozšířené `test_overlay_shift.py`, `test_events.py`, `test_core.py`, `test_ui.py`, `test_recorder_sidecars.py`.
