# Video Story – audit závažných nedostatků

Stav k 9. 11. 2026, větev `ui-feedback-toolbar-transcribing` (commit `833e5fb`).

## Jak audit vznikl a jak ho číst

- **Rozsah:** celý plugin – zadání a dokumentace (`video-story-spec.md`, `video-story-spec-v2.md`, oba implementační plány, `README.md`, `docs/installation.md`, oba `SKILL.md`, `tests/manual/acceptance.md`) a všech asi 17 000 řádků `src/video_story/`.
- **Dva pohledy:** část A čte dokumenty očima business analytika, části B a C čtou kód očima seniorního vývojáře (B funkční vady, C návrh).
- **Co tu není:** styl, pojmenování, drobné neefektivity. Jen věci, které vedou ke ztrátě dat, k uvíznutí agenta, k bezpečnostnímu nebo soukromí ohrožujícímu chování, nebo které dělají kód dlouhodobě neudržitelným.
- **Stav nálezu:** *ověřeno* = celá cesta v kódu přečtena, mechanismus sedí; *pravděpodobné* = mechanismus přečten, spouštěč nebyl reprodukován.
- **Výchozí stav:** 384 jednotkových testů prochází (`uv run pytest tests/unit`). Nálezy níže jsou tedy místa, která testy nepokrývají – typicky chybové cesty, souběh procesů a dlouhé nahrávky.
- Audit sám nic v kódu neměnil. Opravy provedené týž den podle rozhodnutí autora shrnuje následující kapitola; texty nálezů níže popisují stav před opravami.

## Stav po opravách (9. 11. 2026)

| Nález | Stav | Co se změnilo |
| --- | --- | --- |
| A1 | vyřešeno | `video-story-spec.md` přepsána jako platný popis aktuálního chování; spec v2 a oba plány označeny jako historické; `acceptance.md` přepsán. |
| A2 | částečně | Tvrzení o soukromí opraveno v README, `SKILL.md` a instrukcích MCP. Záznam schránky bez vypínače a snímání všech obrazovek zůstávají (známé omezení ve specifikaci). |
| A3 | vyřešeno | Agent jedná jen po Process. Auto stop, nedostatek místa i chyba zápisu končí uložením a otevřením editoru; pád nebo neotevření editoru končí stavem `cancelled`. Auto process je při otevření editoru vždy odškrtnutý. |
| A4 | otevřeno | Stavy a pravidla jsou nově popsány ve specifikaci (kap. 9); verzování `state.json` a nikdy nenastavený stav `discarded` zůstávají. |
| A5 | částečně | Poruchy a obnova jsou ve specifikaci (kap. 10); zaveden limit 1 GB volného místa. |
| A6 | vyřešeno | Poctivá matice ověření ve specifikaci, README a instalační příručce. |
| A7 | odloženo | Rozhodnutí autora: zatím neřešit. |
| B1 | vyřešeno | Hlídání 1 GB před Record i za běhu; zapisovač zvuku chybu přežije a `stop()` neblokuje; chyba zápisu nahrávání zastaví s hláškou a uloží. |
| B2 | vyřešeno | Neúspěšný snímek se přeskočí a počítá (`failed_grabs`), snímání se samo obnoví. |
| B3 | částečně | Limit 1 GB. Ukládání nezměněných snímků a retence podle velikosti zůstávají. |
| B4 | vyřešeno | Manifest vzniká při Record, keypointy se ukládají hned, v okně se nepřepisuje, osiřelé nahrávky dokončí příští start (`host/recovery.py`), přepis lze v editoru opakovat, chybějící model nebo FFmpeg se oznámí při Record. |
| B5 | vyřešeno | Relace se při změně složky přesune (`SessionController.relocating`); Settings ukládá do souboru, se kterým bylo okno spuštěno. |
| B6 | vyřešeno | Auto process se odškrtne při výběru jiné nahrávky; Process se odmítne, dokud přepis čekané nahrávky není úplný. |
| B7 | odloženo | Rozhodnutí autora: zatím neřešit. V instalační příručce přibylo upozornění. |
| B8 | odloženo | Rozhodnutí autora: FFmpeg z pracovního adresáře nevadí; ověřený `ffmpeg.exe` se přibalí, až vznikne skript pro distribuci. |
| B9 | otevřeno | Zmírněno: ztracený požadavek už nevede ke zpracování celé nahrávky, relace skončí `cancelled`. |
| B10 | otevřeno | |
| B11 | otevřeno | Zmírněno opravou B2: odpojený monitor už snímání neshodí. |
| B12 | otevřeno | |
| B13 | částečně | Přibylo Retry transcription. Neatomický zápis přepisu zůstává. |
| B14–B17 | otevřeno | |
| C1 | částečně | Relaci ve stavu `editing` už žádná cesta nepřevádí na `processed` bez stisku Process. Více zapisovatelů `state.json` zůstává. |
| C2 | částečně | Finalizace je opakovatelná a manifest se přesouvá poslední. Dvojí umístění zůstává. |
| C3 | otevřeno | Zkopírovaný text se už neztrácí při zahozeném tiku. |
| C4 | vyřešeno | Zůstala jedna cesta přepisu (proudový pracovní proces editoru). |
| C5 | částečně | Rekordér vystavuje chybu, důvod zastavení a počet nepořízených snímků a okno je sleduje. Velké třídy zůstávají. |
| C6 | otevřeno | |

## Shrnutí – co řešit nejdřív

