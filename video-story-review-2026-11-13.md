# Video Story – kontrola pluginu (13. 11. 2026)

Kontrola celého pluginu: oba skilly, spouštěče, instalace, hostitelská část (CLI, MCP), nahrávání, přepis, editor, export a archivace. Hledal jsem jen zásadní chyby návrhu a implementace a výkonnostní potíže; drobnosti a nepravděpodobné okrajové případy vynechávám.

Navazuje na `video-story-review.md` z 9. 11. 2026. Co se tam uzavřelo, neopakuju; body, které tam zůstaly otevřené a jsou stále vážné, uvádím znovu s aktuálním důkazem a s původním označením (B10, B11, B15, B17, C6). Podle tehdejšího rozhodnutí vynechávám heslo v plánu, neautentizovaný MCP přes HTTP, FFmpeg z pracovního adresáře a skrývání oken před snímáním.

## Stav po opravách (13. 11. 2026)

| Nález | Stav | Co se změnilo |
| --- | --- | --- |
| D1 | vyřešeno | Export po zakódování spočítá snímky obrazové stopy zapsaného souboru (kopie proudu do nulového výstupu týmž FFmpeg, bez `ffprobe`) a při méně než `délka × 24 − 3` snímcích skončí chybou `OUTPUT_TRUNCATED` ještě před přejmenováním `.part.mp4`. Stejně skončí, když FFmpeg ohlásí chybu čtení vstupu. Archivace hotové MP4 ověří podruhé a bez úspěchu nic nesmaže. Prázdné a chybějící snímky se z plánu vyřadí s varováním (předchozí snímek zůstane déle). Ověřeno skutečným FFmpeg 7.1: zamčený snímek → chyba, 6 snímků i zvuk zůstaly. |
| H1 | vyřešeno | Spouštěče hledají kořen v pořadí `VIDEO_STORY_ROOT` → tři úrovně nad skriptem → totéž za junctionem nebo symlinkem; jinak vypíšou JSON `ROOT_NOT_FOUND` a skončí kódem 21. Ověřeno skutečným během přes junction z `D:\git` a `%TEMP%`, z Git Bashe, s mezerou v cestě a se symlinkem; cena asi 15 ms na volání. Linux ověřen ve WSL s náhradním `uv`, macOS nespuštěn. `video-story.sh` má v indexu nově příznak spustitelnosti. |
| H2 | vyřešeno | `timeline` vždy zapíše osu jako prostý text do `<nahrávka>/agent/timeline.txt` (nebo `--out`) a vrátí `timeline_file`; text je v JSON jen do 12 000 znaků (`--inline-chars`). `mouse` bez `--at` píše `agent/mouse.jsonl`. `describe` už nevrací celý přepis, `sessions` vrací krátké položky, `frames --window` nejvýš 24 snímků. MCP nástroje stránkují po řádcích (`first_line`, `max_chars`, `next_line`). Na 26min nahrávce: `timeline` 57 620 → 4 089 znaků, `describe` 44 385 → 5 083, `mouse` 204 004 → 828, `sessions` 25 764 → 6 984. |
| D2 | vyřešeno | Rozhodnutí autora: video dál obsahuje jen stopy zapnuté v editoru, ale archivace navíc každou existující stopu převede do `archive/audio.m4a` a `archive/computer.m4a` (vestavěný kodér AAC, 96 kb/s) a zkopíruje `tracks.json`. Zvuk se převádí jako první krok a zapisuje se do pole `audio` v `archive.json`. Délka každého `.m4a` se po zápisu změří a porovná s WAV (tolerance 150 ms); `audio/` se smaže, jen když prošly všechny stopy. Rozpracovaný archiv ze starší verze si zvuk dopřevede. Ověřeno skutečným FFmpeg 7.1: vypnutá stopa počítače není ve videu, ale je v `computer.m4a` v plné délce; zkrácený nebo nečitelný zvuk archivaci zastaví a nic se nesmaže. Už archivovaným nahrávkám to nepomůže. |
| D3 | vyřešeno | Rozhodnutí autora: automatická archivace zůstává bez dotazu. `archive_after_days` přijme 0 = nikdy (v Settings „Never“). Archivace, která selže dřív, než se cokoli smaže, dá nahrávce stav `failed` a nahrávka je dál běžně použitelná (důvod je ve stavovém řádku a v popisku řádku seznamu); automaticky se zkouší nejvýš třikrát, ruční Archive Video počítadlo vynuluje. Rozpracovaný archiv (smazané snímky některé obrazovky) zůstává zamčený a naváže při příštím startu. Nový příkaz **Cancel Archiving** v nabídce řádku jde použít, dokud se nic nesmazalo; nahrávka dostane stav `cancelled` a automatická archivace ji už nezařadí, ruční funguje dál. Nahrávka vyžádaná přes `edit --session` nebo žádostí agenta se v tom běhu editoru automaticky nearchivuje. Archivační proces běží s prioritou pod normálem a automatická fronta čeká, až skončí přepis nahrávky, na kterou čeká agent. Náhled přeskočí prázdný snímek uprostřed. Věta „Nic se nemaže automaticky“ ve specifikaci je zpřesněná. Archivovaná nahrávka dál nejde zpracovat (rozhodnutí autora). Zrušení ověřeno skutečným pracovním procesem a FFmpeg: běžící archivace se zastavila, snímky zůstaly, fronta pokračovala další nahrávkou a pozdější ruční archivace doběhla. |
| H3 | vyřešeno | Rozhodnutí autora: jedna složka stavu `~/.video-story` (na Windows `C:\Users\<uživatel>\.video-story`) s `config.json`, `models/` a `bin/ffmpeg/`; stejné pravidlo na všech třech systémech. Pořadí: proměnná `VIDEO_STORY_HOME` → kořen kódu, pokud v něm leží `config.json` (vývoj; celý stav zůstává u zdroje) → `~/.video-story`. `VIDEO_STORY_ROOT` znamená už jen kořen kódu. Model a FFmpeg se hledají nejdřív ve složce stavu, pak u kódu (distribuce je může přibalit); stahuje se vždy do složky stavu. Nouzové ukládání configu jinam je pryč: nezapisovatelná složka stavu skončí chybou `STATE_NOT_WRITABLE`. `doctor` a `config path` říkají, která složka se používá a proč; nový `setup --check` (kód 0 = připraveno, 4 = chybí model nebo FFmpeg) používají startovací skripty místo vlastních kontrol cest. Testy mají vždy vlastní dočasnou složku stavu. Ověřeno během: na tomto stroji beze změny (vývojový režim, stejný model i FFmpeg), s `VIDEO_STORY_HOME` se config zapisuje tam a model se najde u kódu, bez `config.json` u kódu míří stav do `~/.video-story`. Nezkoušeno: skutečná instalace z cache pluginů, stahování do nové složky, `.sh` startér na Linuxu a macOS. |
| H4 | částečně – čeká na ruční zkoušku | Napsáno podle dokumentace hostitelů, v Codexu ani Cursoru nevyzkoušeno. Instalační skripty mají přepínače `-Claude`, `-Codex`, `-Cursor`: Codex dostane odkazy v `~/.agents/skills` (starý odkaz v `~/.codex/skills` se odstraní), Cursor v `~/.cursor/skills` jen tehdy, když skill už nečte z jiné složky. Přibyl kořenový `plugin.json` ve formátu Agent Plugins, který čte Codex i Cursor; Codex si marketplace přečte ze stávajícího `.claude-plugin/marketplace.json`. MCP server se do pluginu nepřibaluje; příručka a skripty dávají hotovou konfiguraci pro Codex (`~/.codex/config.toml`) a Cursor (`~/.cursor/mcp.json`). `SKILL.md` říká, že spouštěč musí běžet mimo sandbox a že o svolení má agent požádat uživatele. Instalační příručka má přepsanou část o třech hostitelích. Postup ruční zkoušky je v `tests/manual/hosts.md`. Zjištění z dokumentace Codexu: výchozí sandbox na Windows ukončí podřízené procesy, když příkaz skončí, takže okno spuštěné uvnitř sandboxu nepřežije; jestli stačí běh mimo sandbox, ukáže až zkouška. Kód odpojování procesu se neměnil. |

