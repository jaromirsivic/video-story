# Video Story – implementační plán: kontextové menu titulků, Undo/Redo, dvojklik a drobnosti editoru

Stav: **část A (editor) implementována 14. 11. 2026**, schváleno po odpovědích Q1–Q6 a N1–N4; platné chování je ve [video-story-spec.md](video-story-spec.md) (kap. 8 Editor). Ruční test v reálném editoru ještě neproběhl; u splitteru se chyba offscreen nepodařilo vyvolat, oprava (linky na celých fyzických pixelech, překreslení po posunu) se musí ověřit na displeji se škálováním 125 %. **Část B (kreslení) implementována 14. 11. 2026** (Redo `Ctrl+Y`, lišta Sketch / Snap to grid / Clear, `grid_source` v sidecar a ve `frames`, texty `GRID` a `RESTORED`); ani ona zatím neprošla ručním testem při nahrávání. Z nahrávky `20261114-014631` (celá, 0:00–8:14, Process z editoru, master instrukce „Goal: an implementation plan“).

## 1. Shrnutí cíle

**Část A – editor (napřed):**

1. **Pravé tlačítko na stopě narration** otevře kontextové menu, jehož obsah závisí na zámku:
   - **Zamčeno:** jediná položka **Unlock narration track**. Dokud zámek nejde otevřít (přepis ještě běží, nahrávka je archivovaná), je položka **zašedlá** a nedá se na ni kliknout.
   - **Odemčeno:** **Add New Subtitle** (vždy), **Delete Subtitle** (jen nad existujícím titulkem), oddělovač a **Lock narration track**, který celou stopu znovu zamkne.
   - **Add New Subtitle** vytvoří na místě kliknutí titulek dlouhý 1 s, uživatel do něj napíše text a pak ho tažením roztáhne. Když tam není aspoň 1 s volna (např. klik doprostřed titulku), zobrazí se modální okno, že sem titulek přidat nejde a ať klikne tam, kde je aspoň vteřina volna.
   - **Delete Subtitle** titulek smaže **bez potvrzení**. Vrátí ho Ctrl+Z.
   - Odemknout jde jen ručně: klikem na zámek nebo položkou **Unlock narration track**. Nic jiného stopu samo neodemkne.
2. **Ctrl+Z / Ctrl+Y** (undo/redo) pro úpravy titulků: přidání, smazání, posun, roztažení nebo zkrácení tažením a uložení z okna Edit. Vrátit jde vždy aspoň jeden krok, plán počítá se zásobníkem více kroků.
   Při **zamčené** stopě Ctrl+Z ani Ctrl+Y nefungují: uživatel musí stopu nejdřív odemknout.
3. **Dvojklik na titulek** udělá totéž co tlačítko **Edit**, ale jen pro titulek, na který uživatel klikl, i když se v tom čase překrývá s titulkem druhé stopy. Funguje i při zamčené stopě (jako tlačítko Edit). **Playhead se dvojklikem vůbec nepohne, ani na okamžik.**
4. **Zámek** (odemčený) dostane ve světlém režimu zelenou tlačítka **Process** (`#1e9e3e`). V tmavém režimu zůstane `#2fd36f`.
5. **Čas pod přehrávačem** na **dvě desetinná místa** se stejnou strategií jako v In, Out a length, tedy **ořez** (floor): `00:11:34.701 / 00:20:02.067` → `00:11:34.70 / 00:20:02.06`.
6. **Dvojitá čára splitteru** mezi náhledem a řádkem In/Out bude vždy dvojitá, ať je splitter kdekoli.
7. **Tlačítko „⋯“ (menu Folders)** vpravo od New Folder bude vypadat jako tři tečky v záhlaví složky: stejně velké a v barvě textu motivu, viditelné ve světlém i tmavém režimu.

**Část B – kreslení (až po dokončení části A):**

