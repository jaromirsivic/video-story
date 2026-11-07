# Video Story – implementační plán

Stav: **implementováno; ověřeno na Windows a Linuxu (X11) lokálně a na macOS přes GitHub Actions** 7. 11. 2026; rozhodnutí jsou v kapitole 13, stav milníků v kapitole 15. Zadání je v [video-story-spec.md](video-story-spec.md).

Tento plán je česky (vznikl před rozhodnutím). Vše ostatní v repozitáři – README, instalace, skill, UI, hlášky, logy, CLI nápověda, kód a komentáře – je anglicky.

---

## 1. Co jsem zjistil o prostředí

Z průzkumu tohoto počítače a existujících projektů vycházejí tyto závěry, které plán ovlivňují:

| Zjištění | Dopad |
| --- | --- |
| Není nainstalován Rust ani MSVC na PATH (jsou jen Build Tools 2022 a cmake). Je Python 3.11, **uv 0.12**, Node 23, **FFmpeg 7.1 (winget)**. | Nativní jednosouborová binárka (Rust) by vyžadovala zprovoznit toolchain. Python + uv je okamžitě použitelný a odpovídá tvým existujícím skillům. |
| `D:\git\convert_video_to_text` obsahuje ověřený offline přepis: whisper.cpp b5454 (`whisper-cli.exe`), model `ggml-small.bin` (466 MB), bundlovaný FFmpeg, testovací české vzorky, PowerShell skripty. | Model i vzorky lze přímo znovu použít. Transkripční pipeline (FFmpeg → 16 kHz mono PCM → whisper small, `auto`) je již vyřešená a ověřená. |
| Skill `screen-master` (`~/.codex/skills/screen-master`): Python + uv, `mss` pro snímání všech monitorů, polohu kurzoru, overlay pro kreslení, bootstrap přes `run.cmd` → `bootstrap-windows.ps1` → `run.py` do „digest-addressed“ prostředí v cache. | Osvědčený vzor pro spouštění Python skillu z Codexu/Claude bez globální instalace Pythonu. Převezmu jeho principy (zamčené závislosti, žádný runtime stav uvnitř skillu, JSON na stdout). |
| Codex: `~/.codex/skills/*` (skilly jsou NTFS junction do `D:\git\...`), `agents/openai.yaml`; Codex CLI není na PATH, ale Codex app je nainstalovaná (`~/.codex` je plně používán). | Registrace skillu = junction do repozitáře, stejně jako u `convert-audiovideo-to-text`. |
| Claude Code 2.1.268; pluginy mají `.claude-plugin/plugin.json`, `skills/<name>/SKILL.md`, volitelně `.mcp.json`. | Plugin pro Claude Code je standardní adresářová struktura; lokální instalace přes marketplace z adresáře. |
| ChatGPT (web i desktop) **neumí spustit lokální proces ani stdio MCP server**. Vlastní MCP konektory jsou jen vzdálené (Streamable HTTP/SSE) v Developer Mode. | Pro ChatGPT je jediná cesta lokální HTTP MCP server + tunel (ngrok apod.) nebo použití Codexu. Viz otázka 2. |
| Tento počítač má 2 monitory: DISPLAY1 2560×1600 při 150 % DPI (logicky 1707×1067, primární), DISPLAY2 1920×1080 při 100 % na pozici x=2560. | Ideální testovací konfigurace pro mapování kurzoru napříč monitory s různým DPI. |
| Python wheels `pywhispercpp 1.5.1`, `mss`, `sounddevice 0.5.6`, `PySide6 6.12`, `pynput`, `mcp` se na Windows resolvují jako hotové binární balíčky (ověřeno `uv pip install --dry-run`). | Žádná kompilace při instalaci. |

---

## 2. Architektura a volba technologie

### 2.1 Doporučení: Python 3.12 + uv, Qt (PySide6) pro UI

Zadání nechává jazyk na návrhu. Navrhuji **Python**, protože:

- všechny potřebné platformní schopnosti mají zralé, cross-platformní knihovny s hotovými wheels (viz tabulka níže);
- whisper.cpp je dostupný jako `pywhispercpp` (CPU wheels pro Windows, macOS, Linux), takže není nutné bundlovat binárky pro každou platformu;
- odpovídá tvým existujícím skillům (`screen-master`) a na tomto stroji je okamžitě k dispozici (uv, Python);
- implementace a ladění jsou výrazně rychlejší než v Rustu; distribuce pomocí uv je spolehlivá (`uv sync` vytvoří `.venv` se zamčenými verzemi).

Alternativa **Rust** (jedna binárka, bez runtime) je čistší pro koncovou distribuci, ale vyžaduje nasadit toolchain (Rust + MSVC + cmake pro whisper-rs), build v CI pro tři OS a výrazně více práce na UI (egui/winit overlay). Pokud je cílem veřejná distribuce bez Pythonu, lze na Rust přejít později, protože návrh odděluje jádro od UI a OS vrstev. Viz otázka 1.

| Oblast | Knihovna | Proč |
| --- | --- | --- |
| UI (ovládací okno, nastavení, dialogy, overlay pro kreslení, průběh) | **PySide6 (Qt 6)** | Spolehlivé „always on top“, bezrámová průhledná overlay okna per monitor, správné DPI, enumerace obrazovek, nativní vzhled na všech třech OS. Tkinter má s průhledným overlay na Linuxu a macOS problémy. |
| Snímání obrazovek | **mss** | Rychlý raw capture každého monitoru (Windows GDI s `CAPTUREBLT`, macOS CoreGraphics, Linux X11). Zachytí i overlay s kresbou, protože snímá složený obraz. |
| Kódování obrázků | **Pillow** | PNG (rychlý compress level) / JPEG s kvalitou. Kódování běží v thread poolu mimo capture smyčku. |
| Poloha kurzoru | vlastní `platform` vrstva (Win32 `GetCursorPos` přes ctypes, macOS Quartz `CGEventGetLocation` × backing scale, Linux Xlib `XQueryPointer`) | Potřebujeme fyzické pixely a mapování na konkrétní monitor; knihovny vracejí logické souřadnice nekonzistentně. |
| Zvuk | **sounddevice** (PortAudio) + `wave` | Výběr vstupního zařízení, výchozí zařízení, callback zápis 16bit PCM. Na Linuxu je nutný systémový `libportaudio2`. |
| Přepis | **pywhispercpp** (whisper.cpp, CPU) | Přímé volání whisper.cpp s modelem `ggml-small.bin`, `language=auto`, průběžné callbacky. Záložní backend: externí `whisper-cli` (již máš ověřený na Windows). |
| Konverze zvuku | **FFmpeg** (systémový nebo nakonfigurovaná cesta) | Zadání vyžaduje FFmpeg pro přípravu 16 kHz mono PCM. |
| MCP server | **mcp** (oficiální Python SDK) | stdio transport pro Claude Desktop/Codex, Streamable HTTP pro ChatGPT. |
| CLI | `argparse` (stdlib) | Minimum závislostí. |

