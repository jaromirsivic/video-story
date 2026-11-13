# Implementační plán H5: spuštění, oprava prostředí a čekání

Datum: 13. 11. 2026. Verze plánu: 1. Stav: **implementováno jádro H5-1 až H5-6, místní ověření provedeno, H5-8 neúplný**.

Průběh realizace 13. 11. 2026: H5-0 má výchozí i následné měření GUI v oddělené složce. H5-1 až H5-6 mají cílené testy. H5-7: celá Windows sada 954 testů prošla, následovaly cílené kontroly oprav a přerušeného přenosu. Ubuntu/WSL: 32 testů prošlo, dva Windows specifické byly přeskočeny. Model i FFmpeg byly skutečně staženy do vlastní testovací složky. H5-8: po výslovném povolení uživatele proběhly modelové testy Claude Code s Opus 5.5 a Codex CLI se Sol Medium. Prošly editorové cesty CLI i nativní MCP a negativní kontrola chybějícího modelu. Cursor s výslovně schváleným Grokem 4.7 prošel novým CLI otevřením, čekáním a běžným zavřením bez Process. Jeho nativní MCP katalog má 14 nástrojů, ale modelové MCP edit/Process zůstává neověřené: první obnovená relace nástroje neviděla a další pokus zablokoval limit čerpání Cursoru. H5-9: proveden souvislý průchod diffem a cílené opravy. Testovací agenti neměnili zdroje. Úplné scénáře zůstávají otevřené, podrobnosti a důkazy jsou v [matici hostitelů](tests/manual/hosts.md).

Závazný zdroj: [H5: deterministické spuštění, oprava prostředí a čekání](docs/h5-startup-and-wait.md). Kontext nálezu: [review pluginu](video-story-review-2026-11-13.md). Plán zachovává opravy H1 a H3 a navazuje na ověření hostitelů z H4. Pokud se při realizaci objeví podstatný rozpor se zdrojem, vyjasnit jej před navazující prací.

Tento soubor vznikl jako plán. Následný výslovný pokyn uživatele autorizoval implementaci. Popis kroků níže zachovává původní zadání a není tvrzením, že zbývající zkoušky proběhly. Stávající rozpracované změny v review a zdrojovém návrhu byly zachovány.

## 1. Výsledek a hranice

První akce skillu je spuštění hotového `.bat` / `.sh` nebo dostupného lokálního MCP nástroje. Připravené prostředí otevře nahrávání či editor bez synchronizace, stahování a předchozí diagnostiky agentem. Chybí-li součást, vrátí se rozpoznatelná chyba. Agent oznámí opravu, provede ji podle skillu a zopakuje původní požadavek. Po potvrzení připraveného okna čeká na Process nebo ukončení relace.

Součástí výsledku jsou:

- Společný postup pro nahrávání, editor, připojení k otevřenému editoru a MCP.
- Opravy prostředí přes uv, instalace modelu a FFmpeg přes aplikační `setup`, navázání přerušeného stahování.
- Kratší výchozí čekání a správné zacházení s limity i úlohami hostitele na pozadí.
- Skutečné ověření v Claude Code, Cursoru a Codexu pro CLI i MCP. Pouhé jednotkové testy ani načtení seznamu MCP nástrojů nestačí.

Nevytvářet instalační progress dialog, nový obecný správce úloh ani tři hostitelské varianty aplikace. Neměnit pravidlo „agent jedná až po Process“, archivaci ani obsah nahrávek. Nepřidávat do H5 opravy nahrávání z N1–N6 nebo úplnou portaci z P1. Nízkou úroveň `record --seconds ...` používanou CI nerozšiřovat o požadavek modelu, pokud pouze pořizuje testovací záznam bez přepisu.

## 2. Zjištění v aktuálním kódu