Jednotkové testy: 917 prošlo (812 po první dávce D1, H1, H2; 832 po D2; 885 po D3; 901 po H3), lint čistý. Nezkoušeno: jiné verze FFmpeg než 7.1, skutečný hostitel MCP, Codex, Cursor, macOS. Zbylé nálezy jsou beze změny implementace.

**H5 (13. 11. 2026):** po rozšíření návrhu následovala uživatelem vyžádaná implementace společného startu, opravy závislostí a čekání. Nová Windows sada prošla 954 testy, ověření ve všech hostitelích ještě není úplné. Výše uvedených 917 testů je původní stav před H5. Podrobnosti jsou v [návrhu H5](docs/h5-startup-and-wait.md), [implementačním plánu](implementation-plan-h5-startup-and-wait.md) a [matici ověření](tests/manual/hosts.md).

Při opravě D1 se ukázala drobnost mimo zadání: je-li snímek nejblíž středu nahrávky prázdný, archivace skončí chybou `ARCHIVE_FAILED` při zápisu náhledu (nic se nesmaže, ale nahrávku nejde archivovat). Souvisí s D3.

## Jak číst stav ověření

| Označení | Význam |
| --- | --- |
| **změřeno** | spustil jsem to nebo změřil na skutečných nahrávkách v `~/Videos/VideoStory` (92 složek nahrávek, 46 574 sidecarů, nejdelší nahrávka 26 min) |
| **přečteno** | cesta kódem je přečtená od začátku do konce, ale nespouštěl jsem ji |
| **předpoklad** | plyne z dokumentace hostitele nebo z kódu, nešlo to tady vyzkoušet |

Nic z toho neběželo v Codexu, v Cursoru, na Linuxu ani na macOS. Vše, co se jich týká, je **přečteno** nebo **předpoklad**.

## Shrnutí – co řešit nejdřív

