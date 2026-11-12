# Video Story – specifikace

Stav: **platný popis aktuálního chování aplikace** (11. 11. 2026). Tento dokument se udržuje spolu s kódem; když se chování změní, mění se i tady. Nahrazuje původní zadání v1 a rozšíření v2 ([video-story-spec-v2.md](video-story-spec-v2.md), ponecháno jako historický záznam). Implementační plány ([implementation-plan.md](implementation-plan.md), [implementation-plan-editor.md](implementation-plan-editor.md), [implementation-plan-mouse.md](implementation-plan-mouse.md), [implementation-plan-live-transcription.md](implementation-plan-live-transcription.md)) popisují, jak návrh vznikal, a tam, kde se s tímto dokumentem liší, neplatí. Ověření je v [tests/manual/acceptance.md](tests/manual/acceptance.md).

## 1. Účel

Video Story je plugin se dvěma skilly (`video-story`, `video-story-edit`) pro AI agenty. Uživatel při práci na počítači nahlas popisuje, co dělá nebo co chce udělat. Plugin nahrává mikrofon, v pravidelném intervalu snímá všechny obrazovky a zaznamenává polohu kurzoru, kresby na obrazovce, zkopírované texty, označené okamžiky a akce myši s prvky pod myší (Windows). Zvuk přepíše lokálně a uživatel pak v editoru rozhodne, kterou část nahrávky a s jakými pokyny agentovi předá.

Celý plugin (rozhraní, hlášky, skilly, kód) je anglicky. Přepis zachovává jazyk řeči. Tato specifikace a plány jsou česky.

## 2. Základní pravidla

1. **Agent jedná jen po stisku Process.** Nahrávku agentovi předá výhradně zelené tlačítko **Process** v editoru, ať ho stiskne uživatel, nebo odpočet **Auto process**. Každý jiný konec (zavření editoru, pád editoru, editor se neotevřel, pád nahrávacího okna) znamená „nahrávka je uložená, nic nedělej“.
2. **Nahrávka se neztrácí.** Ať nahrávání skončí jakkoli, nahrávka se uloží a otevře v editoru. Pokud okno spadne dřív, dokončí ji příští spuštění.
3. **Nahrávání se nikdy nepozastavuje.** Časová osa je souvislá od stisku Record do konce; při otevřeném dialogu Stop se nahrává dál.
4. **Nahrávání a přepis běží lokálně.** To, co si agent z výsledku přečte (přepis, zkopírované texty, titulky oken, prvky pod myší a jejich obsah, snímky, které otevře), putuje k modelu, na kterém agent běží, a pokud je to vzdálený model, opouští počítač. Obsah polí pro heslo se nikdy nečte.

## 3. Podporovaná prostředí

| Oblast | Stav |
| --- | --- |
| Windows 10/11 | Ověřeno, včetně více obrazovek s různým DPI. Hlavní platforma. |
| Linux X11 | Nahrávání ověřeno (Fedora). Okno editoru neověřeno. Wayland není podporován. |
| macOS 13+ | Ověřeno jen na runneru GitHub Actions; na fyzickém Macu (Retina, mikrofon) neověřeno. FFmpeg je nutné nainstalovat ručně. |
| Claude Code | Ověřeno na Windows (skill připojený do `~/.claude/skills`). |
| Codex, Claude Desktop (MCP stdio), ChatGPT (MCP přes HTTP a tunel) | Zdokumentováno, nevyzkoušeno. |

## 4. Nahrávací okno

Po pokynu „Start Video Story“ agent spustí `run --detach` a otevře se malé okno, které zůstává nad ostatními. Záznam začne až stiskem **Record**.

| Prvek | Chování |
| --- | --- |
| **Record** | Spustí nahrávání. Nespustí ho, když je na disku složky nahrávek méně než 1 GB. Když chybí model Whisper nebo FFmpeg, hned to oznámí a zeptá se, zda nahrávat i tak. |
| **Stop** (`Ctrl+Alt+S`) | Během nahrávání otevře dialog s volbou dalšího postupu (kap. 7). Zavření okna křížkem během nahrávání dělá totéž. |
| **Settings** | Otevře nastavení. Je vidět jen mimo nahrávání. |
| **Keypoint** (`Ctrl+Alt+K`) | Během nahrávání stojí na místě Settings. Označí aktuální okamžik jako důležitý; další lze vložit po 3 s (tlačítko odpočítává). |
| **Draw** (`Ctrl+Space`) | Zapne kreslení (kap. 6), opětovným stiskem ho vypne. Funguje i `Ctrl+F1`; tooltip uvádí obě zkratky. Jen během nahrávání. Opuštění kreslení (tlačítko Draw, `Ctrl+Space`, `Ctrl+F1`, `Esc`, křížek v titulku nástrojové lišty) zároveň smaže všechny kresby (Clear), takže další kreslení začíná s prázdnou obrazovkou. |
| Motiv (ikona slunce / měsíce) | Malé tlačítko jen s ikonou vedle Draw: přepíná okno mezi světlým a tmavým motivem (stejné ikony jako v editoru). Volba se pamatuje pro příští spuštění. |
| **Hide to Tray** (ikona) | Malé tlačítko jen s ikonou vedle motivu: skryje okno do oznamovací oblasti (tray). Tam je modrá ikona aplikace (bílá kamera s červenou tečkou) a bublina řekne, že nahrávání lze zastavit `Ctrl+Alt+S` nebo kliknutím na ikonu zobrazit okno. Kliknutí levým i pravým tlačítkem na ikonu okno znovu zobrazí (nahrávání nezastaví). Po Discard se okno zobrazí samo. |

Klávesové zkratky Stop, Keypoint a Draw platí jen během nahrávání; tlačítka je ukazují v závorce za názvem i v tooltipu. Na Windows fungují v celém systému (ať je vpředu kterákoli aplikace, stisk do ní nedojde), na Linuxu a macOS jen když má fokus okno Video Story. Kombinaci, kterou už zabrala jiná aplikace (např. `Ctrl+Space` přepínač vstupní metody), Video Story použije jen ve svých oknech; proto má Draw i `Ctrl+F1`. Na klávesnicích s AltGr ji Windows hlásí jako Ctrl+Alt, takže AltGr+S/K během nahrávání zkratku také spustí.