8. **Ctrl+Y (redo) na kreslicí vrstvě** při nahrávání: vrátí, co vzalo Ctrl+Z (nakreslený tvar nebo tah gumy). Jen jako zkratka, **bez tlačítka**.
9. **Okno Drawing tools, první řádek**, nově `Sketch (S)` | `Snap to grid (Ctrl+G)` | `Clear (Ctrl+Alt+C)`:
   - tlačítko **Undo zmizí** (Ctrl+Z dál funguje jako zkratka);
   - na jeho místě je nové **zamačkávací** tlačítko **Snap to grid (Ctrl+G)**. Stisk ho zamáčkne a zůstane zamáčknuté, další stisk ho odmáčkne. Zamáčknuté funguje, jako kdyby uživatel držel Ctrl: všechny body se přichytávají na mřížku. Na **sketchi** je mřížka po celou dobu vidět, po odmáčknutí zmizí. Na **screenu** se mřížka nezobrazuje, ale body se na ni přichytávají stejně;
   - **Sketch** bude také zamačkávací: text zůstane vždy `Sketch (S)` (už se nepřepíná na „Screen“). Zamáčknuté = bílé plátno, odmáčknuté = obrazovky.

## 2. Kroky – část A (editor)

### Krok 1 – Datová vrstva: přidání a smazání titulku
[editor/recording.py](src/video_story/editor/recording.py), [editor/timeline/narration_edit.py](src/video_story/editor/timeline/narration_edit.py)

- `narration_edit.apply(segments, old, new)` dnes vyžaduje `len(old) == len(new)`. Rozšířím ho tak, aby `old` mohlo být prázdné (přidání) a `new` prázdné (smazání). Při stejné délce zůstane dnešní mapování 1:1, jinak se `old` odebere a `new` přidá. `validate` (žádný překryv v jedné stopě) se kontroluje jako dnes.
- `LoadedRecording.apply_segment_edit` se tím naučí přidávat a mazat bez nové cesty pro zápis souborů (`transcript.json` a `transcript.txt` přes `_store_transcript`, včetně retry z `core/fileio.py`).
- Nová čistá funkce `narration_edit.free_slot(segments, track, at_ms, duration_ms, length_ms=1000) -> (start, end) | None`. Najde mezeru v dané stopě, která obsahuje `at_ms`. Když je mezera ≥ 1 s, vrátí 1 s dlouhý úsek **začínající v místě kliknutí**. Když by přesáhl konec mezery nebo konec nahrávky, posune ho doleva, aby se vešel. Klik do titulku nebo do mezery < 1 s vrátí `None`. Časy se zaokrouhlí na 10 ms (`STEP_MS`).
- **Ověření:** unit testy na `apply` (přidání, smazání, výměna, chybný `old`) a na `free_slot` (prázdná stopa, klik uprostřed mezery, klik těsně před dalším titulkem → posun doleva, mezera 0,99 s → `None`, klik do titulku → `None`, konec nahrávky, spodní stopa).

### Krok 2 – Zásobník Undo/Redo titulků
nový modul [editor/subtitle_undo.py](src/video_story/editor/subtitle_undo.py) (bez Qt), napojení ve [window.py](src/video_story/editor/window.py)

