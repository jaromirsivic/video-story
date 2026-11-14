# Video Story – implementační plán: kreslení se Shiftem, Clear zpět na `Ctrl+Alt+C`

Stav: **implementováno (12. 11. 2026)**; platné chování je ve [video-story-spec.md](video-story-spec.md) (kap. 6, Kresby). Z nahrávky `20261112-210902` (celá, 0:00–6:21.6, 12. 11. 2026), upraveno podle odpovědí na otázky (12. 11. 2026). **Pravidlo pro čáru v kap. 1 nahrazuje vše, co o čáře zaznělo v nahrávce.**

## 1. Cíl

Při kreslení (na obrazovkách i na Sketch plátně – je to jedno) mění **Shift** chování nástrojů na **levém** tlačítku myši:

| Nástroj | Se Shiftem |
|---|---|
| Obdélník `R` | kreslí **čtverec** (dokud je Shift držený) |
| Elipsa `E` | kreslí **kruh** (dokud je Shift držený) |
| Čára `L` | **spojené čáry** (viz níže) |
| Štětec `B` | **uzavřený tvar** – konec tahu se stále spojuje s prvním bodem |
| Šipka `A`, guma `U` | nic |

**Pravé tlačítko myši kreslí vždy šipku a Shift na něj nemá vliv.** Kde v nahrávce zaznělo „pravé tlačítko“, myslí se levé.

Navíc **Clear** se vrací z `C` na **`Ctrl+Alt+C`** (`C` se neosvědčilo) a **`Ctrl+Z` při drženém Shiftu nedělá nic** (Undo uprostřed akce se Shiftem nedává smysl).

**Čára (závazné pravidlo):**

1. Stisk levého tlačítka začne čáru, tažením se kreslí jako dnes.
2. **Puštění tlačítka bez Shiftu** čáru ukončí v místě puštění (jako dnes).
3. **Puštění tlačítka s drženým Shiftem** čáru neukončí: její konec dál sleduje kurzor i bez stisknutého tlačítka. Je jedno, zda byl Shift stisknutý už před začátkem čáry, nebo až během tažení.
4. Dokud je Shift držený, každý **stisk** levého tlačítka ukončí rozkreslenou čáru v místě stisku a ze stejného bodu hned začne další. Pro ni zase platí body 2–5.
5. **Puštění Shiftu:**
   - tlačítko je stisknuté → čára se dál kreslí tažením a skončí puštěním tlačítka (bod 2);
   - tlačítko stisknuté není → čára **hned skončí** tam, kde je kurzor.
6. Každá čára je samostatný tvar `line` s vlastním `id`. Čáry vzniklé takto za sebou (aspoň dvě) mají společné **`group`**; samostatná čára `group` nemá.
7. Spojené čáry mohou pokračovat **přes více obrazovek** (kap. 2, krok 6). První čára je ukotvená tam, kde začalo kreslení.

**Čára a šipka přes obrazovky:** čára i šipka (levým i pravým tlačítkem) jde nakreslit z jedné obrazovky na druhou; během kreslení je vidět na všech obrazovkách, přes které vede, a po dokončení se rozdělí na hranách obrazovek (krok 6). Obdélník, elipsa a štětec zůstávají na obrazovce, kde začaly (beze změny).

**Štětec se Shiftem:** Shift musí být stisknutý **před** stiskem tlačítka myši. Během tahu se poslední bod stále spojuje s prvním (spoj je součást tahu štětce, ne zvláštní čára). Kreslení skončí a tvar se uzavře, jakmile uživatel **pustí Shift nebo tlačítko myši** – co nastane dřív. Shift stisknutý až během tahu nic nemění.

**Metadata pro AI:** každý tvar nakreslený v režimu Shiftu to nese v JSONu i v timeline (čtverec, kruh, uzavřený tvar štětce, skupina spojených čar), aby AI věděla, že uživatel záměrně označil oblast (uzavřený tah kolem něčeho), nakreslil pravidelný tvar nebo vedl jednu lomenou trasu.

## 2. Kroky

Všechno je v `src/video_story/ui/overlay.py`, pokud není řečeno jinak. Pořadí od nejjednoduššího; každý krok jde otestovat samostatně.

### Krok 1 – Clear zpět na `Ctrl+Alt+C`, Undo se Shiftem nic

