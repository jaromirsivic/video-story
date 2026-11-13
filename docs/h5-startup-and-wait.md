# H5: deterministické spuštění, oprava prostředí a čekání

Datum: 13. 11. 2026. Stav: hlavní cesty implementovány, úplné ověření hostitelů dosud neuzavřeno.

Aktuální průběh a důkazy: [implementační plán](../implementation-plan-h5-startup-and-wait.md) a [ověření hostitelů](../tests/manual/hosts.md). Sekce „Výchozí problém“ zachycuje stav před implementací, další sekce definují požadavky.

Rozšíření nálezu H5 z [kontroly pluginu](../video-story-review-2026-11-13.md). Tento dokument zachycuje výsledný návrh z diskuse s uživatelem. Nahrazuje původní stručný návrh H5 a dřívější úvahu o modálním okně s průběhem stahování. Zápis dokumentu neznamená realizaci změn ani ověření běhu v hostitelích.

## Cíl a rozsah

Spuštění Video Story i Video Story Editoru musí být rychlé, opakovatelné a stejné při prvním i každém dalším použití. V běžném případě jsou všechny součásti připravené. Agent proto nejdřív zavolá hotový spouštěč nebo dostupný MCP nástroj a teprve konkrétní chyba spustí diagnostiku a opravu.

Postup musí fungovat v Claude Code, Cursoru a Codexu. Pro každého hostitele platí stejný aplikační kontrakt. Odlišnosti shellu, oprávnění, životnosti procesů a limitů nástrojů řeší stručné pokyny pro příslušného hostitele, nikoli tři nezávislé implementace aplikace. Ověření musí zahrnout lokální CLI i připojení přes MCP a obě okna. MCP server pro záznam obrazovky musí běžet na zařízení a v uživatelské relaci, jejíž obrazovka a mikrofon se mají nahrávat.

Tento návrh neřeší chybějící funkce Linuxu a macOS z P1. Spouštěč `.sh` musí být přenositelný, ale jeho existence sama neprokazuje úplnou podporu nahrávání na těchto systémech.

## Výchozí problém a ověřený stav kódu

- Oba skilly předepisují `wait --timeout 240`, CLI má výchozí hodnotu 240 sekund. Pokud hostitel ukončí příkaz dříve, agent nedostane běžnou odpověď `timed_out: true`. Původní review uvádí 415 volání Claude Code s ručně zvýšeným limitem `timeout: 300000`. Jde o historické pozorování uvedené v review, při rozšíření nebyly tyto přepisy znovu přeměřeny.
- MCP čekání na nahrávání i editor má výchozí hodnotu 60 sekund. Při klientském limitu 60 sekund nemá dlouhé čekání rezervu na zpracování a doručení odpovědi. Neplatí, že selže každé volání: dokončená relace se vrátí dřív. Nahrávací MCP čekání navíc posílá přibližně po pěti sekundách zprávu o průběhu, editorové ne. Na prodlužování klientského limitu těmito zprávami se návrh nespoléhá.
- Skillové spouštěče `.cmd` a `.sh` kontrolují `uv` a volají běžné `uv run`. Kořenové `start-video-story.bat` a `.sh` naopak provádějí `setup --check` a při chybění součástí automaticky volají `setup`. Pravidla startu proto dnes nejsou společná.
- `setup --check` už poskytuje rychlé hledání modelu a FFmpeg. `doctor` dělá důkladnější diagnostiku včetně kontrolního součtu modelu. Rychlý start nesmí volat celý `doctor`.
- Model je v tomto checkoutu přítomný, FFmpeg byl při kontrole nalezen v systémové cestě přes WinGet. Binární model, `bin/` a `.venv/` jsou v `.gitignore`. Přítomnost na vývojovém zařízení nezaručuje připravenost nové instalace pluginu.
- Stahování zapisuje `.part` od začátku. Přerušený přenos neumí navázat. Spouštění odpojeného nahrávacího okna dnes vrací výsledek už po vytvoření procesu, nikoli po potvrzení připraveného GUI.

