# Video Story – implementační plán: dvě stopy titulků, odemykání a úpravy titulků myší

Stav: **implementováno (14. 11. 2026)**, schváleno včetně tvaru pro agenta (Q2); platné chování je ve [video-story-spec.md](video-story-spec.md) (kap. 8 Editor, 9 Předání agentovi). Ruční test v reálném editoru z kroku 7 ještě neproběhl. Upraveno podle odpovědí z 14. 11. 2026 (rizika a otázky Q1–Q7). Z nahrávky `20261114-003417` (celá, 0:00–20:02, Process z editoru, master instrukce „Goal: an implementation plan“ – nic se zatím neimplementuje).

## 1. Shrnutí cíle

Stopa **narration** v editoru přestane být jen ke čtení:

1. **Dvě stopy titulků pod sebou** (horní a spodní), každá vysoká jako dnešní narration. Dnešní titulky leží v horní stopě, spodní je zpočátku prázdná.
   **Účel spodní stopy:** hlavně odkládací místo pro **prohazování titulků**. Uživatel si titulek odloží dolů, uvolní místo nahoře, přeskládá ostatní a titulek vrátí, aniž by narážel na zakázané překryvy (červené náhledy). Vedlejší použití jsou dva titulky platné ve stejnou chvíli. Ve většině nahrávek zůstane spodní stopa prázdná.
2. **Zámek** zůstává, výchozí stav je zamčeno. Přesune se z prvního řádku hlavičky (vedle „narration“) do jejího spodního řádku, vlevo pod nápis. Kliknutím na něj se stopa odemkne a zamkne. **Odemknout jde až po dokončení přepisu.**
3. **Odemčená stopa:** titulky jde myší roztahovat, zkracovat, posouvat v čase a přesouvat mezi horní a spodní stopou.
4. **Zóny titulku** (podle šířky obdélníčku v pixelech při aktuálním zoomu). **Zóny se nekreslí, poznají se jen podle kurzoru.**
   - **žlutá** – 16 px u každého okraje: táhnutí mění začátek nebo konec titulku (doprava za pravý okraj = prodloužit, doleva = zkrátit; u levého okraje obráceně);
   - **zelená** – zbytek uprostřed (min. 16 px): táhnutí posouvá celý titulek doleva, doprava, nahoru a dolů (do druhé stopy);
   - **oranžová** – jen tam, kde se dva titulky ve stejné stopě dotýkají (konec A = začátek B). Vezme si 6 px z každé žluté zóny u společné hrany. Táhnutí posune společnou hranu: jeden titulek se zkrátí a druhý prodlouží, jako „roll edit“ v DaVinci Resolve. Když jeden z titulků žlutou zónu nemá, má oranžová zóna jen 6 px, a to ve žluté zóně toho druhého.
   - Šířka **≥ 48 px**: všechny zóny. **16–48 px**: celý titulek je zelený, jde jen posouvat. **≤ 16 px**: titulek sám nemá žádnou zónu. Když se ho ale dotýká soused, který má na té straně žlutou zónu (je ≥ 48 px), vznikne v sousedovi oranžová zóna a přes ni jde i malý titulek roztáhnout nebo zkrátit. Jinak si ho uživatel musí přiblížit zoomem.
5. **Pravidla:**
   - minimální délka titulku **0,5 s**, platí i pro roll;
   - čas se zaokrouhluje na **10 ms** (setiny sekundy);
   - ve stejné stopě se titulky **nesmí překrývat**, ale smí se dotýkat. Mezi horní a spodní stopou se překrývat smí;
   - **magnet:** okraj titulku se přichytí k okrajům sousedních titulků v cílové stopě, ke **keypointům**, k **pauzám** a k **playheadu**.
