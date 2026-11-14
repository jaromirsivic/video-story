# Video Story – implementační plán: černá barva, lišta kreslení, Sketch/Screen, nové nástroje

Stav: **implementováno (12. 11. 2026)**; platné chování je ve [video-story-spec.md](video-story-spec.md) (kap. 2 a 6). Původně (z nahrávky `tests/20261112-202447`, crop 0:18.7–7:27.1, 12. 11. 2026; upraveno podle odpovědí na otázky).

## 1. Cíl

1. **Černá** jako 9. barva palety (klávesa `9`); vybraná černá má bílé orámování, aby bylo vidět.
2. **Lišta kreslení**: tlačítko Close zmizí (zůstává křížek v titulku, `Esc` a `Ctrl+Space`/`Ctrl+F1`). Clear se posune na místo Close a dostane klávesu **`C`** (místo `Ctrl+Alt+C`), Undo na místo Clear a na místo Undo přijde přepínač **Sketch ↔ Screen** (klávesa `S`).
3. **Sketch/Screen**: po každém zapnutí Draw je na tlačítku „Sketch“ a v pozadí jsou snímky obrazovek. Klik na Sketch → všechny obrazovky zbělají (čisté bílé plátno, zoom funguje dál), tlačítko se změní na „Screen“; další klik vrátí snímky obrazovek. **Přepínání nic nemaže** – kresby zůstávají vidět na obou pozadích.
4. **Sketch v metadatech pro AI**: každý snímek, každý tvar a timeline říkají, zda uživatel kreslí na bílém plátně (skicuje), nebo na snímcích obrazovek.
5. **Nástroje**: tlačítka jen ikona + písmeno v závorce. Pořadí a klávesy: Rectangle `R`, Ellipse `E` (ikona kruhu, kreslí elipsu), Line `L`, Brush `B`, Arrow `A`, Eraser `U`. Guma: tažení levým tlačítkem smaže každý objekt, jehož **čáry** se dotkne nebo ji protne; `Ctrl+Z` smazané objekty vrátí.
6. Všechny nástroje jsou na **levém** tlačítku; **pravé tlačítko vždy kreslí šipku** a nejde to změnit (to už platí, jen to musí platit i pro gumu).

**Beze změny:** Stop zůstává `Ctrl+Alt+S`; žádné nové zkratky pro Stop se nezavádějí.

## 2. Kroky

Každý krok je samostatně testovatelný; pořadí je od nejjednoduššího.

### Krok 1 – černá barva (`src/video_story/ui/palette.py`, `overlay.py`)

- `PALETTE.append(PaletteColor("Black", "#000000"))`, `PALETTE_COLUMNS = 9`; zkratky `1`–`9` se odvozují z `len(PALETTE)` samy (`_create_shortcuts`, `keyPressEvent`).
- `OverlayToolbar.set_color` už bílé orámování pro `#000000` má – jen ověřit, že je na světlé liště vidět (případně přidat tenký šedý vnější okraj).
- Upravit docstringy „8 colors“.
- **Ověření:** `tests/unit/test_overlay.py` ř. 101–108 (`len(PALETTE) == 9`) a ř. 201 (klávesa `9` vybere černou); test stylu vybraného černého vzorku (`3px solid #FFFFFF`).

### Krok 2 – první řádek lišty (`OverlayToolbar.__init__`)

- Řádek 1: `[Sketch (S)] [Undo (Ctrl+Z)] [Clear (C)]`, Close odstranit. Signál `close_requested` zůstává (křížek v titulku), `TOOLBAR_SHORTCUTS["Close"]` pryč.
- Clear: klávesa `C` místo `Ctrl+Alt+C` v `_create_shortcuts` i v `OverlayWindow.keyPressEvent` (bez Ctrl/Alt, jako ostatní písmena nástrojů); `Ctrl+Alt+C` se odstraní.
- Mřížka bude mít 6 sloupců: každé tlačítko řádku 1 zabírá 2 sloupce, řádek 2 má 6 nástrojů (krok 3).
- Přepínač: `QPushButton`, text `Sketch (S)` / `Screen (S)` podle stavu, signál `sketch_toggled`. Není „zamačkávací“ – text sám říká, kam se přepne.
- **Ověření:** `test_toolbar_order_cross_and_drag` (ř. 482–491) – nové pořadí akcí `["Sketch (S)", "Undo (Ctrl+Z)", "Clear (C)"]`; `C` smaže vše, `Ctrl+Alt+C` už nic nedělá; křížek dál opouští kreslení.

### Krok 3 – kompaktní tlačítka a nové tvary Ellipse, Line (`overlay.py`, `icons.py`, `host/events.py`)