Stavový řádek ukazuje uplynulý čas, čas do automatického zastavení, počet obrazovek, snímků a keypointů a při přepisu během nahrávání (kap. 6) i to, o kolik přepis zaostává (`text −0:25`). Zavření okna před nahráváním ukončí relaci stavem `cancelled`.

## 5. Nastavení

Dialog **Settings** nabízí položky v tomto pořadí; ostatní klíče `config.json` se mění jen příkazem `config set`.

| Položka | Klíč | Výchozí | Rozsah |
| --- | --- | --- | --- |
| Recording Folder | `recording_folder` | `~/Videos/VideoStory` (macOS `~/Movies/VideoStory`) | – |
| Capture Interval | `capture_interval_ms` | 500 ms | 1–1000 |
| Image Format | `image_format` | JPEG | JPEG, PNG |
| JPEG Quality | `jpeg_quality` | 70 % | 20–100, jen pro JPEG |
| Audio Input | `audio_input` | systémový výchozí vstup | – |
| Computer Audio (Record computer audio) | `computer_audio` | zapnuto | zatím jen Windows; jinde zašedlé |
| Mouse (Record mouse actions) | `mouse_actions` | zapnuto | zatím jen Windows; jinde zašedlé. Jeden přepínač pro všechno: akce myši, gesta touchpadu, titulky oken, prvky pod myší a jejich obsah |
| Live Transcription | `live_transcription` | **Light – low CPU use (recommended)** | **Off – transcribe in the editor**, Light, **Faster – more CPU** (kap. 6); s backendem `whisper-cli` zašedlé (Off) |
| Auto Stop Recording | `auto_stop_minutes` | 120 min | 1–1440 |
| Archive After | `archive_after_days` | 30 dní | 1–3650 |
| Drawing Grid | `drawing_grid_px` | 16 px | 8–128, libovolné celé číslo; rozteč mřížky, na kterou se při kreslení s Ctrl zarovnává (kap. 6) |
| Recordings | – | **Open Folder** (mazání nahrávek je jen v editoru: videa a složky) | – |

Změna **Recording Folder** platí okamžitě i pro už otevřené okno: jeho relace se přesune do nové složky, takže nahrávka, editor i čekající agent pracují s novou složkou. Během nahrávání nastavení otevřít nelze.

`config.json` leží v kořeni instalace pluginu (mimo Git); pokud tam nejde zapisovat, použije se uživatelský konfigurační adresář.

## 6. Co se zaznamenává