| # | Nález | Závažnost | Ověření |
| --- | --- | --- | --- |
| D1 | Archivace smaže snímky, aniž ověří, že MP4 obsahuje celou nahrávku | kritická | přečteno + pokus s FFmpeg |
| H1 | Spouštěč volaný přes junction nebo symlink funguje jen z pracovního adresáře uvnitř repozitáře | vysoká | změřeno |
| H2 | Výstup `timeline`, `describe` a `mouse` nemá limit; od zhruba 10 minut nahrávky ho hostitel ořízne | vysoká | změřeno |
| N1 | Stopa mikrofonu je kratší než časová osa (až 5 s za 26 min), přepis se rozchází se snímky | vysoká | změřeno |
| D2 | Archivace trvale zahodí zvukovou stopu vypnutou v editoru | vysoká | přečteno |
| H3 | Stav (config, model, FFmpeg, prostředí) žije v instalačním adresáři – instalace jako plugin ho při aktualizaci ztratí (C6) | vysoká pro distribuci | přečteno |
| H4 | Codex a Cursor: jiné cesty ke skillům, sandbox, žádná MCP konfigurace; nic není vyzkoušeno | vysoká pro cíl „běží ve třech hostitelích“ | předpoklad |
| H5 | Limity `wait`, společný start a příprava závislostí. [Rozšířený návrh](docs/h5-startup-and-wait.md) pro Claude Code, Cursor a Codex | střední až vysoká | implementováno, hostitelské ověření neúplné |
| D3 | Automatická archivace nejde vypnout ani zrušit, selhaná nahrávka zůstane zamčená | vysoká | přečteno |
| N2 | Odpojený mikrofon ani změna výstupního zařízení se nepozná | vysoká | přečteno |
| E1 | Process na jiné než čerstvé nahrávce předá agentovi tu čerstvou | střední | přečteno |
| E2 | `timeline` čte jeden soubor na každý tik; první volání na 26min nahrávce trvalo 30 s | střední | změřeno |
| P1 | Linux a macOS: chybí globální zkratky, akce myši, zvuk počítače; okna se budou chovat jinak | střední (dnes), vysoká (až na ně dojde) | přečteno |

Ostatní nálezy (N3–N6, E3–E5) jsou střední a jsou popsané níže.

---

## H. Skilly a běh v hostitelích (Claude, Codex, Cursor)

### H1 · Spouštěč přes junction funguje jen náhodou

**Závažnost:** vysoká · **Ověření:** změřeno

`skills/*/scripts/*.cmd` počítají kořen repozitáře jako `%~dp0..\..\..` a `.sh` jako `cd "$SCRIPT_DIR/../../.."`. Obojí je čistě textové. Claude Code hlásí jako adresář skillu cestu odkazu (`C:\Users\User\.claude\skills\video-story`), takže kořen vyjde `C:\Users\User\.claude`.

- Z pracovního adresáře `D:\git\video-story` příkaz projde, protože `uv` si projekt najde podle pracovního adresáře.
- Z jiného adresáře (zkoušeno z `%TEMP%`) skončí `error: Failed to spawn: video-story … program not found`, návratový kód 2.
- V přepisech relací projektu `D--git-uu-analyst-set-ai` je tatáž chyba 17krát.
- Chyba není JSON, takže porušuje slib ze `SKILL.md` („errors are a JSON object on stderr“).

Na Linuxu a macOS bude chování stejné: `cd` i `pwd` pracují ve výchozím stavu s logickou cestou.

**Návrh:** v `.sh` použít `cd -P` a `pwd -P`. V `.cmd` ověřit, že `%VS_ROOT%\pyproject.toml` existuje, a když ne, zjistit cíl odkazu (PowerShell `(Get-Item …).Target`) nebo číst kořen z proměnné `VIDEO_STORY_ROOT`, kterou už `core/config.py:83` zná. Chybu „kořen nenalezen“ vypsat jako JSON s kódem.

### H2 · Výstup pro agenta nemá limit ani stránkování

**Závažnost:** vysoká · **Ověření:** změřeno

`host/events.py:667` spojí celou časovou osu do jednoho řetězce a `cli.py:47` ho vypíše jako jeden řádek JSON. Přepínač pro zápis do souboru neexistuje.

| Příkaz (nahrávka 20261112-074257, 26 min) | Znaků |
| --- | --- |
| `timeline --session …` (oříznutý rozsah) | 57 620 |
| `describe --session …` | 44 385 |
| `mouse --session …` (bez `--at`) | 204 004 |
| `sessions` | 25 764 |

Osa roste o 2 400–3 300 znaků na minutu nahrávky. Claude Code výstup nad 30 000 znaků ořízne nebo odloží do souboru; Codex a Cursor ořezávají také. Dvouhodinová nahrávka dá zhruba 300 000 znaků. Protože je to jeden fyzický řádek, nejde odložený soubor ani číst po částech. Agent pak jedná podle neúplné osy a neví o tom.

**Návrh:** `timeline --out <soubor>` (prostý text se skutečnými konci řádků), na stdout jen cesta, počty a rozsah. Alternativně `--max-chars` s ukazatelem `next_start_ms`. Z `describe` vyřadit zdvojený přepis (`transcript` i `transcript_in_range`, `runner.py:478-488`). `mouse` bez `--at` omezit stejně.

### H3 · Stav žije v instalačním adresáři (C6, stále otevřeno)

**Závažnost:** vysoká pro distribuci · **Ověření:** přečteno; chování cache pluginů je předpoklad

`core/config.py:86` vrací jako kořen `Path(__file__).resolve().parents[3]`. V něm leží `config.json`, `models/` (548 MB), `bin/ffmpeg/` a `.venv` (374 MB). `platformdirs` se používá jen pro výchozí složku videí a jako nouzové místo pro config.