### 2.2 Vrstvy a členění kódu

Zadání požaduje oddělit sdílené funkce od integrací s hostiteli a od částí závislých na OS. Struktura balíčku `video_story`:

```
src/video_story/
  core/        # čistá logika bez Qt a bez OS volání – plně unit-testovatelná
    config.py        # schéma config.json, validace (1–1000 ms, formáty, kvalita), načtení/uložení
    session.py       # životní cyklus nahrávky (stavový automat, adresáře, state.json, zámek)
    timeline.py      # společná časová osa: recorded_ms bez pauz, formát HH:MM:SS.mmm
    naming.py        # názvy snímků `S_mmm_screenN.ext` a doprovodných JSONů
    screens.py       # model obrazovek (id, bounds, scale), deterministické číslování screen1..N
    cursor.py        # mapování globální polohy kurzoru na obrazovku + pixely jejího snímku
    manifest.py      # manifest.json (schéma + zápis)
    transcript.py    # transcript.txt / transcript.json ze segmentů
  capture/     # běhové komponenty záznamu (vlákna), používají platform/ přes rozhraní
    scheduler.py     # tikání v intervalu, pauzy, dropování při zahlcení
    screen_grabber.py# mss + Pillow, thread pool pro kódování
    audio_recorder.py# sounddevice → audio.wav, pauza = nezapisovat
    recorder.py      # orchestrace: start/pause/resume/stop/discard, zápis state.json
  platform/    # jediné místo s OS-specifickým kódem (rozhraní + implementace)
    base.py          # Protocol: list_screens(), cursor_position(), set_dpi_awareness(), ...
    windows.py, macos.py, linux.py
  transcribe/  # offline přepis
    ffmpeg.py        # příprava 16 kHz mono PCM, detekce ffmpeg
    whisper_backend.py # abstrakce: PyWhisperCppBackend (výchozí), WhisperCliBackend (externí binárka)
    pipeline.py      # orchestrace přepisu + progress callback + zápis výstupů
  ui/          # PySide6
    app.py, control_window.py, settings_dialog.py, stop_dialog.py,
    discard_dialog.py, overlay.py (kreslení), progress_dialog.py
  host/        # integrace s hostiteli
    cli.py           # příkazy run/wait/status/frames/transcribe/config/devices/setup/doctor/mcp
    mcp_server.py    # nástroje MCP nad stejným jádrem
    handoff.py       # výstupní JSON pro agenta (co a kde najde)
  __main__.py
```

Pravidla závislostí: `core` nezávisí na ničem; `capture` a `transcribe` závisí na `core` + `platform`; `ui` závisí na `capture` a `transcribe`; `host` závisí na všem, ale nic nezávisí na `host`. Qt se nikde mimo `ui` neimportuje.

### 2.3 Procesní model a předání agentovi

Shellové nástroje hostitelů mají časové limity (Claude Code Bash max. 10 min, Codex podobně). Nahrávání může trvat déle, proto:

1. `video-story run` spustí **odpojený GUI proces** (`video-story ui --session <id>`) a ihned vrátí JSON `{"session_id", "state_file", "pid"}`.
2. GUI proces průběžně zapisuje `state.json` (`status`: `ready | recording | paused | processing | processed | discarded | failed`, časy, cesty).
3. Agent volá `video-story wait --session <id> --timeout 240` v cyklu, dokud stav není koncový. Výstupem je JSON s cestou k `manifest.json`, `transcript.txt`, `transcript.json` a adresáři snímků. `wait` také detekuje, že GUI proces zemřel (→ `failed`).
4. Po koncovém stavu se okno zavře a automatizovaná část končí; agent pokračuje podle přepisu.

Pomocný příkaz `video-story frames --session <id> --at <ms> [--screen cursor|screenN|all] [--window <ms>]` vrátí cesty snímků nejbližších k danému času (nejbližší snímek **před nebo v** čase), aby agent nemusel procházet tisíce souborů. Totéž nabízí MCP nástroj.

Jediná instance: zámek v pracovním adresáři; druhé `run` vrátí `{"error":"ALREADY_RUNNING","session_id":...}`.

---

## 3. Hostitelské integrace

| Hostitel | Mechanismus | Co dodáme |
| --- | --- | --- |
| **Claude Code** (CLI / desktop app Code tab) | Plugin: `.claude-plugin/plugin.json`, `skills/video-story/SKILL.md`, volitelně `.mcp.json`. | Plugin instalovatelný z lokálního adresáře (marketplace soubor `.claude-plugin/marketplace.json`) i jako skill přes `~/.claude/skills/video-story` (junction). Skill instruuje agenta k `run` → `wait` → čtení podkladů (obrázky čte vestavěným `Read`). |
| **Codex** (app i CLI) | Skill v `~/.codex/skills/video-story` (junction na `skills/video-story` v repu) + `agents/openai.yaml`. Volitelně MCP v `~/.codex/config.toml`. | Tentýž `SKILL.md`; obrázky Codex zobrazí přes svůj nástroj pro prohlížení obrázků. |
| **Claude Desktop** (chat) | stdio MCP server v `claude_desktop_config.json`. | Nástroje `video_story_start`, `video_story_wait`, `video_story_status`, `video_story_get_transcript`, `video_story_list_frames`, `video_story_get_frame` (vrací obrázek jako MCP image content, volitelně zmenšený). |
| **ChatGPT** | Pouze vzdálený MCP (Streamable HTTP) v Developer Mode. | `video-story mcp --transport http --port 8765` + návod na tunel (ngrok/cloudflared). Bez tunelu nelze. Přepis a obrázky se předávají přes MCP nástroje stejně jako u Claude Desktop. Viz otázka 2. |