6. **Zobrazení během tažení:** původní obdélníček zůstane na místě a nad ním se kreslí **náhled** nové polohy a délky: zelený obdélník s 80 % neprůhlednosti a lehce zaoblenými rohy, kterým prosvítá pozadí. Když by se titulek překryl s jiným ve stejné stopě, náhled **zčervená** a puštění tlačítka nic nezmění. **Escape** zruší jen právě prováděnou úpravu. Puštění levého tlačítka změnu provede a uloží.
7. **Předání agentovi:** agent dostane titulky **každé stopy zvlášť**, každý titulek jako jeden řádek s celým textem. Titulky se pro agenta nespojují. Když je spodní stopa prázdná, výstup je stejný jako dnes a o spodní stopě se nic nepíše. Řádky z obou stop, které se časově překrývají, nesou vzájemný odkaz. Titulky, které se jen dotýkají, spolu nesouvisí. Přesný tvar je v kroku 6.
8. **Náhled pod obrazem, export SRT a render MP4** naopak text spojují: horní titulek, mezera a spodní titulek. Když je titulek jen v jedné stopě, zobrazí se jen ten.
9. **Edit** otevře okno s jedním nebo dvěma víceřádkovými poli, podle toho, kolik titulků je pod playheadem (horní, spodní).
10. **Vzhled obdélníčků:** dnes mají ostré rohy a nestejný okraj (dole a vlevo 2 px, nahoře 1 px, viz snímek v 10:16). Nově budou mít lehce zaoblené rohy a stejný okraj 2 px dokola.

## 2. Kroky

### Krok 1 – Datový model: titulek patří do stopy
[core/transcript.py](src/video_story/core/transcript.py)
- `Segment` dostane pole `track: int = 0` (0 = horní, 1 = spodní). `to_dict` ho zapíše jen pro `track != 0` a `from_dict` ho čte s výchozí hodnotou 0. Staré `transcript.json` tak zůstanou platné a soubory bez spodní stopy se nezmění ani o bajt.
- Nová čistá funkce `display_cues(segments) -> list[Segment]` pro náhled, SRT a render. Časovou osu rozdělí v každém začátku a konci titulku a každý úsek dostane text „horní + mezera + spodní“ (nebo jen ten, který tam je). Sousední úseky se stejným textem se spojí, takže pro samotnou horní stopu je výsledek totožný s dneškem.
- `transcript_to_text` (`transcript.txt`, agent ho dostává jako `transcript_txt`) píše titulky spodní stopy se značkou `[lower track]`. Bez spodní stopy je soubor beze změny.
- `merge_segments` (průběžný přepis) se nemění: přepis běží jen, když je stopa zamčená, a nový přepis plní jen horní stopu.
- **Ověření:** unit testy na round-trip JSON se stopou i bez ní, na `display_cues` (bez překryvu, částečný překryv, spodní titulek uvnitř horního, jen spodní, dotyk bez překryvu) a na to, že starý `transcript.json` přečtený a znovu zapsaný se nezmění.

### Krok 2 – Čistá logika úprav (nový modul, bez Qt)
nový [editor/timeline/narration_edit.py](src/video_story/editor/timeline/narration_edit.py)
- Konstanty: `EDGE_PX = 16`, `ROLL_PX = 6`, `MIN_INTERACTIVE_PX = 16`, `FULL_ZONES_PX = 48`, `MIN_DURATION_MS = 500`, `STEP_MS = 10`, `SNAP_PX` (stejný jako u In/Out).
- `zone_at(box_px, x, neighbours) -> Zone` vrátí `NONE / MOVE / RESIZE_START / RESIZE_END / ROLL`. Oranžová zóna leží jen ve žluté zóně souseda, který ji má, a ovládá i titulek ≤ 16 px.
- `DragState` (výchozí titulek, zóna, výchozí x a stopa) a `propose(drag, x, lane, ms_per_px, segments, magnets) -> Proposal(segments_after, valid, reason)`:
  - zaokrouhlení na 10 ms;
  - magnet v pixelech: okraje sousedních titulků v **cílové** stopě, keypointy, pauzy, playhead. U posunu se přichytává bližší z obou okrajů;
  - posun zachovává délku, změna okraje drží min. 0,5 s, roll drží min. 0,5 s u obou titulků;
  - neplatné: překryv ve stejné stopě, začátek < 0, konec > délka nahrávky. U titulku, který byl kratší než 0,5 s už z přepisu, je posun dovolený, ale zkracování ne.
- **Ověření:** tabulkové unit testy na každou zónu, každou hranici šířky (15/16/17, 47/48 px), roll titulku ≤ 16 px přes oranžovou zónu souseda, magnet na každý cíl, zakázaný překryv, minimum 0,5 s, krok 10 ms, přesun do druhé stopy s překryvem i bez něj a případ 18 px + 50 px z nahrávky (17:20–19:20).