- Jeden krok = dvojice `(old, new)` n-tic segmentů. **Undo** provede `apply_segment_edit(new, old)` a krok přesune do zásobníku redo. **Redo** provede `apply_segment_edit(old, new)` a krok vrátí zpět do undo. Každá nová úprava zásobník redo vyprázdní. Platí pro přidání, smazání, tažení (posun, roztažení, roll dvou titulků, přesun mezi stopami) i pro uložení z okna Edit.
- Všechny cesty, které titulky mění (`apply_segment_edit` v okně, `edit_subtitle`, nové add/delete), po úspěšném zápisu uloží krok. Limit je 50 kroků.
- Zásobníky patří k nahrávce: vymažou se při výběru jiné nahrávky, při **Transcribe again** a při změně přepisu zvenku (průběžný přepis).
- **Zkratky** jako `QAction` v `_build_actions` (`WindowShortcut`): **Ctrl+Z** (Undo) a **Ctrl+Y** (Redo). Když má fokus textové pole (In/Out, hledání), nechají zkratku tomu poli: akce se v tu chvíli nespustí. Stavový řádek ohlásí, co se vrátilo nebo zopakovalo („Undone: subtitle deleted 00:01:02.30“, „Redone: …“), případně „Nothing to undo.“ / „Nothing to redo.“.
- **Undo/Redo jen při odemčené stopě** (odpověď N1): při zamčené stopě Ctrl+Z/Ctrl+Y titulky nezmění, jen stavový řádek napíše „Narration is locked: unlock it to undo.“. Zásobníky se zamčením nemažou, takže po odemčení jde vrátit i úprava udělaná před zamčením (třeba uložení z okna Edit při zamčené stopě).
- **Ověření:** unit testy zásobníku (inverze všech typů kroků, redo po undo, vyprázdnění redo novou úpravou, limit, vymazání). Integrační test v okně: smazat → Ctrl+Z → titulek zpět a `transcript.json` je bajtově shodný s původním → Ctrl+Y → zase smazaný. Totéž pro posun tažením. Při zamčené stopě se nic nezmění.

### Krok 3 – Kontextové menu na stopě narration
[editor/timeline/view.py](src/video_story/editor/timeline/view.py), [window.py](src/video_story/editor/window.py)

- `TimelineView.contextMenuEvent`: když je pozice ve stopě narration, včetně její hlavičky vlevo (`narration_rect().contains(pos)`), vyšle nový signál `narration_menu_requested(lane, time_ms | None, segment | None, global_pos)`. V hlavičce je `time_ms` `None`.
- Okno sestaví `QMenu` podle stavu:
  - **zamčeno:** `Unlock narration track`, `setEnabled(narration_editable)`. Kliknutí volá `timeline.set_narration_unlocked(True)`, takže stavový řádek ohlásí odemčení stejně jako klik na zámek;
  - **odemčeno, v těle stopy:** `Add New Subtitle`, nad titulkem i `Delete Subtitle`, oddělovač a `Lock narration track`;
  - **odemčeno, v hlavičce:** jen `Lock narration track` (bez času nejde nic přidat).
- **Add New Subtitle:** `free_slot(...)`. Když vrátí `None`, otevře se modální `QMessageBox.information` s textem „A subtitle cannot be added here: there is not enough free space. Right-click a place with at least one second free.“. Jinak se otevře okno Edit (`SubtitleDialog`) pro nový 1s titulek s prázdným textem, protože titulek bez textu se na timeline nekreslí („no words, no box“). **OK** titulek uloží (jeden krok Undo), **Cancel** nic nepřidá. Stopa zůstane odemčená, takže ho uživatel hned roztáhne.
- **Delete Subtitle:** smaže hned, bez modálního okna. Stavový řádek: „Subtitle deleted (Ctrl+Z restores it).“.
- **Lock narration track:** `set_narration_unlocked(False)`. Rozběhnuté tažení se tím zruší, jako dnes při kliku na zámek.
- Během tažení titulku, scrubu nebo panování se menu neotevře.
- **Ověření:** pytest-qt: zamčeno → menu má jen Unlock. Během přepisu je Unlock zašedlý. Unlock z menu odemkne. Odemčeno, mezera → Add a Lock. Odemčeno, titulek → Add, Delete a Lock. Add do mezery 2 s → nový segment `[t, t+1000]`. Add do titulku → modální okno (patchnuté) a žádná změna. Delete → segment pryč bez dialogu. Lock z menu zamkne. Spodní stopa funguje stejně.

### Krok 4 – Dvojklik na titulek = Edit jen pro něj, playhead stojí
[editor/timeline/view.py](src/video_story/editor/timeline/view.py), [window.py](src/video_story/editor/window.py)