| Místo | Důsledek pro realizaci |
| --- | --- |
| `skills/*/scripts/*.cmd`, `*.sh` | Kontrolují uv a spouštějí běžné `uv run`. Nahradit implicitní přípravu prostředí deterministickým startem |
| `start-video-story.bat`, `.sh` | Volají samostatné `setup --check` a někdy `setup`. Odstranit tuto odlišnou instalační větev |
| `host/cli.py::setup_status` | Základ rychlé kontroly už existuje. Oddělit jej od CLI, doplnit potřebná metadata a sdílet |
| `host/cli.py::cmd_setup`, `host/doctor.py::run_doctor` | Setup dnes zjišťuje i obrazovky a zvuk. Instalaci oddělit od plné diagnostiky, aby problém mikrofonu nezablokoval opravu modelu |
| `host/runner.py::launch_control_window` | Rodič zapíše READY před startem GUI a po spawn znovu přepíše stav PID. Přidat skutečné potvrzení GUI a odstranit možnost přepsání novějšího stavu dítěte |
| `host/controller.py::SessionController.__init__` | Zapisuje READY ještě před vytvořením okna. Stav relace sám o sobě není potvrzení připraveného GUI |
| `host/editor_launcher.py::launch_editor` | Zapisuje `open` hned po spawn nebo odeslání attach požadavku. Nečeká na přijetí požadavku editorem |
| `editor/window.py::check_attach_requests`, `attach`, `_on_loaded` | Attach může čekat na dialog nebo načtení nahrávky. Potvrzení musí patřit konkrétnímu požadavku a vybrané nahrávce |
| `ui/control_window.py::_on_processed` | Volá `launch_editor` v GUI vlákně. Nové čekání na připravenost tam nesmí okno zablokovat |
| `host/mcp_server.py` | Výchozí čekání 60 s, nahrávání má async polling s progress, editor používá blokující funkci ve vlákně |
| `host/downloads.py`, `transcribe/ffmpeg.py::download_ffmpeg` | `.part` se přepisuje od začátku. FFmpeg navíc používá `TemporaryDirectory`, který nelze využít při dalším spuštění |
| `tests/conftest.py` | Izoluje složku stavu, ale stále dovoluje najít model v checkoutu. Test chybějících součástí musí izolovat i kořen kódu a PATH |
| `scripts/install-hosts.*`, `docs/installation.md` | MCP příklady stále používají běžné `uv run`. Je nutné změnit i konfiguraci startu serveru |

## 3. Rozhodnutí pro implementaci

### 3.1 Spouštěč a připravené prostředí

Zvolit přímý start Pythonu z `<kořen-kódu>/.venv`, na Windows `Scripts/python.exe`, na POSIX `bin/python`. Spouštěč nejprve ověří dostupnost uv, ale při běžném startu uv nespouští k synchronizaci. Ignorovat cizí aktivované virtuální prostředí. Zachovat `VIDEO_STORY_ROOT` a pravidla H1 pro odkazované skilly.

Přidat malý `scripts/bootstrap.py` závislý jen na standardní knihovně. Musí být spustitelný i při chybějícím balíčku `video_story`. Ověří verzi interpretu vůči projektovým požadavkům, dostupnost hlavního balíčku a předá řízení jeho CLI. Skutečné spuštění interpretu je současně jeho kontrola, nedělat před ním další proces jen pro `python --version`. Nepřítomný interpret i jeho selhání před vstupem do bootstrapu musí shell převést na čitelnou chybu bez nutnosti dalšího Pythonu.

Sdílený shellový vstup umí obecné podřízené příkazy, tedy i `setup`, `status`, `wait` a `mcp`. Skillové `.bat` / `.sh` a kořenové spouštěče jsou jeho tenké obálky. Existující `.cmd` zůstanou kompatibilní. Výchozí otevření nahrávání versus editoru řeší argumenty, instalační logika se nekopíruje. Zejména `setup` nesmí být blokován tím, že nemá model, který má právě nainstalovat.

`pyproject.toml` je zdroj podporovaného Pythonu. Nyní platí `>=3.12,<3.14`. Opravná větev může zvolit Python 3.12, ale nesmí bez kontroly stáhnout nejnovější nepodporovanou verzi. Pro knihovny použít `uv sync --locked --project <kořen>` a cílové prostředí stejné jako ve spouštěči. Předávat argumenty jako argumenty, nikdy neskládat příkaz z chybové zprávy. `--locked` při nesouladu lockfile končí chybou místo jeho aktualizace, viz [uv: locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/).

### 3.2 Jedna kontrola aplikačních součástí

Přidat malý modul `host/readiness.py`. Vrátí nalezené cesty a strukturované problémy, nic nestahuje ani nezapisuje. Použije stávající hledání z `core/config.py` a `transcribe/ffmpeg.py`. `setup --check` bude jeho adaptér, ne další implementace kontroly.

Kontrolovat přítomnost a dostupnost modelu, u standardního modelu známou velikost. Očekávaný hash a velikost standardního modelu přesunout z těžšího modulu `doctor` do lehkého společného místa. Vlastní model z explicitní konfigurace nesmí být odmítnut pouze odlišnou velikostí ani přepsán distribuovaným modelem. FFmpeg hledat ve stávajícím pořadí, na POSIX kontrolovat spustitelnost. Neprovádět hashování, spuštění FFmpeg ani inicializaci Whisperu v běžném startu.

Známé chybějící knihovny rozlišovat pomocí nezbytných importů a levného hledání kritických modulů. Neoznačovat jakýkoli `ImportError`, chybu vlastního kódu nebo nedostupný mikrofon za důvod přeinstalovat prostředí. Při volbě `whisper-cli` respektovat tuto konfiguraci místo požadavku na nepoužitý backend.

### 3.3 Chyby a přenos výsledku

