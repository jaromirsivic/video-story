# Video Story – specifikace

Stav: **platný popis aktuálního chování aplikace** (9. 11. 2026). Tento dokument se udržuje spolu s kódem; když se chování změní, mění se i tady. Nahrazuje původní zadání v1 a rozšíření v2 ([video-story-spec-v2.md](video-story-spec-v2.md), ponecháno jako historický záznam). Implementační plány ([implementation-plan.md](implementation-plan.md), [implementation-plan-editor.md](implementation-plan-editor.md)) popisují, jak návrh vznikal, a tam, kde se s tímto dokumentem liší, neplatí. Ověření je v [tests/manual/acceptance.md](tests/manual/acceptance.md).

## 1. Účel

Video Story je plugin se dvěma skilly (`video-story`, `video-story-edit`) pro AI agenty. Uživatel při práci na počítači nahlas popisuje, co dělá nebo co chce udělat. Plugin nahrává mikrofon, v pravidelném intervalu snímá všechny obrazovky a zaznamenává polohu kurzoru, kresby na obrazovce, zkopírované texty a označené okamžiky. Zvuk přepíše lokálně a uživatel pak v editoru rozhodne, kterou část nahrávky a s jakými pokyny agentovi předá.

Celý plugin (rozhraní, hlášky, skilly, kód) je anglicky. Přepis zachovává jazyk řeči. Tato specifikace a plány jsou česky.

## 2. Základní pravidla

1. **Agent jedná jen po stisku Process.** Nahrávku agentovi předá výhradně zelené tlačítko **Process** v editoru, ať ho stiskne uživatel, nebo odpočet **Auto process**. Každý jiný konec (zavření editoru, pád editoru, editor se neotevřel, pád nahrávacího okna) znamená „nahrávka je uložená, nic nedělej“.
2. **Nahrávka se neztrácí.** Ať nahrávání skončí jakkoli, nahrávka se uloží a otevře v editoru. Pokud okno spadne dřív, dokončí ji příští spuštění.
3. **Nahrávání se nikdy nepozastavuje.** Časová osa je souvislá od stisku Record do konce; při otevřeném dialogu Stop se nahrává dál.
4. **Nahrávání a přepis běží lokálně.** To, co si agent z výsledku přečte (přepis, zkopírované texty, snímky, které otevře), putuje k modelu, na kterém agent běží, a pokud je to vzdálený model, opouští počítač.

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
| **Draw** (`Ctrl+Alt+D`) | Zapne kreslení (kap. 6), opětovným stiskem ho vypne. Jen během nahrávání. |

Klávesové zkratky Stop, Keypoint a Draw platí jen během nahrávání; tlačítka je ukazují v závorce za názvem i v tooltipu. Na Windows fungují v celém systému (ať je vpředu kterákoli aplikace, stisk do ní nedojde), na Linuxu a macOS jen když má fokus okno Video Story. Kombinaci, kterou už zabrala jiná aplikace, Video Story použije jen ve svých oknech. Na klávesnicích s AltGr ji Windows hlásí jako Ctrl+Alt, takže AltGr+S/K/D během nahrávání zkratku také spustí.

Stavový řádek ukazuje uplynulý čas, čas do automatického zastavení, počet obrazovek, snímků a keypointů. Zavření okna před nahráváním ukončí relaci stavem `cancelled`.

## 5. Nastavení

Dialog **Settings** nabízí položky v tomto pořadí; ostatní klíče `config.json` se mění jen příkazem `config set`.

| Položka | Klíč | Výchozí | Rozsah |
| --- | --- | --- | --- |
| Recording Folder | `recording_folder` | `~/Videos/VideoStory` (macOS `~/Movies/VideoStory`) | – |
| Capture Interval | `capture_interval_ms` | 500 ms | 1–1000 |
| Image Format | `image_format` | JPEG | JPEG, PNG |
| JPEG Quality | `jpeg_quality` | 70 % | 20–100, jen pro JPEG |
| Audio Input | `audio_input` | systémový výchozí vstup | – |
| Auto Stop Recording | `auto_stop_minutes` | 120 min | 1–1440 |
| Archive After | `archive_after_days` | 30 dní | 1–3650 |
| Recordings | – | **Open Folder**, **Delete All Recordings** (s potvrzením; relace otevřeného okna se nemaže) | – |

