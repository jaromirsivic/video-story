# Video Story – implementační plán: oboustranná šipka, Pause, Mute Mic / Mute PC, nové rozložení ovládacího okna

Stav: **implementováno (14. 11. 2026)**; platné chování je ve [video-story-spec.md](video-story-spec.md) (kap. 4 Nahrávací okno, 5 Nastavení, 6 Kresby / Pauzy / Ztlumení, 8 Editor). Plán upravený podle odpovědí (13. 11. 2026, 3 kola). Ruční test z kap. 3 (nahrávka s ověřením v reálném okně) ještě neproběhl. Z nahrávky `20261113-232115` (celá, 0:00–5:07, Process z editoru, master instrukce „Goal: an implementation plan“ – nic se zatím neimplementuje).

## 1. Shrnutí cíle

1. **Oboustranná šipka:** když uživatel kreslí šipku (levým tlačítkem s nástrojem Arrow, nebo pravým tlačítkem – „nezáleží na tom, jestli levým nebo pravým“) a stiskne **Shift**, šipka se kreslí s hrotem na obou koncích. Datová struktura šipky to musí umět nést a AI se to dozví ze záznamu (sidecar + řádek `DRAW` v timeline).
   *Mění to dosavadní pravidlo z [implementation-plan-shift-drawing.md](implementation-plan-shift-drawing.md): „pravé tlačítko kreslí vždy šipku a Shift na něj nemá vliv“ – nově Shift na šipku vliv má, u obou tlačítek.*
2. **Ovládací okno – přeskládání tlačítek:**
   - **Před nahráváním:** **Record** (zůstává jako dnes), Settings, přepínač světlého/tmavého tématu, Hide to Tray. Zašedlé **Draw zmizí** (kreslit stejně nejde, dokud se nenahrává).
   - **Během nahrávání:**
     - 1. řádek: **Stop**, **Pause**, **Keypoint**, téma, Hide to Tray
     - 2. řádek: **Mute Mic**, **Mute PC**, **Draw**
     - 3. řádek: stavový text jako dnes
3. **Pause / Resume** (`Ctrl+Alt+P`): zastaví nahrávání – snímky, mikrofon, zvuk počítače, myš, auto stop. **Čas pauzy se z nahrávky vystřihne** (nenahrává se ticho), po Resume nahrávka plynule navazuje. Místo pauzy je vidět v editoru (šedá čára, při najetí „Paused“ + délka, magnet pro In/Out) i v kontextu pro AI. Během pauzy nejde kreslit: otevřené kreslení zamrzne a pokus kreslit nabídne Resume v modálním okně.
4. **Mute Mic** (`Ctrl+Alt+M`, ikona mikrofonu): od stisku se místo mikrofonu nahrává ticho, ať uživatel mluví nebo ne; dalším stiskem se ztlumení zruší.
5. **Mute PC** (`Ctrl+Alt+L`): totéž pro zvuk počítače (např. puštěné video). Zvuk počítače se nahrává vždy; když ho má uživatel v Settings vypnutý, nahrávání začne s Mute PC už zapnutým (nahrává se ticho).
6. **Mute ovládá výhradně uživatel:** Pause/Resume ani Stop → Continue Recording na ztlumení nesahají; během pauzy lze ztlumení zapnout i zrušit.

## 2. Kroky

### Část A – oboustranná šipka

**A1. Model tvaru** – [overlay.py](src/video_story/ui/overlay.py) `Shape`
- Nové pole `heads: str = "end"` s hodnotami `"end"` (dnešní šipka), `"both"` (oboustranná) a `"start"` (jen pro kus rozdělené šipky, viz A3). Stará data bez pole = `"end"`.
- Pro šipku znamená `shift=True` „kreslena se Shiftem = oboustranná“ (obdobně jako čtverec/kruh).

**A2. Kreslení se Shiftem** – `_Span`, `_note_shift`, `_begin_span`, `_release_span`
- `_Span` dostane `double: bool`. Při stisku (levé tlačítko s Arrow i pravé tlačítko) se nastaví podle drženého Shiftu; `_note_shift` ho u rozkreslené šipky mění **živě** (stejně jako čtverec u obdélníku): Shift stisknutý během tažení → oboustranná, puštěný → zase jednostranná. Rozhoduje stav při puštění tlačítka.
- Chování čáry (spojené čáry) se nemění; `_release_span` dál řetězí jen `Tool.LINE` s levým tlačítkem.
- Zrušit omezení „Shift jen pro levé tlačítko“ pro šipku; guma Shift dál ignoruje.