| Výsledek | Návratový kód CLI | Povinná informace |
| --- | --- | --- |
| `ok: true` | 0 | Identifikátor relace, PID, způsob připojení a další krok čekání |
| `SETUP_REQUIRED` | 4 | `missing` a seznam problémů s komponentou, důvodem a relevantní cestou |
| `UV_MISSING` | 20, zachovat | Uv není dostupné |
| `ROOT_NOT_FOUND` | 21, zachovat | Spouštěč nenašel platný kořen |
| `PYTHON_MISSING` | 22, nový | Interpret prostředí chybí nebo jej nelze spustit |
| `ENVIRONMENT_INVALID` | 23, nový | Nevyhovující verze či známá chybějící knihovna |
| `START_FAILED` | 24, nový | Konkrétní důvod, log a dostupné identifikátory pro zjištění výsledku |

Dosavadní doménové chyby a kódy zachovat. `NO_RECORDINGS`, `CONFIG_INVALID`, `STATE_NOT_WRITABLE`, zamítnutá oprávnění ani obsazené okno nejsou instalační chyba. Rozšířit `CliError` a MCP převod tak, aby se neztratilo pole `missing` nebo podrobný důvod. Úspěch CLI jde na stdout, chybový JSON a diagnostika na stderr. Na stdout stdio MCP nesmí proniknout text spouštěče, diagnostika ani běžný CLI výsledek.

### 3.4 Potvrzení skutečného startu

Použít malý protokol přes existující atomický zápis souborů ve `.work`. Každý start či attach má vlastní token, požadovaný režim a relaci, konečný termín a samostatnou odpověď dítěte `ready` nebo `error`. Podklady požadavku zapisuje rodič, potvrzení vlastní GUI. Nepoužívat více zapisovatelů jednoho startup stavu a nepřepisovat naslepo `state.json` ze staré kopie rodiče.

Výchozí limit potvrzení je 30 sekund. Nečekat pevně 30 sekund, vrátit výsledek ihned po potvrzení. Limit upravit pouze podle výsledku měření, vždy pod limitem startovacího nástroje. Nahrávání potvrdí viditelné okno a běžící Qt smyčku. Editor potvrdí okno a načtenou požadovanou nahrávku, bez čekání na přepis či dokončení úloh na pozadí. U attach potvrdí požadavek stávající editor, nikoli odesílající proces.

Chybějící závislosti zjistit ještě před vytvořením tohoto požadavku, relace, recovery nebo nového okna. Předání výsledku kontroly dítěti je interní součást konkrétního požadavku, nikoli veřejný přepínač `--skip-check`. Přímé spuštění GUI bez takového kontextu vykoná stejnou kontrolu samo. Základní identitu konfigurace zachovat v celém startu, ať se dítě kvůli jinému cwd nepřepne na jinou konfiguraci.

Po timeoutu odpovědět `START_FAILED` s důvodem `readiness_timeout` a dostupnými identifikátory, nikoli znovu instalovat. GUI nesmí dodatečně převzít již prošlý attach požadavek. Před jeho převzetím i před odesláním potvrzení zkontroluje platnost požadavku. Pokud už uživatel s oknem pracuje nebo vznikla nahrávka, nic automaticky nezavírat ani nemazat. Nový proces, který selhal ještě před zpřístupněním okna, bezpečně ukončit v jeho vlastní chybové větvi. Běžící sdílený editor se kvůli neúspěšnému attach nikdy neukončuje.

Při přerušení samotného nástroje agent nejdřív vyzvedne stav úlohy nebo dostupnou relaci. Spouštěč musí rozeznat probíhající start a existující okno pomocí relace a zámku, aby opakování nevytvořilo duplicitu. Soubory požadavku a odpovědi se ponechají dost dlouho pro vyhodnocení výsledku, následně se uklidí jen pro dokončený požadavek.

### 3.5 Instalace a čekání

`setup` se oddělí od úplného `doctor`. Má opravit model a FFmpeg, vrátit přehled a skončit. Sken obrazovek, mikrofonu a myši zůstane ve výslovné diagnostice. Důkladné ověření součásti po stažení je povinné. Použitelný systémový nebo explicitně nastavený FFmpeg ani vlastní model se nesmí plošně přepsat.

Výchozí `wait` bude 45 sekund v CLI i MCP, společná konstanta pro obě relace. Menší kladnou hodnotu dovolovat i pod jednu sekundu pro hostitele s krátkým limitem. Zachovat neblokující CLI kontrolu s timeoutem 0. Nečíselné, záporné či nekonečné hodnoty řešit explicitně a konzistentně, ne tiše změnit na dlouhé čekání. Výslovně delší čekání zůstane jen pro klienta, který nastaví dostatečný limit. MCP horní mez 240 sekund není záruka podpory hostitelem.

MCP starty s čekáním na GUI i čekání na editor nesmějí blokovat smyčku serveru. Volání `mcp` pouze inicializuje server, připravenost modelu a FFmpeg kontrolují až nástroje pro otevření okna. Instalaci agent provede přes lokální CLI, nevytvářet nový MCP správce instalačních úloh. Selhání jednoho startu nesmí ukončit server.

## 4. Kroky realizace