Změna **Recording Folder** platí okamžitě i pro už otevřené okno: jeho relace se přesune do nové složky, takže nahrávka, editor i čekající agent pracují s novou složkou. Během nahrávání nastavení otevřít nelze.

`config.json` leží v kořeni instalace pluginu (mimo Git); pokud tam nejde zapisovat, použije se uživatelský konfigurační adresář.

## 6. Co se zaznamenává

- **Zvuk:** `audio/audio.wav`, 16bit PCM, mono, vzorkovací frekvence zařízení.
- **Obrazovky:** v každém intervalu snímek každé obrazovky, i nezměněný. Název `<sekundy>_<ms>_screenN.<jpg|png>`, čas od začátku nahrávky. `screen1` je primární obrazovka, další jsou řazené podle polohy.
- **Doprovodný JSON** ke každému snímku (stejný název s příponou `.json`, ve složce `frames_info/`; ve `frames/` jsou jen obrázky): obrazovka a poloha kurzoru v pixelech jejího snímku (počátek vlevo nahoře), dokončené kresby, právě kreslený tvar, zoom, zkopírovaný text.
- **Kresby:** obdélník (výchozí nástroj), štětec, šipka; pravé tlačítko myši kreslí vždy šipku; 8 barev, výchozí červená. `Esc` kreslení opustí, `Ctrl+Z` vrátí, `Ctrl+Alt+C` smaže vše. Kolečko myši při kreslení přiblíží obrazovku až na 800 %. Kresba je vidět ve snímcích; do JSONu se tvar dostane až po puštění tlačítka myši, s identifikátorem a časy stisku a puštění. Nahrávací okno zůstává nad kreslicí vrstvou.
- **Zkopírovaný text:** každý nový text zkopírovaný během nahrávání (nejvýš 65 535 znaků) se zapíše jednou, do JSONů nejbližšího snímku. Co bylo ve schránce před nahráváním, se neukládá.
- **Keypointy:** okamžiky označené tlačítkem Keypoint (`Ctrl+Alt+K`); ukládají se do `manifest.json` hned při vložení.
- **Manifest:** `manifest.json` vzniká při stisku Record (obrazovky, nastavení, čas začátku) a doplňuje se při uložení. Pole `stop_reason` říká, proč nahrávka skončila sama (`auto_stop`, `low_disk`, `capture_failed`, `recovered`), `frames.failed_grabs` počet nepořízených snímků.

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

V nahrávacím okně se nic nepřepisuje. Pokud se editor nepodaří otevřít, nahrávka zůstane uložená a relace skončí stavem `cancelled`.

## 8. Editor

Editor se otevře po nahrávání, nebo samostatně pokynem „Edit Video Story“ (`edit --detach`). Běží nejvýš jeden; další žádost ho jen zvedne a načte do něj požadovanou nahrávku. Při startu editor dokončí nahrávky, jejichž okno spadlo (kap. 10).