| # | Nález | Závažnost |
| --- | --- | --- |
| B1 | Plný disk nebo chyba zápisu: nahrávání tiše pokračuje bez ukládání, Stop pak navždy zamrzne | kritická |
| A7 | Heslo k testovacímu stroji ve veřejném repozitáři | vysoká, okamžitě |
| B7 | MCP server přes HTTP nemá autentizaci a umí `config set` včetně cesty ke spouštěné binárce | vysoká (kritická, pokud se HTTP režim používá) |
| B2 | Jediný neúspěšný snímek obrazovky (UAC, zámek PC) ukončí snímání do konce nahrávky | vysoká |
| B3 | Úložiště roste asi 3,2 GB/h bez kontroly volného místa | vysoká |
| B4 | Nahrávku po selhání zpracování nebo pádu nelze dokončit žádným nástrojem | vysoká |
| B5 | Změna Recording Folder v Settings rozbije běžící relaci | vysoká |
| B6 | Process a Auto process se řídí zobrazenou nahrávkou, ne tou, na kterou agent čeká | vysoká |
| A2 | Tvrzení „nic neopouští počítač“ neplatí; schránka se zaznamenává bez vědomí uživatele | vysoká |
| A3 | Není definováno, kdy smí agent začít jednat – chybové cesty končí „zpracuj celou nahrávku“ | vysoká |
| A1 | Neexistuje jeden platný popis produktu; dokumenty si odporují | vysoká |

---

## A. Zadání a dokumentace (pohled business analytika)

### A1 · Neexistuje jeden platný popis produktu

**Závažnost:** vysoká · **Stav:** ověřeno

Požadavky jsou rozložené do šesti dokumentů, které si odporují navzájem i s implementací. Kdo chce vědět, jak se produkt má chovat, musí číst kód.

| Téma | Dokument | Skutečnost |
| --- | --- | --- |
| Volby po Stop | spec v1: Process / Continue / Discard; spec v2 §5: čtyři volby včetně Process Recording | tři volby: Video Editor / Continue / Discard (README, SKILL); `acceptance.md` řádky 5 a V4 stále ověřují Process Recording |
| Co se stane po Process | spec v2 §6.3 a potvrzený bod 5: editor zůstává otevřený | Process editor zavře (`editor/window.py:1193`) |
| Kde běží přepis | spec v2 §5.2: v nahrávacím okně s dialogem, potom se otevře editor | editor se otevře hned a přepisuje na pozadí; `acceptance.md` krok 6 popisuje obojí zároveň |
| Výchozí hodnoty | spec v1 a plán §4.1: 250 ms, PNG, auto stop 30 min, model small | 500 ms, JPEG 70, 120 min, large-v3-turbo; `acceptance.md` řádek 2 tvrdí „500 ms and PNG“ |
| Stav dokumentů | spec v2: „návrh k potvrzení, nic není implementováno“; plán editoru: „návrh k odsouhlasení“ | obojí je hotové (kapitoly 13 a 14 téhož plánu) |
| Struktura | plán §2.2 a §10 slibuje `scheduler.py`, `handoff.py`, `discard_dialog.py`, `.mcp.json`, `config.example.json` | žádný z těch souborů neexistuje |

Funkce přidané 9. 11. nemají zadání vůbec: keypointy, záznam schránky, Instructions a šablony, Auto process, textová časová osa, přepis v editoru s navázáním, editace titulků, mazání nahrávek, zoom při kreslení, spouštěcí skripty. Existují jen jako odstavce v README (jeden z nich má přes 2 500 znaků) a v `SKILL.md`, tedy v textech psaných pro jiného čtenáře a k jinému účelu.

**Směr řešení:** jeden živý dokument „aktuální chování“ (funkční specifikace po oblastech), vedle něj záznam rozhodnutí s datem (co se změnilo a proč). Spec v1 a v2 označit jako historické. `acceptance.md` přegenerovat z aktuálního chování. README zkrátit na to, co potřebuje nový uživatel.

### A2 · Tvrzení o soukromí neodpovídá tomu, co produkt dělá

**Závažnost:** vysoká · **Stav:** ověřeno

1. **„Nic neopouští počítač“ neplatí.** README („no audio or images leave the machine“), `SKILL.md` („nothing is uploaded“) i instrukce MCP serveru (`host/mcp_server.py:89`) to slibují. Smyslem pluginu je ale předat přepis, texty ze schránky a snímky obrazovek agentovi – a ten je posílá poskytovateli modelu. `video_story_get_frame` vrací obrázek přímo jako obsah odpovědi; ve scénáři ChatGPT navíc přes veřejný tunel. Lokální je jen nahrávání a přepis.
2. **Schránka se zaznamenává bez vědomí uživatele.** Sledování začíná s Record bezpodmínečně (`ui/control_window.py:243-252`), každý nový text až do 65 535 znaků jde do sidecaru (`ui/clipboard.py:27`, `capture/recorder.py:380`) a přes `timeline` a `frames --at` k agentovi. Tooltip tlačítka Record říká jen „audio, screens and cursor“. V `Config` není vypínač. Kód nerespektuje formáty, kterými správci hesel na Windows žádají o vynechání ze sledování schránky (`ExcludeClipboardContentFromMonitorProcessing`). Heslo nebo token zkopírovaný během nahrávání tak skončí v prostém textu na disku na 30 dní a v kontextu modelu.
3. **Snímají se vždy všechny obrazovky**, bez možnosti některou vynechat nebo nahrávání pozastavit, a ukládají se nešifrovaně do `~/Videos/VideoStory`.

**Směr řešení:** formulaci opravit na „nahrávání a přepis běží lokálně; podklady, které si agent přečte, odcházejí poskytovateli modelu“. Schránku udělat viditelným nastavením s indikací v okně a respektovat vylučovací formáty. V zadání definovat, co je citlivý obsah a jak se mu uživatel vyhne (výběr obrazovek, pauza).

### A3 · Není definováno, kdy smí agent začít jednat

**Závažnost:** vysoká · **Stav:** ověřeno

Základní pravidlo zní rozumně: agent jedná po stisku Process, zavření editoru znamená „nedělej nic“. Chybové a automatické cesty ho ale obcházejí a každá jinak:

| Cesta | Výsledek pro agenta | Kde |
| --- | --- | --- |
| Uživatel stiskl Process | `processed` s cropem | `host/editor_session.py:242` |
| Uživatel zavřel editor | `cancelled`, nedělej nic | `host/editor_session.py:294` |
| Auto stop po 120 min | `processed`, celá nahrávka, bez editoru a bez potvrzení | `ui/control_window.py:481-484` |
| Snímací vlákno spadlo | totéž | `ui/control_window.py:485-492` |
| Editor spadl nebo byl ukončen | `processed`, „act on the whole recording“, případně s neúplným přepisem | `host/runner.py:161-178` |
| Editor se nepodařilo otevřít | `processed` bez přepisu | `host/controller.py:240-248` |
| Auto process | Process se stiskne sám 5 s po dokončení přepisu; volba je trvalá | `editor/window.py:1372-1407` |