Kroky provádět v tomto pořadí. Každý má vlastní výstup a kontrolu před pokračováním. Pracovat v aktivní feature větvi, zachovat existující změny a nevytvářet nový worktree bez pokynu.

### H5-0: Výchozí měření a testovací prostředí

**Práce:** zaznamenat commit, čistotu relevantních souborů, verzi Pythonu, uv a hostitelů. Připravit oddělenou složku nahrávek a stavu s vypnutou automatickou archivací. Pořídit malou referenční nahrávku pro editor. Změřit současné spuštění obou oken a rozlišit návrat CLI od skutečného zobrazení. Nevydávat dnešní rychlý návrat po spawn za rychlost připraveného GUI.

**Ověření:** nejméně jeden studený a deset opakovaných startů obou oken na stejném zařízení. Zapsat medián a nejpomalejší opakovaný start, nikoli neprokázané tvrzení o přesném limitu. Reálná nahrávka a zvuk se používají až při realizaci a zkouškách.

**Výstup:** výchozí záznam do `tests/results/h5/`, dostupné platformy a hostitelské varianty, všechny další kroky zatím nehotové.

### H5-1: Společná kontrola a kontrakt chyb

**Soubory:** nový `host/readiness.py`, případně lehký modul metadat modelu, `core/config.py`, `transcribe/ffmpeg.py`, `host/cli.py`, `host/doctor.py`, `host/mcp_server.py`.

**Práce:** implementovat kontrolu podle kapitoly 3.2, chyby podle 3.3 a převod výsledku do CLI/MCP. Zapojit `setup --check`, zachovat jeho stávající `ok`, `missing`, cesty a exit 4. Tím zatím neměnit samotné spouštění GUI.

**Ověření:** nové cílené testy `test_readiness.py`, rozšíření `test_cli_output.py`, `test_mcp_server.py`, `test_state_root.py` a testů FFmpeg. Otestovat model chybějící, prázdný, zkrácený, vlastní model, FFmpeg v konfiguraci/PATH/stavu, chybějící obě součásti, nedostupnou cestu a neplatnou konfiguraci. Testy musí selhat při volání sítě, hashe celého modelu, subprocessu FFmpeg nebo zápisu během rychlé kontroly.

**Hotovo, když:** stejná situace vrací stejný kód a komponenty přes CLI i MCP a kontrola je bez vedlejších účinků.

### H5-2: Bootstrap a společné shellové spouštěče

**Soubory:** nový `scripts/bootstrap.py`, společné shellové vstupy a obálky v `skills/*/scripts/`, `start-video-story.bat`, `start-video-story.sh`, případně `__main__.py` a projektový CLI entry point.

**Práce:** zavést přímý interpreter z `.venv`, lehký bootstrap a řízené chyby před i po startu Pythonu. Odstranit automatický `setup` z kořenových spouštěčů. Zachovat obecné argumenty, `.cmd` kompatibilitu, H1 a spustitelnost `.sh`. Běžné spuštění se nesmí změnit na volání globálního `video-story` nalezeného v PATH.

**Ověření:** přizpůsobit `test_launchers.py`, který nyní očekává volání stubu uv, a přidat `test_bootstrap.py`. Spustit reálné obálky proti řízenému prostředí pro chybějící uv, chybějící `.venv`, rozbitý interpret a knihovnu. Ověřit cesty s mezerami a Unicode, cizí cwd, junction/symlink, přenos návratového kódu a argumentů. Stub uv musí prokázat, že start nic neinstaluje. Test se nesmí zachránit skutečným uv/Pythonem z počítače.

**Hotovo, když:** připravený start používá jeden bootstrapový Python proces a GUI, bez dalších diagnostických procesů a bez změn prostředí.

### H5-3: Zapojení kontroly a potvrzení GUI

**Soubory:** nový malý `host/startup.py`, `host/runner.py`, `host/editor_launcher.py`, `host/editor_session.py`, `host/controller.py`, `ui/app.py`, `ui/control_window.py`, `editor/app.py`, `editor/window.py`, CLI a MCP adaptéry.

**Práce:** aplikovat kontrolu na všechny cesty otevření. Před recovery editoru ji vykonat jednou, nepoužívat duplicitní sken před startem a znovu v dítěti. Přidat token a odpověď GUI podle 3.4. `Status.READY` a editorové `open` nezaměňovat s potvrzením startu. Rodič po spawn nepřepíše novější stav dítěte. Editorové potvrzení připojit ke správné generaci načítání a k `_on_loaded`, včetně případu již načtené nahrávky.

Přechod Stop → editor v `_on_processed` přesunout do pracovního vlákna nebo neblokujícího čekání s Qt timerem. Zachovat již uloženou nahrávku i její cesty při chybě startu editoru. Rozšířit výsledek této chyby o původní důvod, aby agent mohl opravit součást a otevřít uloženou nahrávku znovu. Nespustit nové nahrávání místo dokončení této cesty. Odstranit původní startovní nabídku „record anyway“ pro známé chybějící součásti. Pozdější změnu prostředí během otevřeného okna stále hlásit srozumitelně.