- **Zvuk:** `audio/audio.wav`, 16bit PCM, mono, vzorkovací frekvence zařízení.
- **Zvuk počítače:** `audio/computer.wav` – co počítač během nahrávání přehrával (video na pozadí, hovor); 16bit PCM, mono, stejná vzorkovací frekvence jako mikrofon, začíná ve stejném okamžiku. Zatím jen Windows (WASAPI loopback výchozího výstupního zařízení, balíček `soundcard`); na Linuxu a macOS stopa nevzniká, kód je na ně připravený (`capture/computer_audio.py`). Stopa se drží hodin nahrávky: když nic nehraje, doplní se ticho, a když se zařízení rozejde s hodinami o víc než 100 ms, ticho se doplní nebo přebytek zahodí. Když výstup nejde otevřít (vypnuto v Settings, žádné výstupní zařízení), nahrávka prostě nemá druhou stopu; selhání této stopy nahrávání nikdy nezastaví. Manifest ji popisuje v `computer_audio` (jinak `null`).
- **Obrazovky:** v každém intervalu snímek každé obrazovky, i nezměněný. Název `<sekundy>_<ms>_screenN.<jpg|png>`, čas od začátku nahrávky. `screen1` je primární obrazovka, další jsou řazené podle polohy.
- **Doprovodný JSON** ke každému snímku (stejný název s příponou `.json`, ve složce `frames_info/`; ve `frames/` jsou jen obrázky): obrazovka a poloha kurzoru v pixelech jejího snímku (počátek vlevo nahoře), dokončené kresby, právě kreslený tvar, zoom, zkopírovaný text.
- **Kresby:** nástroje obdélník `R` (výchozí), elipsa `E`, čára `L`, štětec `B`, šipka `A` a guma `U`; na liště jen ikona a písmeno. Guma tažením levým tlačítkem smaže každý objekt, jehož čáry se dotkne nebo ji protne (vnitřek obdélníku či elipsy ne); zasáhne-li jednu čáru skupiny (spojené čáry, kusy čáry nebo šipky přes víc obrazovek), smaže celou skupinu na všech obrazovkách. `Ctrl+Z` vrátí vše, co smazal poslední tah gumou, se stejnými id a ve stejném pořadí. Všechny nástroje jsou na levém tlačítku, pravé tlačítko myši kreslí vždy šipku. 9 barev (klávesy `1`–`9`, 9. je černá s bílým rámečkem výběru), výchozí červená. Lišta: **Sketch/Screen** `S`, **Undo** `Ctrl+Z` (při drženém Shiftu nedělá nic), **Clear** `Ctrl+Alt+C`; tlačítko Close není, kreslení opustí křížek v titulku lišty nebo `Esc`. **Sketch:** po každém zapnutí kreslení jsou v pozadí obrazovky a tlačítko říká „Sketch“; klik (nebo `S`) zobrazí na všech obrazovkách čisté bílé plátno (zoom funguje dál) a tlačítko se změní na „Screen“; další klik vrátí obrazovky. Přepínání nic nemaže. Snímek nese `background` (`screen`, `sketch`, mimo kreslení `null`), každý tvar `background`, na kterém vznikl; timeline má řádky `SKETCH on` / `SKETCH off` a u tvaru z plátna `(on the sketch canvas)`. **Shift** (jen levé tlačítko; pravé tlačítko kreslí vždy šipku a Shift na něj nemá vliv, stejně jako na šipku `A` a gumu): obdélník kreslí **čtverec** a elipsa **kruh**, dokud je Shift držený (živě, i během tažení). Štětec se Shiftem kreslí **uzavřený tvar** (poslední bod se stále spojuje s prvním), ať je Shift stisknutý před stiskem tlačítka, nebo až během tahu: stisk Shiftu uprostřed tahu konec hned spojí se začátkem a dál platí totéž jako u tahu začatého se Shiftem; kreslení skončí puštěním Shiftu nebo tlačítka, co nastane dřív. **Čára:** puštění tlačítka bez Shiftu čáru ukončí; puštění s drženým Shiftem (stisknutým před začátkem čáry i během tažení) ji neukončí a její konec dál sleduje kurzor; dokud je Shift držený, každý stisk levého tlačítka ukončí čáru v místě stisku a začne z něj další (**spojené čáry**); puštění Shiftu bez stisknutého tlačítka ukončí čáru hned u kurzoru, se stisknutým tlačítkem až jeho puštěním. Změna nástroje, Clear nebo opuštění kreslení rozkreslenou čáru zahodí. **Čára a šipka přes obrazovky:** jdou kreslit z jedné obrazovky na druhou (vidět jsou během kreslení na všech) a po dokončení se rozdělí na hranách obrazovek na jeden tvar na obrazovku; kus šipky s hrotem je `arrow`, ostatní kusy `line` s `part_of: "arrow"`; Undo i guma berou kusy jednoho tvaru jako celek. **Ctrl (mřížka):** dokud je Ctrl držený, body, kde uživatel kliká, se zarovnávají na mřížku s roztečí **Drawing Grid** (Settings, výchozí 16 px, v logických pixelech obrazovky, mřížka začíná v jejím levém horním rohu): u obdélníku a elipsy oba rohy (se Shiftem má čtverec či kruh stranu v násobcích rozteče), u čáry začátek a konec, u spojených čar (`Ctrl+Shift`) každý lomový bod, u šipky (levé `A` i pravé tlačítko) konec a hrot; body, kde se čára nebo šipka láme na hraně obrazovky, se nezarovnávají. Štětec a guma se nezarovnávají. Ctrl se čte v okamžiku každého bodu, jde ho tedy stisknout i pustit během tažení (zarovnání naskočí i bez pohybu myši). Na plátně Sketch se mřížka při drženém Ctrl zobrazí světle šedou (`#E2E2E2`) a je vidět i ve snímcích; na obrazovce se nezobrazuje, zarovnání funguje stejně. Snímek nese `grid` (rozteč v pixelech snímku dané obrazovky, dokud je Ctrl v kreslení držený, jinak `null`); timeline má řádky `GRID on` / `GRID off` (Ctrl držený kratší dobu než 1 s bez zarovnaného tvaru se vynechá, je to zkratka jako `Ctrl+Z`). JSON tvaru: `shift: true` u čtverce, kruhu, uzavřeného tvaru a spojených čar, `closed: true` u uzavřeného štětce (`closed_ms`, když ho Shift uzavřel až během tahu), `grid` (rozteč v pixelech snímku, když byl aspoň jeden bod zarovnaný), `group` (spojené čáry a kusy rozděleného tvaru), `segment` (pořadí spojené čáry); timeline je označuje `(square, Shift)`, `(circle, Shift)`, `(closed shape, Shift)` / `(closed shape, Shift from <ms>)`, `(snapped to the N px grid, Ctrl)`, `(connected lines group g, segment n)` a `(... split at a screen edge, group g)`. Kolečko myši při kreslení přiblíží obrazovku až na 800 %. Kresba je vidět ve snímcích; do JSONu se tvar dostane až po puštění tlačítka myši, s identifikátorem a časy stisku a puštění (rozkreslený tvar ohlašuje `drawing_now` bez geometrie). Spojená čára se zapíše hned, jak ji klik ukončí, takže timeline ukazuje lomenou čáru segment po segmentu mezi slovy. Tvar začatý, ale nedokončený (useknutý koncem rozsahu nebo zrušený `Esc`, zavřením kreslení či klikem bez velikosti) má v timeline řádek `DRAWING`. Nahrávací okno zůstává nad kreslicí vrstvou.
- **Zkopírovaný text:** každý nový text zkopírovaný během nahrávání (nejvýš 65 535 znaků) se zapíše jednou, do JSONů nejbližšího snímku. Co bylo ve schránce před nahráváním, se neukládá.
- **Myš** (zapnuté Mouse, zatím jen Windows): `input/mouse.jsonl`, jeden JSON na řádek, zapisuje se průběžně (pád ztratí nejvýš poslední sekundu). Obsahuje:
  - stisk a puštění tlačítek (levé, prostřední, pravé, X1 = zpět, X2 = vpřed), kolečko svisle i vodorovně (i jemný posun), dráhu tažení (pohyb bez stisknutého tlačítka se neukládá, poloha kurzoru je v doprovodných JSONech), stav Ctrl/Shift/Alt/Win a příznak `injected` (událost vytvořená programem);
  - gesta touchpadu dvěma prsty (pinch, posun) z Precision Touchpadu, jen hotová gesta; tři a více prstů se nezaznamenávají;
  - prvek pod myší (UI Automation): při každém stisku, kde skončilo tažení nebo dvojklik, kde začal scroll a kde se kurzor zastavil aspoň na 0,6 s – role, název, okno a proces, ohraničení, nejvýš tři pojmenovaní předci a obsah: hodnota pole, odstavec v místě kliku, po tažení nebo dvojkliku označený text. Obsah má nejvýš 4096 znaků (delší končí `...`) a každý text je v souboru jen jednou. Pole pro heslo se nečte nikdy, okna Video Story a kreslicí vrstva se neprohledávají;
  - titulek a proces okna jednou na okno (při změně titulku znovu).
  Hook myši i UI Automation běží ve dvou malých pomocných procesech, aby nezpomalovaly kurzor ani okno; skončí spolu s Video Story. Když něco z toho nejde (např. to zablokuje bezpečnostní politika), nahrává se dál bez toho a manifest to v sekci `mouse` řekne (jinak `null`); `doctor` ukáže, co jde.