Repozitář je zároveň plugin a marketplace (`.claude-plugin/`). Instalace touto cestou zkopíruje plugin do cache s verzí v cestě. Po aktualizaci by zmizel `config.json` (složka nahrávek se vrátí na výchozí, takže nahrávky „zmizí“) a model, FFmpeg i prostředí by se stahovaly znovu. Dokumentace na to upozorňuje a doporučuje odkaz, ale tím je plugin nainstalovatelný jen jako checkout.

**Návrh:** config do `user_config_dir`, model a FFmpeg do `user_data_dir`, s jednorázovým přesunem ze starého místa. Prostředí může zůstat u kódu.

### H4 · Codex a Cursor: nic není vyzkoušeno a několik věcí nebude sedět

**Závažnost:** vysoká vzhledem k cíli · **Ověření:** předpoklad (dokumentace hostitelů, čtení skriptů)

- **Cesty ke skillům.** `scripts/install-hosts.*` umí jen `~/.claude/skills` a `~/.codex/skills`. Aktuální dokumentace Codexu uvádí uživatelské skilly v `~/.agents/skills` a `~/.codex/skills` už nezmiňuje. Cursor čte `~/.cursor/skills`, `~/.agents/skills` a pro kompatibilitu i `~/.claude/skills` a `~/.codex/skills`. Odkaz v `~/.agents/skills` by pokryl oba. Instalační příručka Cursor nezmiňuje vůbec a pro Codex odkazuje jen první skill.
- **Sandbox.** Codex i Cursor spouštějí příkazy agenta v omezeném prostředí. Skill potřebuje zapisovat mimo pracovní adresář (`.venv`, složka nahrávek), při prvním běhu síť a hlavně otevřít okno, které přežije konec příkazu. `platform/process.py:73-79` okno odpojuje (`DETACHED_PROCESS`, na POSIXu `start_new_session`), ale nepoužívá `CREATE_BREAKAWAY_FROM_JOB`. Hostitel, který úklid dělá přes job object nebo jmenný prostor procesů, okno ukončí spolu s příkazem a agent dostane `failed`. `SKILL.md` o potřebě běhu mimo sandbox neříká nic.
- **MCP jako spolehlivější cesta chybí v balení.** MCP server běží mimo sandbox příkazů a oba hostitelé ho umějí. Plugin ale nemá `.mcp.json` a dokumentace popisuje MCP jen pro Claude Desktop.
- **H1 a H2 platí i tady**, pravděpodobně ostřeji (kratší limity výstupu).

**Návrh:** vyzkoušet v obou hostitelích cestu Record → Stop → Process a zapsat výsledek do matice ověření. Do instalace přidat `~/.agents/skills` a konfiguraci MCP pro Codex (`~/.codex/config.toml`) a Cursor (`~/.cursor/mcp.json`). Do `SKILL.md` doplnit, že `run --detach` a `edit --detach` musí běžet mimo sandbox.

### H5 · Deterministické spuštění, oprava prostředí a limity čekání

**Závažnost:** střední až vysoká · **Stav:** implementováno jádro, úplné ověření hostitelů otevřené

Společné spouštěče bez automatické instalace, kontrola součástí, potvrzení připravenosti GUI, cílený setup s navazováním a výchozí wait 45 s jsou doplněny. Windows sada 954 testů prošla, Linux/WSL cíleně 32 prošlo a 2 byly přeskočeny. Skutečná instalace modelu/FFmpeg a editorové Process přes CLI v Codex desktopu proběhly. Modelové relace Claude Code s Opus 5.5 a Codex CLI se Sol Medium následně prošly editorovým CLI otevřením a běžným zavřením, nativním MCP edit/Process a kontrolou chybějícího modelu. Cursor CLI s Grokem 4.7 prošel novým otevřením, čekáním a běžným zavřením bez Process. Katalog MCP ukazuje 14 nástrojů, modelová MCP část ale zůstává neověřená kvůli nedostupnosti nástrojů v první obnovené relaci a limitu čerpání při dalším pokusu. Úplné scénáře všech tří hostitelů nejsou potvrzené. Viz [aktuální matice](tests/manual/hosts.md).

Následující popis zachycuje původní problém a dohodnutý postup před opravou:

`SKILL.md` stále předepisuje `wait … --timeout 240`. Při kratším limitu hostitelského nástroje nemusí agent dostat odpověď aplikace. MCP čeká standardně 60 sekund, takže klient s 60sekundovým limitem nemá rezervu. Nejde o selhání každého volání: relace může skončit dřív. Průběžná hlášení nahrávacího MCP čekání nenahrazují správné nastavení limitů. `setup` navíc neumí navázat přerušené stahování modelu.

Start dnes není sjednocený: skillové spouštěče předávají řízení běžnému `uv run`, kořenové spouštěče samy kontrolují a doinstalovávají model a FFmpeg. To neodpovídá požadavku na rychlé spuštění s opravou až po konkrétní chybě.