- `Tool`: přidat `ELLIPSE = "ellipse"`, `LINE = "line"`, `ERASER = "eraser"`.
- Tlačítka nástrojů: ikona + `(R)`, `(E)`, `(L)`, `(B)`, `(A)`, `(U)`; plný název jen v tooltipu (`Ellipse (E): drag from corner to corner`). `TOOLBAR_SHORTCUTS` přestane být klíčovaný textem tlačítka (text už název neobsahuje) – zkratka se předá přímo v definici tlačítka.
- `icons.py`: `ellipse_icon` (kruh), `line_icon`, `eraser_icon`.
- `paint_shape`: `drawEllipse(QRectF(p0, p1).normalized())`, `drawLine(p0, p1)`; `is_degenerate` jako u obdélníku / šipky.
- `shape_to_json`: ellipse a line ukládají `from`/`to` jako obdélník a šipka.
- Klávesy `E`, `L`, `U` v `_create_shortcuts` i v `OverlayWindow.keyPressEvent` (bez Ctrl).
- `host/events.py::_geometry`: `line` → `from x,y to x,y`; `ellipse` → `corners ...` (bounding box). Timeline tedy ukáže `DRAW #3 ellipse #FF0000 screen1 corners ...`.
- **Ověření:** unit testy kreslení elipsy a čáry (sidecar JSON, `drawings()`), klávesy E/L/U, pořadí tlačítek `["(R)", "(E)", "(L)", "(B)", "(A)", "(U)"]`; `tests/unit/test_events.py` řádky DRAW pro ellipse/line.

### Krok 4 – guma (`OverlayWindow` mouse eventy, `DrawingOverlay` historie)

- Levé tlačítko s nástrojem Eraser nezačne tvar, ale gesto gumy (cesta v canvas souřadnicích). Pravé tlačítko kreslí šipku i se zvolenou gumou, prostřední posouvá zoom.
- Při každém pohybu: úsečka poslední → aktuální bod se testuje proti **čáře** každého tvaru na **této** obrazovce: `QPainterPathStroker` (šířka `STROKE_WIDTH` + tolerance ~6 px na obrazovce, tj. `/ zoom` v canvasu) a `intersects` s obtaženou úsečkou. Obdélník a elipsa se testují jen obrysem (tah uvnitř je nesmaže), šipka včetně hrotu, štětec celou cestou. Zasažený tvar zmizí hned (živá odezva).
- Kurzor gumy: malý kroužek o velikosti tolerance.
- Historie: dnešní `_history` (seznam oken) nahradit seznamem akcí `add(window, shape)` / `erase(window, [(index, shape)])`. Jeden tah gumou = jedna akce; **`Ctrl+Z` po tahu gumou vrátí všechny objekty, které ten tah smazal**, na původní místo v pořadí a se stejným id. `pop_shape` → obecné odebrání / vložení tvaru podle id. Clear a opuštění kreslení historii vyprázdní jako dnes.
- Timeline: text `REMOVED` rozšířit na `Undo, Clear, Eraser or the drawing layer closed`; tvar vrácený přes Undo se v sidecarech objeví znovu se stejným id → `host/events.py` ho ukáže jako `RESTORED #3` (dnes se id po zmizení znovu neobjeví, takže tuto větev je potřeba přidat).
- **Ověření:** unit testy: tah přes obrys obdélníku/elipsy ho smaže, tah uvnitř ne, tah přes cestu štětce ji smaže, tah mimo nic nesmaže, guma neovlivní jinou obrazovku, pravé tlačítko s gumou kreslí šipku, Undo po gumě vrátí přesně smazané tvary (id, pořadí), další Undo pak maže poslední nakreslený tvar; `test_events.py` pro `RESTORED`; při zoomu se tolerance chová stejně na obrazovce.

### Krok 5 – Sketch / Screen (`overlay.py`)

- `DrawingOverlay`: stav `_sketch`, `toggle_sketch()`, signál pro lištu, klávesa `S` (v `_create_shortcuts` i `keyPressEvent`). `show()` i `hide()` ho vrací na `False` (každé zapnutí Draw začíná snímky obrazovek a tlačítkem „Sketch“).
- Přepnutí **nemění tvary ani historii** – kresby z obrazovek zůstávají vidět na bílém plátně a naopak; nevzniká žádný `REMOVED`.
- `OverlayWindow.paintEvent`: ve sketch režimu nejdřív neprůhledně bílé pozadí přes celé okno (místo `HIT_TEST_FILL` / snímku), pak tvary. `set_zoom` dovolí zoom i bez snímku, když je sketch zapnutý; zoom a posun zůstávají při přepnutí stejné.
- Nahrávací okno a lišta zůstanou nad bílou vrstvou (`keep_above`), takže jsou na snímcích vidět – stejně jako dnes.
- **Ověření:** unit testy: po `show()` je sketch vypnutý, `S` i tlačítko přepnou a změní text tlačítka, přepnutí zachová tvary a historii Undo, `paintEvent` při sketch vyplní bílou (grab okna), zoom ve sketch funguje bez snímku. `tests/integration/test_overlay_capture.py`: snímek `mss` ve sketch režimu je bílý (kromě nahrávacího okna a lišty) a nakreslený tvar je na něm vidět.