**A3. Kreslení, guma, rozdělení přes obrazovky**
- `paint_shape` a `shape_outline`: hrot podle `heads` – na konci, na začátku (`_arrow_head(head, tail)`), nebo na obou; dřík zkrátit na příslušných koncích.
- `_commit_span` pro šipku přes více obrazovek: dnes má hrot jen poslední kus (`tool=arrow`), ostatní jsou `line` s `part_of: "arrow"`. U oboustranné: první kus `arrow` s `heads="start"`, poslední `arrow` s `heads="end"`, prostřední `line` + `part_of`. Šipka jen na jedné obrazovce → jeden kus `heads="both"`.
- Náhled rozkreslené šipky (`_span_view_shape`) musí hroty ukazovat živě.

**A4. Export do sidecaru** – `shape_to_json`
- `"heads": "both"` / `"start"` jen když není `"end"` (staré záznamy beze změny), `"shift": true` u šipky kreslené se Shiftem.

**A5. Timeline pro AI** – [events.py](src/video_story/host/events.py)
- `_geometry`: oboustranná šipka `ends A B` místo `tail A head B` (u ní ocas není).
- `_shape_note`: `(double-headed, Shift)`; u kusů rozdělené šipky rozlišit „head part at the start / at the end“.
- Aktualizovat `legend` v timeline.

**A6. UI texty** – tooltip nástroje Arrow („Shift: arrow with two heads“), tooltip o pravém tlačítku, docstring modulu overlay.

### Část B – Pause / Resume

**B1. Hodiny s pauzou** – [core/timeline.py](src/video_story/core/timeline.py) `RecordingClock`
- `pause()` / `resume()`, `paused` a seznam pauz (perf_counter od–do). `elapsed_ns()` nepočítá čas pauz → **pauza se z časové osy vystřihne**, osa zůstane souvislá.
- `ms_at(stamp)`: stamp uvnitř pauzy → `None` (událost se zahodí), jinak odečíst pauzy před ním. `perf_counter_at(elapsed)` přičte dosavadní pauzy.
- Upravit docstringy („the recording never pauses“ v timeline.py a recorder.py).

**B2. Recorder** – [capture/recorder.py](src/video_story/capture/recorder.py)
- `pause()` / `resume()` / `paused`; capture smyčka během pauzy nesnímá a čeká na událost (kontrola volného místa na disku běží dál).
- Auto stop počítá z `elapsed` → pauza ho zastaví sama.
- `add_keypoint` během pauzy nic nedělá.
- Manifest (i provizorní, kvůli obnově po pádu): `pauses: [{"at_ms": …, "duration_ms": …}]` (`at_ms` na časové ose nahrávky = místo střihu, `duration_ms` = jak dlouho pauza reálně trvala). Pauza, která trvá ve chvíli Stop, se uzavře okamžikem Stop.

**B3. Zvuk během pauzy**
- Mikrofon ([audio_recorder.py](src/video_story/capture/audio_recorder.py)): stream zůstává otevřený (žádné znovuotevírání zařízení), bloky z doby pauzy se zahazují v callbacku (příznak `paused`).
- Zvuk počítače ([computer_audio.py](src/video_story/capture/computer_audio.py)): data z doby pauzy zahodit a `_expected_frames` / `_pad_to` počítat z recording clocku (bez pauz), jinak by doplnil ticho za celou pauzu.
- Živý přepis čte rostoucí WAV – během pauzy jen nepřibývá, úprava se nečeká (ověřit).

**B4. Myš a schránka** – [input_recorder.py](src/video_story/capture/input_recorder.py) přes `clock.ms_at` (události v pauze padnou samy); schránku během pauzy nezaznamenávat.

**B5. Kreslení během pauzy – zamrzne** – [overlay.py](src/video_story/ui/overlay.py), [control_window.py](src/video_story/ui/control_window.py)
- **Kreslení není otevřené:** během pauzy ho nejde otevřít – **Draw zašedlé**, zkratky `Ctrl+Space` / `Ctrl+F1` nic nedělají.
- **Kreslení je otevřené při Pause:** zůstane otevřené i se všemi tvary, Sketch plátnem a zoomem, ale **zamrzne** – nic v něm nejde změnit. `DrawingOverlay` dostane `set_frozen(bool)`:
  - tlačítka plovoucího toolbaru (Sketch/Screen, Undo, Clear, nástroje, barvy, křížek) jsou zašedlá; toolbar i ovládací okno jde dál přesouvat;
  - Draw v ovládacím okně je zašedlé (zavřením by tvary zmizely);
  - rozkreslený tvar (tlačítko ještě držené, čekající spojené čáry) se při Pause zahodí jako při Esc (`_cancel_span` / `_current = None`) – zamrznout může jen hotový stav.