Dvouhodinová nahrávka, na kterou uživatel zapomněl, tak skončí tím, že agent sám provede vše, co během ní zaznělo – třeba s instrukcí „Implement“. Pád editoru má opačný důsledek než jeho zavření, přestože uživatel v obou případech nic nepotvrdil.

**Směr řešení:** do zadání dát jedno pravidlo – agent jedná jen po výslovném potvrzení uživatele. Automatické a chybové cesty mají končit stavem, který agentovi říká „nahrávka je uložená, zeptej se uživatele“, ne `processed`.

### A4 · Kontrakt pro agenta není popsán ani verzován

**Závažnost:** střední · **Stav:** ověřeno

- Pole `state.json`, stavy relace a události editoru existují jen v kódu a v próze `SKILL.md`. `state.json` nemá verzi schématu (manifest ji má).
- Stav `discarded` je deklarován jako koncový (`core/session.py:61`) a instrukce MCP ho agentovi popisují (`host/mcp_server.py:77`), ale žádný kód ho nenastaví – `SessionController.discard()` vrací relaci do `ready`.
- Agent má dva různé protokoly podle toho, jak editor vznikl: `wait --session` vrací stavy, `wait --editor` vrací události s jiným tvarem výsledku. Rozhodnutí „Process stisknutý nad jinou nahrávkou ukončí čekající relaci s jejím vlastním cropem“ je popsáno jen v plánu editoru a vede k vadě B6.
- `docs/installation.md` ve výčtu MCP nástrojů neuvádí `video_story_wait_editor`.

**Směr řešení:** jeden popsaný a verzovaný kontrakt (JSON schéma výsledku předání), stejný tvar pro obě cesty. Mrtvý stav odstranit.

### A5 · Chybí nefunkční požadavky a chybové scénáře

**Závažnost:** vysoká · **Stav:** ověřeno

Zadání ani akceptační checklist neobsahují nic o tom, co se má stát, když dojde místo na disku, odpojí se mikrofon nebo monitor, spadne okno, selže přepis nebo se zamkne počítač. Právě v těchto místech leží většina vad z části B. Chybí také meze:

- **Disk:** změřeno na skutečné nahrávce (2560×1600 + 1920×1080, 500 ms, JPEG 70) asi 3,2 GB a 28 800 souborů za hodinu; při PNG řádově 14–43 GB/h. Žádný požadavek na varování nebo strop.
- **Délka:** auto stop lze nastavit až na 1 440 minut, ale WAV má strop 4 GB (asi 12,4 h při 48 kHz) a přepis v okně načítá celý zvuk do paměti.
- **Interval 1 ms** je povolený, ačkoli plán sám říká, že není reálně dosažitelný.

Plán navíc slibuje, že `doctor` opraví nahrávku po pádu (§5); `host/doctor.py` nic takového nemá.

**Směr řešení:** doplnit kapitolu nefunkčních požadavků (disk, paměť, maximální délka, chování při každé z uvedených poruch) a ke každé akceptační test.

### A6 · Deklarovaný rozsah neodpovídá ověřenému

**Závažnost:** střední · **Stav:** ověřeno

README nazývá Video Story pluginem pro Claude Code, Codex, Claude Desktop a ChatGPT na Windows, Linuxu a macOS. Podle `acceptance.md` (řádky 6 a E6) je skutečně vyzkoušené jen Claude Code přes junction na Windows. Codex, Claude Desktop a ChatGPT nebyly nikdy spuštěny, editor nebyl ověřen mimo Windows, macOS nikdy na fyzickém stroji a automatické stažení FFmpeg tam nefunguje. Instalace jako plugin z marketplace reálně nefunguje (viz C6) a dokumentace sama doporučuje junction.

**Směr řešení:** do README matici podpory se třemi poctivými stavy (ověřeno / implementováno, neověřeno / nepodporováno).

### A7 · Přihlašovací údaje ve veřejném repozitáři

**Závažnost:** vysoká, řešit okamžitě · **Stav:** ověřeno

`implementation-plan.md:590` obsahuje IP adresu, login a heslo testovacího stroje s Fedorou. Soubor je verzovaný a repozitář je veřejný na GitHubu. Adresa je z privátního rozsahu, takže dosah je omezený, heslo je ale kompromitované.

**Směr řešení:** heslo změnit, řádek odstranit a přepsat historii (`git filter-repo`), protože je ve všech dosavadních commitech.

---

## B. Funkční vady (pohled vývojáře)

### B1 · Plný disk nebo chyba zápisu: tichá ztráta dat a zamrzlé Stop

**Závažnost:** kritická · **Stav:** ověřeno

- `capture/audio_recorder.py:165-175` – `_write_loop` nemá ošetření výjimek. První `OSError` z `writeframes` ukončí zapisovací vlákno; callback dál plní omezenou frontu a zahozené bloky jen počítá.
- `capture/audio_recorder.py:214` – `stop()` volá blokující `self._queue.put(None)`. S plnou frontou a mrtvým konzumentem se nikdy nevrátí.
- `capture/recorder.py:389-396` – chyba zápisu snímku se jen zaloguje do soukromého `_error`; snímání pokračuje.
- `ui/control_window.py:477-501` – okno sleduje jen `running` a `auto_stopped`, chybu zápisu nevidí. `closeEvent` (`:509-511`) během zpracování zavření odmítá.

Stejně skončí i zvuk delší než 4 GB (stdlib `wave` vyhodí výjimku při zápisu hlavičky).

**Scénář:** disk se zaplní ve 40. minutě. Stavový řádek dál počítá čas, uživatel mluví dalších 20 minut do prázdna. Po Stop → Video Editor okno ukazuje „Opening the Video Story Editor…“ navždy, nejde zavřít, `state.json` zůstává `recording` a agentův `wait` nikdy neskončí. Pomůže jen Správce úloh.