- `CLEAR_SHORTCUT = "Ctrl+Alt+C"`; `OverlayWindow.keyPressEvent`: `key == Qt.Key.Key_C and ctrl and alt` (holé `C` už nic nedělá). `_create_shortcuts` použije sekvenci beze změny kódu.
- `Ctrl+Z`: v `keyPressEvent` jen bez Shiftu; `QShortcut("Ctrl+Z")` na `Ctrl+Shift+Z` nereaguje, ale `DrawingOverlay.undo()` navíc ověří `queryKeyboardModifiers()` a se Shiftem nic neudělá (pojistka pro AltGr/nativní cesty).
- Tlačítko na liště „Clear (Ctrl+Alt+C)“ – ověřit, že výpočet šířky sloupců lišty (`column_width`) dál vychází.
- **Ověření:** `tests/unit/test_overlay.py` ř. ~211 obrátit (Ctrl+Alt+C maže, `C` ne), ř. ~501 pořadí akcí `["Sketch (S)", "Undo (Ctrl+Z)", "Clear (Ctrl+Alt+C)"]`; nový test: `Ctrl+Shift+Z` nic nevrátí.

### Krok 2 – sledování Shiftu

- Shift se čte z `event.modifiers()` u událostí myši a navíc z `QGuiApplication.queryKeyboardModifiers()` časovačem (~30 ms), který běží jen během rozkresleného tvaru nebo spojených čar. Důvod: kreslicí vrstva nemusí mít fokus klávesnice (nemusí dostat `keyReleaseEvent`), a puštění Shiftu **bez pohybu myši** musí ukončit čáru (bod 5), štětec a překreslit čtverec na obdélník.
- Jediné místo reakce: `DrawingOverlay._on_shift_changed(held)`, které předá změnu oknu s rozkresleným tvarem.

### Krok 3 – čtverec a kruh

- Funkce `_constrain_square(start, end) -> QPointF`: strana = `max(|dx|, |dy|)`, znaménka podle tažení (čtverec roste do kvadrantu kurzoru).
- Okno si pamatuje skutečnou polohu kurzoru (`self._raw_end`); `points[-1]` se z ní počítá při pohybu, puštění i změně Shiftu – s drženým Shiftem omezená. Přepínání je živé; o výsledku rozhoduje stav Shiftu při puštění tlačítka.
- `Shape` dostane `shift: bool` (tvar vznikl v režimu Shiftu). Uložená geometrie je čtvercová, takže `from`/`to` sedí.
- **Ověření:** tažení s `ShiftModifier` dá `from`/`to` se stejnou šířkou i výškou a `shift`; puštění Shiftu uprostřed tažení vrátí volný obdélník bez `shift`.

### Krok 4 – uzavřený štětec

- Při stisku štětce s drženým Shiftem `shift = True` (= uzavřený).
- `paint_shape`: uzavřený štětec jako `drawPolygon` bez výplně (spoj poslední–první bod stejným perem); `shape_outline`: `closeSubpath()`, aby guma zasáhla i spoj.
- Puštění Shiftu během tahu dokončí tvar jako puštění tlačítka (`_commit_shape`); další pohyb se stisknutým tlačítkem už nic nekreslí a následné puštění se ignoruje.
- **Ověření:** Shift před stiskem → uzavřený; Shift až během tahu → otevřený; puštění Shiftu ukončí tvar dřív než tlačítko; guma zasáhne uzavírací spoj.

### Krok 5 – spojené čáry (na jedné obrazovce)

- Stav spojených čar drží **`DrawingOverlay`** (ne okno), protože mohou přejít na jinou obrazovku: `_chain` = rozkreslená čára, kotevní bod v globálních logických souřadnicích, okno kotvy, `group` (přidělí se, až začne druhá čára) a pořadí čáry `segment`.
- Okna kreslicí vrstvy zapnou `setMouseTracking(True)`, aby `mouseMoveEvent` chodil i bez tlačítka; mimo spojené čáry se takové pohyby ignorují hned na začátku.
- Přechody podle kap. 1:
  - `mouseReleaseEvent` (čára): Shift držený → čára zůstává rozkreslená (`_chain`), jinak `_commit_shape` jako dnes;
  - `mousePressEvent` v `_chain`: ukončí čáru v místě stisku, přidělí `group` (pokud ještě není) a začne další čáru ze stejného bodu se stisknutým tlačítkem; čára nulové délky (stisk na místě kotvy) se neuloží, kotva zůstane;
  - `_on_shift_changed(False)` bez stisknutého tlačítka: čára skončí na poloze kurzoru (`QCursor.pos()`), stav se vynuluje; se stisknutým tlačítkem nic.