`SKILL.md` je jeden zdroj pravdy (anglicky) a leží v `skills/video-story/`. Spouštěcí skripty (`scripts/video-story.cmd`, `scripts/video-story.sh`) jsou ve stejné složce, aby fungovaly relativní cesty z jakéhokoli hostitele; volají `uv run --project <repo> video-story ...`.

Obsah skillu (zkráceně): kdy se aktivovat („Start Video Story“, „Video Story“), jak spustit a čekat, jak interpretovat `manifest.json` a přepis, jak spojit výrok s časem a najít snímek obrazovky s kurzorem, jak postupovat při `discarded`/`failed`, že se nemá nic stahovat ani posílat do cloudu.

---

## 4. Datový model

### 4.1 Konfigurace `config.json`

Umístění: kořen instalace pluginu (= tento repozitář), v Gitu ignorováno; `config.example.json` je verzovaný. Pokud kořen není zapisovatelný (globální instalace na macOS/Linux do systémové cesty), použije se uživatelský konfigurační adresář a cesta se zapíše do logu. Viz otázka 6.

```json
{
  "recording_folder": "C:\\Users\\User\\Videos\\VideoStory",
  "capture_interval_ms": 250,
  "image_format": "png",
  "jpeg_quality": 85,
  "audio_input": null,
  "auto_stop_minutes": 30,
  "whisper_model_path": null,
  "whisper_backend": "pywhispercpp",
  "whisper_threads": 8,
  "ffmpeg_path": null
}
```

- `capture_interval_ms`: celé číslo 1–1000 včetně, výchozí 250.
- `image_format`: `png` (výchozí) | `jpeg`; `jpeg_quality`: 20–100, výchozí 85; UI pole se zobrazí jen pro JPEG.
- `audio_input`: `null` = systémový výchozí vstup; jinak název zařízení (při startu se hledá podle názvu, při nenalezení se použije výchozí a zaloguje se varování).
- `auto_stop_minutes`: 1–1440, výchozí **30**. Po dosažení limitu nahrávání samo skončí a rovnou se spustí zpracování (ekvivalent Process Recording); manifest nese `auto_stopped: true`.
- `whisper_model_path`: `null` = `models/ggml-small.bin` v repozitáři; `ffmpeg_path`: `null` = FFmpeg na PATH, jinak automaticky stažená kopie v `bin/ffmpeg/` (viz kap. 7).
- Pořadí v Nastavení: Recording Folder, Capture Interval, Image Format (+ JPEG Quality), Audio Input (přesně podle zadání) a jako pátá položka **Auto Stop Recording** (minuty). Ostatní klíče se v UI neukazují (jen CLI `config set`).

### 4.2 Adresáře nahrávky

```
<recording_folder>/
  .work/<session_id>/          # pracovní složka během nahrávání (stejný svazek → finální přesun je přejmenování)
    state.json, lock, audio.wav, frames/..., recording.log
  <session_id>/                # po Process Recording (session_id = 20261107-143015)
    manifest.json
    audio.wav                  # zdrojový zvuk, 16bit PCM, mono, vzorkovací frekvence zařízení
    transcript.txt
    transcript.json
    recording.log
    frames/
      0_000_screen1.png  0_000_screen1.json
      0_000_screen2.png  0_000_screen2.json
      0_250_screen1.png  ...
```

Discard Recording smaže `.work/<session_id>` celý. Pracovní složka je uvnitř Recording Folder (`.work`), aby finalizace nebyla kopírování gigabajtů mezi svazky (rozhodnutí 6).

### 4.3 Snímky a doprovodný JSON

Název: `<sekundy>_<ms tři číslice>_screen<N>.<png|jpg>`, čas = milisekundy od začátku nahrávky (nahrávání se nikdy nepozastavuje, časová osa je souvislá). Ukládají se všechny snímky včetně nezměněných (rozhodnutí 9). Doprovodný JSON:

```json
{
  "time_ms": 250,
  "screen": "screen1",
  "cursor": { "screen": "screen2", "x": 412, "y": 388 },
  "captured_at": "2026-11-07T14:30:15.250+01:00"
}
```

`screen` je snímaná obrazovka (shoduje se s názvem souboru), `cursor.screen` je obrazovka, na které se kurzor nachází, `x`/`y` jsou pixely jejího snímku (počátek vlevo nahoře). Pokud kurzor není na žádné obrazovce (přechodný stav), `cursor` je `null`.

Číslování obrazovek: `screen1` = primární obrazovka, ostatní seřazené podle `(x, y)` levého horního rohu ve virtuálním prostoru. Mapování je zapsáno v manifestu.

### 4.4 `manifest.json`

```json
{
  "schema_version": 1,
  "session_id": "20261107-143015",
  "started_at": "...", "finished_at": "...",
  "recorded_duration_ms": 183250,
  "auto_stopped": false, "auto_stop_minutes": 30,
  "capture_interval_ms": 250, "image_format": "png", "jpeg_quality": null,
  "screens": [
    { "id": "screen1", "primary": true, "bounds": {"x":0,"y":0,"width":2560,"height":1600}, "scale": 1.5 },
    { "id": "screen2", "primary": false, "bounds": {"x":2560,"y":0,"width":1920,"height":1080}, "scale": 1.0 }
  ],
  "audio": { "file": "audio.wav", "device": "Microphone (Realtek)", "sample_rate": 48000, "channels": 1 },
  "frames": { "dir": "frames", "count": 1466, "dropped": 0 },
  "transcript": { "txt": "transcript.txt", "json": "transcript.json", "language": "cs", "model": "ggml-small", "backend": "pywhispercpp" }
}
```

### 4.5 Přepis

`transcript.txt` (UTF-8):

```
[00:00:00.000 --> 00:00:03.480] Otevři ten soubor, co mám na druhé obrazovce.
[00:00:03.480 --> 00:00:07.120] A tady, kde mám kurzor, přidej tlačítko.
```

`transcript.json`: `{"language":"cs","segments":[{"start_ms":0,"end_ms":3480,"text":"..."}]}`. Časy jsou odhad hranic segmentů podle whisper.cpp.

---

## 5. Chování za běhu (stavový automat)

```
Idle ──Record──▶ Recording ──Stop──▶ Recording + StopDialog (nahrávání dál běží)
                      ▲                 │ Continue Recording (jen zavře dialog)
                      └─────────────────┤
                                        │ Process Recording ──▶ Processing ──▶ Processed (okno se zavře)
                                        │ Discard Recording ──▶ ConfirmDiscard (nahrávání dál běží)
                                                                   │ Yes → zastavit, smazat .work/<id> → Idle
                                                                   │ No  → zpět do StopDialog
Recording ──auto stop (limit minut)──▶ Processing
```