**Směr řešení:** zapisovací vlákno musí chybu zachytit, zaznamenat a frontu dál vyprazdňovat; `stop()` nesmí blokovat. `Recorder` má vystavit zdravotní stav (chyba, živé vlákno, čas posledního zvukového bloku), který okno v `_tick` kontroluje a při první chybě nahrávání viditelně ukončí.

### B2 · Jediný neúspěšný snímek ukončí snímání do konce nahrávky

**Závažnost:** vysoká · **Stav:** mechanismus ověřen, spouštěč na Windows pravděpodobný

- `capture/recorder.py:360-361` volá `grabber.grab(screen)` bez ošetření; jakákoli výjimka opustí smyčku (`:325-329`) a vlákno skončí.
- `mss` na Windows vyhazuje výjimku při selhání `BitBlt`, k čemuž dochází při zobrazení zabezpečené plochy (dialog UAC, Win+L, Ctrl+Alt+Del).
- Auto stop se kontroluje jen uvnitř téže smyčky (`:297`). Po jejím konci okno otevře modální hlášku (`ui/control_window.py:485-491`) a zvuk se nahrává dál, dokud ji někdo nepotvrdí.

**Scénář:** uživatel při předvádění instalátoru vyvolá UAC, nebo se počítač během výkladu zamkne. Zbytek nahrávky nemá žádné snímky. Pokud u počítače není, zvuk se nahrává hodiny za limit auto stopu.

**Směr řešení:** selhání snímku brát jako chybu jedné obrazovky v jednom tiku – započítat, v dalším tiku zkusit znovu, po N selháních grabber otevřít znovu. Auto stop hlídat nezávisle na snímacím vlákně.

### B3 · Úložiště roste asi 3,2 GB/h bez jakékoli kontroly

**Závažnost:** vysoká · **Stav:** ověřeno, změřeno

`capture/recorder.py:364-387` zapisuje každý snímek každé obrazovky bez ohledu na to, zda se změnil. V celém `src/video_story` není jediná kontrola volného místa. Retence (`host/retention.py:123-136`) maže jen podle stáří a jen při startu nahrávacího okna.

Změřeno na nahrávce `20261109-203427` (644 s): 560 MB, 5 156 souborů v jednom adresáři, tedy asi 3,2 GB a 28 800 souborů za hodinu; dvouhodinová nahrávka má kolem 6,4 GB a 57 600 souborů. Při denním používání to je 100–200 GB v třicetidenním okně na systémovém disku – a pak nastává B1.

**Směr řešení:** kontrola volného místa před Record a průběžně (varování, pak čisté ukončení). Nezměněný snímek neukládat znovu, jen na něj odkázat (rozhodnutí 9 v plánu to zakázalo; při těchto číslech stojí za přehodnocení). Retence i podle velikosti.

### B4 · Nahrávku po selhání zpracování nebo pádu nelze dokončit

**Závažnost:** vysoká · **Stav:** ověřeno

- `host/controller.py:177-201` – manifest se zapisuje až po úspěšném přepisu. Při chybě je stav `failed` a soubory zůstanou v `.work/<id>`.
- Přepis v nahrávacím okně běží jen po auto stopu nebo pádu snímání, tedy u nejdelších nahrávek. Chybějící model nebo FFmpeg se před Record nekontroluje.
- Rozložení obrazovek, nastavení a keypointy žijí jen v paměti (`capture/recorder.py:128, 157, 176`), takže po pádu procesu nejsou nikde.
- `host/editor_launcher.py:33-50` – editor ukazuje jen finální složky s manifestem. `video-story transcribe` nad pracovní složkou (`host/cli.py:288-322`) manifest nevytvoří a nahrávku nepřesune.
- Uživatel se dozví jen „The recorded files were kept in the work folder“ (`ui/control_window.py:375-379`).

Samotný `audio.wav` je po pádu v pořádku (stdlib `wave` opravuje hlavičku při každém bloku), takže data existují – jen se k nim nelze dostat.

**Scénář:** čerstvá instalace bez `video-story setup`, dvouhodinová nahrávka skončí auto stopem, přepis selže. Nahrávka leží ve skryté složce `.work`, žádný nástroj ji neotevře a po 30 dnech ji retence smaže.

**Směr řešení:** manifest zapsat už při startu nahrávání (obrazovky, nastavení, čas) a keypointy přidávat průběžně. Vždy nejdřív finalizovat, přepis brát jako opakovatelný krok nad finální složkou. Přidat příkaz `recover <session>` a předletovou kontrolu modelu a FFmpeg při Record.

### B5 · Změna Recording Folder v Settings rozbije běžící relaci

**Závažnost:** vysoká · **Stav:** ověřeno

- `ui/settings_dialog.py:242-255` uloží novou složku do `config.json`; `ui/control_window.py:396-397` vymění jen `controller.config`. `controller.paths` vznikl při startu (`ui/app.py:52`) a ukazuje dál na starou složku.
- Nahrávka tedy vznikne ve staré složce. `launch_editor(self.controller.config, …)` (`ui/control_window.py:351-356`) ji hledá v nové, selže a okno spadne do `editor_failed` – relace je `processed` bez přepisu.
- Agentovo `wait --session <id>` (`host/runner.py:54-57`) hledá `state.json` podle uložené konfigurace, tedy také v nové složce, a skončí chybou `UNKNOWN_SESSION`.
- `_save` navíc ukládá do výchozího `config.json` i tehdy, když bylo okno spuštěno s `--config`.

**Scénář:** uživatel otevře okno, v Settings přesune složku na větší disk a nahrává. Dostane „The editor could not be opened“ a agent ztratí relaci.

**Směr řešení:** relaci hledat podle cesty ke stavovému souboru, kterou `run --detach` vrací, ne podle měnitelné konfigurace. Změnu složky buď uplatnit až na další relaci a říct to, nebo relaci před prvním Record založit znovu.

### B6 · Process a Auto process se řídí zobrazenou nahrávkou

**Závažnost:** vysoká · **Stav:** ověřeno