- **Pokus kreslit během pauzy** – stisk tlačítka myši na overlay, kolečko (zoom), prostřední tlačítko (posun), klávesové zkratky kreslení (`Esc`, `Ctrl+Z`, `Ctrl+Alt+C`, `S`, `R/E/L/B/A/U`, `1`–`9`, `0`) nebo `Ctrl+Space` / `Ctrl+F1`: nic se nenakreslí ani nezmění a zobrazí se **modální okno**:
  - titulek „Recording paused“, text „Drawing on the screen is not possible while the recording is paused. Resume the recording?“, tlačítka **Yes** / **No** (výchozí Yes, Esc = No);
  - **Yes** → nahrávání se ihned obnoví (stejně jako Resume) a kreslení pokračuje se stávajícími tvary; kliknutí, které okno vyvolalo, se nekreslí;
  - **No** → zůstává pauza, kreslení dál zamrzlé;
  - okno se otevře jen jednou (další pokusy, dokud je otevřené, se ignorují), musí být nad overlayem (always-on-top, `raise_()` / `activateWindow()` jako u Stop dialogu) a po zavření se skryje (známý PySide pitfall: dialog po `exec` výslovně skrýt, jak to dělá `_on_stop`).
- Stop během pauzy s otevřeným kreslením se chová jako dnes Stop během kreslení.
- Recorder během pauzy nesnímá, takže zamrzlé kreslení ani modální okno se do nahrávky nedostanou; po Resume první snímek ukáže tvary tak, jak byly.

**B6. Ovládací okno** – [control_window.py](src/video_story/ui/control_window.py)
- Tlačítko **Pause (Ctrl+Alt+P)** ↔ **Resume (Ctrl+Alt+P)** (`pause_icon` / `play_icon` z [icons.py](src/video_story/ui/icons.py) už existují), systémová zkratka v `RecordingHotkeys`.
- Stav: `Paused 00:01:05 | auto stop in …` (odlišná barva), tooltip ikony v trayi.
- Během pauzy zašedlé Keypoint a Draw; Mute Mic a Mute PC fungují dál (C4). Resume (tlačítkem, `Ctrl+Alt+P` nebo Yes v modálním okně z B5) rozmrazí kreslení, je-li otevřené.
- Stop během pauzy funguje; **Continue Recording** ve Stop dialogu vrátí okno do pauzy (Resume zůstává na uživateli).

**B7. Kontext pro AI** – [events.py](src/video_story/host/events.py): řádek `65000 PAUSE 42 s (the user paused the recording here; the 42 s are cut out, nothing was recorded meanwhile)`, počet `pauses` v `counts`, legenda; v `describe` / manifestu `pauses`. Aktualizovat popis v [skills/video-story/SKILL.md](skills/video-story/SKILL.md).

**B8. Editor – značky pauz** – [editor/timeline/view.py](src/video_story/editor/timeline/view.py) (a model, kde se čtou keypointy)
- Každá pauza = **šedá svislá čára** v místě `at_ms`, kreslená stejně jako zelené keypointy, jen šedě.
- Při najetí myší tooltip **„Paused 42 s“** (délka ve formátu `m:ss` od minuty výš), obdobně jako „Keypoint N“.
- **Magnet:** úchyty In/Out i playhead se k šedé čáře pauzy přichytávají stejně jako k zeleným keypointům (rozhodnuto).

### Část C – Mute Mic / Mute PC

**C1. Recordery** – `set_muted(bool)` v `AudioRecorder` a `ComputerAudioRecorder`: místo vzorků se zapisují nuly stejné délky (WAV zůstává synchronní s časovou osou, živý přepis dostane ticho). Ztlumení a pauza jsou nezávislé: v pauze se nic nezapisuje, ztlumení jen určuje, co se zapisuje mimo pauzu.

**C2. Zvuk počítače se nahrává vždy, když to jde** – [recorder.py](src/video_story/capture/recorder.py) `_start_computer_audio`
- Dnes se `ComputerAudioRecorder` při `computer_audio=false` vůbec nespustí, a pak by Mute PC nešlo zrušit. Nově se spustí vždy, když ho platforma umí (Windows), a **nastavení `computer_audio=false` znamená „začít s Mute PC zapnutým“** – stopa `computer.wav` pak obsahuje ticho, dokud uživatel Mute PC nezruší.
- Popisek v [settings_dialog.py](src/video_story/ui/settings_dialog.py) upravit, aby to říkal (např. „Record computer audio (off: starts with Mute PC on)“); `doctor`, `config` a SKILL.md popis `computer_audio` sladit.
- Platforma bez zvuku počítače (Linux/macOS) nebo zařízení, které nejde otevřít: Mute PC je **zapnuté a zašedlé** s tooltipem proč.

