# Implementační plán: složky nahrávek (akordeony v editoru)

Zdroj: nahrávka `20261112-143853` (10:50, česky, celý rozsah, instrukce „implementation plan,
nic neimplementuj") a odpovědi na otevřené otázky v chatu (2026-11-12, tři kola).
Stav: **verze 4, schváleno a implementováno 2026-11-12** (migrace: 29 nahrávek v `default`).

## Cíl

Nahrávky v `~/Videos/VideoStory` už nebudou ležet naplocho, ale ve **složkách**
(`<recording_folder>/<složka>/<session_id>/`). Složka **`default`** (malé d) existuje vždy
(když chybí, vytvoří se) a nejde smazat.

Dvě nezávislé vlastnosti složek:

- **Hlavní složka** (main folder) – právě jedna; ukládají se do ní nové nahrávky; může být
  kdekoli v pořadí; v záhlaví má **tučný název**. Nastavuje se položkou menu
  **Set as Main Folder**. Na začátku je hlavní `default`.
- **Připnutá složka** (pinned) – nejvýš jedna, může být i žádná; je vždy úplně nahoře.
  Připíná se **tlačítkem se špendlíkem** v záhlaví (s potvrzením), dalším klikem se
  odepne. Špendlík je vidět u všech složek: prázdný = nepřipnutá, plný = připnutá.
  Na začátku je připnutá `default`.

Panel **Recordings** v editoru ukáže každou složku jako akordeon se záhlavím
`▾ název (N)   [📌] [⋯]` a nahrávkami pod ním. Uživatel může:

- vytvořit složku tlačítkem se složkou a plusem vpravo vedle nadpisu „Recordings“
  (dialog s předvyplněným názvem a zaškrtnutým „Move selected video to the new folder“);
  nová složka se zařadí **na konec**,
- tlačítkem se špendlíkem připnout složku nahoru,
- v menu `⋯`: **Set as Main Folder**, **Move Up**, **Move Down**, **Archive All**,
  **Delete All Videos (N)**, **Delete Folder** (bez Move to Top/Bottom, bez Rename),
- v kontextovém menu nahrávky: pod „Open Folder Location“ nová položka
  „Move Video to Folder…“ / „Move Videos to Folder… (N)“ s dialogem a comboboxem složek.

Sbalené složky si editor pamatuje mezi spuštěními (výchozí stav: vše rozbalené).
Hromadný výběr (Shift/Ctrl) a hromadné akce fungují jen v rámci jedné složky; přepínání
obrazovek kolečky ●○ v řádku funguje dál ve všech složkách.

Tlačítko **Delete All Recordings** v Settings okna nahrávání **zmizí**.

Jednorázovou migraci stávajících 29 nahrávek do `default` udělám **já ručně**, ne skill.

## Rozhodnutí z odpovědí

| # | Otázka | Rozhodnutí |
|---|--------|------------|
| 1 | Jméno výchozí složky | `default` (malé d), jak už je na disku |
| 2 | Smazání `default` | nejde (Delete Folder je u ní vypnuté) |
| 3 | Kam nové nahrávky | do hlavní složky (Set as Main Folder); hlavní může být kdekoli |
| 4 | Move to Bottom | ne |
| 5 | Pamatovat sbalené složky | ano |
| 6 | Počet videí v záhlaví | ano, `název (N)` |
| 7 | Mazání v menu složky | **Delete All Videos (N)** jen videa, **Delete Folder** videa i složku; byla-li hlavní, hlavní se stane `default` |
| A | Pořadí nahoře | žádné Move to Top; tlačítko se špendlíkem, max. jedna připnutá složka, výchozí `default` |
| B | Připnutí jiné složky | nová připnutá jde úplně nahoru, dosavadní připnutá o jedno níž (na 2. místo) |
| C | Nová složka | vždy na konec |
| D | Delete All v Settings | tlačítko zrušit, mazání ze Settings nebude |
| E | Odlišení hlavní složky | jen tučný název, bez štítku |
| F | Nahrávka ve sbalené složce | složku rozbalit (a uložit jako rozbalenou) |
| G | Klik na špendlík připnuté složky | odepne ji; pořadí se nemění, žádná složka není připnutá |
| H | Vzhled špendlíku | vidět u všech složek; prázdný, po připnutí plný |
| – | Přesun nahrávky mezi Stop a Process | povolený (viz Rizika – omezení „agent čeká“ odpadá) |

## Kroky

### 1. Datová vrstva – kde nahrávka leží (`core/session.py`)

- `SessionPaths` dostane volitelné pole `folder: str | None`. `final_dir` =
  `recording_folder / folder / session_id`; když `folder` není dané, dohledá se
  (projde podsložky kořene, jejich počet je malý). Díky tomu ~15 míst, která dnes
  skládají `SessionPaths(recording_folder, id)` (CLI, controller, window, MCP), funguje
  beze změny.
- Nenalezená nahrávka (ještě nefinalizovaná) patří do **hlavní složky**. `finalize()`
  si cílovou složku zjistí **jednou** na začátku přesunu a celou nahrávku přesune do
  ní (vysvětlení v Rizicích). Když hlavní složka mezitím zmizela, použije `default`.
- `list_sessions()` prochází `.work/` a každou složku (ne kořen); vrací cesty s `folder`.
- `new_session_id()` hlídá unikátnost přes všechny složky.
- Ověření: unit testy dohledání `final_dir`, `list_sessions` se složkami, finalizace
  do hlavní složky i do `default` po smazání hlavní, unikátnost id.

### 2. Složky, pořadí, hlavní, připnutá, sbalení (`core/folders.py`)

- `DEFAULT_FOLDER = "default"`, `ensure_default()`, `list_folders()` v uloženém pořadí,
  `main_folder()` / `set_main(name)`, `pinned_folder()` / `pin(name)`,
  `move_up/move_down(name)`, `create_folder(name)`, `delete_folder(name)`,
  `set_collapsed(name, bool)`, `validate_name()`,
  `default_folder_name(now)` → `New folder 20261112-151530` (formát `YYYYMMDD-HHMMSS`).
- Stav v `<recording_folder>/folders.json`:
  `{"order": ["default", ...], "main": "default", "pinned": "default", "collapsed": []}`
  (`"pinned": null` = žádná připnutá).
  Zápis přes `core/fileio` (atomicky, s retry), protože ho čte i okno nahrávání a CLI
  v jiných procesech.
- Pravidla:
  - připnutá složka (pokud nějaká je) je vždy `order[0]`;
  - **pin(X)**: X jde na `order[0]`, dosavadní připnutá (pokud byla) na `order[1]`,
    ostatní drží svoje pořadí;
  - **unpin()**: `pinned = null`, pořadí se nemění (bývalá připnutá zůstane první,
    ale dá se už posouvat);
  - **Move Up / Move Down** nemohou připnutou složkou pohnout a nic nemůže předběhnout
    připnutou (Move Up na 2. místě je vypnuté); bez připnuté se posouvá volně;
  - **nová složka** na konec `order`;
  - **set_main(X)** mění jen `main`, pořadí ne;
  - **smazání hlavní** → `main = "default"`; **smazání připnuté** → `pinned = null`
    (žádná připnutá, stejně jako po odepnutí).
- Načtení: složka na disku, která v `order` chybí, se přidá na konec podle abecedy;
  položka bez složky na disku se zahodí; `main`/`pinned` mířící na neexistující složku
  → `default`; poškozený soubor = výchozí stav a varování v logu.
- Validace názvu: neprázdný po oříznutí, bez `<>:"/\|?*`, bez tečky/mezery na konci,
  ne rezervovaná jména Windows (`CON`, `NUL`, …), ne `.work` ani nic od tečky, ne tvar
  session id, unikátní **bez ohledu na velikost písmen** (tedy ani `Default`), max. ~100 znaků.
- Ověření: unit testy všech pravidel výše (pin s přesunem předchozí na 2. místo, pin
  bez předchozí, unpin beze změny pořadí, Move Up/Down s připnutou i bez ní, nová na
  konec, hlavní kdekoli, smazání hlavní/připnuté),
  sbalení přežije restart, poškozený / chybějící `folders.json`.

### 3. Přesun nahrávek (`core/folders.py`)

- `move_recordings(items, target)`: `os.replace` adresáře (stejný disk) s retry
  z `core/fileio`; odmítne jen nahrávku, která se archivuje nebo je ve frontě
  archivace (archivační proces v ní zapisuje a maže). Když existuje
  `.work/<id>/state.json`, přepíše v něm cesty (`recording_dir`, `manifest`,
  `transcript_*`, `frames_*`), aby `wait`/`status` ukazovaly novou cestu.
- Nahrávku mezi Stop a Process (status `editing`) přesunout **jde**: Process skládá
  cesty pro agenta (`finish_recording_session` → `paths.final_dir`) až v okamžiku
  stisku, takže agent dostane novou cestu.
- Ověření: unit testy přesunu, odmítnutí archivované, přepisu `state.json` a přesunu
  nahrávky ve stavu `editing` a následného Process (výsledek míří do nové složky).

### 4. Host vrstva a Settings

- `controller` / `recovery` / `cli record`: finalizace do hlavní složky (krok 1);
  obnovená nahrávka po pádu jde do hlavní složky v okamžiku obnovy.
- `retention` (archivace po `archive_after_days`), `editor_launcher`, `runner`,
  `cli sessions` jedou přes `list_sessions` → pokryjí všechny složky. `sessions`
  dostane v JSON pole `folder`.
- `ensure_default()` při startu editoru, okna i při finalizaci.
- **Settings**: odstranit tlačítko „Delete All Recordings...“ a jeho obsluhu
  (`ui/settings_dialog.py`), funkce `delete_all_recordings` a `summarize_recordings`
  (`host/retention.py`) a jejich testy (`tests/unit/test_retention.py`). Open Folder
  zůstává.
- Ověření: stávající unit/integration testy (`test_recording`, `test_editor_flow`,
  `test_archive`, retention, settings) po úpravě fixtur, které dnes zakládají
  nahrávky v kořeni.

### 5. Model seznamu (`editor/recordings_model.py`)

- Plochý model se dvěma druhy řádků: `FolderHeader(name, count, main, pinned,
  expanded)` a `RecordingItem` (dostane `folder`). Pořadí: složky podle
  `folders.json`, v každé nahrávky od nejnovější (jako dnes).
- Záhlaví nejsou vybíratelná (`flags` bez `ItemIsSelectable`); `item(row)`, `row_of()`,
  `default_row()` vrací jen nahrávky; nový `folder_of(row)` a `rows_of_folder(name)`.
- Sbalení = `setRowHidden` na řádcích složky, stav z `folders.json`.
- Volba flat model + záhlaví místo `QTreeView` drží beze změny delegát, výběr i
  stávající testy řádků; jen se přidá kreslení záhlaví.

### 6. Zobrazení (`editor/recordings_view.py`, `editor/window.py`)

- Řádek záhlaví: šipka ▾/▸, `název (N)` – název se elidovat `…`, když by se dotkl
  tlačítek, počet zůstane vidět vždy – vpravo tlačítko se **špendlíkem** a tlačítko
  `⋯`. Hlavní složka má **tučný** název. Špendlík je vidět u všech složek: u
  nepřipnutých **prázdný** (obrys), u připnuté **plný** (kreslený pro světlé i tmavé
  téma).
- Klik na záhlaví sbalí/rozbalí a uloží stav; klik na `⋯` nebo pravé tlačítko na
  záhlaví otevře menu složky; klik na špendlík připne s potvrzením nebo odepne
  (krok 8).
- Výběr jen v jedné složce: `selectionCommand`/`mousePressEvent` ve `RecordingsListView`
  – Shift/Ctrl na řádek jiné složky výběr nerozšíří, ale začne nový v té složce;
  `moveCursor` přeskakuje záhlaví a skryté řádky.
- Nadpis „Recordings“ dostane vpravo `QToolButton` s ikonou složky s plusem
  (kreslenou jako ostatní ikony editoru, pro světlé i tmavé téma), tooltip
  „Create new folder“.
- Otevře-li se nahrávka ve sbalené složce (nová nahrávka po Stop, požadavek agenta,
  výsledek hledání, přesun), složka se **rozbalí a uloží jako rozbalená**.
- Ověření: offscreen `window.grab()` screenshot (světle i tmavě), unit testy výběru
  (Shift přes hranici složky), skrytí řádků, elidování s počtem, tučné písmo hlavní.

### 7. Dialog „Create new folder“ (`editor/folder_dialogs.py`)

- Modální, uprostřed okna: pole s názvem `New folder YYYYMMDD-HHMMSS` (označený celý,
  ať jde rovnou přepsat), checkbox „Move selected video to the new folder“ / „Move
  selected videos (N) to the new folder“ **zaškrtnutý**, tlačítka Save / Cancel;
  chybný název = hláška pod polem a vypnuté Save.
- Save: vytvoří složku **na konci** (krok 2), případně do ní přesune vybrané nahrávky
  (krok 3), obnoví seznam, odroluje na novou složku a vybere přesunutou nahrávku.
- Checkbox je vypnutý, když nic přesunout nejde (nic vybráno nebo se vybraná
  nahrávka archivuje).

### 8. Tlačítko se špendlíkem a menu složky `⋯`

- **Špendlík** (nepřipnutá složka) → potvrzení „Pin the folder <název> to the top,
  above all other folders?“ Yes / No; Yes = `pin()` (předchozí připnutá jde na 2.
  místo), špendlík se změní na plný, seznam se překreslí. Tooltip „Pin to top“.
- **Špendlík** (připnutá složka) → potvrzení „Unpin the folder <název>? No folder
  will stay pinned to the top.“ Yes / No; Yes = `unpin()`, špendlík se změní na
  prázdný, pořadí zůstane. Tooltip „Unpin“.
- Menu `⋯`:
  - **Set as Main Folder** – vypnuté u hlavní; tooltip „New recordings are saved to
    the main folder“.
  - **Move Up**, **Move Down** – vypnuté u připnuté složky, Move Up i u složky hned
    pod připnutou (nebo u první, když není nic připnuté), Move Down u poslední.
  - **Archive All** → stávající `archive_recordings()` s nahrávkami složky (stejné
    potvrzení, archivovatelné filtruje `archivable()`); vypnuté, když není co archivovat.
  - **Delete All Videos (N)** → stávající `delete_recordings()` (stejné potvrzení jako
    hromadné Delete Videos); složka zůstane prázdná. Vypnuté při N = 0.
  - **Delete Folder** → potvrzení „Delete the folder <název> and all N videos in it?“;
    smaže nahrávky (stejné kontroly jako Delete), pak složku; záhlaví zmizí. Byla-li
    hlavní, hlavní se stane `default`. Když se některou nahrávku smazat nepodaří,
    složka zůstane a hlášení řekne proč. U `default` vypnuté s tooltipem „The default
    folder cannot be deleted“.
- Ověření: unit testy (povolené/zakázané položky, pin s potvrzením Yes/No, unpin, pořadí po
  přesunech, smazání hlavní / připnuté, mazání s odmítnutou nahrávkou); testy
  přepisují potvrzovací dialogy jako dnes.

### 9. „Move Video to Folder…“ v kontextovém menu nahrávky

- Hned pod „Open Folder Location“: `Move Video to Folder…`, při výběru více
  `Move Videos to Folder… (N)`.
- Dialog: combobox složek kromě aktuální (v pořadí jako v seznamu), OK / Cancel.
  Nejsou-li jiné složky, položka je vypnutá s tooltipem „Create a folder first“.
- Přesun zobrazené nahrávky: uvolnit ji jako před archivací (`_unload_for_archive`),
  zastavit běžící přepis této nahrávky, přesunout, znovu otevřít z nové cesty a přepis
  znovu spustit, když nebyl hotový. Cílová složka se rozbalí (krok 6).
- Ověření: unit test přesunu zobrazené i nezobrazené nahrávky, nahrávky ve stavu
  `editing`, odmítnutí archivované / ve frontě; ruční test na Windows (zámky souborů).

### 10. Dokumentace

- `video-story-spec.md` (živá specifikace: složky, hlavní/připnutá, řádek Settings bez
  Delete All), docstring `core/session.py`, `SKILL.md` obou skillů (cesta
  `<recording_folder>/<složka>/<session_id>`, nové nahrávky v hlavní složce, věta
  o mazání ze Settings pryč), README (odstavec o Settings a složce nahrávek).

### 11. Jednorázová migrace (udělám já, až bude kód hotový)

1. Zavřít editor a okno Video Story (drží soubory otevřené).
2. Složka `default` už na disku je (uživatel ji založil v 2:39) – nic se nepřejmenovává.
3. Přesunout všech 29 adresářů `YYYYMMDD-HHMMSS[-n]` z kořene do `default`
   (`os.rename`, stejný disk, nic se nekopíruje); `.work/` zůstává v kořeni.
4. Ověřit: v kořeni žádný session adresář, v `default` 29, `video-story sessions`
   a editor je všechny ukážou (`default (29)`, tučně, připnutá), `describe` posledních
   nahrávek vrací nové cesty.

## Rizika

- **Zámky na Windows**: přejmenování adresáře selže, dokud je v něm otevřený soubor
  (přehrávač, náhledy, hledání, přepis, archivace). Proto uvolnění a retry; co se
  nepodaří, zůstane na místě s hlášením.
- **Nahrávka mezi Stop a Process** (status `editing`): agent v tu chvíli jen čeká ve
  smyčce `wait` a nic nečte; cesty mu předá až Process. Process je skládá z aktuálního
  umístění, takže přesun před Process je bezpečný – jen se přepíše `state.json`, ať
  průběžný `wait` neukazuje starou cestu. (Ve verzi 3 byl tento přesun zbytečně
  zakázaný.)
- **Přesun nahrávky, se kterou agent právě pracuje** (po Process, uživatel znovu
  otevře editor a přesune ji): agentův příkaz rozjetý přesně během přesunu může
  selhat, další příkaz už nahrávku najde v nové složce (hledá se podle id).
  `state.json` se přepíše. Nízké riziko, nic se neblokuje.
- **Více procesů nad `folders.json`**: editor, okno nahrávání a CLI agenta jsou tři
  různé procesy. Editor soubor přepisuje (pin, hlavní, pořadí, sbalení), okno
  nahrávání ho čte při uložení nahrávky, CLI při hledání nahrávky. Na Windows selže
  přepis souboru (WinError 5), když ho v tu chvíli jiný proces čte, a čtení
  rozepsaného souboru by vrátilo nesmysl. Řešení: atomický zápis + čtení s opakováním
  (`core/fileio`, stejně jako u `state.json`).
- **Hlavní složka se změní nebo zmizí během nahrávání**: nahrávka jde do hlavní
  složky platné v okamžiku uložení (Stop). Přesun z `.work` jde soubor po souboru,
  takže cíl se určí jednou na začátku – jinak by změna hlavní složky uprostřed
  přesunu rozdělila nahrávku do dvou složek. Smazaná hlavní složka → `default`.
- **Velikost písmen u nových složek**: `default` je vždy s malým d. Riziko se týká
  složek, které uživatel zakládá: Windows nerozliší `Projekt` a `projekt` (je to jedna
  složka), Linux ano (dvě složky). Unikátnost se proto kontroluje bez ohledu na
  velikost písmen, aby se aplikace chovala na všech systémech stejně.
- Přesun mezi složkami je vždy na stejném disku (`os.replace`), takže nehrozí
  kopírování gigabajtů. Změna Recording Folder v Settings přesune (jako dosud) jen
  otevřenou relaci; nová složka nahrávek dostane vlastní `default` a `folders.json`.
- Fixtury testů dnes zakládají nahrávky v kořeni – budou se upravovat ve více
  souborech (pomocná funkce do `conftest`).
- Výstup Render (`<recording_folder>/<id>_<screen>.mp4`) zůstane v kořeni vedle složek.

## Předpoklady

- Starý plochý layout kód číst **nebude** (stejně jako u minulé změny layoutu); staré
  nahrávky převede jednorázová migrace.
- Hromadné akce i Shift výběr jen v jedné složce; Ctrl+klik na jinou složku začne nový
  výběr tam.
- Pořadí, hlavní, připnutá a sbalení se ukládají do `folders.json` ve složce nahrávek
  (ne do QSettings), takže putují s ní.

## Otevřené otázky

Žádné. (I – odepnutí se potvrzuje modálem stejně jako připnutí; plán schválen
2026-11-12.)
