# Video Story – zadání rozšíření v2 (editor s tlačítkem Process, zoom náhledu, nové výchozí hodnoty)

Stav: **návrh zadání k potvrzení** (7. 11. 2026). Sepsáno podle slovního zadání autora; nic z toho zatím není implementováno. Navazuje na `video-story-spec.md` (nahrávání), `implementation-plan.md` a `implementation-plan-editor.md` (editor, export). Body označené **K potvrzení** jsou místa, kde zadání připouští více výkladů; u každého je uveden navrhovaný výklad, který bude použit, pokud nebude řečeno jinak.

## 1. Účel

Rozšířit Video Story tak, aby uživatel mohl po nahrání (nebo kdykoli později) nahrávku **oříznout v editoru a nechat agenta zpracovat jen vybraný úsek**. Vedle toho se mění několik výchozích hodnot a chování kreslení a náhledu.

Dotčené části: ovládací okno nahrávání (Video Story), Video Story Editor, skilly `video-story` a `video-story-edit`, CLI a MCP server.

## 2. Nové výchozí hodnoty nahrávání

| Nastavení | Dosud | Nově |
| --- | --- | --- |
| Image Format | PNG | **JPEG** |
| JPEG Quality | 85 % | **70 %** |

- Platí pro novou instalaci (výchozí `config.json`). Existující uložené nastavení uživatele se nemění.
- Rozsahy zůstávají (JPEG Quality 20–100 %), PNG zůstává volitelné.
- Všechny ostatní části (sidecar JSON, export, editor) musí s JPEG snímky fungovat beze změny (již dnes podporováno).

## 3. Kreslení na obrazovku (Draw on Screen)

1. Po otevření kreslicí vrstvy je **výchozí nástroj Obdélník** (dosud štětec). Volba nástroje se během jedné nahrávky zachovává; při dalším otevření vrstvy v téže nahrávce zůstává naposledy zvolený nástroj. **K potvrzení:** při každém novém *spuštění okna* je výchozí opět obdélník (nástroj se neukládá do konfigurace).
2. Paleta má **8 barev** místo 16, výchozí zůstává červená. **K potvrzení (návrh sady):** červená `#FF0000`, oranžová `#FF8C00`, žlutá `#FFFF00`, zelená `#00C000`, azurová `#00E5FF`, modrá `#0055FF`, purpurová `#FF00FF`, bílá `#FFFFFF`. Klávesové zkratky a chování nástrojů se nemění.
3. Barvy v sidecar JSON (`drawings[].color`) se zapisují stejně jako dosud (`#RRGGBB`).

## 4. Zoom náhledu v editoru

1. Nad náhledem (obrázek vybrané obrazovky) funguje **kolečko myši jako zoom**: otáčení nahoru přibližuje, dolů oddaluje.
2. Zoom je **ukotvený k poloze kurzoru myši**: bod obrázku pod kurzorem zůstává pod kurzorem i po změně měřítka.
3. Rozsah měřítka: od „celá obrazovka v náhledu“ (výchozí, obvykle zmenšení pod 100 %) do **maximálně 800 % originálu** (8 pixelů náhledu na 1 pixel snímku). Při oddálení pod výchozí měřítko se obrázek vrátí do stavu „celý v náhledu“ a vycentruje.
4. Krok zoomu: **K potvrzení (návrh)** násobek 1,25 na jeden dílek kolečka (plynulé, cca 10 dílků od 100 % do 800 %).
5. Zobrazení přiblíženého obrázku: viditelný výřez lze **posouvat tažením levým tlačítkem** v náhledu; **K potvrzení (návrh)** dvojklik vrací „celý v náhledu“. Měřítko a posun se zachovávají při změně snímku během přehrávání i při změně vybrané obrazovky se stejným rozlišením; při výběru jiné nahrávky nebo obrazovky s jiným rozlišením se vrací na „celý v náhledu“.
6. Kurzor a kresby se vykreslují ve správné poloze v každém měřítku. Zoom náhledu **neovlivňuje export** ani crop; jde čistě o prohlížení.
7. Titulky pod náhledem a transport zůstávají beze změny; kolečko nad časovou osou dál ovládá časovou osu (Ctrl+kolečko zoom osy).

## 5. Tlačítko Video Editor v dialogu Stop (nahrávací okno)

1. Dialog po stisku **Stop** má čtyři volby: **Process Recording**, **Continue Recording**, **Discard Recording** a nově **Video Editor**. Nahrávání běží dál, dokud uživatel nerozhodne (stávající pravidlo).
2. Volba **Video Editor**:
   1. zastaví nahrávání a provede **stejné zpracování jako Process Recording** (převod zvuku, přepis, manifest, přesun do finální složky) s dialogem průběhu,
   2. po dokončení zpracování **zavře nahrávací okno a otevře Video Story Editor** s touto (poslední) nahrávkou označenou,
   3. relace pro agenta **ještě nekončí**: `state.json` přejde do nového, nefinálního stavu `editing` (viz kap. 8), aby čekající agent dál čekal.