- Spojené čáry skončí (rozkreslená čára se zahodí, uložené zůstanou) také změnou nástroje, opuštěním kreslení (`Esc`, Draw, křížek), Clear; přepnutí Sketch/Screen je nepřeruší. Během nich je zakázaný zoom kolečkem (jako u tažení) a pravé tlačítko se ignoruje.
- Časy: `started_ms` = stisk, kterým čára začala, `finished_ms` = stisk / puštění Shiftu, kterým skončila. `drawing_now` hlásí `line`.
- **Ověření** (unit testy přesně podle pravidla): Shift+stisk, puštění, pohyb, stisk ×3, puštění Shiftu bez tlačítka → 4 čáry, konec každé = začátek další, všechny se stejným `group` a `segment` 1–4; tah bez Shiftu, pak Shift, puštění tlačítka → čára pokračuje; puštění Shiftu se stisknutým tlačítkem → čára skončí až puštěním tlačítka; samostatná čára nemá `group`; stisk na místě nic neuloží; změna nástroje ukončí. Integrační `tests/integration/test_overlay_capture.py`: rozkreslená čára bez stisknutého tlačítka je ve snímku vidět.

### Krok 6 – čára a šipka přes více obrazovek

Dnes tvar patří oknu, kde začal; při tažení na jiný monitor se část za hranou (u šipky i hrot) nevykreslí a `shape_to_json` ořízne souřadnice každou zvlášť na okraj, takže šikmá šipka dostane v JSONu jiný směr. Pro čáru a šipku se to mění takto:

- Rozkreslená čára / šipka se vede v **globálních logických souřadnicích** (od začátku ke kurzoru) a drží ji `DrawingOverlay`, ne okno. Každé okno vykreslí tu část, která leží na jeho obrazovce: převod `window.to_canvas(global − window.geometry().topLeft())` (počítá i se zoomem dané obrazovky), ořez zajistí painter. Hrot šipky kreslí jen okno, na jehož obrazovce leží konec.
- Při stisku tažení Qt posílá události oknu, kde tlačítko stisklo (implicitní grab) – globální poloha z `event.globalPosition()` proto stačí i tehdy, když kurzor už je nad jinou obrazovkou. Bez tlačítka (spojené čáry) dostává události okno pod kurzorem; obojí vede do stejného stavu v `DrawingOverlay`.
- Při dokončení se tvar **rozdělí na hranách obrazovek**: pro každou obrazovku, přes kterou vede, se úsečka ořízne obdélníkem obrazovky (Liang–Barsky, nová čistá funkce `clip_segment`) a uloží jako samostatný tvar toho okna.
  - **Čára:** všechny kusy jsou `line`.
  - **Šipka:** kus s koncem (hrotem) je `arrow`, ostatní kusy jsou `line` s `"part_of": "arrow"` (dřík šipky).
  - Kusy jednoho tvaru mají stejné `group` (přidělí se i u jinak samostatné čáry či šipky, která přešla obrazovky), stejné `segment` a stejné časy. Úsek mimo všechny obrazovky (mezera mezi monitory) se neuloží. Tvar, který obrazovku nepřešel, se nedělí a `group` nemá.
- **Undo a guma:** kusy jednoho rozděleného tvaru jsou **jeden krok** historie (jedno `Ctrl+Z` vrátí celou šipku/čáru) a guma, která zasáhne jeden kus, smaže všechny kusy toho tvaru (a Undo je vrátí). Spojené čáry se dál vrací po jednotlivých čarách.
- Platí pro čáru tažením (s i bez Shiftu), spojené čáry a šipku na levém (`A`) i pravém tlačítku – jedna cesta kódu.
- **Ověření:** unit test `clip_segment`; test se dvěma okny kreslicí vrstvy (screens z fixture): čára z jedné obrazovky na druhou dá dva tvary `line` s navazujícími body na hraně a stejným `group`; šipka dá `line` (`part_of: arrow`) na výchozí obrazovce a `arrow` s hrotem na cílové; jedno `Ctrl+Z` odstraní oba kusy; guma na jednom kusu smaže oba. Integrační test: rozkreslená šipka přes hranu je vidět ve snímcích obou obrazovek. Ruční test na dvou monitorech s různým měřítkem.