**Ověření:** rozšířit `test_runner.py`, `test_editor_launcher.py`, `test_editor_session.py` a Qt testy ovládacího okna/editoru, přidat testy startup protokolu. Ověřit opožděné GUI, pád před potvrzením, zavření během startu, timeout, odmítnutý attach, attach blokovaný dialogem, načtení jiné nahrávky, rychlý Process a ztracenou odpověď nástroje. Test zastaralého zápisu rodiče nesmí dovolit vrátit stav z RECORDING zpět do READY. Při timeoutu se sdílený editor nezavře a prošlý attach později nezmění jeho obsah.

**Hotovo, když:** úspěšný návrat znamená připravené správné okno. Při chybějících součástech nevznikne nová relace ani recovery a předchozí okno zůstane použitelné.

### H5-4: Cílený setup a navazovatelné stahování

**Soubory:** `host/cli.py`, `host/doctor.py`, nový úzce zaměřený modul setupu podle potřeby, `host/downloads.py`, `transcribe/ffmpeg.py`.

**Práce:** oddělit instalaci od hardwarové diagnostiky. Výsledek setupu musí rozlišit instalované, už použitelné a neopravitelné součásti. Zajistit, že vadný FFmpeg z PATH nebude i po úspěšném stažení stále vybírán před opravenou kopií. Systémový soubor nepřepisovat, případnou změnu aplikační volby cesty uvést ve výsledku. Vlastní model neznámého původu automaticky nenahrazovat.