- `TimelineView.mouseDoubleClickEvent`: nad boxem titulku (`narration_segment_at`) vyšle `segment_edit_dialog_requested(segment)`. Okno zavolá upravenou `edit_subtitle(segments=[segment])`, takže `SubtitleDialog` dostane jen ten jeden titulek. Přehrávání se před otevřením okna zastaví, jako u tlačítka Edit.
- **Playhead se nepohne vůbec.** Dnes první stisk dvojkliku hned přesune playhead (levý stisk v těle timeline = scrub). Nově levý stisk **na boxu titulku** playhead hned nepřesune:
  - když se myš pohne o víc než `QApplication.startDragDistance()`, začne obyčejný scrub jako dnes (při odemčené stopě místo něj tažení titulku, jako dnes);
  - když se tlačítko pustí bez pohybu, spustí se jednorázový `QTimer` na `QApplication.doubleClickInterval()`. Teprve po jeho vypršení se playhead přesune na místo kliknutí, takže jednoduchý klik funguje jako dnes, jen o ten interval později;
  - když přijde dvojklik, timer se zruší a playhead zůstane, kde byl.
  - Stisk mimo boxy titulků (prázdné místo stopy, pravítko, stopy obrazovek) přesouvá playhead okamžitě jako dnes.
- Dvojklik funguje při zamčené i odemčené stopě, stejně jako tlačítko Edit (odpověď N2).
- **Ověření:** pytest-qt: dvojklik na horní titulek, který se překrývá se spodním → okno má jedno pole s textem horního titulku a `playhead_ms` se během celého dvojkliku nezměnil (sledovat signál `playhead_moved`: žádná emise). Jednoduchý klik na titulek → playhead se přesune až po intervalu dvojkliku. Klik do prázdna → přesun hned. Dvojklik do prázdna nic neotevře.

### Krok 5 – Barva zámku ve světlém režimu
[editor/timeline/view.py](src/video_story/editor/timeline/view.py)

- `_paint_lock` vezme barvu z `self.colors["lock"]`: v `DARK_COLORS` zůstane `#2fd36f`, v `LIGHT_COLORS` bude `#1e9e3e` (zelená tlačítka Process; konstanta se přesune do sdíleného místa, aby ji používalo okno i timeline).
- **Ověření:** unit test, že `LIGHT_COLORS["lock"]` je zelená Process. Ruční kontrola obou motivů tlačítkem Theme.

### Krok 6 – Čas pod přehrávačem na dvě desetinná místa
[editor/preview.py](src/video_story/editor/preview.py), [window.py](src/video_story/editor/window.py)

- `format_crop_time` (dnes ve `window.py`, `HH:MM:SS.cc`, uřízne poslední číslici, tedy floor na setiny) se přesune do sdíleného modulu (např. `editor/timefmt.py`) a použije se v `PreviewPanel` pro `time_label` i pro jeho výchozí text `00:00:00.00 / 00:00:00.00`. Strategie je stejná jako v In, Out a length.
- **Ověření:** unit test formátu (`694701` → `00:11:34.70`, `1202067` → `00:20:02.06`). Existující testy, které čtou `time_label`, se upraví.

### Krok 7 – Dvojitá čára splitteru vždy dvojitá
[editor/separators.py](src/video_story/editor/separators.py)

- **Pravděpodobná příčina:** obrazovka běží se škálováním (2560×1600, nejspíš 150 %). `SeparatorHandle` kreslí dvě čáry perem širokým 1 logický pixel na pozicích `middle ± 2`. Při necelém poměru (1,5) padá jedna z čar podle polohy úchytu mezi fyzické pixely a vykreslí se slabě nebo vůbec. Proto se to při tažení mění. Druhá možná příčina: u spodní meze může spodní panel úchyt částečně překrýt.
- **Postup:**
  1. Reprodukovat při 150 % (a pro kontrolu při 100 %): zalogovat `handle.geometry()`, `devicePixelRatioF()` a polohu obou čar v obou stavech.
  2. **Oprava pro DPI:** kreslit čáry jako vyplněné obdélníky zarovnané na fyzické pixely: tloušťka 1 fyzický pixel nebo víc, pozice spočítané ve fyzických pixelech a převedené zpět, bez antialiasingu. Mezera mezi čarami pak bude vždy celý počet fyzických pixelů.
  3. Pokud se potvrdí i překryv: zajistit, aby úchyt nikdy nebyl pod sousedním widgetem.