3. Pokud je editor již otevřený (jiná instance), nahrávací okno ho požádá o načtení této nahrávky a zvednutí okna. **K potvrzení (návrh):** běžící editor načte nahrávku a stav relace je `editing` stejně jako při novém otevření.
4. Pokud editor nelze spustit (chyba), relace skončí jako `processed` s celým rozsahem a chyba se zapíše do `message`; nahrávka se neztratí.

## 6. Tlačítko Process v editoru

1. Vedle **Render** přibude tlačítko **Process**, **zelené**, stejně velké. Render zůstává beze změny (export jedné vybrané obrazovky do MP4).
2. **Process** předá agentovi (Claude Code, Codex, …, tj. tomu, kdo relaci spustil) pokyn zpracovat nahrávku **pouze v rozsahu aktuálního cropu [In, Out]**:
   - crop platí pro **všechny monitory** (výběr stopy v editoru je pro Process nepodstatný),
   - výchozí crop je celá nahrávka, tj. Process bez úprav znamená „zpracuj celou nahrávku“.
3. Chování po stisku Process:
   1. hranice cropu se uloží k nahrávce (kap. 7),
   2. relace přejde do finálního stavu `processed` s údajem o rozsahu (kap. 8); čekající agent se probudí,
   3. editor **zůstane otevřený** (uživatel může dál renderovat), tlačítko Process se na chvíli označí jako odeslané (např. „Sent to agent“) a znovu zpřístupní. **K potvrzení:** má se editor po Process naopak zavřít? Návrh: zůstat otevřený.
4. Process funguje ve **dvou situacích**:
   - **po nahrávání** (editor otevřený volbou Video Editor ze Stop dialogu): agent čeká na téže relaci (`wait --session <id>`) a po Process dostane výsledek s rozsahem,
   - **samostatně** (editor otevřený pokynem „Edit Video Story“ bez předchozího nahrávání): uživatel vybere kteroukoli dřívější nahrávku, upraví crop a stiskne Process; agent, který editor otevřel, obdrží id nahrávky a rozsah a zpracuje ji znovu (typicky chce zopakovat nebo upřesnit dřívější akci). Pro tento případ skill `video-story-edit` po otevření editoru **čeká** na pokyn (kap. 8), dosud se jen otevřel a skončil.
5. Agent zpracuje **jen vyříznutou část**: přepis omezený na segmenty protínající [In, Out] (časy se nemění, zůstávají na časové ose nahrávky), snímky jen z tohoto rozsahu. Části mimo rozsah agent ignoruje.
6. Pokud uživatel editor zavře křížkem **bez stisku Process**, agent **nic nezpracovává** (rozhodnutí autora):
   - po nahrávání (stav `editing`): relace skončí finálním stavem `cancelled` se zprávou „editor closed without Process; recording kept“ a s `recording_dir` – nahrávka zůstává uložená ve finální složce (lze ji později otevřít přes Edit Video Story), ale čekající agent se dozví, že nemá nic dělat, a jen to oznámí jednou větou,
   - samostatně: agent dostane výsledek `closed` bez rozsahu a nic nezpracovává.

## 7. Uložení hranic cropu k nahrávce

1. Hranice cropu se ukládají **do složky nahrávky**, soubor `crop.json`: `{"start_ms": …, "end_ms": …, "updated_at": …}`.
2. Ukládají se **při každé změně** (tažení úchytů, klávesy I/O, editory In/Out, Reset Crop), ne jen při Process, aby se při dalším otevření nahrávky obnovily přesně tak, jak je uživatel naposledy nastavil.
3. Při výběru nahrávky v editoru se crop načte z `crop.json`; chybí‑li, je crop celá nahrávka. Hodnoty mimo délku nahrávky se oříznou.
4. `crop.json` nemá vliv na nahrávání ani na retenci; `Delete All` a retence ho mažou spolu s nahrávkou. Export (Render) ho dál používá jako výchozí rozsah (stávající chování: Render bere crop z editoru).

## 8. Rozhraní pro agenta (CLI, MCP, state.json, skilly)

### 8.1 Stavy relace nahrávání