- **Seznam nahrávek** vlevo, nejnovější nahoře a vybraná. Řádek ukazuje náhled, datum, délku s velikostí složky nahrávky v závorce (KB, MB, GB nebo TB na jedno desetinné místo, např. `00:27 (12.3 MB)`) a začátek řeči; pohyb myší po řádku nahrávkou projíždí. Kontextová nabídka: **Open Folder Location**, **Archive Video** / **Archive Videos (N)**, **Delete Video** / **Delete Videos (N)** (N = počet vybraných nahrávek; u archivace jen těch, které ještě archivovat jdou). Na náhledu řádku je štítek (černý text na bílém): `TRANSCRIBING...` a `ARCHIVING...` s procenty na jedno desetinné místo, `ARCHIVED` u archivované nahrávky.
- **Vyhledávání** pod seznamem: textové pole, vedle něj tlačítko s lupou a tlačítko **Refresh** jen s ikonou kruhové šipky (F5). Enter nebo lupa hledá slovo či frázi přesně, jak je napsaná, ale bez ohledu na velká/malá písmena a diakritiku (`prilis` najde `příliš`) v titulcích (přepisu). Nejdřív se prohledá zobrazená nahrávka (její odpovídající obdélníčky na stopě narration hned zežloutnou), potom ostatní ve vlákně na pozadí; každá nahrávka s nálezem dostane v seznamu žlutý rámeček a po kliknutí na ni jsou žluté její odpovídající obdélníčky. Vymazání pole hledání zruší. Fráze se hledá v rámci jednoho titulku.
- **Náhled** s transportem (na In, krok zpět, přehrát, stop, krok vpřed, na Out; Stop se vrací tam, kde bylo naposledy stisknuto Play), titulky pod náhledem, kolečko myši přibližuje až na 800 %, přiblížený obraz se posouvá tažením levým i prostředním tlačítkem, přepínač světlého a tmavého vzhledu. Plocha kolem snímku nese nevýrazný diagonální vodoznak `screenN ŠxV` (opakovaný), barevně odlišený podle obrazovky: screen1 šedá, screen2 modrá, screen3 zelená, screen4 červená, screen5 znovu šedá atd. (tmavší text na světlém podkladu; v tmavém motivu je modrá screen2 světlejší než ostatní, aby nesplývala s černou).
- **Časová osa:** jedna stopa na obrazovku s miniaturami, jedna zvuková stopa, pod ní zamčená stopa **narration** (zámeček v hlavičce, poloviční výška zvukové stopy, nejde upravovat ani posouvat): obdélníček všude, kde titulek obsahuje text, v pauzách nic; od přiblížení 40 px/s ukazují obdélníčky text titulku zkrácený trojtečkou, najetí myší ukáže celý text, zoom `-`/posuvník/`+` u pravého okraje (bez tlačítka Fit; levý konec posuvníku je celá nahrávka, `Ctrl+0` tam skočí). Od náhledu ji odděluje tenká linka (táhlo rozdělovače). Kliknutí nebo tažení červené čáry po puštění tlačítka myši přesune pod ni i tmavý trojúhelník na pravítku (místo, odkud Play začne a kam se Stop vrací). Úchyty **In/Out** (nebo klávesy `I`/`O`; pole In, Out a délka ukazují `HH:MM:SS.ss`) nastavují crop společný pro všechny obrazovky; ukládá se do `crop.json` při každé změně a přichytává se ke keypointům. Kliknutí na stopu vybírá obrazovku pro náhled a export.
- **Přepis** běží na pozadí editoru (Whisper large-v3-turbo přes whisper.cpp, CPU, automatický jazyk). Zelená šipka pod stopou narration ukazuje postup (ve světlém motivu tmavší zelená) a odhad zbývajícího času, titulky přibývají průběžně. Zavření editoru přepis přeruší, příští výběr nahrávky naváže. Když přepis selže, tlačítko **Retry transcription** ho spustí znovu od místa, kde skončil. Halucinované titulkové kredity se zahazují.
- **Edit** opraví text titulku (časy zůstávají); změna se zapíše do `transcript.json` i `transcript.txt`.
- **Instructions:** hlavní pokyny pro agenta k celé nahrávce, uložené v `instructions.txt`; šablony Draft Intent, Create Plan a Implement.
- **Render:** export vybrané obrazovky a cropu do MP4 (H.264 + AAC, 24 fps, rozlišení obrazovky, kurzor dokreslen) a SRT vedle něj. Editor zůstává otevřený.
- **Process:** předá agentovi crop a pokyny a editor zavře (kap. 9).

Render a Process jsou dostupné, až když je přepis zobrazené nahrávky úplný (nebo definitivně selhal).

Stavový řádek editoru ukazuje id nahrávky a vybranou obrazovku s rozlišením, během přepisu i jeho postup; crop ani stav vůči agentovi v něm nejsou.

### Archivace