- **Keypointy:** okamžiky označené tlačítkem Keypoint (`Ctrl+Alt+K`); ukládají se do `manifest.json` hned při vložení.
- **Přepis během nahrávání** (Live Transcription, výchozí Light): od stisku Record se řeč přepisuje průběžně, takže editoru po Stopu zbývá jen konec. Přepis běží v samostatném procesu se sníženou prioritou, aby nebrzdil práci ani nahrávání:
  - **Light** – nejnižší priorita (Windows: background mode = procesor, disk i paměť až po všem ostatním; Linux `SCHED_IDLE`; macOS `PRIO_DARWIN_BG`), čtvrtina logických procesorů (2–8 vláken);
  - **Faster** – priorita mírně pod normální, polovina procesorů (2–16 vláken);
  - **Off** – přepisuje až editor.
  Model, dekódování a mix stop (mikrofon + zvuk počítače) jsou stejné jako v editoru. Zvuk se bere po oknech do 30 s, která končí v pauze řeči (nejdřív v mixu, pak jen v mikrofonu, jinak natvrdo po 30 s, a pak se poslední, možná useknutý segment přepíše znovu v dalším okně). Okno bez řeči se whisperu nepředá. Jazyk se určí z prvního okna s řečí a pak zůstane pro celou nahrávku (delší pasáže v jiném jazyce se proto mohou přeložit). Dosavadní text se whisperu jako kontext (prompt) nepředává: měření ukázalo, že slova nezlepší a titulky slije do dlouhých bloků. Hotová okna se ukládají do `transcript.json`/`.txt` pracovní složky (`complete: false`, `transcribed_ms`). Když začnou vypadávat snímky, přepis si sám ubere polovinu vláken a po 2 minutách klidu je vrátí. Spadlý přepis se spustí znovu (nejvýš dvakrát); nahrávání tím nikdy neskončí. Na baterii se chová stejně jako na síti.
- **Manifest:** `manifest.json` vzniká při stisku Record (obrazovky, nastavení, čas začátku) a doplňuje se při uložení. Pole `stop_reason` říká, proč nahrávka skončila sama (`auto_stop`, `low_disk`, `capture_failed`, `recovered`), `frames.failed_grabs` počet nepořízených snímků, `live_transcription` průběh přepisu během nahrávání (režim, vlákna, priorita, počet oken, kam se přepis dostal, chyba; jinak `null`).

Obrazovka, kterou nejde sejmout (zamčený počítač, dialog UAC, odpojený monitor), nahrávání neukončí: po tu dobu pro ni snímky chybí a snímání se samo obnoví.

## 7. Konec nahrávání

Dialog po stisku **Stop** nabízí tři volby; nahrávání běží, dokud uživatel nerozhodne.

| Volba | Výsledek |
| --- | --- |
| **Video Editor** (výchozí) | Ukončí nahrávání, nahrávku hned uloží a otevře ji v editoru. Nahrávací okno se zavře. |
| **Continue Recording** | Zavře dialog, nahrává se dál. |
| **Discard Recording** | Po potvrzení smaže soubory nahrávky a okno se vrátí do stavu Record; relace pokračuje. |

Nahrávání skončí samo ve třech případech a vždy stejně jako volbou Video Editor, tedy uložením a otevřením editoru:

- uplyne **Auto Stop Recording**;
- na disku složky nahrávek zbývá **méně než 1 GB** – uživatel dostane hlášku, že je potřeba alespoň 1 GB volného místa, jinak nelze v nahrávání pokračovat;
- nahrávku **už nejde zapisovat** nebo selže snímání – uživatel dostane hlášku s důvodem.

Přepis během nahrávání (kap. 6) se při ukončení nahrávání hned zastaví: rozpracované okno (nejvýš 30 s zvuku) se zahodí, hotová část se uloží s nahrávkou a editor na ni naváže. Při Discard se smaže s nahrávkou. Pokud se editor nepodaří otevřít, nahrávka zůstane uložená a relace skončí stavem `cancelled`.

## 8. Editor

Editor se otevře po nahrávání, nebo samostatně pokynem „Edit Video Story“ (`edit --detach`). Běží nejvýš jeden; další žádost ho jen zvedne a načte do něj požadovanou nahrávku. Při startu editor dokončí nahrávky, jejichž okno spadlo (kap. 10).