### Krok 3 – Nahrávka v paměti: stopy a uložení úprav
[editor/recording.py](src/video_story/editor/recording.py)
- Seznamy titulků podle stop. `segments_between(start, end, track)` pro kreslení.
- `segment_at(t)` dnes předpokládá titulky bez překryvu. Nahradí ho `segments_at(t) -> dict[track, Segment]` a `subtitle_at(t)`, které vrátí spojení podle `display_cues`.
- `apply_segment_edit(old: list[Segment], new: list[Segment])` nahradí jeden titulek (posun, okraj) nebo dva (roll, nebo oba texty z Edit) najednou. Před zápisem znovu ověří pravidla z kroku 2 a zapíše `transcript.json` + `.txt` jako dnešní `replace_segment` (soubory napřed, s retry podle [core/fileio.py](src/video_story/core/fileio.py)).
- **Ověření:** unit testy na zápis, odmítnutí neplatné změny a `segments_at` při překryvu stop.

### Krok 4 – Časová osa: dvě stopy, zámek, zóny, tažení, náhled
[editor/timeline/view.py](src/video_story/editor/timeline/view.py)
- **Rozložení:** `narration_rect` bude mít výšku `2 * NARRATION_LANE_H + 1` (dnešní `NARRATION_H` na stopu) a k ní `narration_lane_rect(track)`. Upravit `sizeHint`, `setMinimumHeight`, `tracks_bottom` a pruh přepisu pod tím.
- **Hlavička:** „narration“ v horním řádku, zámek vlevo ve spodním řádku (podle šipky v nahrávce 0:56). Zamčený zámek je dnešní ikona, odemčený má zvednutý třmen. Tooltipy:
  - „Locked: click to edit the subtitles“;
  - „Unlocked: drag the subtitles; click to lock“;
  - během přepisu je zámek zašedlý: „Unlocks once the transcription is complete“.

  Nový stav `narration_unlocked` (výchozí `False`, vrací se při každém `set_recording`) a signál `narration_lock_toggled`.
- **Kreslení obdélníčků:** antialiasing, `drawRoundedRect` s poloměrem cca 3 px, pero 2 px, obdélník zarovnaný tak, aby okraj byl 2 px nahoře, dole i po stranách. Mezera 1 px mezi dotýkajícími se titulky zůstane.
- **Kurzor podle zóny** (jen odemčeno; zóny se nekreslí): `SizeHorCursor` pro žlutou, `SizeAllCursor` pro zelenou, `SplitHCursor` pro oranžovou a normální šipka bez zóny.
- **Myš:** odemčeno a stisk v zóně → `self._drag = "segment"` s `DragState`. Na prázdném místě stopy a na titulku bez zóny zůstane posun playheadu jako dnes. Úchyty In/Out mají přednost jen ve svých pár pixelech. Zamčeno = beze změny.
- **Tažení:** `mouseMoveEvent` počítá `propose(...)`, cílovou stopu bere podle svislé polohy myši nad narration a ukládá náhled. Při `paint` se náhled kreslí přes stopu: zelená `#00C000` s alfa 204 (80 %), zaoblené rohy, při neplatné změně červená.
- **Escape:** okno má `Escape` jako zkratku Stop (`stop_action` v [window.py](src/video_story/editor/window.py)). Během tažení Escape **zruší jen právě prováděnou úpravu** (náhled zmizí, titulek zůstane, jak byl) a přehrávání nezastaví. View má `cancel_segment_drag() -> bool` a `stop_playback` ho volá jako první; když vrátí `True`, dál nic nedělá. Tažení se zruší i při ztrátě fokusu nebo přepnutí nahrávky.
- **Puštění:** při platné změně emituje `segment_edit_requested(old, new)`, jinak se nestane nic.
- **Ověření:** testy view přes `QTest` (offscreen, jako [test_editor_subtitles.py](tests/unit/test_editor_subtitles.py)): hit-test zón a kurzorů, zámek a jeho zašednutí během přepisu, tažení s náhledem, Escape (tažení zrušeno, přehrávání běží dál), červený náhled při překryvu, výška stop.