**C3. Recorder a manifest** – `set_mic_muted` / `set_computer_muted`, manifest `mutes: [{"track": "mic"|"computer", "from_ms": …, "to_ms": …}]` (časy na ose bez pauz; interval trvající při Stop se uzavře koncem nahrávky; ztlumení od začátku má `from_ms: 0`). Změna během pauzy se zapíše s časem místa pauzy.

**C4. Ovládací okno**
- **Mute Mic (Ctrl+Alt+M)** a **Mute PC (Ctrl+Alt+L)** jako checkable tlačítka: ikona mikrofonu / reproduktoru, zapnuté ztlumení = přeškrtnutá ikona a zvýrazněné tlačítko. Nové ikony `mic_icon`, `mic_off_icon`, `speaker_icon`, `speaker_off_icon` ve stylu [icons.py](src/video_story/ui/icons.py).
- Systémové zkratky v `RecordingHotkeys` (registrované jen během nahrávání, jako ostatní; fungují i během pauzy).
- **Stav ztlumení řídí jen uživatel:** Pause, Resume, Stop → Continue Recording ho nemění. Výchozí stav se nastaví jen na začátku nové nahrávky (Record – i po Discard): Mute Mic vypnuté, Mute PC podle Settings.

**C5. Kontext pro AI** – `MUTE mic` / `UNMUTE mic`, `MUTE computer` / `UNMUTE computer` (aby AI vědělo, že ticho je záměr a ne chyba); `computer muted from the start` když nahrávka začala s Mute PC. Případně tenká značka na zvukových stopách v editoru (není požadováno – neplánuji).

### Část D – Rozložení ovládacího okna

**D1.** [control_window.py](src/video_story/ui/control_window.py) – místo jednoho `QHBoxLayout` tři řádky (`QVBoxLayout`): řádek 1 tlačítka, řádek 2 (Mute Mic, Mute PC, Draw – skrytý mimo nahrávání), řádek 3 stavový text.
- Klid: Record, Settings, téma, tray. Draw se skryje (ne zašedne).
- Nahrávání: Stop, Pause, Keypoint, téma, tray / Mute Mic, Mute PC, Draw.
- Pauza: Stop, **Resume**, Keypoint (zašedlé), téma, tray / Mute Mic, Mute PC, Draw (zašedlé; otevřené kreslení zamrzlé, viz B5).
- Zpracování po Stop / Discard: návrat do klidového rozložení; `_show_recording_controls` a `_fit_width` přepsat na nové řádky.
- Draw se zkratkami `Ctrl+Space` / `Ctrl+F1` mimo pauzu beze změny.

### Část E – Dokumentace

- [video-story-spec.md](video-story-spec.md) (kap. 6 Kresby, ovládací okno, manifest `pauses` / `mutes`, význam `computer_audio`), [README.md](README.md).
- [skills/video-story/SKILL.md](skills/video-story/SKILL.md): oboustranná šipka v popisu `DRAW`, řádky `PAUSE` a `MUTE`, Pause/Mute a zkratky v popisu okna, nový význam `computer_audio`; [skills/video-story-edit/SKILL.md](skills/video-story-edit/SKILL.md): šedé značky pauz na časové ose.
- Docstringy `overlay.py`, `control_window.py`, `hotkeys.py`, `recorder.py`, `timeline.py`.

## 3. Ověření