**Dohodnutý postup:** skill nejprve zavolá hotový `.bat` / `.sh` nebo dostupný lokální MCP nástroj. Spouštěč ověří uv a připravený Python bez instalace, aplikace rychle zkontroluje model a FFmpeg. Při úspěchu otevře okno, potvrdí připravenost a agent čeká. Při problému vrátí rozpoznatelnou chybu, agent informuje uživatele, cíleně opraví prostředí nebo spustí `setup` a zopakuje původní start. Modální progress bar není potřeba. Běžný start nesmí načítat celý model, kontrolovat aktualizace ani přistupovat k síti.

Úplný kontrakt, opravy, pravidla čekání a podmínky přijetí obsahuje [H5: deterministické spuštění, oprava prostředí a čekání](docs/h5-startup-and-wait.md). Návrh výslovně požaduje ověření nahrávání i editoru v **Claude Code, Cursoru a Codexu**, přes CLI i MCP. Dokumentované limity hostitelů nejsou důkazem skutečné funkčnosti. H5 se uzavře až po implementaci a těchto zkouškách.

---

## D. Ztráta dat

### D1 · Archivace maže snímky bez ověření videa

**Závažnost:** kritická · **Ověření:** přečteno + pokus s FFmpeg 7.1

Před smazáním snímků obrazovky je jediná kontrola `export/archive.py:260`:

```python
if not mp4.is_file() or mp4.stat().st_size == 0:
```

a `ffmpeg_export.py:295` zkoumá jen návratový kód. Pokus se seznamem šesti snímků ve formátu projektu, kde čtvrtý chyběl nebo měl nulovou délku: FFmpeg skončil kódem 0, video mělo 3,0 s a zvuk 6,0 s. Chybu čtení bere jako konec vstupu.

Stačí jediný snímek, který nejde otevřít (antivirus, zástupný soubor OneDrive, výpadek síťové složky), a MP4 je tiše zkrácené. Hned potom se smažou všechny snímky té obrazovky (`archive.py:274`) a na konci zvuk (`:279`). Na jednu nahrávku je to málo pravděpodobné, ale archivace běží bez dozoru nad vším starším než 30 dní a je nevratná.

**Návrh:** před označením obrazovky za hotovou porovnat délku MP4 (poslední `out_time`, který `_FfmpegRun` už čte, nebo `ffprobe`) s `duration_ms` a selhat při odchylce. Při plánování vyřadit chybějící a prázdné snímky a hlášku „Error during demuxing“ brát jako chybu.

### D2 · Archivace zahodí zvukovou stopu vypnutou v editoru

**Závažnost:** vysoká · **Ověření:** přečteno

`ffmpeg_export.py:364` bere do exportu jen stopy zapnuté v `audio/tracks.json` a archivace volá týž export. Pak `archive.py:279` smaže celý adresář `audio/`. Kdo si v editoru vypnul zvuk počítače kvůli novému přepisu, přijde po archivaci o tu stopu natrvalo; s oběma stopami vypnutými nemá archiv žádný zvuk. Stejně se do archivu vypálí kurzor podle nastavení `editor_draw_cursor`.

**Návrh:** archivace má ignorovat výběr stop a uložit všechny existující, nejlépe jako samostatné zvukové proudy.

### D3 · Automatickou archivaci nejde vypnout ani zrušit

**Závažnost:** vysoká · **Ověření:** přečteno

Že se staré nahrávky archivují při startu editoru, je rozhodnutí autora; nezpochybňuju ho. Problém je v tom, co kolem něj chybí:

- `archive_after_days` má minimum 1 (`core/config.py:40`), hodnota „nikdy“ neexistuje.
- Zařazenou nahrávku nejde otevřít (`editor/window.py:1513`) a po archivaci je Process trvale vypnutý (`:2341`). `edit --session <id>` na nahrávku starší 30 dní tedy skončí hláškou o archivaci a agent s ní už pracovat nemůže.
- Archivace, která opakovaně selhává (málo místa, chyba FFmpeg), nechá nahrávku zamčenou a každý start editoru ji kóduje znovu od začátku (`archive.py:231` maže hotové MP4).
- Pracovní proces archivace nemá sníženou prioritu (`editor/archiving.py:103-114`). Editor otevřený po Stop tak spustí whisper a x264 nad starými nahrávkami zároveň s přepisem, na který čeká Process.
- Specifikace tvrdí „Nic se nemaže automaticky“, přitom se snímky a zvuk mažou.

**Návrh:** dovolit 0 = nikdy, při startu se jednou zeptat se seznamem a uvolněným místem, přidat zrušení, dokud není hotová žádná obrazovka, selhanou nahrávku nechat upravitelnou. Archivaci spouštět s nízkou prioritou a odložit ji, dokud je některá relace ve stavu `editing`.

---

## N. Nahrávání

### N1 · Stopa mikrofonu není svázaná s hodinami nahrávky (B10, stále otevřeno)

**Závažnost:** vysoká · **Ověření:** důsledek změřen, příčina je předpoklad

Rozdíl délky `audio/audio.wav` a `recorded_duration_ms`:

| Nahrávka | Délka | Mikrofon − osa | Zvuk počítače − osa |
| --- | --- | --- | --- |
| 20261112-074257 | 26,4 min | −5 280 ms | +53 ms |
| 20261112-164651 | 5,6 min | −3 857 ms | +57 ms |
| 20261110-155714 | 5,9 min | −2 541 ms | – |
| 20261111-111340 | 12,6 min | −2 364 ms | +44 ms |
| 20261112-100631 | 6,9 min | −1 992 ms | +36 ms |
| 20261112-210902 | 6,4 min | +78 ms | +63 ms |