- **Idle**: okno s Record (červený kruh), Settings (ozubené kolo), Draw on Screen neaktivní.
- **Recording**: Record se změní na Stop, Settings neaktivní (změny nastavení uprostřed nahrávky by rozbily konzistenci), Draw on Screen aktivní. Běží scheduler snímků a audio stream. Čas snímku je odvozen od monotónních hodin (`time.perf_counter_ns`) od startu.
- **Stop dialog**: nahrávání zvuku i snímání **pokračuje**, dokud uživatel nepotvrdí Process Recording nebo smazání (rozhodnutí 7). Continue Recording pouze zavře dialog. Časová osa je tedy vždy souvislá a dialog je ve snímcích vidět. Overlay pro kreslení zůstává, jak je.
- **Auto stop**: po `auto_stop_minutes` nahrávání skončí samo a přejde do Processing; otevřené dialogy se zavřou.
- **Processing**: dialog „Transcribing Audio“ s indikátorem průběhu (odhad z konce posledního segmentu / délky zvuku). Po dokončení přesun `.work/<id>` → `<id>`, zápis manifestu, `state.json = processed`, zavření okna.
- Zavření okna křížkem během nahrávání se chová jako Stop (otevře dialog); v Idle zavře okno se stavem `cancelled` (agent dostane informaci, že nahrávka nevznikla).

Snímání: scheduler tiká v nastaveném intervalu; pro každý tik jednou přečte polohu kurzoru a sejme všechny obrazovky (`mss` je rychlé, jednotky ms), raw data předá thread poolu na kódování. Fronta je omezená (např. 2 s snímků); při zahlcení (1 ms interval na 4K monitorech není reálně kódovatelný) se tik vynechá a počítadlo `dropped` v manifestu to přizná. Zadání požaduje přijmout krajní hodnotu 1 ms – UI ji přijme, log a manifest zaznamenají skutečnou dosaženou frekvenci.

Zvuk: `sounddevice.InputStream` na vybraném zařízení, `dtype=int16`, `channels=1` (pokud zařízení mono neumí, nahraje se stereo a smíchá do mono), vzorkovací frekvence zařízení (typicky 48 kHz). Zápis průběžně do `audio.wav` (hlavička se doplní při uzavření; při pádu procesu umí `doctor` hlavičku opravit).

---

## 6. Kreslení na obrazovce

- Tlačítko **Draw on Screen** (aktivní jen při Recording) otevře na **každé obrazovce** bezrámové, průhledné, always-on-top overlay okno; kurzor je křížek.
- Nástroje (rozhodnutí 8): **Brush** (volná kresba), **Rectangle**, **Arrow**. Paleta **16 kontrastních barev**, výchozí červená. Šířka tahu 4 px v logických pixelech (škáluje se s DPI). Plovoucí panel nástrojů v rohu primární obrazovky: tři nástroje, paleta, Undo (`Ctrl+Z`), Clear (`C`), Close (`Esc`).
- Kresba je **součástí snímku** díky tomu, že `mss` snímá složený obraz včetně overlay oken (Windows BitBlt s `CAPTUREBLT`, Linux X11 s kompozitorem). Ověří se v akceptačním testu.
- Kresba zůstává zobrazena, dokud ji uživatel nesmaže, nezavře overlay nebo neskončí nahrávání. Overlay nepropouští kliknutí – během kreslení uživatel nepracuje s aplikacemi pod ním („kreslení“ je samostatný režim, který se vypne `Esc`, tlačítkem Close nebo opětovným stiskem Draw on Screen).
- Ovládací okno je ve snímcích také vidět (je nad ostatními okny). Přijímáme pro v1.

---

## 7. Přepis a předání

1. FFmpeg: `ffmpeg -nostdin -y -i audio.wav -ac 1 -ar 16000 -c:a pcm_s16le .work/<id>/audio16k.wav` (protokoly omezeny na `file,pipe`).
2. Whisper: `pywhispercpp.Model(model_path, n_threads=...)`, `language="auto"`, bez GPU, callback nových segmentů → průběh. Výstup segmenty (t0, t1 v centisekundách → ms).
3. Záložní backend `whisper-cli` (externí binárka, konfigurovatelná cesta) používá stejné parametry jako tvůj `transcribe.ps1` (`-l auto -oj`). Na Windows umožní znovu využít ověřenou instalaci v `convert_video_to_text`.
4. Zápis `transcript.txt`, `transcript.json`, `manifest.json`; smazání `audio16k.wav`; přesun do finální složky.
5. `state.json = processed`; `wait` vrátí agentovi JSON s cestami; okno se zavře.
6. Příkaz `video-story transcribe <dir>` umožní přepis zopakovat (např. po pádu).

Model: kopie `ggml-small.bin` je v `models/` tohoto repozitáře (mimo Git, rozhodnutí 3). `video-story setup` ověří model (SHA-1 proti oficiální tabulce) a na jiném stroji ho stáhne z Hugging Face s kontrolou hashe.

FFmpeg (rozhodnutí 4): použije se `ffmpeg` z PATH nebo z `ffmpeg_path`. Pokud chybí, `setup` (a `doctor`) ho automaticky stáhne do `bin/ffmpeg/<os>-<arch>/` z ověřeného zdroje (Windows: gyan.dev/BtbN release, Linux: johnvansickle static build / BtbN) s kontrolou hashe a zalogovaným původem. Stahování probíhá jen v `setup`/`doctor`, nikdy během nahrávání.

---

## 8. Platformní vrstva a známá omezení

| OS | Obrazovky | Kurzor | Overlay / always-on-top | Zvuk | Poznámky |
| --- | --- | --- | --- | --- | --- |
| Windows 10/11 | mss (GDI), per-monitor DPI awareness v2 nastavené při startu procesu | `GetCursorPos` (fyzické px) | Qt frameless + `WA_TranslucentBackground` | PortAudio bundlovaný | Primární vývojová a testovací platforma. |
| macOS 13+ | mss (CoreGraphics); potřeba oprávnění **Screen Recording** a **Microphone** (systém se zeptá) | Quartz, body × `backingScaleFactor` | Qt, fungují | PortAudio bundlovaný | Při prvním spuštění vyžaduje schválení v System Settings; `doctor` to ověří. |
| Linux X11 | mss (XGetImage) | Xlib `XQueryPointer` | Qt; průhlednost vyžaduje kompozitor | nutný `libportaudio2` | Plně podporováno. |
| Linux Wayland | capture jen přes xdg-desktop-portal (mss to neumí) | globální poloha kurzoru není dostupná | overlay bez globální polohy | – | **Mimo rozsah v1**; `doctor` ohlásí nepodporovanou relaci. Viz otázka 10. |