- `editor/window.py:1208-1215` (`actions_allowed`) a `:1372-1389` (odpočet Auto process) testují úplnost přepisu jen u právě zobrazené nahrávky.
- `process()` (`:1165-1180`) pak dokončí `self.recording_session`, i když jde o jinou nahrávku – jen to zaloguje. `finish_recording_session` nastaví `processed` a `progress = 1.0` bez kontroly jejího přepisu.
- Odpočet se znovu nabíjí při každém výběru nahrávky. Po Process se okno zavře a `closeEvent` (`:1657`) zabije běžící přepis.

**Scénář:** Stop → Video Editor nad nahrávkou A, přepis poběží několik minut, Auto process je zaškrtnutý (volba je trvalá). Uživatel klikne na starší hotovou nahrávku B a pět sekund se na ni dívá. Process se stiskne sám: A je `processed` s částečným přepisem, editor se zavře, přepis A je ukončen. Agent jedná nad neúplným podkladem a nedostane ani upozornění, které má cesta přes pád editoru.

**Směr řešení:** Process i odpočet podmínit přepisem nahrávky, na kterou agent čeká. Odpočet spouštět jen přechodem „přepis čekané nahrávky dokončen“.

### B7 · MCP server přes HTTP: bez autentizace, s `config set`

**Závažnost:** vysoká, kritická při použití HTTP režimu · **Stav:** ověřeno

- `host/mcp_server.py:718-740` spouští `streamable-http` bez jakéhokoli ověření; `docs/installation.md:142-151` doporučuje vystavit ho veřejným tunelem.
- `tool_config` (`:409-437`) dovolí nastavit libovolný klíč konfigurace, tedy i `ffmpeg_path`, `whisper_cli_path`, `whisper_backend` a `recording_folder`. `transcribe/ffmpeg.py:121-126` nastavenou cestu spustí, pokud soubor existuje.
- `video_story_get_frame`, `get_transcript` a `timeline` vracejí obsah všech uložených nahrávek. `video_story_export` zapisuje na libovolnou absolutní cestu. `resolve_session` (`host/runner.py:54-64`) přijme libovolný adresář a `tool_wait_editor` libovolný soubor.

**Scénář:** kdokoli, kdo zná adresu tunelu, si stáhne snímky všech obrazovek a texty schránky za posledních 30 dní. Nastavením `ffmpeg_path` na vlastní spustitelný soubor (na Windows stačí síťová cesta) a zavoláním exportu nebo doctoru spustí kód pod účtem uživatele. Totéž zvládne i agent ovlivněný podvrženým textem přes stdio.

**Směr řešení:** HTTP režim nespustit bez tokenu. Klíče konfigurace rozdělit na bezpečné a na cesty ke spustitelným souborům, které přes MCP nastavit nejdou. Vstupní cesty omezit na složku nahrávek.

### B8 · Spouštěné binárky nejsou důvěryhodně určeny

**Závažnost:** vysoká (bezpečnost) · **Stav:** ověřeno

1. **FFmpeg z pracovního adresáře.** `transcribe/ffmpeg.py:128` používá `shutil.which("ffmpeg")`, které na Windows hledá nejdřív v aktuálním adresáři, a to včetně `.bat` a `.cmd`. Nahrávací okno i editor startují s `cwd=Path.cwd()` (`host/runner.py:141`, `host/editor_launcher.py:112`), což je projekt, ve kterém agent pracuje. PATH má navíc přednost před přibalenou kopií. Klonovaný repozitář s `ffmpeg.bat` v kořeni tak dostane svůj soubor spuštěný při nejbližším přepisu nebo exportu.
2. **Stažení bez ověření.** `transcribe/ffmpeg.py:246-247` nastaví `sha256=None`, když odpověď GitHub API nemá `digest`, a `host/downloads.py:79-81` pak kontrolu mlčky přeskočí. Když digest existuje, pochází ze stejného zdroje jako soubor (průběžná značka `latest` třetí strany). Linuxový zdroj má jen MD5 ze stejného serveru. Binárka se hned poté spouští.

**Směr řešení:** preferovat přibalenou kopii, z PATH brát jen absolutní cesty mimo pracovní adresář, GUI procesy spouštět s neutrálním `cwd`. Stahovat konkrétní vydání s hashem uloženým v repozitáři a bez hashe selhat.

### B9 · Požadavky na připojení k editoru se tiše ztrácejí

**Závažnost:** vysoká · **Stav:** ověřeno (část o zámku: mechanismus ověřen, časové okno pravděpodobné)

- `editor/window.py:1475-1486` – `check_attach_requests` nic nedělá, dokud je otevřený modální dialog nebo se nahrávka načítá.
- `host/editor_session.py:195-207` – požadavek starší 60 s se smaže jen se záznamem v logu: bez události `closed`, bez zrušení relace. Druhá cesta zahození (`_decline_attach_requests`, `window.py:1488-1511`) agenta informuje; obě se rozešly.
- Spouštěč mezitím už odpověděl `attached: true` s PID živého editoru (`host/editor_launcher.py:85-99`).
- Editor při zavírání drží zámek jedné instance ještě několik sekund po tom, co přestal požadavky obsluhovat (`window.py:1654-1665`, zámek se uvolní až po `app.exec()`).

**Scénář:** editor renderuje dlouhou nahrávku nebo má minutu otevřený dialog Instructions. Uživatel v novém nahrávacím okně zvolí Stop → Video Editor. Požadavek propadne. `wait --editor` vidí živý proces a nikdy neskončí; relace zůstane `editing`, dokud někdo editor nezavře, a pak se přes náhradní cestu stane `processed` s celou nahrávkou.

**Směr řešení:** požadavky číst z disku vždy a držet je ve frontě v paměti, dokud dialog neskončí. Každý zahozený požadavek musí projít cestou, která agenta informuje. Spouštěč má čekat na potvrzení přijetí a jinak spustit editor sám.

### B10 · Zvuk a obraz nemají společné hodiny

**Závažnost:** střední · **Stav:** mechanismus ověřen, odchylka změřena