Ze 14 nejdelších nahrávek je mikrofon kratší u 13. Ztráta je nepravidelná (0,05–1,1 %), takže nejde o rozdíl taktů, ale o ztracené vzorky. `capture/audio_recorder.py:160-163` zapisuje bloky bez časové značky a stav přetečení loguje jen na úrovni DEBUG; počítadlo zahozených bloků přitom hlásí nulu. Pravděpodobná příčina je přetečení vstupu PortAudio, když zpětné volání čeká na GIL během kódování snímků. Stopa `computer.wav`, která se podle hodin doplňuje tichem, drží do 80 ms.

Dopad: časy v přepisu vycházejí z délky zvuku, takže ke konci nahrávky jsou věty `SAY` o sekundy dřív než snímky. Agent si pro „tohle tlačítko“ vezme snímek a polohu kurzoru z jiného okamžiku. Export do MP4 se rozchází stejně a obě stopy se míchají posunuté.

**Návrh:** zacházet s mikrofonem jako `computer_audio._write` (`computer_audio.py:234-242`): každému bloku dát čas z hodin nahrávky a chybějící vzorky doplnit tichem. Stav přetečení logovat jako WARNING a počet uložit do manifestu.

### N2 · Odpojený mikrofon a změna výstupu se nepoznají

**Závažnost:** vysoká · **Ověření:** přečteno (chování PortAudio při odpojení nezkoušeno)

- `audio_recorder.py:199-205`: vstupní proud nemá `finished_callback` a nikdo nesleduje, jestli přibývají vzorky. Odpojí-li se náhlavní souprava, okno dál ukazuje nahrávání a vyprávění tam končí.
- `computer_audio.py:168-169`: výstupní zařízení se čte jednou na začátku. Po připojení sluchátek je zbytek `computer.wav` ticho, bez chyby.

**Návrh:** hlídač (žádné nové vzorky 2 s → pokus o znovuotevření, jinak chyba přes stávající `recorder.error`). U zvuku počítače každé 1–2 s zkontrolovat výchozí výstup a smyčku otevřít znovu.

### N3 · Rozložení obrazovek se čte jen na začátku (B11, stále otevřeno)

**Závažnost:** střední · **Ověření:** přečteno

`capture/recorder.py:393` je jediné volání `list_screens()`. Po zadokování, odpojení monitoru nebo změně rozlišení uprostřed nahrávky vznikají černé či špatně oříznuté snímky, nový monitor se nenahrává a kurzor je `null`. U notebooku s dokovací stanicí to není vzácné.

**Návrh:** znovu načíst rozložení při signálech Qt `screenAdded` / `geometryChanged` a změnu zapsat do manifestu, nebo nahrávání s hláškou zastavit.

### N4 · Snímání, kódování a kreslení sdílejí jeden proces a jeden GIL

**Závažnost:** střední · **Ověření:** přečteno; velikost dopadu je předpoklad

Snímání, čtyři kódovací vlákna, kreslicí vrstva i ovládací okno běží v jednom procesu Pythonu. Dokumentace v `platform/windows_mouse_hook.py:4-6` sama uvádí naměřené zdržení až 170 ms. Důsledky:

- Pravděpodobná příčina N1.
- Tahy štětcem se při kódování snímků trhají.
- `core/config.py:27` dovoluje interval snímání 1 ms. Pod zhruba 100 ms smyčka jen vytíží jádra a počítá zahozené tiky. Fronta drží až 8 tiků syrových snímků, tedy asi 0,8–1 GB při třech 4K obrazovkách.

**Návrh:** dolní mez intervalu kolem 100 ms. JPEG ukládat rovnou do souboru (ne přes `io.BytesIO`), případně kódovat v pomocném procesu.

### N5 · Kreslicí vrstva překresluje celé obrazovky při každém pohybu myši

**Závažnost:** střední · **Ověření:** přečteno, neměřeno

`ui/overlay.py:1112` volá `update()` bez oblasti, při tažení čáry nebo šipky se překreslí všechna okna (`:2090-2091`), `paintEvent` vyplní celou plochu a vykreslí všechny tvary (`:901`). Při zvětšení se každým překreslením škáluje celý snímek obrazovky (`:918`). Guma při každém pohybu počítá obrys všech tvarů (`:550`). Na 4K se dvěma monitory a mnoha tahy to bude znát.

**Návrh:** `update(oblast)`, hotové tvary držet v pixmapě pro každé okno, obrysy pro gumu ukládat.

### N6 · Hák myši zapisuje do roury přímo ve zpětném volání

**Závažnost:** střední · **Ověření:** přečteno; v žádném logu se neprojevilo

`platform/windows_mouse_hook.py:170-171` volá `os.write` uvnitř zpětného volání nízkoúrovňového háku. Rodič čte rouru po bajtech (`windows_input.py:275`, `bufsize=0`). Roura pojme asi 80 řádků; zasekne-li se rodič na GIL během tažení, zápis se zablokuje, kurzor se zpomalí v celém systému a Windows hák po limitu tiše odebere. Po třech restartech záznam myši končí.

**Návrh:** zpětné volání jen přidá událost do fronty, zapisuje samostatné vlákno. Rodič čte po blocích.