- **Seznam nahrávek** vlevo, po **složkách** (kap. 11): každá složka je akordeon se záhlavím `▾ název (N)  📌 ⋯` – trojúhelník (rozbaleno / sbaleno; klik kamkoli na záhlaví přepíná, stav se pamatuje v `folders.json`, výchozí je vše rozbalené; složka, jejíž nahrávka se má zobrazit – nová nahrávka, požadavek agenta, výsledek hledání, přesun – se rozbalí), název (tučně u **hlavní složky**, kam se ukládají nové nahrávky; dlouhý název se zkrátí `…`, počet videí zůstane vidět), **špendlík** (prázdný; plný u **připnuté složky**, která stojí vždy úplně nahoře; nejvýš jedna připnutá, může být i žádná; klik připne – připnutá jde nahoru, dosavadní připnutá o jedno níž – nebo odepne, pořadí zůstane; obojí s potvrzením) a **⋯** s nabídkou **Set as Main Folder**, **Move Up**, **Move Down** (nic nepředběhne připnutou složku), **Archive All**, **Delete All Videos (N)** (smaže jen videa, složka zůstane) a **Delete Folder** (po jednom potvrzení smaže videa i složku; hlavní se pak stane `default`, připnutá nezůstane žádná; u `default` vypnuté; složku, ve které zůstalo něco jiného, ponechá). Vpravo od nadpisu „Recordings“ je tlačítko **Create new folder** (složka s plusem): dialog s názvem předvyplněným `New folder RRRRMMDD-HHMMSS` a zaškrtnutým „Move selected video(s) to the new folder“; nová složka přibude na konec. Výběr se Shiftem nebo Ctrl funguje jen v jedné složce (klik do jiné složky začne nový výběr tam), hromadné akce tedy také. Ve složce je nejnovější nahrávka nahoře; při startu je vybraná nejnovější ze všech. Řádek ukazuje náhled, datum, délku s velikostí složky nahrávky v závorce (KB, MB, GB nebo TB na jedno desetinné místo, např. `00:27 (12.3 MB)`) a začátek řeči; pohyb myší po řádku nahrávkou projíždí. Kolečka vpravo nahoře (jedno na obrazovku, nejvýš čtyři) přepínají obrazovku náhledu, a to ve všech řádcích najednou: kliknutý řádek ukáže zvolenou obrazovku, ostatní obrazovku na stejné pozici, a když jich mají méně, svou poslední (klik na třetí kolečko: nahrávky se třemi a čtyřmi obrazovkami ukážou třetí, se dvěma druhou, s jednou první). Kontextová nabídka: **Open Folder Location**, **Move Video to Folder...** / **Move Videos to Folder... (N)** (dialog s comboboxem ostatních složek; nahrávku, která se archivuje nebo na archivaci čeká, přesunout nejde; zobrazená nahrávka se uvolní a otevře znovu z nového místa a její přepis pokračuje; nahrávku mezi Stop a Process přesunout jde, `state.json` dostane novou cestu a Process ji předá agentovi), **Render Video...** (těsně nad Archive Video; viz Render níže), **Archive Video** / **Archive Videos (N)**, **Delete Video** / **Delete Videos (N)** (N = počet vybraných nahrávek; u archivace jen těch, které ještě archivovat jdou). Na náhledu řádku je štítek (černý text na bílém): `TRANSCRIBING...`, `ARCHIVING...` a `RENDERING...` s procenty na jedno desetinné místo, `ARCHIVED` u archivované nahrávky. **Čerstvá nahrávka** – ta, která přišla ze Stop dialogu volbou Video Editor, jinak (editor otevřený samostatně) nejnovější nahrávka – má v seznamu světle zelené pozadí (v tmavém motivu tlumeně zelené), kdykoli není vybraná, aby se uživateli neztratila, když se dívá na jiné.
- **Vyhledávání** pod seznamem: textové pole, vedle něj tlačítko s lupou a tlačítko **Refresh** jen s ikonou kruhové šipky (F5). Enter nebo lupa hledá slovo či frázi přesně, jak je napsaná, ale bez ohledu na velká/malá písmena a diakritiku (`prilis` najde `příliš`) v titulcích (přepisu). Nejdřív se prohledá zobrazená nahrávka (její odpovídající obdélníčky na stopě narration hned zežloutnou), potom ostatní ve vlákně na pozadí; každá nahrávka s nálezem dostane v seznamu žlutý rámeček a po kliknutí na ni jsou žluté její odpovídající obdélníčky. V titulku pod náhledem je hledaná fráze tučně a červeně. Vymazání pole hledání zruší. Fráze se hledá v rámci jednoho titulku.
- **Náhled** s transportem (na In, krok zpět, přehrát, stop, krok vpřed, na Out; Stop se vrací tam, kde bylo naposledy stisknuto Play), titulky pod náhledem (oblast titulku má vždy výšku aspoň dvou řádků, takže jeden řádek textu zabírá stejně místa jako dva a náhled nad ním neposkakuje; nejvýš pět řádků, pak se posouvá), kolečko myši přibližuje až na 800 %, přiblížený obraz se posouvá tažením levým i prostředním tlačítkem, přepínač světlého a tmavého vzhledu. Plocha kolem snímku nese nevýrazný diagonální vodoznak `screenN ŠxV` (opakovaný), barevně odlišený podle obrazovky: screen1 šedá, screen2 modrá, screen3 zelená, screen4 červená, screen5 znovu šedá atd. (tmavší text na světlém podkladu; v tmavém motivu je modrá screen2 světlejší než ostatní, aby nesplývala s černou).
- **Myš u transportu:** vlevo od tlačítek transportu (přes řádky transportu, času a titulku; vpravo je stejně široký sloupec s přepínačem vzhledu nad Edit, takže transport zůstává uprostřed) je obrázek myši shora (`editor/assets/mouse.svg`, předloha mouse-template-v11). Jak se pohybuje červená čára, ukazuje podle `input/mouse.jsonl`, co se v tu chvíli s myší dělo: stisknuté tlačítko je vyplněné červeně (levé, pravé, prostřední = celý svislý modul kolečka kromě kolečka a jeho šipek, X2 = vpřed přední boční tlačítko, X1 = zpět zadní boční tlačítko), otáčené kolečko ostře žlutě spolu se šipkou směru (svislé kolečko se šipkou nahoru/dolů, vodorovné se šipkou doleva/doprava). Tlačítko svítí od stisku do puštění, kolečko po dobu sloučeného posunu; obojí aspoň 200 ms, aby byl vidět i rychlý klik nebo jeden zub. Mění se jen výplně; obrys je ve světlém motivu černý s bílou výplní, v tmavém bílý s tmavou výplní. Události vytvořené programem (`injected`) se nezobrazují, kliky do Video Story a na kreslicí vrstvu ano. Nahrávka bez záznamu myši ukáže myš bledou a nic se nerozsvítí.
- **Okno Mouse:** klik na obrázek myši otevře okno **Mouse** se vším, co je o myši zaznamenáno v okamžiku červené čáry. Okno je nemodální: editor zůstává ovladatelný a okno jde s červenou čarou při přehrávání, krokování i tažení; druhý klik ho jen zvedne. Je asi 1100 px široké (nejvýš 80 % editoru), panely jsou pod sebou přes celou šířku, krátká pole v panelu vedle sebe (čtyři sloupce), víceřádková pole přes celou šířku; všechna pole jsou jen pro čtení, text jde označit a zkopírovat. V hlavičce čas, obrazovka s rozměrem a poloha kurzoru. Panely shora:
  - **Buttons and wheel** – pět obdélníků Left, Middle, Right, X1 back, X2 forward (držené tlačítko červeně, s dobou držení) a čtyři šipky kolečka nahoru/dolů/doleva/doprava (směr, kterým se otáčí, žlutě); svítí stejně jako obrázek myši, aspoň 200 ms, události `injected` se nerozsvítí;
  - **Action** – právě probíhající akce (CLICK, DOUBLE-CLICK, TRIPLE-CLICK, DRAG, SCROLL, ZOOM-SCROLL, GESTURE; jinak `Idle`), její čas, modifikátory, `Injected`, Start, Now (u tažení poslední bod dráhy a ujetá vzdálenost), End (konec tažení je znám dopředu, nahrávka je hotová), Scroll / gesture;
  - **Element under mouse** – poslední prvek zjištěný UI Automation do toho okamžiku: kdy a při čem (press, release, scroll, rest) a před kolika sekundami, role, název, ohraničení v pixelech snímku obrazovky, až tři předci, cíl tažení a obsah (value / text / selected) ve víceřádkovém poli; u pole pro heslo jen poznámka, že se nečte;
  - **Window** – titulek a proces okna poslední události myši a od kdy v něm uživatel pracuje;
  - **Clipboard** – poslední text zkopírovaný do toho okamžiku (z JSONů snímků): kdy, před kolika sekundami, délka, kolikátá kopie nahrávky to je, celý text; před první kopií „Nothing copied yet“;
  - **Recent mouse events** – pět akcí před okamžikem a tři po něm jako řádky `timeline` (bez časů snímků), aktuální označená `<- now`, jinak řádek `--- now <ms> ---`.
  Data se poskládají při prvním otevření pro danou nahrávku na pozadí (čte se jeden JSON na snímek kvůli schránce), do té doby okno ukazuje `Loading...`. Bez záznamu myši ukáže jen schránku a v hlavičce „Mouse actions were not recorded“.