- **Ověření:** offscreen test, který vykreslí `SeparatorHandle` do `QImage` s `devicePixelRatio` 1,0, 1,25, 1,5 a 1,75 a s úchytem posunutým o 0–3 px, a spočítá řádky s barvou čáry. Musí být vždy dva oddělené. Ruční kontrola: táhnout splitter nahoru a dolů až na doraz.

### Krok 8 – Tlačítko „⋯“ jako tečky v záhlaví složky
[window.py](src/video_story/editor/window.py), [editor/recordings_view.py](src/video_story/editor/recordings_view.py)

- Znak `FOLDERS_MENU_TEXT = "⋯"` se nahradí ikonou nakreslenou stejnou funkcí `draw_menu_dots`, kterou kreslí tečky záhlaví složky. Velikost teček bude stejná jako tam, barva bude barva textu motivu. Nová funkce `menu_dots_icon(size)` vedle `new_folder_icon` a její nastavení v `_apply_icons`, aby se ikona po přepnutí Theme překreslila.
- Zkontrolovat stylesheet `QToolButton::menu-indicator`, který ve světlém režimu nejspíš za zmizení znaku může, aby ikonu neskrýval.
- **Ověření:** offscreen render tlačítka ve světlém i tmavém motivu: v oblasti tlačítka jsou pixely barvy textu. Ruční kontrola vedle záhlaví složky.

### Krok 9 – Dokumentace a testy části A
- [video-story-spec.md](video-story-spec.md) kap. 8 (Editor): kontextové menu se zámkem, Undo/Redo, dvojklik bez pohybu playheadu, formát času, barva zámku, tlačítko ⋯.
- `SKILL.md` se v části A nemění: agent dál dostává jen výsledné titulky (`SAY` řádky).
- Celá sada `pytest` (unit a integration). Ruční test v reálném editoru: zámek z menu, přidat, smazat, Ctrl+Z/Ctrl+Y, dvojklik, oba motivy, splitter při 150 %.
- Commit části A.

## 3. Kroky – část B (kreslení, až po části A)

### Krok 10 – Redo na kreslicí vrstvě (jen zkratka Ctrl+Y)
[ui/overlay.py](src/video_story/ui/overlay.py)

- `DrawingController` dnes drží `_history: list[_Action]` (tvar přidán / tah gumy odebral tvary) a `undo()` z něj bere poslední krok. Přibude `_redo: list[_Action]`:
  - `undo()` vrácený krok vloží do `_redo`;
  - nová metoda `redo()` vezme poslední krok z `_redo`: u přidaného tvaru ho znovu přidá (`add_shape`, stejné `id`, stejné pořadí kreslení, všechny kusy rozděleného tvaru), u tahu gumy tvary znovu odebere. Pak krok vrátí do `_history`;
  - každý nový tvar nebo tah gumy (`_commit_shapes`, `_record_erase`), **Clear** a zavření vrstvy (`_destroy_windows`) `_redo` vyprázdní;
  - při držení Shift (rozpracovaná Shift akce) redo nic nedělá, stejně jako undo.