Schránka: `ui/clipboard.py:60-63` čte celý obsah pětkrát za sekundu v GUI vlákně a nekontroluje formáty, kterými správci hesel označují obsah vyloučený ze sledování. Stačilo by reagovat na `dataChanged` nebo na číslo sekvence schránky.

---

## E. Editor a předání agentovi

### E1 · Process na jiné nahrávce předá agentovi tu, na kterou čeká

**Závažnost:** střední · **Ověření:** přečteno

Po Stop → Video Editor uživatel vybere starší nahrávku, stiskne Process a potvrdí dotaz „Do you really want to process it?“. `editor/window.py:2292-2301` pak dokončí relaci čerstvé nahrávky a `host/editor_session.py:266-269` jí dosadí její vlastní ořez a instrukce. Agent čekající na `wait --session` dostane čerstvou nahrávku a jen poznámku „Process was pressed while X was shown“. Uživatel přitom potvrdil zpracování té zobrazené.

**Návrh:** buď ve výsledku vrátit cesty, ořez a instrukce zobrazené nahrávky, nebo v dotazu říct, co se skutečně odešle.

### E2 · `timeline` čte jeden soubor na každý tik

**Závažnost:** střední · **Ověření:** změřeno

`host/events.py:131-134` otevře a rozebere každý sidecar, bez indexu a bez cache. Na nahrávce 20261112-074257 (6 332 sidecarů) trvalo první volání `timeline` 30,7 s, opakované 0,49 s. Rozdíl dělá studená souborová cache (příčinu jsem dál nerozebíral, pravděpodobně se přidává antivirus). Dvouhodinová nahrávka má 28 800 sidecarů; první volání se pak dostane přes limit příkazu z H5.

**Návrh:** při dokončení nahrávky zapsat jeden `frames_info/index.jsonl` se změnami stavu a číst ten.

### E3 · Seznam nahrávek se načítá v GUI vlákně a bez cache (B16 zčásti)

**Závažnost:** střední dnes, vysoká při stovce nahrávek · **Ověření:** přečteno + změřeno

`editor/recordings_model.py:297` volá `scan_recordings` synchronně; pro každou nahrávku čte manifest, celý přepis a prochází všechny soubory kvůli velikosti (`:251`). Obnovení se spouští i samo po každém přepisu a archivaci. Dnes (42 nahrávek, 10 GB) trvá 0,16–0,72 s. Při stovce nahrávek po 2 GB vychází odhadem 3 s s teplou cache a 10–15 s se studenou, se zamrzlým oknem.

Související: křivka zvuku se počítá znovu při každém výběru a nejde zrušit (`timeline/waveform.py:158`), při přehrávání se celá časová osa překresluje asi 30krát za sekundu v Pythonu (`timeline/view.py:917-923`) a zavření editoru čeká v GUI vlákně až desítky sekund na pracovní vlákna (`window.py:2922-2939`).

**Návrh:** načítat v pracovním vlákně, velikost a počet snímků ukládat podle času změny adresáře, měnit jednotlivé řádky místo celého modelu.

### E4 · Export závisí na sestavení FFmpeg a umí zahodit hotové video (B15, stále otevřeno)

**Závažnost:** střední · **Ověření:** přečteno; chování jiných verzí FFmpeg je předpoklad

- `ffmpeg_export.py:420-425`: když cílové MP4 drží otevřené přehrávač, přejmenování selže a `finally` smaže hotový dlouhý render.
- `:130` používá `-filter_complex_script`, které FFmpeg 7.1 hlásí jako zastaralé. Větev se použije jen u grafu nad 12 000 znaků, takže ji krátké testy neprojdou. `setup` stahuje nejnovější sestavení bez horní meze verze.
- `libx264` je napevno (`:150`) a FFmpeg z PATH má přednost. Distribuční FFmpeg bez x264 (Fedora `ffmpeg-free`) by shodil Render i archivaci, a ta pak nahrávku zamkne (D3).

**Návrh:** jednou zjistit verzi a kodéry, použít `-/filter_complex`, kde je k dispozici, a při neúspěšném přejmenování nechat `.part.mp4` na místě.

### E5 · Živost relace je holé číslo procesu a čtenář zapisuje konečný stav (B17, stále otevřeno)

**Závažnost:** střední · **Ověření:** přečteno

`platform/process.py:21-23` bere odepřený přístup k procesu jako „neběží“. `host/runner.py:176-179` pak z `wait` nebo `status` zapíše do `state.json` stav `FAILED` a pozdější Process se ignoruje (`editor_session.py:257`). V hostiteli, který příkazy spouští s jinými právy než okno, by tak čtení stavu zničilo živou relaci.

Dále: `.work` se nikdy nečistí (dnes 122 položek) a `active_session` ji prochází celou; `state.json` nemá verzi schématu a neznámý stav vyvolá výjimku (`core/session.py:358`).

**Návrh:** zámek operačního systému na relaci, jak ho už používá `host/editor_lock.py`; odepřený přístup brát jako „běží“; `status` nesmí stav měnit.

---

## P. Linux a macOS

### P1 · Co dnes chybí nebo se bude chovat jinak

**Závažnost:** střední dnes, vysoká při portaci · **Ověření:** přečteno