Časy snímků jdou z `perf_counter`, časy přepisu z pořadí vzorku ve WAV. Callback ignoruje časovou značku převodníku a příznak přetečení jen zaloguje na úrovni debug (`capture/audio_recorder.py:157-159`). Při přetečení fronty se zahodí celé bloky bez doplnění ticha (`:161-163`) a `dropped_blocks` se do manifestu nezapíše. Odpojení mikrofonu nikdo nezjistí.

Ve všech čtyřech kontrolovaných nahrávkách je zvuk o 0,4–0,6 s kratší než časová osa. Kód neumí říct, zda chybí začátek (pak je každý čas v přepisu o půl sekundy dřív) nebo konec.

**Scénář:** náhlavní souprava se ve 5. minutě ze 40 odpojí – přepis tam bez varování končí. Nebo se pod zátěží ztratí bloky a každá další věta se přiřadí ke starším snímkům, než které popisuje.

**Směr řešení:** čas prvního bloku vztáhnout k hodinám nahrávky a posun uložit do manifestu. Za ztracené bloky zapsat ticho. Počet ztrát a rozdíl délek ukládat a v okně varovat, když zvuk přestane přibývat.

### B11 · Rozložení obrazovek se čte jen na začátku

**Závažnost:** střední · **Stav:** ověřeno

`self.screens` se přiřadí jednou (`capture/recorder.py:211`) a používá se pro každý snímek i pro mapování kurzoru. Odpojení od doku nebo připojení projektoru uprostřed nahrávky znamená černé či částečné snímky odebrané obrazovky, nová se nenahrává vůbec a kurzor na ní je `null` nebo na špatné obrazovce. Na macOS a Linuxu chybějící oblast vyhodí výjimku, tedy B2.

**Směr řešení:** rozložení průběžně kontrolovat (signály Qt `screenAdded` a `geometryChanged`). Při změně buď zapsat novou epochu rozložení do manifestu a sidecarů, nebo nahrávání s jasnou hláškou ukončit.

### B12 · macOS Retina: tři části počítají s jiným měřítkem

**Závažnost:** střední · **Stav:** pravděpodobné (nekonzistence ověřena čtením, fyzický Mac nebyl k dispozici)

`platform/macos.py:145-146` odvozuje `pixel_ratio` z `CGDisplayPixelsWide`, `core/screens.py` jím násobí kurzor i `image_size`, ale `mss` snímá v nominálním rozlišení (1×) a overlay násobí body kresby hodnotou `devicePixelRatio` z Qt (2,0). Skutečná šířka snímku se s tím nikde neporovnává. Ať `CGDisplayPixelsWide` vrací body, nebo pixely, jedna ze tří věcí (kurzor, kresby, velikost v manifestu) bude vůči obrázku dvojnásobná.

**Směr řešení:** měřítko každé obrazovky odvodit z prvního pořízeného snímku (`shot.width / screen.width`) a tutéž hodnotu použít pro kurzor, kresby i manifest.

### B13 · Přepis v editoru: neatomický zápis, ztracený segment, nevratné selhání

**Závažnost:** střední · **Stav:** mechanismus ověřen, četnost selhání pravděpodobná

- `core/transcript.py:91-95` zapisuje `transcript.txt` a `.json` přímo, bez dočasného souboru a opakování – na rozdíl od ostatních sdílených souborů. `editor/recording.py:214-235` to dělá po každém segmentu, v GUI vlákně, vždy celý soubor.
- Když zápis selže, `editor/transcription.py:224-228` výjimku zaloguje a pokračuje; segment není v paměti ani v souboru a `transcribed_ms` se posune dál. V „úplném“ přepisu je díra.
- Proces ukončený uprostřed zápisu nechá zkrácený JSON; `transcript_status` ho bere jako „čeká od nuly“, nahrávka se přepíše znovu a ruční opravy titulků jsou pryč.
- Selhaný přepis je trvalý (`editor/window.py:1341-1342`): jediný způsob opakování je zavřít editor, a to agentovi pošle `cancelled`. Zaseknutý pracovní proces nikdo nehlídá.

**Směr řešení:** zapisovat atomicky přes `core/fileio.atomic_write_text`, dávkovaně a mimo GUI vlákno; při chybě segment držet v paměti a zkusit znovu. Přidat akci „zkusit znovu“, která naváže od `transcribed_ms` a relaci agenta nechá žít.

### B14 · Neúspěšné Discard se ohlásí jako úspěšné

**Závažnost:** střední · **Stav:** obsluha ověřena, zamčený soubor jako spouštěč pravděpodobný

`ui/control_window.py:282-296` výjimku z `controller.discard()` ukáže a hned poté okno přepne do stavu Record s textem „Recording discarded“. `core/session.py:177-194` maže bez opakování a zastaví se na prvním zamčeném souboru. Další Record zapisuje do téže pracovní složky a snímky se jmenují podle času.

**Scénář:** uživatel zahodí pokus, protože na obrazovce bylo něco citlivého. Mazání skončí v půlce a další, kratší nahrávka se finalizuje i s pozdějšími snímky té zahozené.

**Směr řešení:** mazání opakovat a při neúspěchu zůstat v chybovém stavu. Každý pokus má mít vlastní podsložku nebo vlastní id relace, aby se zbytky nemohly smíchat.

### B15 · Export: závislost na konkrétní verzi FFmpeg a zahozený hotový render

**Závažnost:** střední · **Stav:** obsluha ověřena; selhání na novější verzi pravděpodobné

- `export/ffmpeg_export.py:115-116` používá `-filter_complex_script`, jakmile graf přesáhne 12 000 znaků (zhruba 2–3 minuty pohybu myši). FFmpeg 7.1 tu volbu hlásí jako zastaralou. `export/plan.py:31-33` má natvrdo konstantu pro chování verze 7.1. Instalovaná verze přitom není nijak omezena – `setup` stahuje nejnovější sestavení a z PATH může přijít cokoli. Krátké testovací klipy jdou jinou větví, takže testy projdou.
- `export/ffmpeg_export.py:400-409` po celém kódování udělá prosté `os.replace` bez opakování; při neúspěchu `finally` smaže i hotový dočasný soubor. Stačí mít předchozí MP4 otevřené v přehrávači a dlouhý render je pryč.