Zachovat `.part` při přerušení a uložit metadata zdroje, očekávaného součtu a dostupného validátoru. Navazovat HTTP Range jen na odpovídající reprezentaci. Při `206` ověřit začátek `Content-Range`, při `200` zahájit nový soubor a nikdy nepřipojit celý obsah za starý. Při změně zdroje nebo neplatném rozsahu restartovat přenos. `416` nepovažovat automaticky za dokončení, úplnost potvrdit délkou a součtem. Po dokončení ověřit celý soubor a atomicky jej zveřejnit. HTTP chování vychází z [RFC 9110, range requests](https://www.rfc-editor.org/rfc/rfc9110.html#name-range-requests).

Pro FFmpeg přesunout rozpracovaný archiv z náhodné dočasné složky do stabilního pracovního umístění ve složce stavu, vázaného na zdroj a platformu. Rozbalovat do staging složky, ověřit spuštění a potřebné kodéry, teprve pak zpřístupnit novou kopii. Zachovat validní starou kopii při neúspěchu. Druhé souběžné `setup` nesmí zapisovat do téhož přenosu, použít malý instalační OS zámek a výsledek `SETUP_IN_PROGRESS` s cestou k diagnostice. Nezaměňovat jej s chybějícím modelem a nezavádět další paralelní instalaci.

Průběh instalace zapisovat omezeně do stderr nebo logu s fází, počtem bajtů a časem poslední změny. Finální JSON zůstane jeden. Nenutit GUI progress bar. Chyba sítě, oprávnění, nedostatku místa nebo nepodporovaného zdroje musí uvést komponentu a nechat ostatní hotové součásti na místě.

**Ověření:** rozšířit `test_doctor.py` a `test_transcribe.py`, doplnit testy setupu a HTTP integraci s místním serverem a malými soubory. Pokrýt obnovení v novém procesu po tvrdém přerušení, `200/206/416`, změněný validátor, chybný hash, chybějící délku, ignorovaný Range, souběh, FFmpeg staging a zachování starého souboru. V jednotkových testech nestahovat skutečný 548MiB model.

**Hotovo, když:** instalace bez obrazovky a mikrofonu může připravit součásti, přerušený přenos bezpečně naváže a druhý start už nic zbytečně nestahuje.

### H5-5: Čekání, MCP a návody dalších kroků

**Soubory:** `host/cli.py`, `host/runner.py`, `host/editor_session.py`, `host/mcp_server.py`, společná konstanta čekání.

**Práce:** změnit všechny výchozí hodnoty, generované `next` a fallbacky na 45 sekund. Validovat parametry podle 3.5, omezit poslední sleep zbývajícím časem do deadline. Zachovat rychlý návrat konečného stavu a správné rozlišení nahrávacího `final` a editorových událostí. Zpřístupnit status a ostatní MCP nástroje i během čekajícího startu nebo editorového wait. Progress je doplňkový, aplikace nespoléhá na prodloužení limitu klienta.

**Ověření:** parametrizované testy obou relací přes CLI i MCP, okamžité dokončení, několik navazujících čekání, hodnoty 0 pro CLI, malé kladné hodnoty, záporné hodnoty, NaN a nekonečno. Časovou logiku ověřovat převážně řízenými hodinami, ne dlouhými sleep v jednotkových testech. Jeden protokolový test skutečného lokálního stdio serveru musí doložit inicializaci bez modelu, `SETUP_REQUIRED` a následnou dostupnost dalších nástrojů. Přerušení čekání nezmění platnou živou relaci na failed ani nevytvoří další wait proces bez převzetí výsledku předchozího.

**Hotovo, když:** CLI i MCP dodržují stejný význam výsledků a běžný timeout pouze vybízí k dalšímu čekání.

### H5-6: Skilly, registrace hostitelů a ruční spuštění

**Soubory:** oba `SKILL.md`, `docs/installation.md`, `scripts/install-hosts.ps1`, `scripts/install-hosts.sh`, `README.md` a specifikace pouze v dotčených pasážích, `tests/manual/hosts.md`.

**Práce:** první krok skillu je start, potom rozhodovací tabulka chyb. Každá opravná větev má konkrétní příkazy, cílový projekt a zachování původních argumentů. U chybějícího uv uvést platformní instalační postup a obnovení jeho dostupnosti v aktuálním shellu. Nezáviset na tom, že nový PATH převezme až další chat. Agent reportuje skutečně prováděné kroky a po téže opakované chybě nepokračuje nekonečnou reinstalací.

MCP konfigurace všech tří hostitelů přepnout na stejný deterministický vstup s argumentem `mcp`. Na Windows respektovat, že `.bat` vyžaduje správné vyvolání přes `cmd.exe`, nikoli předpoklad libovolného shellu. Zkontrolovat quoting cest s mezerami v TOML i JSON. Ověřit i navázání po opravě prostředí, kdy je nutné znovu připojit server. Rozlišit Claude Code s lokálním shellem od čistého Claude Desktop MCP klienta, který bez dalšího nástroje nemůže nainstalovat chybějící Python.

Ruční start bez agenta zobrazí konkrétní chybu a opravný příkaz bez automatického stahování. Agentní režim nemá interaktivní `pause`. V pokynech oprávnění respektovat již udělené povolení a možnosti hostitele, nevyžadovat obecné vypnutí sandboxu ani předstírat, že zamítnutí vyřeší reinstalace.

**Ověření:** `test_install_hosts.py`, kontrola generovaných konfigurací a návodů, vyhledání zbývajících aktivních instrukcí s `--timeout 240`, výchozím 60sekundovým MCP wait a běžným `uv run` v cestě startu. Historická review mohou staré hodnoty zachovat, musí být zřejmé, že popisují původní stav.

**Hotovo, když:** každý hostitel má jednoznačný postup pro start, opravu, opakovaný start, dlouhou instalaci a čekání. Návod nerozšiřuje časové limity na základě domněnky.

### H5-7: Automatické ověření a měření

Po cílených testech jednotlivých kroků jednou spustit celou jednotkovou sadu a lint. Příprava vývojového prostředí pro testy je oddělená od měřeného běžného startu:

```text
uv sync --locked --all-groups
uv run --no-sync ruff check src tests scripts
uv run --no-sync pytest tests/unit -q -p no:cacheprovider
git diff --check
```

Existující CI má Windows, Linux a macOS. Zachovat tuto matici, změnit přípravu na respektování lockfile a přidat testy bootstrapu, obálek a lokálního MCP. Offscreen Qt ani Xvfb test se nesmí vydávat za skutečné ověření integrace v Claude, Cursoru či Codexu. Nové integrační testy registrovat v souladu s `VIDEO_STORY_INTEGRATION=1`.

Použít vlastní dočasný `VIDEO_STORY_HOME`, `VIDEO_STORY_CONFIG`, složku nahrávek a řízený kořen kódu/PATH. Při testování chybějícího modelu nesmazat skutečný model uživatele. Samotné přesměrování HOME nestačí, protože hledání má fallback do checkoutu. Testovací varianty prostředí vytvářet odděleně, ne odinstalací knihoven pracovního projektu.

Zopakovat měření H5-0 se stejnými daty. Zaznamenat zvlášť čas bootstrapu, kontroly a skutečné připravenosti GUI. Povinný výsledek: běžná kontrola nemá síť, hash celého modelu ani synchronizaci a nevytváří nadbytečné diagnostické procesy. Pokud opakovaný start prokazatelně zpomalí, vysvětlit konkrétní fázi a odstranit regresi zavedenou H5 před přijetím. Výchozí časový limit readiness upravit podle měření, ne prodlužovat bez rozboru.

### H5-8: Ověření ve skutečných hostitelích

Provést matici a scénáře z kapitoly 5. Jde o povinný krok po implementaci, ne o součást současné přípravy plánu. Testující používá reálná okna a skutečná hostitelská volání. Na stroji bez hostitele ponechá výsledek `NEOVĚŘENO`, nikdy jej nenahradí unit testem.

### H5-9: Review, úklid a uzavření

Provést jeden souvislý průchod celého diffu. Zaměřit se na vedlejší účinky před kontrolou, race rodiče a GUI, opožděný attach, převzetí výsledku na pozadí a ochranu existujícího modelu/nahrávek. Opravený konkrétní nález znovu ověřit cíleným testem.

Ukončit jen procesy a servery vytvořené zkouškou, obnovit dočasně upravené hostitelské konfigurace a ponechat výsledky. Do review zapsat skutečně dokončené kroky a zbývající neověřené platformy. H5 označit za vyřešený až po splnění akceptačních podmínek, ne po pouhém průchodu pytest.

## 5. Matice Claude, Cursor a Codex

Záznamy ukládat do `tests/results/h5/<hostitel>-<varianta>-<os>/`. Tento adresář je podle `.gitignore` lokální. Do verzovaného `tests/manual/hosts.md` doplnit stručný výsledek, verze, datum a odkazy na uchované důkazy. Každý záznam obsahuje commit, OS, shell, verzi hostitele, režim oprávnění, CLI/MCP, příkaz nebo nástroj, úplný JSON, exit code, dobu běhu a pozorování okna. Přihlašovací údaje do záznamu nepatří.

| Hostitel a varianta | CLI nahrávání + editor | MCP nahrávání + editor | Stav při sepsání plánu |
| --- | --- | --- | --- |
| Claude Code, lokální CLI | Povinné | Povinné | NEPROVEDENO |
| Cursor, editorový Agent | Povinné | Povinné | NEPROVEDENO |
| Cursor, CLI | Povinné pro deklarovanou podporu CLI | Povinné pro deklarovanou podporu CLI | NEPROVEDENO |
| Codex, CLI | Povinné | Povinné | NEPROVEDENO |
| Codex, desktopová aplikace | Povinné | Povinné | NEPROVEDENO |

Základní úplnou matici provést na Windows. Na Linuxu a macOS ověřit skutečný `.sh`, prostředí, chybové větve a dostupné lokální GUI/MCP varianty. U nedostupné varianty uvést důvod a nezveřejnit tvrzení o ověřené podpoře. Claude Desktop bez shellu je samostatně popsané omezení bootstrapu, ne náhrada povinného testu Claude Code.

### 5.1 Společné scénáře pro každou povinnou variantu

| ID | Postup | Kritérium úspěchu |
| --- | --- | --- |
| V1 | V připraveném prostředí požádat o Start Video Story, pracovat z cizího adresáře | První nástrojová akce je start, není doctor ani instalace, okno je připravené při OK a přežije návrat nástroje alespoň 60 s |
| V2 | Nahrávat nejméně přes dva intervaly wait, pak Stop → editor → Process | Několik čekání na stejnou relaci, po Process správný přepis a snímky. Agent nejedná před Process |
| V3 | Edit Video Story s konkrétní uloženou nahrávkou, pak Process a zvlášť zavření bez Process | Správná nahrávka a editor_session, při zavření žádné provádění diktovaných úkolů |
| V4 | Otevřený editor, attach jiné nahrávky, nejprve s dialogem a pak bez něj | OK až po skutečném přijetí. Při timeoutu zachovaný původní editor a žádné pozdější převzetí prošlého požadavku |
| V5 | V izolovaném prostředí chybí postupně uv, interpret, knihovna | Rozpoznaná chyba, agent popíše a provede správnou opravu, původní režim se úspěšně spustí |
| V6 | V izolovaném kořeni chybí model, FFmpeg, pak obojí | Žádné nové okno/relace před opravou, správný seznam, instalace chybějících součástí, opakovaný start |
| V7 | Po úspěchu odstranit jen testovací součást a znovu spustit | Stejná detekce jako při prvním startu, bez příznaku „už instalováno“ |
| V8 | MCP server spustit bez modelu a FFmpeg, zavolat start a následně jiný nástroj | Server se připojí, start vrátí SETUP_REQUIRED, server nezanikne. Po CLI opravě lze start zopakovat |
| V9 | Pomalá instalace přes hranici jednoho nástrojového čekání, pak přerušení a navázání | Agent informuje o průběhu, sleduje stejný proces, žádná duplicitní instalace, bezpečný konečný soubor |
| V10 | Krátký klientský limit či předání běžícího příkazu na pozadí | Agent rozliší průběžný handle, timed_out aplikace a chybu hostitele. Převezme výsledek, nevytvoří druhé okno |
| V11 | Zamítnutí oprávnění nebo nepřežití GUI po návratu příkazu | Konkrétní diagnostika hostitele, žádná reinstalace jako domnělá oprava. Po podporovaném povolení nebo lokální MCP cestě opakovaný úspěch |
| V12 | Čerstvá instalace pluginu bez `.venv`, prostředí přes junction/symlink a cesta s mezerami | Agent připraví správný projekt podle lockfile a používá původní složku stavu. Druhý start je bez přípravy |

V5–V7 provést pro nahrávání i editor. Tam, kde Python/uv chybí, MCP z definice nemusí naběhnout: ověřuje se lokální opravná větev a následné připojení. Síťové a instalační destruktivní scénáře používat jen na oddělených testovacích datech. Dlouhý přenos lze pro test limitu nasimulovat lokálním serverem, ale alespoň jednu skutečnou instalaci modelu a FFmpeg dokončit do oddělené složky a ověřit jejich použitelnost.

### 5.2 Rozdíly nástrojů a limitů

- **Claude Code:** zaznamenat limit konkrétního shellového volání a způsob převzetí úlohy na pozadí. U MCP ověřit per-server limit a nespoléhat na jeho prodlužování progress zprávami. Historická hodnota 120 s není záruka aktuální konfigurace. Podklad: [Claude Code MCP](https://code.claude.com/docs/en/mcp#time-limits-on-mcp-tool-calls).
- **Codex:** odděleně v CLI a desktopové aplikaci ověřit, zda krátká doba čekání nástroje pouze vrací handle procesu. Při jeho vrácení pokračovat na stejném handle. MCP `startup_timeout_sec` a `tool_timeout_sec` mají odlišný účel. Testovat 45s wait proti alespoň 60s limitu, nebo kratší čekání při přísnější konfiguraci. Podklad: [oficiální dokumentace OpenAI](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
- **Cursor:** editorový Agent a CLI testovat samostatně, zaznamenat Run Mode, limit a skutečné chování terminálu. Nepoužívat neověřené konfigurační klíče z jiného hostitele. Podklady: [Cursor MCP](https://cursor.com/docs/mcp), [Cursor terminál](https://cursor.com/docs/agent/tools/terminal).

Odkazy doplňují zdrojový H5. Při realizaci ověřit nastavení proti instalované verzi, nevydávat hodnoty z dokumentace za měření hostitele. Výchozích 45 sekund je nastavení aplikace, ne příslib fungování proti libovolně krátkému limitu.

## 6. Rizika a rozhodovací body

| Riziko | Postup |
| --- | --- |
| Editor zpracovává dlouho seznam nahrávek | Změřit odděleně od kontroly závislostí. Nedávat falešné OK a neotevírat v H5 kompletní optimalizaci seznamu z E3 |
| Souběžný zápis rodiče a GUI do relace | Potvrzení vlastní GUI, odstranit přepis starého stavu rodičem a chránit testem |
| Dlouhé čekání při Stop → editor zamrazí Qt | Přesunout čekání mimo GUI vlákno, nahrávku zachovat i při chybě |
| Vadná knihovna potřebná už k běhu setupu | Nejdřív bootstrapová oprava přes uv, až potom aplikační setup |
| FFmpeg na PATH je nefunkční a překrývá staženou kopii | Cíleně vyřešit výběr aplikační cesty, nevydat OK po samotném stažení |
| macOS nemá v dnešním setupu automatický zdroj FFmpeg | Zachovat explicitní nepodporovanou automatickou instalaci a doložit cestu se systémovým FFmpeg. Tím se neprokazuje plná automatická oprava na macOS. Pokud je požadována i ta, doplnění zdroje je nutná samostatně vyjasněná část před deklarací podpory |
| Hostitel nebo platforma nejsou dostupné | Implementaci lze ověřit lokálními testy, danou buňku ale označit NEOVĚŘENO a H5 neuzavřít jako plně ověřený |
| Ochrany hostitele ukončují odpojené GUI | Rozhodnout podle skutečné zkoušky H4. Použít povolenou lokální cestu, žádná automatická změna globální bezpečnostní politiky |

## 7. Podmínky dokončení

- [ ] H5-0 až H5-6 implementovány, každý krok ověřen svými cílenými kontrolami.
- [ ] Připravený běžný start neinstaluje, nesynchronizuje, nečte celý model a neprovádí síťové operace.
- [ ] Chybové výsledky přes shell, CLI i MCP jsou strojově čitelné a vedou k cílené opravě.
- [ ] OK potvrzuje skutečné správné GUI, ne pouze PID, a opakovaný požadavek nevytváří duplicitní okno.
- [ ] Model i FFmpeg se opravují bez přepsání použitelné cizí součásti. Přerušené stahování je ověřeně navazovatelné.
- [ ] Čekání na nahrávání i editor funguje opakovaně a agent rozlišuje timeout aplikace od běhu nástroje na pozadí.
- [ ] H5-7: lint, jednotkové testy, integrační kontroly a srovnání rychlosti mají zaznamenaný výsledek.
- [ ] H5-8: povinné varianty Claude Code, Cursoru a Codexu ověřeny přes CLI i MCP včetně opravné větve, Process a zavření bez Process.
- [ ] `.bat` ověřen na Windows a `.sh` na Linuxu a macOS, případná omezení výslovně uvedena.
- [ ] H5-9: diff zkontrolován, testovací procesy uklizeny a review odpovídá skutečnému stavu.

Dokud zbývá povinný neověřený hostitel nebo selhávající kritický scénář, výsledný stav je „implementováno, ověření neúplné“ nebo „částečně implementováno“. Označení „vyřešeno“ vyžaduje skutečné splnění podmínek.