Kód je čistě oddělený: žádné volání specifické pro Windows není na sdílené cestě bez kontroly platformy. `platform/linux.py` a `macos.py` ale umějí jen seznam obrazovek a polohu kurzoru.

| Oblast | Stav mimo Windows |
| --- | --- |
| Globální zkratky (Stop, Keypoint, Draw) | `ui/hotkeys.py:53-58` spadne na `QShortcut`: fungují jen s fokusem na okně Video Story. Schování do lišty dál slibuje Ctrl+Alt+S |
| Akce myši, prvky pod kurzorem, touchpad | nic se nezaznamená, bez poznámky v manifestu (`platform/base.py:160-170`) |
| Zvuk počítače | nezaznamená se (`computer_audio.py:80-83`) |
| Okna na macOS | všechna jsou `Qt.Tool` bez `WA_MacAlwaysShowToolWindow`: ovládací okno zmizí, jakmile se aktivuje jiná aplikace; vrstva nepřekryje celoobrazovkové plochy |
| Okna na X11 | bez kompozitoru se průhledná vrstva vykreslí černě a nikdo to nekontroluje; ovládací okno se nedostane nad vrstvu, takže ho Sketch zakryje |
| Oprávnění macOS | kontroluje se jen nahrávání obrazovky. Mikrofon ne (odepřený nahraje ticho); budoucí záznam myši bude chtít Accessibility |
| Retina (B12) | `macos.py:145-146` odvozuje měřítko, které se nikdy neporovná se skutečnou velikostí snímku |
| Náhled v editoru | `editor/preview.py:512-519` nepočítá s poměrem pixelů zařízení: rozmazaný obraz na Retině i na Windows s měřítkem nad 100 % |
| FFmpeg | automatické stažení nemá zdroj pro macOS |
| Přepis | `use_gpu: False` je napevno; chování `pywhispercpp` na Apple Silicon a bez AVX2 není ověřeno |

Na Windows zkratka Ctrl+Space po dobu nahrávání nedojde do IDE (doplňování kódu) a AltGr+S na některých rozloženích otevře dialog Stop. Volba zkratek je rozhodnutí autora; doporučuju jen ukázat v okně, když se registrace nepovede – dnes je to jen v logu (`hotkeys.py:188-194`).

**Návrh:** než začne portace, rozhodnout, které funkce jsou na Linuxu a macOS povinné, a u chybějících to říct v manifestu i ve `SKILL.md` (dnes se v sekci Limits píše jen, že systémy „are supported“).

---

## Co je v pořádku

Aby se to nemuselo kontrolovat znovu:

- stdout je vždy jeden objekt JSON, logy jdou na stderr, výstup je UTF-8 (čeština prošla v PowerShellu i Bashi).
- `state.json`, soubory relace editoru, ořez, manifest, `folders.json` a stav archivu se zapisují atomicky a čtou s opakováním.
- Snímky, myš, touchpad a kresby sdílejí jedny monotónní hodiny; plán tiků je absolutní, bez driftu.
- WAV je po pádu čitelný (hlavička se opravuje po každém bloku), manifest vzniká při Record, obnova osiřelých nahrávek je opakovatelná i ze dvou procesů.
- UI Automation běží v pomocném procesu s časovými limity; samotný hák nedělá UIA ani souborové operace.
- Živý přepis: jeden proces, jeden model, nízká priorita, ukončí se při Stop i při pádu okna.
- Export píše do `.part.mp4`, roury FFmpeg se čtou ve vláknech, zrušení proces ukončí a soubor uklidí.
- Editor nenačítá sidecary ani celý WAV do paměti; cache náhledů a miniatur jsou omezené.
- Stažení modelu se ověřuje přes SHA-256.
- Jediná instance editoru je hlídaná zámkem operačního systému.

## Dokumentace

`docs/installation.md` popisuje stav před posledními změnami: tlačítko **Delete All Recordings** (odstraněno), nahrávky přímo ve složce nahrávek (dnes `<složka>/<id>/`), klíče `mouse_actions` a `live_transcription` v tabulce konfigurace chybějí, Codex má odkaz jen na první skill. Není to chyba běhu, ale instalace podle příručky pro Codex neprojde celá.

## Doporučené pořadí

1. **D1, D2** – archivace nesmí nic smazat, dokud neověří délku videa a neuloží všechny stopy. Malá změna, nevratný dopad.
2. **H1** – oprava spouštěčů; bez ní skill mimo repozitář nefunguje v žádném hostiteli.
3. **H2 + E2** – výstup do souboru a index sidecarů; týká se každé nahrávky nad 10 minut.
4. **N1, N2** – mikrofon na hodinách nahrávky a hlídání zařízení.
5. **H4, H5**: společný rychlý start, oprava závislostí agentem, limity čekání a zkouška v Claude Code, Cursoru a Codexu přes CLI i MCP podle [rozšířeného návrhu H5](docs/h5-startup-and-wait.md).
6. **H3** – přesun stavu z instalačního adresáře, než se plugin začne distribuovat.
7. **D3, E1, N3** a zbytek podle potřeby; **P1** jako podklad pro plán portace.

## Zdroje k hostitelům

- Cursor, Agent Skills: <https://cursor.com/docs/context/skills>
- Codex, skilly: <https://learn.chatgpt.com/docs/build-skills>