- **Unit testy:** [test_overlay_shift.py](tests/unit/test_overlay_shift.py) (Shift u šipky levým i pravým tlačítkem, Shift stisknutý/puštěný během tažení, rozdělení přes obrazovky, guma zasáhne oba hroty), [test_overlay.py](tests/unit/test_overlay.py) (`shape_to_json` s `heads`), [test_events.py](tests/unit/test_events.py) (`DRAW … ends … (double-headed, Shift)`, `PAUSE`, `MUTE`); nový test `RecordingClock` (elapsed, `ms_at` v pauze a po ní, `perf_counter_at`); [test_audio_tracks.py](tests/unit/test_audio_tracks.py) (mute = nuly, pauza = nic, délka WAV = délka osy, `computer_audio=false` = ticho do Unmute); [test_recorder_sidecars.py](tests/unit/test_recorder_sidecars.py) (žádné snímky v pauze, auto stop se v pauze nepočítá, `pauses`/`mutes` v manifestu); test ovládacího okna (viditelnost a zašednutí tlačítek v klidu / při nahrávání / v pauze, mute přežije Pause i Stop → Continue); test zamrzlého kreslení (Pause během kreslení: tvary zůstanou, rozkreslený tvar se zahodí, toolbar zašedlý, stisk myši / kolečko / zkratka nic nezmění a otevře právě jedno modální okno, Yes = Resume a kreslení jde dál, No = pauza trvá); test značek pauz v editoru (pozice, tooltip, magnet úchytů In/Out a playheadu).
- **Ruční test** (nahrávka s Instructions „ověř“): oboustranná šipka levým i pravým tlačítkem i přes obě obrazovky; Pause během kreslení → pokus kreslit ukáže modální okno nad overlayem, Yes pokračuje v nahrávání i kreslení; pauza 30 s s mluvením a videem → v editoru střih bez mezery, šedá čára s „Paused 30 s“, úchyt In se k ní přichytí, audio i snímky navazují, auto stop ukazuje správný zbytek; Mute Mic během mluvení → ticho ve waveformu i přepisu; Mute PC s puštěným videem; nahrávání s `computer_audio=false` začne s Mute PC zapnutým a po Unmute se zvuk počítače nahrává; všechny zkratky (`Ctrl+Alt+P/M/L`) z jiné aplikace v popředí; `timeline` ukáže `PAUSE` a `MUTE` řádky.
- `ruff` + celé `pytest`.

## 4. Rizika a předpoklady

- **Pauza mění invariant „souvislá osa = reálný čas“.** Na wall-clock čas spoléhají zvuk počítače (`time.monotonic`), myš (`perf_counter_ns` z hooku), živý přepis a obnova po pádu – každé místo je třeba převést na hodiny bez pauz, jinak se audio a snímky rozjedou.
- **Zkratky ověřeny na tomto počítači (13. 11. 2026):** `Ctrl+Alt+P`, `Ctrl+Alt+M` a `Ctrl+Alt+L` nemá zaregistrované žádný jiný program (`RegisterHotKey` uspěl) a v Qt/overlay se nepoužívají. Na nainstalované **české QWERTZ** klávesnici ale **AltGr+L píše „Ł“** – během nahrávání by tedy AltGr+L místo znaku přepnulo Mute PC. Stejné omezení už dnes mají `Ctrl+Alt+S` (AltGr+S = „đ“) a `Ctrl+Alt+K` (AltGr+K = „ł“); `P` a `M` žádný AltGr znak nemají. Na anglické klávesnici a české QWERTY konflikt není. `Ctrl+Alt+L` zůstává (rozhodnuto); v docstringu `hotkeys.py` a v SKILL.md konflikt s „Ł“ zmínit vedle „đ“ a „ł“.
- `computer.wav` nově vzniká i při `computer_audio=false` (s tichem) – o něco víc dat na disku a v archivu (`computer.m4a`); editor ji ukáže jako stopu se samým tichem.
- Stará nahrávka bez `heads`, `pauses`, `mutes` se musí číst beze změny (výchozí hodnoty).
- Širší první řádek (5 tlačítek s textem zkratek) – okno bude širší; případně zkrátit popisky na ikonu + tooltip.

## 5. Odpovědi a zbývající rozhodnutí

Rozhodnuto (13. 11. 2026): „Start“ = dnešní **Record**, zůstává; „Hide to Trade“ = **Hide to Tray**; pauza čas vystřihne; pauzy v editoru šedou čárou s tooltipem a délkou, i v kontextu pro AI; během pauzy nejde kreslit; zkratky `Ctrl+Alt+M` / `Ctrl+Alt+L`; ztlumení ovládá jen uživatel.

Rozhodnuto (13. 11. 2026, 2. kolo):

- **Zvuk počítače se nahrává vždy** (kde to platforma umí); když je v Settings vypnutý, nahrávání začne s Mute PC zapnutým a nahrává se ticho, dokud ho uživatel nezruší (C2).
- **`Ctrl+Alt+L` zůstává** i přes konflikt s AltGr+L („Ł“) na české QWERTZ.

Rozhodnuto (13. 11. 2026, 3. kolo):

- **Pause během kreslení:** kreslení zůstane otevřené i s tvary, ale zamrzne; pokus kreslit vyvolá modální okno s otázkou na Resume, **Yes** nahrávání obnoví (B5).
- **Magnet v editoru:** úchyty In/Out a playhead se přichytávají i k šedé čáře pauzy (B8).

Žádné otevřené otázky nezbývají; plán čeká na schválení.