- **Časová osa:** jedna stopa na obrazovku s miniaturami, jedna zvuková stopa, pod ní zamčená stopa **narration** (zámeček v hlavičce, poloviční výška zvukové stopy, nejde upravovat ani posouvat): obdélníček všude, kde titulek obsahuje text, v pauzách nic; od přiblížení 40 px/s ukazují obdélníčky text titulku zkrácený trojtečkou, najetí myší ukáže celý text, zoom `-`/posuvník/`+` u pravého okraje (bez tlačítka Fit; levý konec posuvníku je celá nahrávka, `Ctrl+0` tam skočí). Od náhledu ji odděluje táhlo rozdělovače nakreslené jako dvě tenké linky 4 px od sebe (dvojitá linka říká, že jde táhnout). Obraz náhledu od ovládání pod ním odděluje jedna tenká linka stejné barvy (ve světlém i tmavém motivu), která táhnout nejde. Kliknutí nebo tažení červené čáry po puštění tlačítka myši přesune pod ni i tmavý trojúhelník na pravítku (místo, odkud Play začne a kam se Stop vrací). Stisk **prostředního tlačítka myši** časovou osu uchopí: dokud je tlačítko držené, osa jede s myší (táhnu doprava → vidím dřívější čas), stejně jako vodorovný posun dvěma prsty na touchpadu; červená čára ani crop se nemění. Úchyty **In/Out** (nebo klávesy `I`/`O`; pole In, Out a délka ukazují `HH:MM:SS.ss`) nastavují crop společný pro všechny obrazovky; ukládá se do `crop.json` při každé změně a přichytává se ke keypointům. Kliknutí na stopu vybírá obrazovku pro náhled a export.
- **Zvukové stopy:** v hlavičce zvukové stopy jsou za přepínačem roztažení tři ikony. **Mikrofon** a **reproduktor** zapínají a vypínají stopu mikrofonu a stopu zvuku počítače; obě jsou ve výchozím stavu zapnuté a zvuková stopa pak ukazuje i přehrává jejich součet (smíšenou vlnu). Vypnutá stopa má ikonu přeškrtnutou červeně; nahrávka bez zvuku počítače má reproduktor zašedlý a přeškrtnutý a nejde zapnout. Když jsou vypnuté obě, je ve stopě ticho. Volba se ukládá do `audio/tracks.json` a platí pro přehrávání, vlnu, přepis, Render i archivaci. Přepnutí stopy hned změní vlnu i zvuk a vzápětí se zeptá, jako by uživatel klikl na pero.
- **Pero (nový přepis):** ikona pera píšícího na papír po potvrzení (okno „Transcribe again“: narration se smaže a vytvoří znovu přepisem aktuální zvukové stopy, opravy z Edit se ztratí; vypíše, zda je zahrnut mikrofon a zvuk počítače, a když není zahrnuto nic, že narration bude prázdná) smaže přepis a spustí přepis od začátku – i když předchozí přepis ještě běží (ten se ukončí). Bez potvrzení se nic nemění. Render a Process čekají na nový přepis. Archivovaná nahrávka ikony nemá.
- **Přepis** běží na pozadí editoru (Whisper large-v3-turbo přes whisper.cpp, CPU, automatický jazyk). Zelená šipka pod stopou narration ukazuje postup (ve světlém motivu tmavší zelená) a odhad zbývajícího času, titulky přibývají průběžně. Když už část přepsalo nahrávací okno (kap. 6), editor přepisuje jen zbytek. Zavření editoru přepis přeruší, příští výběr nahrávky naváže. Navazující přepis převezme jazyk dosavadního přepisu. Když přepis selže, tlačítko **Retry transcription** ho spustí znovu od místa, kde skončil. Halucinované titulkové kredity se zahazují.
- **Edit** opraví text titulku (časy zůstávají); změna se zapíše do `transcript.json` i `transcript.txt`.
- **Instructions:** hlavní pokyny pro agenta k celé nahrávce, uložené v `instructions.txt`; šablony Draft Intent, Create Plan a Implement.
- **Render** (kontextová nabídka seznamu, **Render Video...**; dole žádné tlačítko Render ani klávesová zkratka není): export do MP4 (H.264 + AAC, 24 fps, rozlišení obrazovky, kurzor dokreslen) a SRT vedle něj. Modální okno nabídne **obrazovku** (combobox, výchozí je obrazovka vybraná na časové ose, u jiné než zobrazené nahrávky první) a prázdné pole s nápisem **Select File Name**; klik do něj otevře Uložit jako (jen MP4). Tlačítko **Render** jde stisknout až po výběru souboru. Renderuje se crop dané nahrávky (u zobrazené aktuální In/Out, jinak její `crop.json`) na pozadí: editor jde dál používat, v seznamu je štítek `RENDERING...` s procenty a na konci nemodální okno „Render finished“ (nebo „Render failed“). Renderovat jde vždy jen jedno video: při výběru více videí nebo během jiného renderu je položka zašedlá. U renderovaného videa je místo ní **Cancel Rendering** (zastaví FFmpeg, rozdělaný soubor se smaže); takové video nejde archivovat, smazat, přesunout ani znovu přepsat. Zavření editoru (i tlačítkem Process) během renderu se nejdřív zeptá, zda okno opravdu zavřít; po potvrzení se render zastaví.
- **Process:** předá agentovi crop a pokyny a editor zavře (kap. 9).