### Krok 5 – Okno editoru
[editor/window.py](src/video_story/editor/window.py), [editor/preview.py](src/video_story/editor/preview.py), [editor/subtitle_dialog.py](src/video_story/editor/subtitle_dialog.py)
- Napojit `segment_edit_requested` na `recording.apply_segment_edit`, pak `_sync_playhead`, obnovit zvýraznění hledání a stavovou hlášku („Subtitle moved: 00:01:02.30–00:01:05.10“). Chybu zápisu ohlásit jako u Edit.
- **Zámek:** odemknout jde jen u úplného přepisu (`transcript_complete`). Během přepisu na pozadí a u archivované nahrávky je zámek zašedlý. Spuštění „Transcribe again“ stopu zase zamkne.
- **Transcribe again:** smaže všechny titulky obou stop a přepíše vše znovu jen do horní stopy, spodní zůstane prázdná. Text potvrzovacího okna se doplní: „úpravy časů titulků a spodní stopa se ztratí“.
- **Náhled pod obrazem:** `subtitle_at` vrátí „horní + mezera + spodní“, nebo jen ten titulek, který tam je.
- **Edit:** zůstává jedno tlačítko. `SubtitleDialog` dostane jeden nebo dva titulky:
  - **horní i spodní pod playheadem:** dvě víceřádková pole pod sebou, nadepsaná „Upper track“ a „Lower track“, každé se svými časy From/To (jen ke čtení jako dnes);
  - **jen horní:** jedno pole, nadepsané „Upper track“;
  - **jen spodní:** jedno pole, nadepsané „Lower track“.

  Save uloží změněné texty najednou (`apply_segment_edit`). Bez titulku pod playheadem zůstane dnešní hláška „No subtitle at the playhead.“
- **Ověření:** testy okna: uložení po tažení, Edit ve všech třech situacích (včetně uložení obou textů), náhled se spojeným textem, zámek během přepisu a po „Transcribe again“.

### Krok 6 – Výstupy pro agenta a export
- [host/events.py](src/video_story/host/events.py) `_speech_events`, **doporučený tvar** (viz kap. 5):
  - **Spodní stopa v rozsahu prázdná:** řádky `SAY` přesně jako dnes, bez zmínky o stopách (ani v `legend`, ani v `counts`).
  - **Spodní stopa něco obsahuje:** každý titulek je dál jeden řádek s celým textem, seřazený podle času. Titulky spodní stopy mají za `SAY` značku `[lower track]`. Řádek, který se časově překrývá s řádkem druhé stopy, dostane odkaz na jeho čas:

    ```text
    0-2000 SAY Horní text. (at the same time as lower track 1000-3000) [cursor ...]
    1000-3000 SAY [lower track] Spodní text. (at the same time as 0-2000) [cursor ...]
    ```

    K tomu věta v `legend`: „The user has a second (lower) subtitle track, mostly used to rearrange subtitles. Lines of the two tracks that overlap in time both apply at that moment: read the upper one first. Lines that only touch are unrelated.“ A `counts.speech_lower`.
- [host/cli.py](src/video_story/host/cli.py) `describe` (`transcript_in_range`) a [mcp_server.py](src/video_story/host/mcp_server.py): stejný tvar se značkou `[lower track]`, jen když spodní stopa něco obsahuje.
- [export/srt.py](src/video_story/export/srt.py) a render MP4: titulky z `display_cues`, tedy spojený text „horní + mezera + spodní“, stejně jako náhled v editoru.
- Vyhledávání ([editor/search.py](src/video_story/editor/search.py)) hledá dál v jednotlivých titulcích obou stop.
- **Ověření:** [test_events.py](tests/unit/test_events.py), [test_export.py](tests/unit/test_export.py), [test_agent_output.py](tests/unit/test_agent_output.py) s nahrávkou se dvěma stopami. Výstup nahrávky s jednou stopou musí být beze změny (snapshot).