**Směr řešení:** verzi FFmpeg jednou zjistit a zvolit odpovídající zápis; přidat integrační test, který vynutí větev se skriptem. Před startem ověřit, že cíl jde přepsat; při neúspěšném přejmenování dočasný soubor ponechat a ohlásit jeho cestu.

### B16 · Editor na dlouhých nahrávkách blokuje GUI vlákno

**Závažnost:** střední · **Stav:** cesty v kódu ověřeny, délka zamrznutí je odhad

- Seznam nahrávek prochází disk synchronně uvnitř vykreslování: `editor/recordings_view.py:81-91, 350-362` staví `FrameIndex` přes `os.scandir` celé složky snímků (57 600 položek pro 2 h) pro každý viditelný řádek a cache se maže při každém resetu modelu (`:309-313`) – tedy i automaticky po dokončení přepisu.
- Nahrazená práce na pozadí se neruší: každý posun přehrávací hlavy zařadí dekódování v plném rozlišení (`editor/preview.py:376-384`), každý výběr nahrávky spustí průchod celým WAV bez možnosti zrušení a bez uložených špiček (`editor/timeline/waveform.py:116-121`). Fronta se pak musí dopočítat i při zavírání okna, což prodlužuje okno z B9.
- Sdílené soubory se zapisují z GUI vlákna s čekacími smyčkami až 3 s (`core/fileio.py`).

**Směr řešení:** procházení disku do pracovního vlákna, jeden index snímků na nahrávku sdílený mezi seznamem a náhledem. Jeden pomocník „poslední požadavek vyhrává“ pro všechny úlohy na pozadí. Špičky zvuku ukládat k nahrávce.

### B17 · Živost relace je holé číslo procesu; retence nezná otevřený editor

**Závažnost:** střední · **Stav:** mechanismus ověřen, spouštěč závisí na okolnostech

- `platform/process.py:11-37` testuje jen existenci procesu s daným PID. Spoléhá na to `active_session`, `session_status` i retence; soubor `lock` relace je jen PID jako text (`host/controller.py:59`). Po pádu okna a přidělení téhož PID jinému procesu vrací `run` navždy `ALREADY_RUNNING`, skill přikáže agentovi tu relaci převzít a `wait` nikdy neskončí.
- Editor neoznačuje zobrazenou nahrávku jako používanou. Retence při startu nahrávacího okna (`ui/app.py:17-30`) může smazat nahrávku, která je právě otevřená nebo se renderuje; další úprava cropu pak složku znovu vytvoří jen s `crop.json`.
- Dvě souběžná `run --detach` projdou kontrolou zároveň a otevřou dvě okna.

**Směr řešení:** stejný zámek operačního systému, jaký už má editor v `host/editor_lock.py`, použít i pro nahrávací okno a pro nahrávku otevřenou v editoru.

---

## C. Návrh a architektura

### C1 · Meziprocesní protokol: sdílené soubory bez vlastníka a bez potvrzení

**Závažnost:** vysoká · **Stav:** ověřeno

Agent, nahrávací okno a editor se domlouvají přes `state.json`, soubor relace editoru a složku požadavků. Všechny tři jsou JSON přepisované stylem „přečti, uprav, zapiš, poslední vyhrává“ a živost protistrany se odhaduje z PID.

- `state.json` zapisují tři různé procesy: okno ze své kopie v paměti (`host/controller.py:71-80`), editor (`host/editor_session.py:214-306`) a také čtenář – `session_status` při zjištění mrtvého procesu stav sám přepíše (`host/runner.py:158-184`). Čtení má tedy vedlejší účinek a pozdější zápis okna (`editor_opened`) může přepsat, co mezitím zapsal editor.
- Relaci ve stavu `editing` ukončují tři různé funkce (`finish_recording_session`, náhradní cesta v `runner.py`, `controller.editor_failed`) a liší se v cropu, instrukcích i v upozornění na neúplný přepis.
- Agent má dva kanály s různým tvarem výsledku (viz A4).
- Žádná zpráva nemá potvrzení přijetí.

Vady B6, B9 a B17 jsou přímým důsledkem. Každá byla nebo bude opravena zvlášť, ale třída chyb zůstane.

**Směr řešení:** každý soubor má právě jednoho zapisovatele; ostatní posílají požadavky. Jedna funkce „ukonči relaci“ s jedním tvarem výsledku. Jeden kanál pro agenta bez ohledu na to, odkud editor vznikl. Čtení bez vedlejších účinků.

### C2 · Dvojí umístění nahrávky a finalizace, kterou nelze zopakovat

**Závažnost:** střední · **Stav:** ověřeno

Nahrávka vzniká v `.work/<id>` a pak se po položkách přesouvá do `<id>` (`core/session.py:161-175`). Přesun není atomický: selže-li po přesunutí `audio.wav` a před `frames/`, existují obě složky, `current_dir` vybere tu neúplnou a opakování skončí na `FileExistsError`. `state.json`, `lock` a `ui.log` navíc zůstávají v `.work/<id>` trvale a soubory relací editoru se nemažou nikdy (v reálné složce 37 položek, z toho 17 osiřelých `editor-*.json`).

Celé uspořádání existuje hlavně proto, že okno drží otevřený `ui.log`. Stojí to výjimky v retenci, v mazání, v hledání relace i v obnově po pádu (B4).

**Směr řešení:** nahrávat rovnou do finální složky a rozpracovanost vyjádřit značkou (například chybějícím `finished_at` v manifestu); logy a stav držet mimo ni. Nebo aspoň finalizaci udělat opakovatelnou.

### C3 · Sidecar jako úplný snímek stavu místo záznamu událostí

**Závažnost:** střední · **Stav:** ověřeno

Každý snímek každé obrazovky má vlastní JSON s celým aktuálním stavem: kurzor, všechny dokončené tvary, zoom, schránka. Z toho plyne:

- 28 800 souborů za hodinu v jednom adresáři (pomalé mazání, antivirus, indexer – tedy i příčina opatrného přesunu v C2).
- Tahy štětcem se ukládají bod po bodu bez zjednodušení (`ui/overlay.py:207-213`) a celý seznam tvarů se opakuje v každém sidecaru, dokud kresba zůstává. Deset čmáranic je řádově 250 kB JSON na tik a obrazovku.
- Textová časová osa – hlavní vstup agenta – se musí zpětně rekonstruovat porovnáváním po sobě jdoucích snímků stavu (`host/events.py:198-242`), včetně heuristik typu „je to tentýž tah, jen delší“. Každé volání `timeline` přečte tisíce souborů.
- Schránka se vyzvedne před rozhodnutím, zda se tik zahodí (`capture/recorder.py:354-359` vs. `:318-321`), takže při přetížení zmizí.

**Směr řešení:** rekordér zapisuje jeden připisovaný soubor událostí (kurzor na jinou obrazovku, tvar přidán a odebrán, schránka, zoom, keypoint). Časová osa je pak jeho přímé čtení a sidecar může zůstat malý nebo odvozený.

### C4 · Dvě cesty přepisu

**Závažnost:** střední · **Stav:** ověřeno

Editor přepisuje v podřízeném procesu, proudově a s navázáním (`transcribe/pipeline.py:272-323`). Nahrávací okno po auto stopu přepisuje ve vlastním procesu, vše nebo nic: `transcribe/whisper_backend.py:69-82` načte celý WAV do paměti (asi 1,15 GB ve špičce pro 2 h; při maximálním auto stopu řádově 14 GB), dialog nejde zrušit (`ui/progress_dialog.py:73-74`) a okno nejde zavřít. Vada B4 i tento paměťový strop pocházejí jen ze slabší cesty, která se navíc používá právě u nejdelších nahrávek.

**Směr řešení:** slabší cestu odstranit. Auto stop má udělat totéž co Stop dialog: finalizovat a přepis nechat proudovému pracovnímu procesu.

### C5 · Příliš velké třídy a chybějící zdravotní kanál

**Závažnost:** střední · **Stav:** ověřeno

- `EditorWindow` (`editor/window.py`, 1 678 řádků, asi 100 metod, 34 stavových polí) drží seznam a mazání, načítání, transport, ukládání cropu, render, instrukce, titulky, motiv, odhad zbývajícího času přepisu, odpočet Auto process s filtrem událostí celé aplikace a protokol s agentem. Ochrana proti dvojímu odeslání Process je časovač vizuální zpětné vazby (`:1138`). Otázku „kdo dostane jakou zprávu v jakém stavu“ lze ověřit jen přes `QMainWindow` – odtud B6 a rozešlé cesty v B9.
- `ControlWindow` je zároveň řada tlačítek, stavový automat, propojení schránky a overlaye (přes `getattr`, `ui/control_window.py:431-452`), správa vláken a spouštěč editoru.
- `ui/overlay.py` (1 272 řádků) míchá model tvarů, okno se zoomem, panel nástrojů a převod do JSON.
- Chyby z rekordéru se k uživateli během nahrávání nedostanou: `Recorder` vzniká bez `on_event` (`host/controller.py:115`) a okno sleduje dvě booleovské hodnoty. Jeden zdravotní snímek by vyřešil B1, B2 i B10 naráz.

**Směr řešení:** z `EditorWindow` vyjmout čtyři části bez závislosti na Qt, testovatelné nad dočasnými soubory: spojení s agentem (stavy čeká / odesláno / zavřeno), řízení přehrávání, úložiště cropu a sledování přepisu. Nahrávacímu oknu dát explicitní stavový objekt s jedním vlastníkem přechodů.

### C6 · Uživatelský stav žije v instalačním adresáři

**Závažnost:** střední · **Stav:** mechanismus ověřen; chování cache pluginů u hostitelů pravděpodobné

`core/config.py:67-76` určuje kořen pluginu jako třetího předka souboru a visí na něm `config.json`, model (548 MB) i stažený FFmpeg; `.venv` vzniká tamtéž. Platí to jen pro editovatelnou instalaci ze zdrojáků. Při instalaci z marketplace se repozitář kopíruje do cache podle verze, takže aktualizace pluginu znamená ztrátu nastavení (složka nahrávek se vrátí na výchozí a nahrávky „zmizí“ z editoru) a nové stažení modelu. Dokumentace proto sama doporučuje junction, což u pluginu určeného k distribuci není řešení.

**Směr řešení:** nastavení do uživatelského konfiguračního adresáře, model a binárky do datového adresáře (`platformdirs`, který už projekt používá). Instalační adresář jen ke čtení; jednorázová migrace ze starého místa.

---

## Co je v pořádku

Aby byl obraz úplný – tyto části obstály:

- Zvládání přetížení při snímání: nedodělaná práce je omezená semaforem a vynechané tiky se počítají.
- Předávání kreseb a schránky mezi GUI a snímacím vláknem je bezpečné (neměnné snímky, zámek).
- Zámek jedné instance editoru je skutečný zámek operačního systému a přežije pád.
- Sdílené JSON soubory (kromě přepisu, viz B13) se zapisují atomicky s ošetřením sdílení na Windows.
- Export je oddělený od GUI a testovaný včetně kontroly výsledku přes `ffprobe`.

## Doporučené pořadí

1. **Hned:** A7 (heslo), B7 (HTTP režim MCP vypnout nebo zabezpečit), B8 (FFmpeg z pracovního adresáře).
2. **Ztráta dat při nahrávání:** B1, B2, B3 – nejlépe společně přes zdravotní kanál z C5.
3. **Dokončitelnost nahrávky:** B4 spolu s C4 (jedna cesta přepisu) a manifestem zapisovaným od začátku.
4. **Správnost předání agentovi:** A3 jako rozhodnutí, potom B5, B6, B9 a jako jejich společný základ C1.
5. **Soukromí:** A2 (formulace, schránka jako vědomá volba).
6. **Dokumentace:** A1, A4, A5, A6 – až po rozhodnutích z bodů 3 a 4, aby se nepsala dvakrát.
7. **Průběžně:** B10–B17, C2, C3, C6.