Render Video a Process jsou dostupné, až když je přepis nahrávky úplný (nebo definitivně selhal). Spodní řádek tvoří u pravého okraje **Auto process**, zelené **Process** a **Instructions**.

Stavový řádek editoru ukazuje id nahrávky a vybranou obrazovku s rozlišením, během přepisu i jeho postup; crop ani stav vůči agentovi v něm nejsou.

### Archivace

- **Archive Video(s)** se po potvrzení („Do you want to archive this video?“ s cestou, u více nahrávek jen počet) zařadí do fronty; archivovat jde jen nahrávku s úplným přepisem. Od té chvíle nahrávku nejde upravovat: vpravo je jen velký text „This video is being archived.“ s procenty.
- Fronta běží v editoru na pozadí, jedna nahrávka po druhé (`archive-stream`). Každá obrazovka se převede na `archive/screenN.mp4` (celá nahrávka, plné audio, stejně jako Render) se `screenN.srt` a `screenN.jpg` (snímek z prostředku nahrávky); hotová obrazovka se zapíše do `archive/archive.json` a hned se smažou její snímky. Po poslední obrazovce se smaže `audio/` a zbytek `frames/`; `frames_info/`, přepis, manifest, crop a pokyny zůstávají.
- Zavřený editor archivaci přeruší čistě (FFmpeg se zastaví, rozdělané soubory se smažou); další start editoru naváže: hotové obrazovky přeskočí, rozdělanou udělá znovu.
- Soubory, které nejde smazat (drží je jiný program), zůstanou; archivace přesto skončí jako hotová a příští start editoru zkusí smazat zbytek ještě jednou.
- Při startu editoru se do fronty zařadí i nahrávky starší než **Archive After**; pokud nemají úplný přepis, nejdřív se přepíšou.
- **Archivovaná nahrávka** ukazuje v náhledu i na časové ose `screenN.jpg`, uprostřed náhledu bliká štítek `ARCHIVED` (černý text na bílém jako na náhledu v seznamu, sekundu svítí, sekundu ne), zvuková stopa je jen čára (bez přepínače roztažení), Play nehraje zvuk a mění se jen titulky. Edit titulků funguje, crop také; Auto process, Process, Instructions a Render Video jsou zakázané.

### Auto process

Zaškrtávátko **Auto process** stiskne Process samo 5 s po dokončení přepisu, pokud na Process čeká agent. Jakékoli kliknutí nebo stisk klávesy odpočet zruší.

- Při otevření editoru je **vždy odškrtnuté**; nikam se neukládá.
- Odškrtne se, když uživatel vybere jinou nahrávku nebo když je editoru předána další nahrávka.
- Platí tedy jen pro nahrávku, u které ho uživatel sám zaškrtl.

## 9. Předání agentovi

Agent po spuštění okna opakovaně volá `wait --session <id>`, dokud stav není koncový.

| Stav | Význam | Co dělá agent |
| --- | --- | --- |
| `ready`, `recording`, `processing` | okno otevřené, nahrává se, nahrávka se ukládá | čeká |
| `editing` | nahrávka je uložená a otevřená v editoru, přepisuje se | čeká |
| `processed` | uživatel stiskl Process | zpracuje rozsah `crop` podle `instructions` |
| `cancelled` | okno zavřeno bez nahrávky; editor zavřen, spadl nebo se neotevřel | nic, jednou větou to oznámí |
| `failed` | okno skončilo neočekávaně nebo se nahrávku nepodařilo uložit | nic, oznámí důvod |

Při samostatně otevřeném editoru agent čeká příkazem `wait --editor <soubor>` na událost `process` (id nahrávky, crop, pokyny) nebo `closed`.

Pokud agent čeká na nahrávku ze Stop dialogu a její přepis ještě není hotový, Process nad jinou nahrávkou se odmítne.

Po `processed` agent pracuje nejdřív s textem: přečte `instructions`, pak `timeline` (řeč s polohou kurzoru a prvkem, nad kterým se kurzor zastavil, kresby s geometrií a časy, zkopírované texty, přechody kurzoru mezi obrazovkami, zoom, keypointy a akce myši v jiných aplikacích – `CLICK`, `DOUBLE-CLICK`, `DRAG`, `SCROLL`, `ZOOM-SCROLL`, `GESTURE` s prvkem, oknem a obsahem zkráceným na 200 znaků – vše v rozsahu cropu) a snímky si přes `frames --at <ms>` vyžádá jen pro okamžiky, které z textu nejdou rozhodnout. Celý obsah prvku (do 4096 znaků), ohraničení a dráhy vrací `mouse --at <ms>`. Při více než 400 akcích myši v rozsahu `timeline` sloučí scrolly a gesta po 10 s (`--mouse full|summary|off` to mění). Přesný postup je ve `skills/video-story/SKILL.md`.

Další příkazy: `describe`, `mouse`, `status`, `sessions`, `transcribe`, `export`, `config`, `devices`, `screens`, `doctor`, `setup`, `mcp`. MCP server nabízí tytéž funkce jako nástroje `video_story_*`.

## 10. Poruchy a obnova