- **Archive Video(s)** se po potvrzení („Do you want to archive this video?“ s cestou, u více nahrávek jen počet) zařadí do fronty; archivovat jde jen nahrávku s úplným přepisem. Od té chvíle nahrávku nejde upravovat: vpravo je jen velký text „This video is being archived.“ s procenty.
- Fronta běží v editoru na pozadí, jedna nahrávka po druhé (`archive-stream`). Každá obrazovka se převede na `archive/screenN.mp4` (celá nahrávka, plné audio, stejně jako Render) se `screenN.srt` a `screenN.jpg` (snímek z prostředku nahrávky); hotová obrazovka se zapíše do `archive/archive.json` a hned se smažou její snímky. Po poslední obrazovce se smaže `audio/` a zbytek `frames/`; `frames_info/`, přepis, manifest, crop a pokyny zůstávají.
- Zavřený editor archivaci přeruší čistě (FFmpeg se zastaví, rozdělané soubory se smažou); další start editoru naváže: hotové obrazovky přeskočí, rozdělanou udělá znovu.
- Soubory, které nejde smazat (drží je jiný program), zůstanou; archivace přesto skončí jako hotová a příští start editoru zkusí smazat zbytek ještě jednou.
- Při startu editoru se do fronty zařadí i nahrávky starší než **Archive After**; pokud nemají úplný přepis, nejdřív se přepíšou.
- **Archivovaná nahrávka** ukazuje v náhledu i na časové ose `screenN.jpg`, uprostřed náhledu bliká štítek `ARCHIVED` (černý text na bílém jako na náhledu v seznamu, sekundu svítí, sekundu ne), zvuková stopa je jen čára (bez přepínače roztažení), Play nehraje zvuk a mění se jen titulky. Edit titulků funguje, crop také; Auto process, Process, Instructions a Render jsou zakázané.

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

Po `processed` agent pracuje nejdřív s textem: přečte `instructions`, pak `timeline` (řeč s polohou kurzoru, kresby s geometrií a časy, zkopírované texty, přechody kurzoru mezi obrazovkami, zoom, keypointy – vše v rozsahu cropu) a snímky si přes `frames --at <ms>` vyžádá jen pro okamžiky, které z textu nejdou rozhodnout. Přesný postup je ve `skills/video-story/SKILL.md`.

Další příkazy: `describe`, `status`, `sessions`, `transcribe`, `export`, `config`, `devices`, `screens`, `doctor`, `setup`, `mcp`. MCP server nabízí tytéž funkce jako nástroje `video_story_*`.

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

## 11. Uložení a archivace

```
<Recording Folder>/
  .work/<id>/                 rozpracovaná nahrávka; trvale tu zůstává state.json, lock, ui.log
  <id>/                       uložená nahrávka (id = RRRRMMDD-HHMMSS)
    manifest.json, recording.log
    transcript.txt, transcript.json     (complete false, dokud editor přepisuje)
    crop.json, instructions.txt         (jen pokud je uživatel nastavil)
    audio/audio.wav
    frames/<s>_<ms>_screenN.jpg         jen obrázky
    frames_info/<s>_<ms>_screenN.json   doprovodné JSONy
    archive/                            po archivaci: screenN.mp4, .srt, .jpg, archive.json
```

Nic se nemaže automaticky. Nahrávky starší než **Archive After** se při startu editoru archivují (kap. 8); relace s běžícím oknem se nearchivuje. Při výchozím nastavení a dvou obrazovkách zabere nahrávka zhruba 3 GB a 29 000 souborů za hodinu.

## 12. Známá omezení

- Zaznamenávají se vždy všechny obrazovky a každý zkopírovaný text; výběr obrazovek, pauza ani vypnutí záznamu schránky zatím nejsou.
- Nezměněné snímky se ukládají znovu; velikost nahrávky roste lineárně s délkou.
- MCP server přes HTTP nemá autentizaci.
- Odpojení nebo připojení monitoru během nahrávání se nepromítne do rozložení obrazovek.
- Zvuk a snímky nemají společné hodiny; odchylka bývá do půl sekundy.
- Další otevřené body jsou v [video-story-review.md](video-story-review.md).