Výkon a disk: PNG snímek 2560×1600 má 1–3 MB; při 250 ms a dvou monitorech vzniká **zhruba 0,5–1,4 GB za minutu**. Kódování PNG v thread poolu zvládne ~4 fps × 2 monitory na moderním CPU, ale disk se plní rychle. Viz otázka 9 (volitelné vynechávání nezměněných snímků).

---

## 9. CLI a MCP rozhraní (přehled)

```
video-story run [--detach]             # otevře ovládací okno; --detach vrátí ihned session_id
video-story wait --session ID [--timeout S]
video-story status --session ID
video-story frames --session ID --at MS [--screen cursor|screenN|all] [--window MS]
video-story transcribe DIR
video-story config get|set KEY VALUE|path
video-story devices                    # seznam zvukových vstupů
video-story setup [--download-model]   # kontrola a příprava prostředků
video-story doctor                     # diagnostika prostředí (OS, oprávnění, ffmpeg, model, monitory)
video-story mcp [--transport stdio|http] [--port N]
```

Všechny příkazy vracejí na stdout jeden JSON objekt (pro agenta), chyby jako JSON na stderr s `code` a `message`; návratový kód ≠ 0 při chybě. Stejný vzor jako `screen-master`.

MCP nástroje mapují 1:1 na CLI (`start`, `wait`, `status`, `get_transcript`, `list_frames`, `get_frame`), `get_frame` vrací obrázek jako image content s volitelným `max_width`.

---

## 10. Struktura repozitáře

```
D:\git\video-story\
  video-story-spec.md, implementation-plan.md, README.md (cs), docs/installation.md (cs)
  pyproject.toml, uv.lock, .gitignore, config.example.json, LICENSE
  .claude-plugin/plugin.json, .claude-plugin/marketplace.json, .mcp.json
  skills/video-story/SKILL.md, skills/video-story/agents/openai.yaml,
  skills/video-story/scripts/video-story.cmd, video-story.sh
  src/video_story/...   (viz 2.2)
  scripts/install-hosts.ps1, install-hosts.sh   # junctiony/symlinky pro Codex a Claude, zápis MCP konfigurace
  tests/unit, tests/integration, tests/samples (czech.wav z convert_video_to_text), tests/manual/acceptance.md
```

Repozitář je Git repozitář s MIT licencí (založen uživatelem 7. 11. 2026). `models/`, `bin/`, `.venv/`, `config.json` jsou v `.gitignore`.

---

## 11. Milníky a postup implementace

Každý milník končí spustitelným stavem a testy. Implementaci budu dělat já (Fable 5.1); samostatné, dobře ohraničené části (např. platformní vrstva pro macOS/Linux, MCP server, testy) deleguji subagentům s úsilím `high`/`xhigh` podle složitosti, jak jsi požadoval.

| M | Obsah | Výstup / ověření |
| --- | --- | --- |
| **M0 Kostra** | git init, pyproject (Python 3.12, uv), balíček, `core/config`, `core/timeline`, `core/naming`, `core/screens`, `core/cursor`, CLI `config`/`doctor`/`devices`, logging. | Unit testy: validace 1–1000 ms, výchozí 250/PNG, názvy souborů, formát `HH:MM:SS.mmm`, mapování kurzoru na dvou syntetických monitorech s různým DPI, uložení/načtení configu. |
| **M1 Záznam bez UI** | `platform/windows`, `capture/*` (scheduler, mss grabber, audio, recorder, state.json, lock), interní dev příkaz `record --seconds N`. | Integrační test na tomto PC: 5 s záznamu, 2 monitory → správný počet souborů, JSON ke každému, zvuk, pauza/resume uprostřed → časová osa bez mezery. |
| **M2 Ovládací okno** | PySide6: control window (always on top), Settings dialog v zadaném pořadí, Stop dialog, Discard confirm, stavový automat, `run --detach`/`wait`/`status`. | Ruční checklist + automatické Qt testy (pytest-qt) pro stavový automat a dialogy. |
| **M3 Kreslení** | Overlay per monitor, nástroje, zachycení ve snímcích. | Ověřit, že kresba je ve snímku obou monitorů. |
| **M4 Přepis a předání** | `transcribe/*` (ffmpeg, pywhispercpp, whisper-cli backend), progress dialog, manifest, transcript formáty, přesun do finální složky, `frames`, `transcribe` příkaz. | Test s `czech.wav` offline (proxy na nefunkční adresu jako u tvého self-testu): vznikne txt + json, jazyk `cs`. |
| **M5 Hostitelé** | `SKILL.md`, Claude plugin manifest + marketplace, Codex junction + `openai.yaml`, MCP server (stdio + http), `install-hosts.*`. | Ukázka end-to-end v Claude Code a Codexu: vyslovený pokyn → agent najde správný snímek a polohu kurzoru. MCP ověřeno MCP Inspectorem a v Claude Desktop, je-li k dispozici. |
| **M6 Linux a dokumentace** | `platform/linux` (X11), `doctor`, README/installation (en), acceptance checklist pro Windows a Linux, úklid. Ověření na Fedora stroji (rozhodnutí 11). | Akceptační test podle kapitoly „Ověření splnění“ na Windows a Linuxu. macOS je odloženo (rozhodnutí 11): `platform/macos` zůstane jako kostra bez ověření. |

Odhad pracnosti: M0–M2 jsou jádro (největší část), M3–M4 střední, M5 menší, M6 Linux ověření.

---

## 12. Mapování na „Ověření splnění“