Relevantní místa: [CLI](../src/video_story/host/cli.py), [spuštění nahrávání a čekání](../src/video_story/host/runner.py), [spuštění editoru](../src/video_story/host/editor_launcher.py), [relace editoru](../src/video_story/host/editor_session.py), [MCP](../src/video_story/host/mcp_server.py), [diagnostika](../src/video_story/host/doctor.py), [stahování](../src/video_story/host/downloads.py), [hledání FFmpeg](../src/video_story/transcribe/ffmpeg.py).

## Společný postup spuštění

1. Skill bez předchozí diagnostiky zavolá připravený `.bat` na Windows nebo `.sh` na Linuxu a macOS s požadavkem `run --detach` či `edit --detach`. Pokud je už dostupný odpovídající lokální MCP nástroj, zavolá jej přímo. Zachová výběr nahrávky, konfiguraci a ostatní původní argumenty.
2. Shellový spouštěč určí kořen aplikace, ověří dostupné `uv` a připravený Python aplikace a rovnou předá řízení aplikaci. Chybějící nebo nepoužitelné prostředí ohlásí řízenou chybou. Nic automaticky neinstaluje.
3. Aplikace provede společnou rychlou kontrolu modelu a FFmpeg. Stejná logika se použije pro nahrávání, editor, přímé spuštění GUI i MCP. Při chybějící součásti vrátí seznam problémů, nevytvoří novou relaci a neotevře nové okno.
4. Při úspěšné kontrole aplikace otevře požadované okno nebo připojí požadavek k existujícímu editoru. Po potvrzení připravenosti vrátí `ok: true` a identifikátor nahrávací nebo editorové relace. Agent začne čekat na dokončení.
5. Při chybě agent stručně oznámí konkrétní problém a následující opravu. Zajistí jen chybějící či prokazatelně vadné součásti pomocí stanoveného instalačního postupu. Po dokončení zopakuje původní spuštění.

Při chybě MCP nástroje skončí pouze požadavek na otevření okna. MCP server zůstane dostupný. Jeho inicializace nesmí vyžadovat model ani FFmpeg a nesmí je stahovat. Chybí-li Python nebo knihovny potřebné ke spuštění samotného serveru, agent použije lokální spouštěč a opravný postup, protože nefunkční server nemůže vrátit aplikační chybu.

## Deterministické spouštěče a Python