- Nový nefinální stav **`editing`**: zpracování dokončeno, nahrávka je ve finální složce, editor je otevřený a čeká se na Process. `wait` ho vrací jako nefinální (agent volá `wait` znovu). Zavření editoru bez Process ukončí relaci stavem `cancelled` (s `recording_dir`, nahrávka zůstává), agent nic nezpracovává.
- Stav `processed` dostává nová pole: `crop: {"start_ms": …, "end_ms": …}` (vždy; bez editoru = celý rozsah) a `processed_via: "stop_dialog" | "editor"`.

### 8.2 Samostatně spuštěný editor

- `video-story edit --detach` vrací kromě `pid` i **`editor_session`** (identifikátor čekání, např. soubor `.work/editor-<token>.json`).
- Nový příkaz **`video-story wait --editor <editor_session> --timeout 240`**: vrací `timed_out`, nebo `{"event": "process", "session_id": …, "crop": {…}}` po stisku Process, nebo `{"event": "closed"}` po zavření editoru. Stejné chování má MCP nástroj `video_story_wait_editor` (nebo rozšíření `video_story_wait`).
- Editor po stisku Process zapíše událost do souboru editor-session; po zavření zapíše `closed`.

### 8.3 Popis nahrávky s rozsahem

- `describe --session <id>` a `video_story_describe` nově vrací `crop` (z `crop.json`, jinak celý rozsah) a **`transcript_in_range`** (jen segmenty protínající rozsah). `frames --at` zůstává; agent sám nevolá časy mimo rozsah.

### 8.4 Skilly

- `video-story` (nahrávání): popis stavu `editing` („uživatel ořezává nahrávku v editoru, čekej dál“) a práce s `crop` ve výsledku: číst `transcript_in_range`, snímky jen v rozsahu.
- `video-story-edit`: po `edit --detach` **čekat smyčkou `wait --editor`**; na `process` zpracovat uvedenou nahrávku v rozsahu (stejná pravidla jako výše), na `closed` skončit jednou větou. Při přímé žádosti o MP4 dál použít `export`.

## 9. Akceptační kritéria

1. Nová instalace nahrává JPEG snímky s kvalitou 70 %; Settings ukazuje JPEG a 70 %; sidecar, `frames`, editor i export fungují s JPEG.
2. Po stisku Draw on Screen je aktivní obdélník; paleta má 8 barev, výchozí červená; kresba je v snímcích i v `drawings` sidecaru.
3. V náhledu editoru kolečko myši přibližuje k bodu pod kurzorem až na 800 % a oddaluje zpět na „celý v náhledu“; přiblížený obraz lze posouvat tažením; kurzor a kresby sedí v každém měřítku; crop ani export se tím nemění.
4. Stop dialog nabízí Video Editor; po volbě proběhne zpracování a otevře se editor s touto nahrávkou; `state.json` je `editing` a `wait` čeká dál.
5. Zelené tlačítko Process v editoru: uloží crop, přepne relaci na `processed` s `crop`, agent čekající na `wait` dostane rozsah a zpracuje jen segmenty a snímky uvnitř; editor zůstane otevřený. Zavření editoru křížkem bez Process po nahrávání ukončí relaci jako `cancelled`; agent nic nezpracuje a nahrávka zůstává uložená.
6. Samostatný editor: po „Edit Video Story“ agent čeká; výběr starší nahrávky, úprava cropu, Process → agent dostane id a rozsah a zpracuje jen tento úsek; zavření editoru bez Process agent oznámí a nic nezpracuje.
7. Crop se ukládá do `crop.json` při každé změně a po opětovném otevření nahrávky je obnoven.
8. Stávající funkce (Process Recording ze Stop dialogu, Render, retence, Delete All) fungují beze změny; jednotkové testy a CI na Windows, Linuxu a macOS procházejí. Ověření na Windows ručně podle rozšířeného akceptačního checklistu.

## 10. Mimo rozsah

Úprava přepisu v editoru, více cropů v jedné nahrávce, zoom náhledu přes klávesnici, ukládání zoomu náhledu, zpracování rozsahu více agenty najednou, Linux/macOS ověření okna editoru (zůstává milník E7).

## 11. Souhrn bodů k potvrzení

1. Výchozí nástroj obdélník jen při otevření okna (nástroj se neukládá do konfigurace) – kap. 3.1.
2. Sada 8 barev palety – kap. 3.2.
3. Krok zoomu 1,25× na dílek kolečka, posun tažením, dvojklik = celý náhled – kap. 4.4–4.5.
4. Při otevření editoru z nahrávání, když už editor běží: načíst nahrávku do běžící instance – kap. 5.3.
5. Po stisku Process editor zůstává otevřený – kap. 6.3.
6. Rozhraní pro agenta: stav `editing`, pole `crop`, `wait --editor`, `transcript_in_range` – kap. 8 (technický návrh, lze upravit).