| Kritérium zadání | Jak se ověří |
| --- | --- |
| Anglické okno nad ostatními okny, Record spouští, kreslení jen při záznamu | M2/M3 ruční test + pytest-qt test aktivity tlačítek. |
| Pořadí nastavení, výchozí 250 ms/PNG, krajní 1 a 1000 ms, persistence po restartu | Unit test configu + ruční test UI. |
| Dva monitory → snímky obou, zvuk, JSON ke každému; kurzor na druhém monitoru → její id a pixely | Integrační test M1 na tomto PC (DISPLAY1 150 %, DISPLAY2 100 %). |
| Stop → Continue zachová nahrávku; odmítnutí smazání zachová soubory; potvrzení smaže a vrátí Record | pytest-qt test stavového automatu + ruční test. |
| Process Recording bez sítě vytvoří txt + JSON; agent má podklady po zavření okna; ukázka spojení pokynu se snímkem | M4 offline test + M5 end-to-end ukázka. |
| Kombinace hostitelů × OS | Matice v `tests/manual/acceptance.md`, vyplněná podle dostupných strojů. |

---

## 13. Otevřené otázky a rozhodnutí

Rozhodnuto uživatelem 7. 11. 2026 (komentáře níže v COMMENT-POINTS). Shrnutí:

| # | Rozhodnutí |
| --- | --- |
| 1 | Python 3.12 + uv + PySide6. |
| 2 | v1 = Claude Code + Codex + Claude Desktop (stdio MCP); ChatGPT jen HTTP MCP + tunel, bez garantované ukázky. |
| 3 | Model `ggml-small.bin` zkopírovat do `models/` v repu (mimo Git). |
| 4 | FFmpeg: systémový; pokud chybí, plugin ho sám detekuje a stáhne/nainstaluje. |
| 5 | JPEG max 100, výchozí 85; zvuk WAV 16bit PCM mono, frekvence zařízení. |
| 6 | Cesty podle doporučení. Navíc nastavení **Auto Stop Recording**, výchozí 30 minut. |
| 7 | **Žádná pauza**: při otevřeném Stop dialogu se nahrává dál, dokud uživatel nepotvrdí zpracování nebo smazání. |
| 8 | Kreslení: obdélníky, šipky, štětec; paleta max. 16 kontrastních barev; výchozí červená. |
| 9 | Ukládat i nezměněné snímky (žádné vynechávání). |
| 10 | Wayland mimo rozsah v1. |
| 11 | Implementovat a ověřovat jen Windows a Linux (Fedora, 172.19.39.105); macOS odloženo. |
| 12 | Repo už je v Gitu. Vše psát anglicky. Instalaci zatím zkoušet jen v Claude. |

Původní otázky:

1. **Technologie.** Python 3.12 + uv + PySide6 (doporučení), nebo Rust jako jedna nativní binárka (vyžaduje instalaci toolchainu, delší vývoj)?[🗩](#comment_point_uvndssvu "comment_point_uvndssvu")
2. **Hostitel „ChatGPT“.** ChatGPT neumí lokální procesy ani stdio MCP. Navrhuji: v1 = Claude Code (plugin/skill) + Codex (skill) + Claude Desktop (stdio MCP); ChatGPT = HTTP MCP server + návod na tunel, bez garantované ukázky. Souhlasíš, nebo máš na mysli pod „ChatGPT“ Codex app / něco jiného?[🗩](#comment_point_vmmqrai5 "comment_point_vmmqrai5")
3. **Whisper runtime a model.** Výchozí backend `pywhispercpp` (cross-platform wheels), záložní `whisper-cli`. Model: odkazovat na existující `D:\git\convert_video_to_text\models\ggml-small.bin` (doporučení, bez duplikace 466 MB), nebo zkopírovat do tohoto repa (`models/`, mimo Git)? Smí `setup --download-model` stahovat z Hugging Face na jiných strojích?[🗩](#comment_point_vmzuly2f "comment_point_vmzuly2f")
4. **FFmpeg.** Použít systémový `ffmpeg` z PATH (doporučení; na tomto PC je) s možností cesty v configu, nebo bundlovat binárky per OS?[🗩](#comment_point_e5u2wxwc "comment_point_e5u2wxwc")
5. **JPEG a zvuk.** Horní hranice JPEG Quality **100**, výchozí **85** (doporučení; alternativa max 90). Zdrojový zvuk: `audio.wav`, 16bit PCM, **mono**, vzorkovací frekvence zařízení (doporučení; alternativa stereo nebo FLAC pro menší velikost).[🗩](#comment_point_bjw6hf3s "comment_point_bjw6hf3s")
6. **Cesty.** Výchozí Recording Folder `~/Videos/VideoStory` (Windows `%USERPROFILE%\Videos\VideoStory`, macOS `~/Movies/VideoStory`, Linux `~/Videos/VideoStory`) – doporučení, protože data jsou velká a uživatel je má najít. Pracovní složka `.work/<id>` uvnitř Recording Folder (doporučení) místo systémového temp. Snímky v podsložce `frames/` (doporučení) místo kořene nahrávky. `config.json` v kořeni repa podle zadání (doporučení; při aktualizaci pluginu z marketplace se může ztratit – u junction instalace ne).[🗩](#comment_point_ehvtqu4z "comment_point_ehvtqu4z")
7. **Pauza při Stop dialogu.** Pozastavit zvuk i snímání, časová osa pauzu vynechá, pauzy se zapíší do manifestu (doporučení). „No“ v potvrzení smazání vrátí do Stop dialogu (doporučení; alternativa rovnou pokračovat v nahrávání).[🗩](#comment_point_32cexx6o "comment_point_32cexx6o")
8. **Kreslení.** Volná kresba, červená, 4 px, Esc/Clear/Undo, overlay nepropouští kliknutí, při Stop se zavře (doporučení). Ovládací okno je vidět ve snímcích (přijmout v1).[🗩](#comment_point_mgfn5j6l "comment_point_mgfn5j6l")
9. **Objem dat.** Přijmout ~0,5–1,4 GB/min při PNG a 250 ms, nebo přidat volitelné nastavení „Skip unchanged frames“ (porovnání hashe raw snímku, výchozí vypnuto)? Doporučuji přidat jako skrytou volbu v configu (ne v UI, aby UI odpovídalo zadání).[🗩](#comment_point_daqf57or "comment_point_daqf57or")
10. **Linux Wayland** mimo rozsah v1 (doporučení), X11 plně.[🗩](#comment_point_hvmwg5wz "comment_point_hvmwg5wz")
11. **Testovací stroje.** Máš k dispozici macOS a Linux pro ověření M6? Pokud ne, implementuji platformní vrstvy podle dokumentace s unit testy a reálné ověření označím jako neprovedené.[🗩](#comment_point_vnnuqnho "comment_point_vnnuqnho")
12. **Repozitář a dokumentace.** `git init` v `D:\git\video-story`, licence MIT, README a instalace česky, vše ostatní anglicky (doporučení). Registraci skillu do `~/.codex/skills` a `~/.claude` provést junctionem jako u `convert-audiovideo-to-text`.[🗩](#comment_point_kuiw44bi "comment_point_kuiw44bi")

---

## 15. Stav implementace (7. 11. 2026)

| Milník | Stav | Důkaz |
| --- | --- | --- |
| M0 kostra a core | hotovo | `tests/unit/test_core.py` |
| M1 záznam bez UI | hotovo | `tests/integration/test_recording.py` (Windows 2 monitory, Fedora Xvfb) |
| M2 ovládací okno, dialogy, run/wait | hotovo | `tests/unit/test_ui.py`, `tests/unit/test_runner.py`, `tests/integration/test_ui_e2e.py` |
| M3 kreslení | hotovo | `tests/unit/test_overlay.py`, `tests/integration/test_overlay_capture.py` (kresba je ve snímku obou monitorů) |
| M4 přepis a doctor | hotovo | `tests/unit/test_transcribe.py`, `tests/unit/test_doctor.py`, `tests/integration/test_transcription.py` (čeština, offline, ~4 s na 14 s zvuku) |
| M5 hostitelé | skill + Claude plugin + MCP server hotovo; Claude Code skill ověřen (junction), Codex a Claude Desktop jen zdokumentováno | `skills/video-story/SKILL.md`, `.claude-plugin/`, `src/video_story/host/mcp_server.py`, `docs/installation.md` |
| M6 Linux a dokumentace | hotovo pro Fedora 44 / X11 (Xvfb + openbox): všechny sady prošly | `tests/manual/acceptance.md` |
| M7 macOS přes GitHub Actions | `platform/macos.py` (ctypes CoreGraphics, body vs. pixely přes `pixel_ratio`), CI matice Ubuntu/Windows/macOS zelená (7. 11. 2026): obrazovky, kurzor, záznam, zachycení overlay na macOS runneru; fyzický Mac s Retinou a mikrofonem zatím neověřen | `.github/workflows/ci.yml`, veřejné anotace běhu, `tests/unit/test_core.py::test_pixel_ratio_mapping` |

Odchylky od původního návrhu: žádná pauza při Stop dialogu (rozhodnutí 7), nástroje kreslení rozšířeny (rozhodnutí 8), položka Auto Stop Recording (rozhodnutí 6), vše v repozitáři anglicky (rozhodnutí 12). Konfigurace `config.json` v kořeni repozitáře; `models/` a `bin/` mimo Git.

Rozšíření ze 7. 11. 2026 (na žádost autora po prvním ručním ověření):

- Sidecar JSON každého snímku (každé obrazovky) obsahuje `drawings`: tvary aktuálně nakreslené na overlay s `tool`, `color`, `screen` a body `{screen, x, y}` v pixelech obrázku dané obrazovky (`from`/`to` u obdélníku a šipky, `path` u štětce). Snímek overlay se přepočítává v GUI vlákně při každé změně kresby; capture vlákno ho čte při každém ticku.
- Retence: `retention_days` (výchozí 30, v Nastavení jako **Keep Recordings**); starší nahrávky (podle času v id relace) se mažou na pozadí při startu okna. V Nastavení přibyla tlačítka **Open Folder** a **Delete All Recordings...** (potvrzovací dialog; relace otevřeného okna se nemaže).
- Výchozí hodnoty: Auto Stop Recording 120 min (dříve 30), Capture Interval 500 ms (dříve 250 ms podle zadání).
- Oprava finalizace na Windows: oddělený okenní proces drží otevřený `ui.log`, proto se nahrávka přesouvá po položkách a `state.json`, `lock` i `ui.log` zůstávají v `.work/<id>`.
- Editor nahrávek (Edit Video Story, export do MP4 + SRT): samostatný plán a stav v `implementation-plan-editor.md`.
- Model přepisu změněn (7. 11. 2026, na žádost autora) z `ggml-small.bin` na **`ggml-large-v3-turbo-q5_0.bin`** (548 MB, ověřováno SHA-256 z Hugging Face); stažení přes `video-story setup`.

---

## 14. Rizika

- **Zachycení overlay kresby** závisí na capture API; na Windows a macOS ověřeno principem, na Linuxu bez kompozitoru nebude průhlednost. Ověří M3.
- **Zahlcení při krátkých intervalech** (1–50 ms) – řešeno dropováním a reportem v manifestu, ne pádem.
- **Oprávnění macOS** (Screen Recording, Microphone) – bez nich snímky černé/zvuk prázdný; `doctor` a první spuštění to vysvětlí.
- **Timeouty hostitelů** – řešeno `run --detach` + `wait` smyčkou; pokud hostitel smyčku neumí (ChatGPT), MCP `wait` používá progress notifikace.
- **Velikost dat** – viz otázka 9.
- **pywhispercpp wheel na konkrétní platformě** (např. Linux ARM) – záložní backend `whisper-cli` a `setup`, který to ohlásí.

# COMMENT-POINTS

## comment_point_uvndssvu

- Status: in progress
- Created: 2026-11-07 10:11:27 +01:00
- Author: User
- Author Abbreviation: US

```markdown
Technologie.** Python 3.12 + uv + PySide6 (doporučení), nebo Rust jako jedna nativní binárka (vyžaduje instalaci toolchainu, delší vývoj)?
```

> **US** ; User ; 2026-11-07 10:11:27 +01:00
>
> Ano.

## comment_point_vmmqrai5

- Status: in progress
- Created: 2026-11-07 10:11:44 +01:00
- Author: User
- Author Abbreviation: US

```markdown
Hostitel „ChatGPT“.** ChatGPT neumí lokální procesy ani stdio MCP. Navrhuji: v1 = Claude Code (plugin/skill) + Codex (skill) + Claude Desktop (stdio MCP); ChatGPT = HTTP MCP server + návod na tunel, bez garantované ukázky. Souhlasíš, nebo máš na mysli pod „ChatGPT“ Codex app / něco jiného?
```

> **US** ; User ; 2026-11-07 10:11:44 +01:00
>
> Ano.

## comment_point_vmzuly2f

- Status: in progress
- Created: 2026-11-07 10:11:58 +01:00
- Author: User
- Author Abbreviation: US

```markdown
Whisper runtime a model.** Výchozí backend `pywhispercpp` (cross-platform wheels), záložní `whisper-cli`. Model: odkazovat na existující `D:\git\convert_video_to_text\models\ggml-small.bin` (doporučení, bez duplikace 466 MB), nebo zkopírovat do tohoto repa (`models/`, mimo Git)? Smí `setup --download-model` stahovat z Hugging Face na jiných strojích?
```

> **US** ; User ; 2026-11-07 10:11:58 +01:00
>
> Zkopírovat do tohoto repa.

## comment_point_e5u2wxwc

- Status: in progress
- Created: 2026-11-07 10:12:33 +01:00
- Author: User
- Author Abbreviation: US

```markdown
FFmpeg.** Použít systémový `ffmpeg` z PATH (doporučení; na tomto PC je) s možností cesty v configu, nebo bundlovat binárky per OS?
```

> **US** ; User ; 2026-11-07 10:12:33 +01:00
>
> Pokud neexistuje systémový ffmpeg, tak si to plugin zdetekuje a nainstaluje si ffmpeg.

## comment_point_bjw6hf3s

- Status: in progress
- Created: 2026-11-07 10:13:26 +01:00
- Author: User
- Author Abbreviation: US

```markdown
JPEG a zvuk.** Horní hranice JPEG Quality **100**, výchozí **85** (doporučení; alternativa max 90). Zdrojový zvuk: `audio.wav`, 16bit PCM, **mono**, vzorkovací frekvence zařízení (doporučení; alternativa stereo nebo FLAC pro menší velikost).
```

> **US** ; User ; 2026-11-07 10:13:26 +01:00
>
> Souhlasím s doporučením pro zvuk i obraz.

## comment_point_ehvtqu4z

- Status: in progress
- Created: 2026-11-07 10:14:09 +01:00
- Author: User
- Author Abbreviation: US

```markdown
Cesty.** Výchozí Recording Folder `~/Videos/VideoStory` (Windows `%USERPROFILE%\Videos\VideoStory`, macOS `~/Movies/VideoStory`, Linux `~/Videos/VideoStory`) – doporučení, protože data jsou velká a uživatel je má najít. Pracovní složka `.work/<id>` uvnitř Recording Folder (doporučení) místo systémového temp. Snímky v podsložce `frames/` (doporučení) místo kořene nahrávky. `config.json` v kořeni repa podle zadání (doporučení; při aktualizaci pluginu z marketplace se může ztratit – u junction instalace ne).
```

> **US** ; User ; 2026-11-07 10:14:09 +01:00
>
> Souhlasím. Navíc ještě do setupu doplň auto stop recording. výchozí hodnota bude 30 minut.

## comment_point_32cexx6o

- Status: in progress
- Created: 2026-11-07 10:15:03 +01:00
- Author: User
- Author Abbreviation: US

```markdown
Pauza při Stop dialogu.** Pozastavit zvuk i snímání, časová osa pauzu vynechá, pauzy se zapíší do manifestu (doporučení). „No“ v potvrzení smazání vrátí do Stop dialogu (doporučení; alternativa rovnou pokračovat v nahrávání).
```

> **US** ; User ; 2026-11-07 10:15:03 +01:00
>
> Ne, dokud uživatel nepotvrdí zastavení nahrávání, nebo zpracování, tak se bude pořád nahrávat.

## comment_point_mgfn5j6l

- Status: in progress
- Created: 2026-11-07 10:16:25 +01:00
- Author: User
- Author Abbreviation: US

```markdown
Kreslení.** Volná kresba, červená, 4 px, Esc/Clear/Undo, overlay nepropouští kliknutí, při Stop se zavře (doporučení). Ovládací okno je vidět ve snímcích (přijmout v1).
```

> **US** ; User ; 2026-11-07 10:16:25 +01:00
>
> Uživatel bude moci kreslit pouze obdélníky, šipky, nebo volná kresba, tedy štětec a bude si moci vybrat s palety maximálně 16 barev, které jsou kontrastní. Výchozí barva bude červená.

## comment_point_daqf57or

- Status: in progress
- Created: 2026-11-07 10:17:13 +01:00
- Author: User
- Author Abbreviation: US

```markdown
Objem dat.** Přijmout ~0,5–1,4 GB/min při PNG a 250 ms, nebo přidat volitelné nastavení „Skip unchanged frames“ (porovnání hashe raw snímku, výchozí vypnuto)? Doporučuji přidat jako skrytou volbu v configu (ne v UI, aby UI odpovídalo zadání).
```

> **US** ; User ; 2026-11-07 10:17:13 +01:00
>
> Ne, ukládat se budou i stejné snímky.

## comment_point_hvmwg5wz

- Status: in progress
- Created: 2026-11-07 10:18:10 +01:00
- Author: User
- Author Abbreviation: US

```markdown
Linux Wayland** mimo rozsah v1 (doporučení), X11 plně.
```

> **US** ; User ; 2026-11-07 10:18:10 +01:00
>
> Souhlasím.

## comment_point_vnnuqnho

- Status: in progress
- Created: 2026-11-07 10:21:42 +01:00
- Author: User
- Author Abbreviation: US

```markdown
Testovací stroje.** Máš k dispozici macOS a Linux pro ověření M6? Pokud ne, implementuji platformní vrstvy podle dokumentace s unit testy a reálné ověření označím jako neprovedené.
```

> **US** ; User ; 2026-11-07 10:21:42 +01:00
>
> Zatím implementuj vše pouze na Windows a Linux. K dispozici máš linux Fedora na IP 172.19.39.105 login fedora heslo phpvpnlinux.

## comment_point_kuiw44bi

- Status: in progress
- Created: 2026-11-07 10:24:39 +01:00
- Author: User
- Author Abbreviation: US

```markdown
Repozitář a dokumentace.** `git init` v `D:\git\video-story`, licence MIT, README a instalace česky, vše ostatní anglicky (doporučení). Registraci skillu do `~/.codex/skills` a `~/.claude` provést junctionem jako u `convert-audiovideo-to-text`.
```

> **US** ; User ; 2026-11-07 10:24:39 +01:00
>
> Repozitář už je v gitu. Všechno piš anglicky. Instalaci zatím zkoušej jen v claude.