- Jedna společná cesta spuštění má tenké obálky pro nahrávání a editor. Uživatel i skill používají stejný `.bat` nebo `.sh`. Dosavadní `.cmd` mohou zůstat jako kompatibilní přesměrování bez vlastní instalační logiky. Kořenové spouštěče přestanou samy stahovat model a FFmpeg.
- Zachovat opravu H1: nezávislost na pracovním adresáři, správné rozlišení junctionů a symlinků, cesty s mezerami a explicitní `VIDEO_STORY_ROOT`. Nepřebírat omylem Python ani prostředí z uživatelova aktuálního projektu.
- Ověřovat interpret prostředí Video Story, ne pouze existenci systémového příkazu `python`. Dnes projekt požaduje Python `>=3.12,<3.14`. Podporované rozmezí musí vycházet z projektu a nesmí se ve spouštěčích nezávisle rozcházet.
- Preferovaný rychlý postup po kontrole dostupnosti `uv` je přímé spuštění připraveného interpretu aplikace. Ten se ověří samotným spuštěním a lehkým vstupním kódem, nikoli dalším samostatným procesem pro každý test. Pokud implementace ponechá `uv run`, musí výslovně zakázat synchronizaci a automatické stahování a ověřit, že při chybějícím prostředí neskončí v jiném Pythonu.
- `uv run --no-sync` vynechává synchronizaci a implikuje `--frozen`, `--no-python-downloads` zakazuje automatické stažení Pythonu a `--offline` omezuje síťové operace uv. To jsou podklady pro případnou variantu s uv, nikoli samostatný důkaz, že spouštěč neprovádí žádnou přípravu. `--no-managed-python` se pro tento účel nehodí, protože vylučuje i již nainstalovaný Python spravovaný uv. Viz [referenční příručka uv](https://docs.astral.sh/uv/reference/cli/#uv-run).
- Dostupnost knihoven ověřovat při nezbytných importech a případně levnou kontrolou metadat kritických balíčků. Nenačítat celý Whisper ani provádět `uv sync` při každém startu. Malá vstupní část má umět oznámit známou chybu prostředí i před načtením hlavní aplikace. Libovolnou výjimku uvnitř aplikace nesmí vydávat za chybějící knihovnu.
- Chyby ještě před startem Pythonu musí mít čitelný kód a návratový stav i bez Pythonu. Na Windows spouštěč použitý agentem nečeká na `pause` a neotevírá zbytečnou konzoli. Viditelná jsou požadovaná aplikační okna.

## Rychlá kontrola v aplikaci

Kontrola má pouze zjistit připravenost. První a stý start používají stejná pravidla, bez zvláštního příznaku „první spuštění“.

| Součást | Běžný start | Instalace nebo výslovný `doctor` |
| --- | --- | --- |
| Model | Najít zvolený soubor, ověřit dostupnost a nenulovou velikost. U známého distribuovaného modelu porovnat očekávanou velikost | Ověřit kontrolní součet, vadný soubor nahradit až po ověření nové kopie |
| Vlastní model z konfigurace | Ověřit dostupnost bez předpokladu velikosti distribuovaného modelu | Ověřit podle pravidel pro daný model, nepřepsat jej automaticky jiným modelem |
| FFmpeg | Najít soubor podle společného pořadí konfigurace, PATH, složka stavu, přibalená kopie. Ověřit přístup a na POSIX spustitelnost | Ověřit spuštění a schopnosti potřebné pro přepis a export |
| Knihovny | Nezbytné importy a levné kontroly prostředí | Připravit kompatibilní prostředí podle `uv.lock` |

Při běžném startu se nesmí číst celý model, počítat jeho SHA-256, načítat jej do paměti, kontaktovat síť, kontrolovat aktualizace nebo spouštět úplný `doctor`. Kontrola nemusí prokázat nepřítomnost každého poškození. Později zjištěná chyba načítání nebo spustitelnosti musí zůstat konkrétní a nesmí vést k falešnému úspěchu.

Kontrola proběhne před vytvářením nové relace, obnovou starých nahrávek a dalšími vedlejšími účinky startu. Ve stejném požadavku se zbytečně neopakuje v shellu, CLI, MCP a GUI. Požadavek na otevření přes již běžící editor použije stejnou kontrolu, její selhání však nezavírá stávající okno ani nezahazuje rozpracovanou práci.

`OK` znamená připravené ovládací nebo editorové okno, ne pouhé přidělení PID a ne dokončený přepis. Potvrzení připravenosti musí mít konečný časový limit a hlásit předčasný konec procesu. Při nejasném výsledku doručení agent nejprve zjistí stav existující relace, aby opakovaným startem neotevřel další okno.

## Výsledky pro agenta

Následující kódy popisují společný kontrakt zavedený při implementaci H5.

| Kód | Význam a reakce |
| --- | --- |
| `UV_MISSING` | Uv není dostupné. Agent zajistí jeho instalaci nebo správné nalezení |
| `PYTHON_MISSING` | Prostředí nemá dostupný použitelný interpret. Agent zajistí podporovaný Python přes uv a připraví prostředí |
| `ENVIRONMENT_INVALID` | Nekompatibilní Python, chybějící prostředí nebo knihovny. Agent opraví prostředí přes uv podle projektového lockfile |
| `SETUP_REQUIRED` | Chybí model nebo FFmpeg, případně je zjištěná konkrétní vada. Výsledek obsahuje všechny zjištěné součásti a důvody |
| `START_FAILED` | Selhal start GUI nebo potvrzení připravenosti. Vrátit důvod a dostupnou cestu k logu, bez plošné přeinstalace |
| `ROOT_NOT_FOUND` | Chybný odkaz nebo instalace pluginu. Zachovat význam z H1, nezaměňovat za chybějící model |
| `ok: true` | Okno je připravené nebo editor potvrdil připojení. Výsledek obsahuje `session_id` nebo `editor_session` a další krok čekání |

Zachovat konkrétní existující chyby, například `NO_RECORDINGS`, `CONFIG_INVALID` a `STATE_NOT_WRITABLE`. Nejde o důvod ke stahování modelu. Existující `ALREADY_RUNNING` má dál vést k převzetí identifikátoru běžící nahrávací relace a čekání.

Příklad aplikační odpovědi:

```json
{
  "ok": false,
  "code": "SETUP_REQUIRED",
  "message": "Chybí model a FFmpeg.",
  "missing": ["model", "ffmpeg"]
}
```

CLI zachová strojově čitelný JSON, nenulový návratový kód při chybě a oddělení diagnostiky od výsledku. Navázat na existující konvenci úspěchu na stdout a chybového JSON na stderr. MCP vrací stejná významová pole v odpovědi nástroje, nikoli holý JSON na stdout protokolového serveru. Skripty i aplikace musí zachovat argumenty a kód ukončení.

## Opravný postup a komunikace

Skill popíše konkrétní opravné větve pro uvedené chyby. Před prvním pokusem o spuštění je neprovádí. Při problému agent oznámí například „Chybí model pro přepis, stahuji ho.“ a při dlouhé instalaci průběžně sdělí stav. Modální okno ani aplikační progress bar pro stahování se nezavádí.

Python a knihovny se instalují výhradně přes uv. Příprava knihoven musí respektovat `uv.lock`, například pomocí `uv sync --locked` pro správný projekt. Instalace uv samotného je předchozí samostatný krok. Stažení modelu a FFmpeg vlastní společný příkaz aplikace `setup`, nikoli jednotlivé skilly. Agent neudržuje vlastní URL, kontrolní součty ani výběr platformního sestavení. `setup` instaluje jen chybějící či zjištěně vadné součásti a zachová pravidla složky stavu z H3.

Je-li agent připojen přes MCP, ale instalace vyžaduje lokální shell, použije zdokumentovaný CLI postup na témže zařízení a se stejnou konfigurací. Opravu nefunkčního Pythonu nelze svěřit MCP serveru, který v něm musí nejprve naběhnout. Čistý MCP klient bez lokálního shellu nemůže sám zajistit instalaci uv a Pythonu. Takovou překážku musí výslovně oznámit. Požadované ověření tří hostitelů proto zahrnuje jejich lokální agentní prostředí, ne jen připojení libovolného vzdáleného klienta.

Po opravě agent zopakuje původní start včetně výběru nahrávky. Pokud stejná chyba zůstane, nepokračuje nekonečnou reinstalací. Oznámí konkrétní překážku. Při ručním spuštění bez agenta se zobrazí stručný důvod a opravný příkaz, bez skrytého stahování.

Instalace může trvat déle než běžný nástrojový požadavek. Agent musí od začátku použít podporovaný dlouhý běh nebo sledovat tutéž úlohu na pozadí. Předání identifikátoru běžící úlohy není dokončení ani důvod spustit stahování znovu. Původní požadavek H5 na navazovatelné stahování zůstává: částečný soubor se využije jen při ověření stejného zdroje, při nepodporovaném navázání se bezpečně začne znovu a hotový model se přijme až po kontrole součtu.

## Čekání a rozdíly mezi hostiteli

Oprava startu neodstraňuje původní problém dlouhého `wait`. Společné pravidlo je: doba jednoho aplikačního čekání musí být kratší než skutečný limit hostitelského nástroje a ponechat rezervu pro režii. Limit inicializace MCP serveru, limit MCP nástroje, limit shellového procesu a interval, po kterém nástroj jen vrátí řízení agentovi, nejsou totéž.

Výchozí čekání je 45 sekund pro CLI i MCP, pokud má hostitel alespoň 60sekundový limit. Při kratším limitu použít kratší interval. Původně navržených 100 sekund pro CLI lze použít jen v prostředí s výslovně nastaveným a ověřeným větším limitem. Žádná hodnota není zárukou pro všechny konfigurace hostitelů. Hodnota 45 sekund je implementovaná, úplná matice hostitelských konfigurací se stále musí dokončit.

`timed_out: true` znamená běžný výsledek jednoho čekání. Agent čeká dál na tutéž relaci a nahrávání neukončuje. Pokud hostitel vrátí handle stále běžícího příkazu, agent nejprve vyzvedne výsledek tohoto příkazu. Při skutečném přerušení nástroje zjistí stav relace a pokračuje vhodným kratším čekáním. Nesmí automaticky považovat nahrávání za zrušené. Průběžná MCP hlášení mohou pomoci se zobrazením stavu, ale nenahrazují správné limity.

| Hostitel | Požadavek na postup a ověření |
| --- | --- |
| Claude Code | Skill stanoví limit shellového nástroje vůči aplikačnímu čekání a postup sledování dlouhé instalace. U MCP rozlišit inicializaci a nástrojové volání. Oficiální dokumentace uvádí per-server `timeout` v milisekundách a `MCP_TOOL_TIMEOUT`. Tvrdý limit neprodlužují zprávy o průběhu. [Dokumentace Claude Code](https://code.claude.com/docs/en/mcp#time-limits-on-mcp-tool-calls) |
| Codex | Respektovat nástroje skutečně dostupné v CLI a desktopové aplikaci, včetně rozlišení předání řízení a ukončení procesu. MCP má dokumentované oddělené `startup_timeout_sec` a `tool_timeout_sec`, výchozí hodnoty 10 a 60 sekund. Proto nestahovat při inicializaci MCP a nepoužívat 60sekundové čekání proti stejnému limitu. [Oficiální dokumentace OpenAI](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) |
| Cursor | Ověřit samostatně editorový Agent a používané CLI. Dokumentace MCP a terminálu nepotvrzuje jeden univerzální časový limit použitelný pro oba. Nepřenášet hodnoty z Claude či Codexu do Cursoru bez ověření. Respektovat Run Mode a použít podporovaný způsob sledování dlouhého procesu. [MCP](https://cursor.com/docs/mcp), [terminál](https://cursor.com/docs/agent/tools/terminal) |

Údaje z dokumentace byly ověřeny 13. 11. 2026, nejsou výsledkem běhového testu Video Story v těchto hostitelích. Historický údaj původního review o 120 sekundách pro shell Claude Code se nesmí používat jako obecná záruka pro všechny verze a konfigurace.

Pro všechny tři hostitele je nutné ověřit, že GUI přežije návrat spouštěcího příkazu. Postup respektuje aktuální oprávnění a již udělená povolení, nezavádí opakované dotazy, pokud nejsou potřeba. Omezení sandboxu nebo zamítnutí spuštění se nesmí maskovat instalací Pythonu či modelu. Použít hostitelem podporovanou lokální cestu podle H4, bez globálního vypínání ochran. Na Windows zdokumentovat vyvolání `.bat` ze skutečného shellu hostitele, včetně PowerShellu a Git Bashe.

## Podmínky přijetí a ověření

H5 lze uzavřít až po realizaci a skutečném ověření ve všech třech hostitelích. Stávající [ruční checklist](../tests/manual/hosts.md) zachycuje předchozí stav včetně čekání 240 sekund. Při implementaci se musí aktualizovat, tento dokument jej dnes neprohlašuje za splněný.

| Scénář | Očekávaný výsledek |
| --- | --- |
| Vše připravené, nahrávání i editor | První akce skillu je start. Bez předchozího `doctor`, synchronizace, sítě a stahování. Přijde potvrzení připraveného okna a agent čeká |
| Chybí uv, Python nebo knihovna | Rozpoznatelná chyba, cílená oprava agentem, opakovaný start stejného režimu |
| Chybí model, FFmpeg nebo obojí | Výsledek obsahuje všechny zjištěné součásti. Před opravou žádné nové okno ani relace, po opravě úspěšný start |
| Závislost byla smazána po úspěšném předchozím použití | Stejná kontrola při dalším spuštění, žádné spoléhání na příznak prvního startu |
| Vlastní model a existující systémový FFmpeg | Platná konfigurace se použije, součásti se zbytečně nestahují a vlastní model se nepřepíše |
| Jiný pracovní adresář, mezery, junction či symlink, nová cache pluginu | Správný kořen, prostředí a složka stavu, žádné použití cizího projektu |
| Nefunkční start GUI nebo ztracená odpověď | Žádné falešné `OK`. Zjištění stavu a konkrétní diagnostika, bez duplicitního okna |
| Čekání trvá více než dva intervaly | Agent pokračuje na stejné relaci. Nástrojové předání na pozadí se nezamění za konec relace |
| Process a zavření bez Process | Agent zpracuje správnou nahrávku pouze po Process. Zavření korektně ukončí čekání |
| Pomalá či přerušená instalace, nedostupná síť | Agent reportuje stav a sleduje stejnou úlohu. Navázání nebo konkrétní chyba, žádné souběžné opakované instalace |
| MCP bez modelu a FFmpeg | Server naběhne, start nástroje vrátí `SETUP_REQUIRED`, server zůstane připojený |
| Omezená oprávnění a životnost GUI | Ověření skutečného výsledku v hostiteli, chyba prostředí se nezamění s blokováním hostitelem |

Každý scénář zaznamenat pro Claude Code, Cursor a Codex včetně verze, OS, shellu, oprávnění a cesty CLI/MCP. Cursor editor a CLI, stejně jako Codex CLI a aplikaci, ověřit odděleně tam, kde se deklaruje jejich podpora. Windows musí mít skutečnou zkoušku `.bat`, Linux a macOS skutečnou zkoušku `.sh`, jinak jejich stav výslovně označit jako neověřený.

Rychlost měřit před změnou a po ní na stejném zařízení, zvlášť studený a opakovaný start nahrávání i editoru. Rozlišit čas rychlé kontroly od načítání GUI a seznamu nahrávek. Doložit, že kontrola nečte celý model ani neprovádí síťové operace. Konkrétní dobu startu nelze slíbit bez měření, požadavek je minimální režie společné kontroly a odstranění duplicitních procesů.

## Dotčené části při budoucí implementaci

- Oba `SKILL.md`, jejich spouštěče a kořenové `start-video-story.*`.
- Sdílený vstup a kontrola připravenosti pro CLI, `launch_control_window`, `launch_editor` a odpovídající MCP nástroje.
- Potvrzení připravenosti oken, návratové chyby a čekání na nahrávací i editorovou relaci.
- `setup`, kontrola prostředí a navazování přerušeného stahování.
- Instalační dokumentace, konfigurace hostitelů, cílené testy kontraktu a ruční checklist všech tří hostitelů.

H1 a H3 zachovat. Na H4 navázat ověřením skutečného hostitele. H5 zůstává otevřený, dokud neprojde jak spouštění a oprava závislostí, tak navazující čekání.