- **Zkratka Ctrl+Y:** `REDO_SHORTCUT = "Ctrl+Y"` vedle `UNDO_SHORTCUT`, aplikační `QShortcut` jako u Undo (řádek ~2478) a obsluha v `keyPressEvent` overlaye (řádek ~1231). Patří i mezi klávesy blokované při pauze (`frozen_key`), jako Ctrl+Z. **Tlačítko Redo nebude.**
- **Timeline pro agenta:** tvar vrácený přes Redo se znovu objeví ve snímcích. Dnešní řádek `RESTORED` říká „back by Undo of an Eraser stroke“ a Redo smazání tvaru gumou dá `REMOVED`. Text `RESTORED` (a legenda v [host/events.py](src/video_story/host/events.py)) se změní na „back by Undo or Redo“. Ověřit, že `RESTORED` (podle `id` ve snímcích) vznikne i pro tvar vrácený Redo po Undo kreslení, nejen po Undo gumy.
- **Ověření:** pytest-qt testy controlleru: nakreslit → Ctrl+Z → Ctrl+Y → tvar zpět se stejným `id`. Guma → Ctrl+Z → Ctrl+Y → tvary zase pryč. Nový tvar po Undo vyprázdní redo. Clear vyprázdní redo. Rozdělená šipka přes dvě obrazovky se vrátí celá. Test timeline: `RESTORED` po Redo. Při pauze je Ctrl+Y blokované.

### Krok 11 – Okno Drawing tools: pryč s Undo, nové Snap to grid, Sketch jako zamačkávací
[ui/overlay.py](src/video_story/ui/overlay.py) (`OverlayToolbar`, `DrawingController`, `OverlayWindow`)

- **První řádek** `OverlayToolbar`: `Sketch (S)` | `Snap to grid (Ctrl+G)` | `Clear (Ctrl+Alt+C)`, každé dvě kolonky široké jako dnes. Tlačítko **Undo se odstraní** včetně signálu `undo_requested` z toolbaru. Zkratka Ctrl+Z zůstává a tooltip okna/nápověda (řádek ~266, ~2473) dál zmiňuje Ctrl+Z a nově i Ctrl+Y.
- **Sketch (S)** jako zamačkávací tlačítko: `setCheckable(True)`, text vždy `Sketch (S)`, `setChecked(sketch)` v `OverlayToolbar.set_sketch` místo přepínání textu. Konstanta `SCREEN_LABEL` a výpočet šířky „s oběma texty“ (řádky ~1406–1411) zmizí. Tooltip: „Sketch on a white canvas on every screen (S); press again for the screens. The drawings stay.“. Klik i klávesa `S` dál volají `toggle_sketch()`, zamáčknutí se jen zrcadlí ze stavu controlleru (aby klávesa a klik nikdy nesouhlasily jen napůl).
- **Snap to grid (Ctrl+G)** jako zamačkávací tlačítko:
  - controller dostane stav `_grid_locked: bool` a metody `set_grid_locked()` / `toggle_grid_lock()`, signál pro toolbar a zkratku `GRID_SHORTCUT = "Ctrl+G"` (aplikační `QShortcut` jako ostatní a obsluha v `keyPressEvent`; při pauze blokovaná jako ostatní kreslicí klávesy);
  - **přichytávání** = `self._ctrl or self._grid_locked`. Nová vlastnost `snapping` nahradí přímé čtení `self._ctrl` v `_snapped_global` a všude, kde se dnes podle Ctrl rozhoduje o mřížce (posun rozpracované čáry v `_note_ctrl`, `_on_ctrl_changed` oken, kreslení mřížky). Změna zamáčknutí udělá totéž co změna Ctrl: rozpracovaný konec čáry se hned přichytí nebo uvolní a okna se překreslí;
  - **mřížka se kreslí** (`_paint_grid`) jen na sketchi a jen při `snapping`. Na screenu se nekreslí nikdy, přichytávání ale funguje;
  - stav se na začátku každého kreslení vynuluje (odmáčknuto) a chová se stejně jako Sketch („every drawing session starts on the screens“): reset na stejném místě (řádek ~1785) a při zavření vrstvy (odpověď N4).