### Krok 7 – Dokumentace
- [video-story-spec.md](video-story-spec.md) kap. 8 (časová osa: dvě stopy, zámek, zóny, pravidla, magnet, náhled, Escape, Edit se dvěma poli, Transcribe again) a kap. 9 (spodní stopa pro agenta).
- `SKILL.md` (řádek `SAY [lower track]`, odkaz „at the same time as“, účel spodní stopy), docstring modulu `view.py`, tooltip stopy.
- **Ověření:** spuštění celé sady `pytest` a ruční test v reálném editoru:
  - zámek je zašedlý během přepisu, po přepisu jde odemknout;
  - roztáhnout, zkrátit pod 0,5 s (nejde);
  - magnet na souseda, keypoint, pauzu a playhead;
  - překryv (červeně), Escape uprostřed tažení;
  - roll mezi dotýkajícími se titulky i u titulku ≤ 16 px;
  - prohození dvou titulků přes spodní stopu;
  - Edit se dvěma poli;
  - Process a kontrola řádků `SAY` v `timeline`.

## 3. Rizika (rozhodnuto 14. 11. 2026)

- **Transcribe again:** smaže všechny titulky obou stop včetně ručních přesunů a přepíše vše znovu jen do horní stopy, spodní zůstane prázdná. *Tak je to správně.* Potvrzovací okno na to upozorní.
- **Escape = Stop:** při tažení nebo úpravě titulku Escape zruší jen tuto rozpracovanou úpravu. Mimo tažení zůstává zkratkou pro Stop.
- **Odemknutí až po přepisu:** brání souběhu ručních úprav s přepisem na pozadí.
- **`transcript.txt` se mění** jen u nahrávek se spodní stopou (značka `[lower track]`). Nahrávky bez ní mají soubor beze změny.
- **Přesnost myši:** při malém zoomu je 10 ms méně než pixel. Dotyk dvou titulků se proto pozná podle stejného času v ms, ne podle pixelů, jinak by se oranžová zóna neobjevila.
- **Výkon:** hit-test zón při každém pohybu myši prochází jen viditelné titulky (`segments_between`), stejně jako dnešní tooltip.

## 4. Předpoklady

- Stav zámku se nepamatuje: každá nahrávka se otevře zamčená (podobně jako Auto process).
- Úpravy se ukládají hned po puštění tlačítka. Undo (`Ctrl+Z`) v editoru zatím není a nepřidává se.
- Zóny se počítají v pixelech obrazovky při aktuálním zoomu, ne v čase.
- Zelená zóna posouvá titulek i svisle: stopu určuje, nad kterou z obou stop je kurzor.
- Kliknutí bez pohybu myši na titulek v odemčené stopě nic nezmění (playhead se neposune). Playhead se posouvá klikem mimo zóny nebo na jiných stopách.

## 5. Rozhodnutí k otázkám Q1–Q7 (14. 11. 2026)

- **Q1 – účel dvou stop:** hlavně odkládací místo pro prohazování titulků bez kolizí, vedlejší použití jsou dva současné titulky (kap. 1 bod 1).
- **Q2 – tvar pro agenta:** stopy se pro agenta nespojují a dotyk nic neznamená. Prázdná spodní stopa = výstup jako dnes. Z variant „každý titulek jeden řádek se značkou stopy a odkazem na překryv“ a „rozdělení na časové úseky (jen horní / obě / jen spodní)“ doporučuji tu první (krok 6), protože:
  - text titulku nejde rozdělit podle času, takže úseky by opakovaly celý text 2–3× a agent by to mohl číst jako opakovanou řeč;
  - počet a časy řádků `SAY` zůstanou stejné jako segmenty přepisu, kurzor a `over` na začátku a konci řádku dál sedí;
  - kdy platí oba titulky, říká odkaz „at the same time as“ s přesnými časy, takže agent překryv nemusí sám počítat.

  *Potvrzeno 14. 11. 2026.*
- **Q3 – titulek ≤ 16 px:** sám nemá zónu. Jde ho roztáhnout nebo zkrátit jen přes oranžovou zónu v sousedovi ≥ 48 px, který se ho dotýká.
- **Q4 – viditelnost zón:** zóny se nekreslí, poznají se podle kurzoru.
- **Q5 – magnet:** sousední titulky, keypointy, pauzy, playhead.
- **Q6 – náhled, SRT, render:** spojení „horní + mezera + spodní“, nebo jen titulek, který tam je.
- **Q7 – Edit:** jedno tlačítko. Okno má dvě víceřádková pole (Upper track, Lower track), když jsou pod playheadem oba titulky, jinak jedno pole nadepsané podle stopy.