| Situace | Chování |
| --- | --- |
| Méně než 1 GB volného místa | Record se nespustí; běžící nahrávání se zastaví s hláškou, uloží a otevře v editoru. |
| Obrazovku nejde sejmout | Nahrávání pokračuje, snímky té obrazovky dočasně chybí. |
| Nahrávku nejde zapisovat | Nahrávání se zastaví s hláškou, co šlo zapsat, se uloží a otevře v editoru. Stop nikdy nezamrzne. |
| Nahrávací okno spadne nebo se ho nepodaří uložit | Soubory zůstanou v pracovní složce. Příští spuštění nahrávacího okna nebo editoru nahrávku dokončí (manifest ze snímků a zvuku, keypointy zůstanou) a zařadí do seznamu editoru. Agentovi se nepředá. |
| Přepis selže | V editoru tlačítko Retry transcription; relace agenta trvá. |
| Editor spadne | Relace skončí stavem `cancelled`; nahrávka i částečný přepis zůstávají. |
| Chybí model nebo FFmpeg | Oznámí se při stisku Record; `video-story setup` je stáhne. |
| Přepis během nahrávání nejde spustit nebo padá | Nahrává se dál; spadlý přepis se spustí znovu nejvýš dvakrát, pak se vzdá (manifest `live_transcription.error`). Editor přepíše, co chybí. Chybí-li model nebo FFmpeg, přepis během nahrávání vůbec neběží. |
| Hook myši nebo UI Automation nejde spustit | Nahrává se bez nich; manifest to uvede v `mouse.error`, soubor v řádku `status`. Pomocný proces, který skončí nebo přestane posílat události, se spustí znovu (nejvýš třikrát). |

## 11. Uložení a archivace

```
<Recording Folder>/
  folders.json                pořadí složek, hlavní, připnutá a sbalené složky (zapisuje editor)
  .work/<id>/                 rozpracovaná nahrávka; trvale tu zůstává state.json, lock, ui.log
  default/                    výchozí složka: vždy existuje (chybí-li, vytvoří se), nejde smazat
  <složka>/<id>/              uložená nahrávka (id = RRRRMMDD-HHMMSS) ve své složce
    manifest.json, recording.log
    transcript.txt, transcript.json     (complete false, dokud editor přepisuje; vznikají už během
                                         nahrávání v .work/<id>, pokud běží Live Transcription)
    crop.json, instructions.txt         (jen pokud je uživatel nastavil)
    audio/audio.wav                     mikrofon
    audio/computer.wav                  zvuk počítače (Windows, pokud šel nahrát)
    audio/tracks.json                   zapnuté stopy (jen pokud je uživatel přepnul)
    frames/<s>_<ms>_screenN.jpg         jen obrázky
    frames_info/<s>_<ms>_screenN.json   doprovodné JSONy
    input/mouse.jsonl                   myš, prvky pod myší, gesta (Windows, zapnuté Mouse)
    archive/                            po archivaci: screenN.mp4, .srt, .jpg, archive.json
```

Nová nahrávka se uloží do **hlavní složky** platné v okamžiku uložení (Stop, automatické zastavení, obnova po pádu); cíl se určí jednou, takže změna hlavní složky během přesunu nahrávku nerozdělí, a chybí-li hlavní složka, použije se `default`. Nahrávka se hledá podle id ve všech složkách, takže ji jde mezi složkami přesouvat (přejmenování adresáře na stejném disku). `folders.json`: `{"order": [...], "main": "default", "pinned": "default" | null, "collapsed": [...]}`; chybějící nebo poškozený soubor znamená výchozí stav, složky na disku, které soubor nezná, se přidají na konec podle abecedy. Název složky: ne prázdný, bez `<>:"/\|?*`, bez tečky na začátku i na konci a mezery na okraji, ne rezervovaná jména Windows ani `.work`, ne tvar id nahrávky, nejvýš 100 znaků a jedinečný bez ohledu na velikost písmen. Nahrávky ležící přímo v Recording Folder (dřívější plochý layout) se nečtou.

Nic se nemaže automaticky. Nahrávky starší než **Archive After** se při startu editoru archivují (kap. 8); relace s běžícím oknem se nearchivuje. Při výchozím nastavení a dvou obrazovkách zabere nahrávka zhruba 3 GB a 29 000 souborů za hodinu.

## 12. Známá omezení

- Zaznamenávají se vždy všechny obrazovky a každý zkopírovaný text; výběr obrazovek, pauza ani vypnutí záznamu schránky zatím nejsou.
- Nezměněné snímky se ukládají znovu; velikost nahrávky roste lineárně s délkou.
- MCP server přes HTTP nemá autentizaci.
- Odpojení nebo připojení monitoru během nahrávání se nepromítne do rozložení obrazovek.
- Zvuk a snímky nemají společné hodiny; odchylka bývá do půl sekundy.
- Zvuk počítače se nahrává jen na Windows. Bez sluchátek zachytí mikrofon i zvuk z reproduktorů, takže ve smíšené stopě je zvuk počítače dvakrát (s malým zpožděním). Změna výchozího výstupního zařízení během nahrávání (připojená sluchátka) se do stopy nepromítne.
- Myš se zaznamenává jen na Windows (rozhraní pro macOS a Linux je připravené, viz implementation-plan-mouse.md kap. 8). Windows zná pět tlačítek myši; další tlačítka si přemapuje software výrobce myši a jako tlačítka nedorazí. Posun a pinch na touchpadu se nad prohlížeči a moderními aplikacemi čtou jen z Precision Touchpadu; touchpad s vlastním ovladačem výrobce gesta nedá. Ve Firefoxu se místo odstavce v místě kliku čte viditelný text; aplikace bez UI Automation (např. Sublime Text) dají jen okno a proces. Prvky v oknech spuštěných jako správce Video Story bez práv správce nepřečte.
- Přepis během nahrávání drží model Whisper v paměti (zhruba 1 GB RAM) po celou dobu nahrávání a vychází vždy z obou zvukových stop; přepnutí stopy v editoru ho zahodí a přepisuje se znovu od začátku. Přepis je jen na CPU.
- Další otevřené body jsou v [video-story-review.md](video-story-review.md).