### Krok 6 – Sketch v metadatech pro AI (recorder, `host/events.py`, `frames`, SKILL.md)

Aby agent věděl, že uživatel skicuje na bílém plátně, a ne kreslí do snímku obrazovky:

- **Sidecar snímku:** `"background": "screen"` nebo `"sketch"` (zapisuje se, jen když je kreslení zapnuté; čte se tam, kde recorder čte `drawings()` a `zoom()`, z nové `DrawingOverlay.background()`).
- **Tvar:** každý tvar dostane `"background"`, na kterém byl dokončen (`shape_to_json`), takže i u tvaru kresleného na obrazovce a pak viditelného na plátně je jasné, k čemu patří.
- **Timeline:** řádky `SKETCH on (white canvas on every screen)` a `SKETCH off (screenshots again)` z změny `background` v sidecarech (jako `ZOOM`/`ZOOM END`); `DRAW` řádek tvaru z plátna má poznámku `(on the sketch canvas)`. Tvar nakreslený před začátkem cropu ve sketch režimu dostane na začátku i řádek `SKETCH on (before the range)`.
- **`frames --at`:** vrací `background` snímku vedle `zoom`; `counts` v `timeline` dostanou `sketch` (počet zapnutí).
- **SKILL.md (kap. 4):** popis řádků `SKETCH` a poznámky u `DRAW`: „uživatel kreslí na bílém plátně – skicuje nápad, nekomentuje obsah obrazovky; snímky z té doby ukazují jen plátno, obsah obrazovky hledej v čase před `SKETCH on`“.
- **Ověření:** `tests/unit/test_events.py` (SKETCH on/off, poznámka u DRAW, stav před rozsahem), `tests/unit/test_recorder_sidecars.py` (`background` v sidecaru), `tests/unit/test_cli_output.py` (`frames` vrací `background`); `tests/integration/test_mouse_timeline_e2e.py` nebo nový e2e: nahrát, přepnout sketch, nakreslit, přepnout zpět a zkontrolovat `timeline`.

### Krok 7 – dokumentace

- `README.md` (popis Draw, Clear `C` místo `Ctrl+Alt+C`), `skills/video-story/SKILL.md` (nové nástroje, geometrie ellipse/line, `SKETCH`, guma v `REMOVED`, `RESTORED`), `video-story-spec.md` (kap. 2 ř. 38 – Close na liště nahradí křížek; ř. 74 – nástroje, 9 barev, guma, sketch, Clear `C`), `tests/manual/acceptance.md` (kreslení), modul docstring `overlay.py`.
- **Ověření:** `uv run pytest tests/unit` zelené; ruční zkouška: nahrát krátké video se všemi nástroji, gumou + Undo a Sketch/Screen a zkontrolovat `timeline` a `frames`.

## 3. Rizika

- Bílá vrstva ve sketch režimu je neprůhledná: uživatel nevidí, co se mezitím děje na ploše (notifikace apod.) – to je záměr, nahrávka v tu dobu ukazuje jen plátno (a metadata to říkají).
- Šířka lišty: 6 nástrojů a 9 barev se musí vejít; s kratšími popisky by lišta měla být spíš užší než dnes.
- Historie Undo se mění ze „seznamu oken“ na „seznam akcí“ – dotkne se Clear a zavírání kreslení; testy undo/clear je nutné projít všechny.
- `RESTORED`: starší nahrávky ani dnešní `events.py` s návratem id nepočítají – nová větev nesmí rozbít párování DRAW/REMOVED u starých nahrávek.

## 4. Rozhodnuto (odpovědi z 12. 11. 2026)

| Otázka | Rozhodnutí |
| --- | --- |
| Kresby při přepnutí Sketch/Screen | Zůstávají vidět, přepínání nic nemaže. |
| `Ctrl+Z` po gumě | Vrátí objekty smazané posledním tahem gumy. |
| Kdy guma maže | Jen při dotyku nebo protnutí čáry objektu (obrys elipsy, cesta štětce…), ne uvnitř plochy. |
| Klávesa Sketch/Screen | `S`. |
| Zkratka Clear | `C` (nahrazuje `Ctrl+Alt+C`). |
| Zkratka Ellipse | Jen `E`. |
| Zkratka Stop | Zůstává `Ctrl+Alt+S`; žádné další zkratky (`Ctrl+Alt+V` byla chyba přepisu). |

## 5. Předpoklady

- Tlačítko Sketch/Screen není „zamačkávací“, jen mění text.
- „Orámování“ u černé = rámeček vybraného vzorku barvy na liště (dnes černý, u černé bílý).
- Guma maže jen celé objekty, ne jejich části, a jen na obrazovce, kde tah začal.