- **Sidecar a timeline pro agenta** ([host/events.py](src/video_story/host/events.py), `DrawingOverlay.grid()`):
  - `grid()` vrací rozteč, kdykoli body přichytávají (Ctrl nebo zamáčknuté tlačítko). Sidecar snímku dostane nové pole `grid_source`: `"ctrl"`, `"toggle"` nebo obojí;
  - `_grid_events` dnes zahazuje krátký Ctrl (pod `GRID_MIN_MS`) bez přichyceného tvaru jako klávesovou zkratku. Úseky ze zamáčknutého tlačítka se zahazovat nesmí;
  - text řádků: `GRID on: Snap to grid switched on (Ctrl+G), points snap to a N px grid …` vs. dnešní `GRID on: Ctrl held, …`; `GRID off (Snap to grid switched off …)` vs. `(Ctrl released …)`. Legenda se doplní. Stará sidecar bez `grid_source` se čtou jako `"ctrl"`, takže staré nahrávky dávají stejný výstup jako dnes;
  - samotná zkratka Ctrl+G je krátký Ctrl, kterou dnešní pravidlo správně zahodí.
- **Ověření:** pytest-qt: toolbar má Sketch, Snap to grid a Clear, žádné Undo. Sketch zamáčknuté ↔ bílé plátno, text se nemění, klávesa `S` zamáčknutí zrcadlí. Snap to grid zamáčknuté → bod se přichytí bez Ctrl (screen i sketch). Na sketchi se kreslí mřížka (pixel test barvy `GRID_COLOR`), na screenu ne. Odmáčknutí → volné body a mřížka zmizí. Ctrl+G přepíná. Při pauze je Ctrl+G blokované. Unit testy `_grid_events`: zamáčknutý úsek kratší než `GRID_MIN_MS` zůstane, text podle `grid_source`, staré sidecar beze změny výstupu.

### Krok 12 – Dokumentace a testy části B
- [video-story-spec.md](video-story-spec.md) (kreslení): Redo (Ctrl+Y), toolbar bez Undo, Snap to grid (Ctrl+G), zamačkávací Sketch, nové texty `GRID on/off` a `RESTORED`.
- `SKILL.md` (a jeho kopie pro Codex/Cursor): popis ovládání kreslení (Ctrl+Y, Snap to grid / Ctrl+G) a řádků `GRID` a `RESTORED`, aby agent věděl, že mřížka může být zapnutá i bez držení Ctrl.
- Celá sada `pytest`, ruční test při nahrávání (obě obrazovky, sketch i screen, zoom). Commit části B.

## 4. Rizika a předpoklady

- **Ctrl+Z/Ctrl+Y a textová pole v editoru:** okno-široká zkratka by mohla přebít vlastní Undo v polích In/Out a ve vyhledávání. Proto se akce v textovém fokusu nespouští.
- **Undo titulků po změně zvenku:** po Transcribe again nebo po průběžném přepisu by staré kroky neplatily. Zásobníky se proto maží a `apply` by stejně vrátil `ValueError`, který se ohlásí ve stavovém řádku.
- **Prázdný titulek se nekreslí:** proto Add hned otevře okno Edit a Cancel nic nepřidá.
- **Zpožděný jednoduchý klik na titulek:** aby se při dvojkliku playhead vůbec nepohnul, přesune jednoduchý klik na box titulku playhead až po intervalu dvojkliku (ve Windows typicky 500 ms). Klik mimo titulky zůstává okamžitý.
- **Příčina splitteru** je hypotéza (necelé DPI). Ověří se reprodukcí, než se začne opravovat.
- **Ctrl+G a heuristika krátkého Ctrl:** přichytávání ze zamáčknutého tlačítka se musí v sidecar odlišit od držení Ctrl, jinak by krátké zapnutí tlačítka timeline zahodila jako zkratku.
- **Agent a změněné texty `GRID` / `RESTORED`:** mění se, co agent čte. Proto se upraví i `SKILL.md`.
- **Předpoklad:** nový titulek jde vždy do stopy, na kterou uživatel klikl (horní nebo spodní).

## 5. Otevřené otázky

Žádné. Všechny otázky (Q1–Q6, N1–N4) jsou zodpovězené a zapracované.