### Krok 7 – metadata pro AI (`shape_to_json`, `host/events.py`)

- JSON tvaru: `"shift": true` u všeho nakresleného v režimu Shiftu; navíc `"closed": true` u štětce; u čar a šipek `"group": n` a `"segment": k` (jen u spojených čar nebo tvarů rozdělených na hranách obrazovek; jinak pole chybí); u dříku šipky `"part_of": "arrow"`.
- Timeline (`events.py`, ř. ~145 popis tvaru, ř. ~314 řádek `DRAW`): `rectangle … (square, Shift)`, `ellipse … (circle, Shift)`, `brush … (closed shape, Shift)`, `line … (connected lines group 2, segment 3)`, `line … (shaft of arrow group 4, continues on screen2)`, `arrow … (group 4, starts on screen1)`. Legenda timeline doplní význam.
- `skills/video-story/SKILL.md`: popis polí a že uzavřený tah štětce obvykle znamená „tuto oblast“, spojené čáry jednu trasu.
- **Ověření:** `tests/unit/test_events.py` – řádky timeline pro všechny čtyři případy; `shape_to_json` bez Shiftu pole nemá.

### Krok 8 – dokumentace a ruční test

- `video-story-spec.md` kap. 6 (Kresby): pravidla z kap. 1; Clear `Ctrl+Alt+C`; Undo se Shiftem nic.
- `README.md`, docstring modulu `overlay.py`, tooltipy v `TOOL_BUTTONS` (Rectangle „Shift: square“, Ellipse „Shift: circle“, Line „Shift: connected lines“, Brush „Shift before drawing: closed shape“).
- `tests/manual/acceptance.md` řádek 1 a `tests/manual/overlay_demo.py`.
- Ruční ověření na Windows se dvěma monitory, včetně Sketch plátna a zoomu jedné obrazovky.

## 3. Rizika a předpoklady

- **Fokus klávesnice:** stisk/puštění Shiftu nemusí dojít do kreslicí vrstvy jako klávesová událost – proto časovač s `queryKeyboardModifiers` (krok 2); na Windows funguje i bez fokusu.
- **Různé měřítko monitorů (DPI):** ověřeno na dvou monitorech (150 % a 100 %): Qt převádí globální body přes obrazovku, na které leží (nativní pixely), takže čára je fyzicky rovná a kusy na hraně navazují; logické globální souřadnice mezi monitory mají mezeru, proto se úsečka vždy vede v souřadnicích okna (`mapFromGlobal`), nikdy lineárně v globálních logických souřadnicích.
- **Zoom jen na jedné obrazovce:** část čáry na zvětšené obrazovce se kreslí ve zvětšeném pohledu; uložené souřadnice odpovídají jako dnes nezvětšenému plátnu.
- **Sledování myši bez tlačítka** (`setMouseTracking`) zvýší počet událostí; mimo spojené čáry se zahodí hned, dopad na výkon zanedbatelný.
- `Ctrl+Alt+C` odpovídá na české klávesnici AltGr+C; při kreslení se nepíše, zkratka platí jen v oknech Video Story během kreslení (jako dřív).

## 4. Rozhodnuto (odpovědi z 12. 11. 2026)

1. Tah bez Shiftu, pak Shift a puštění tlačítka → čára pokračuje; pravidlo v kap. 1 nahrazuje vše z nahrávky.
2. `Ctrl+Z` při drženém Shiftu nedělá nic.
3. Spojené čáry mohou pokračovat na jinou obrazovku (krok 6); první čára je ukotvená tam, kde se začalo.
4. Spojené čáry mají společné `group`; obyčejná čára ho nemá.
5. Čtverec, kruh, uzavřený štětec a spojené čáry jsou v JSONu i timeline označené (krok 7).
6. Pravé tlačítko kreslí vždy šipku, Shift na něj nemá vliv.
7. Šipka přes obrazovky se dělí stejně jako čára: kus s hrotem je šipka, ostatní kusy dřík (`line`, `part_of: arrow`), společné `group` (krok 6).
