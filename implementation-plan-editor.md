# Edit Video Story – implementační plán (jednoduchý editor nahrávek)

Stav: návrh k odsouhlasení (7. 11. 2026). Otevřené otázky jsou v kapitole 12; implementace začne po jejich zodpovězení. Navazuje na `implementation-plan.md` (nahrávání) a používá stejný stack: Python 3.12 + uv, PySide6, FFmpeg, `sounddevice`.

## 1. Cíl a rozsah

Druhá část pluginu: když uživatel požádá o **editaci Video Story**, otevře se samostatné okno „Video Story Editor“ – jednoduchá střižna inspirovaná rozložením DaVinci Resolve:

- vlevo **seznam nahrávek**, které jsou ještě v Recording Folder (nebyly smazané retencí ani ručně); výchozí výběr je **poslední pořízená** nahrávka,
- vpravo nahoře **náhled** (Play/Stop, čas, přepínač titulků; titulky se zobrazují **pod** náhledem, výchozí zapnuto),
- vpravo dole **časová osa**: pro každý monitor jedna videostopa s občasnými miniaturami, jedna zvuková stopa s křivkou; zoom; jediná úprava je **crop** – začátek a konec výřezu, společný pro všechny monitory; výchozí je celá délka,
- kliknutím na stopu monitoru se vybírá, který monitor se zobrazuje v náhledu a který se exportuje,
- jedno velké tlačítko **Render**: dotaz kam uložit, výstup je **MP4 (H.264 + AAC), 24 snímků/s, v rozlišení exportované obrazovky**, ořezaný podle cropu, se zvukem z nahrávky, a vedle něj **SRT** s přepisem (časy posunuté o začátek cropu).

Mimo rozsah (záměrně): více stop, efekty, přechody, úpravy hlasitosti, střih uvnitř (jen ořez od začátku a do konce), mazání nahrávek z editoru, úprava přepisu.

Platformy: **nejprve pouze Windows** (ověření na tomto stroji). Kód se píše přenositelně (PySide6, FFmpeg, `sounddevice` fungují všude; OS‑specifické věci zůstávají v `platform/`), ale Linux a macOS se ověří a případně doladí až v navazující etapě po schválení (kapitola 11).

## 2. Jak to uživatel používá (scénář)

1. Uživatel řekne agentovi „Edit Video Story“ (nebo „otevři editor nahrávek“, „udělej z poslední nahrávky video“). Skill `video-story-edit` spustí `video-story edit --detach`; okno editoru se otevře s označenou poslední nahrávkou. Agent odpoví jednou větou, že editor je otevřený (nečeká – viz otázka 4).
2. Uživatel vybere nahrávku vlevo; dole se načtou stopy, nahoře první snímek. Mezerník přehrává, titulky běží pod náhledem.
3. Přetažením úchytů In/Out na časové ose (nebo klávesami `I`/`O` na pozici přehrávací hlavy) uživatel ořízne začátek a konec. Výřez je zvýrazněn, zbytek ztmaven. Crop platí pro všechny monitory najednou.
4. Kliknutím na stopu monitoru vybere, co se bude exportovat (a co se zobrazuje v náhledu).
5. **Render** → dialog „Uložit jako“ (výchozí `<Recording Folder>/<id>_<screenN>.mp4`) → dialog s průběhem → po dokončení zpráva s cestou k MP4 a SRT. Editor zůstává otevřený.

## 3. Architektura

```text
src/video_story/
  editor/                      # nový balík (GUI editoru) – jen PySide6 + model
    app.py                     # run_editor(session_id | None, config_file) – vstup procesu
    window.py                  # EditorWindow: rozložení, akce, klávesy, Render
    recordings_model.py        # seznam nahrávek (QAbstractListModel) z Recording Folder
    recording.py               # LoadedRecording: manifest, FrameIndex, transcript, audio info
    preview.py                 # PreviewWidget (snímek + kurzor), SubtitleLabel, transport
    player.py                  # Player: hodiny přehrávání řízené zvukem (sounddevice OutputStream)
    timeline/
      model.py                 # TimelineModel: délka, zoom, posun, crop (in/out), playhead, vybraná stopa
      view.py                  # TimelineView (QWidget): pravítko, stopy, úchyty cropu, drag, kolečko
      thumbnails.py            # ThumbnailCache: miniatury snímků na pozadí (QThreadPool)
      waveform.py              # WaveformPeaks: špičky zvuku po blocích (streamované čtení WAV)
    render_dialog.py           # průběh exportu (QProgressDialog nad ExportWorker v QThread)
  export/                      # čistá logika exportu bez Qt – testovatelná a použitelná z CLI
    plan.py                    # ExportPlan: výběr snímků pro interval, concat seznam, sendcmd kurzoru
    srt.py                     # transcript → SRT (ořez na interval, posun časů)
    ffmpeg_export.py           # sestavení a spuštění příkazu ffmpeg, čtení průběhu (-progress)
  host/cli.py                  # nové příkazy: edit [--detach] [--session ID], export ...
  host/mcp_server.py           # nové nástroje: video_story_edit, video_story_export
skills/video-story-edit/       # druhý skill (SKILL.md, agents/openai.yaml, scripts/*)
```

Zásady:

- **Export je nezávislý na GUI** (`export/`), má vlastní CLI `video-story export` a MCP nástroj, takže ho agent může spustit i bez okna a lze ho testovat v CI na všech třech systémech (runnery GitHubu mají FFmpeg).
- Editor běží jako **samostatný proces** (stejně jako ovládací okno nahrávání – `spawn_detached`, `pythonw.exe` na Windows), nezávisle na nahrávacím okně; obě okna mohou běžet současně.
- Znovupoužití: `host.runner.FrameIndex` (index snímků), `core.transcript`, `core.manifest`, `core.screens`, `transcribe.ffmpeg.find_ffmpeg/ensure_ffmpeg`, `platform.process`.

## 4. Datový model editoru

- **Nahrávka** = finální složka `<Recording Folder>/<id>/` s `manifest.json` (jen zpracované nahrávky; rozpracované v `.work` se nenabízejí). Seznam se řadí od nejnovější; každá položka ukazuje datum a čas začátku, délku, počet obrazovek a první řádek přepisu. Seznam se obnoví při otevření okna a tlačítkem Refresh (a po exportu se nic nemění).
- **LoadedRecording**: manifest (obrazovky s `image_size`), `FrameIndex` po obrazovkách, `Transcript`, cesta a parametry `audio.wav` (vzorkovací frekvence, délka), `recorded_duration_ms`. Délka časové osy = `recorded_duration_ms` (zvuk může být o pár ms delší/kratší – ořízne se).
- **TimelineModel**: `duration_ms`, `zoom` (px na sekundu, rozsah „celá nahrávka na šířku“ až 200 px/s), `scroll_ms`, `playhead_ms`, `in_ms`/`out_ms` (výchozí 0 a `duration_ms`, invariant `in < out`, minimální délka 1 s), `selected_screen` (výchozí `screen1`). Čisté Python, bez Qt – unit testy převodů čas↔pixel, zoomu kolem kurzoru, omezení cropu.
- **Snímek v čase t** pro obrazovku S = poslední snímek s `time_ms ≤ t` (stejně jako `frames --at`); před prvním snímkem první snímek. Kurzor a kresby se čtou ze sidecaru.

## 5. Uživatelské rozhraní

Rozložení (QSplitter vodorovně: seznam | pravá část; pravá část svisle: náhled | časová osa):

```text
┌──────────────┬──────────────────────────────────────────────────────────────┐
│ Recordings   │  Preview (screen2)                                           │
│ ──────────── │  ┌──────────────────────────────────────────────────┐        │
│ ▸ 07.11 12:21│  │                 snímek + kurzor                  │        │
│   44 s, 2 scr│  └──────────────────────────────────────────────────┘        │
│   07.11 10:02│  [⏮] [▶ Play] [⏭]   00:00:12.480 / 00:00:44.075   [x] Subtitles│
│   …          │  „A teď bych chtěl vědět, jak je aktuální čas…“  (titulek)   │
│              ├──────────────────────────────────────────────────────────────┤
│              │ [In 00:00:03.000] [Out 00:00:40.000]  Zoom [−][=====][+] [Fit]│
│              │ 0s      5s      10s      15s      20s      25s   …  (pravítko)│
│              │ screen1 ▌▒▒▒[img]▒▒▒▒[img]▒▒▒▒▒[img]▒▒▒▒▒▒▒▒▒▒▒▒▒[img]▒▒▒▐   │
│              │ screen2 ▌▒▒▒[img]▒▒▒▒[img]▒▒▒▒▒[img]▒▒▒▒▒▒▒▒▒▒▒▒▒[img]▒▒▒▐ ◄ vybraná│
│              │ audio   ▌╌▂▃▅▇▆▃▂╌▂▅▇▇▅▃▂╌╌▂▃▅▆▇▅▃▂▁╌╌▂▅▆▇▆▅▃▂╌╌╌▐          │
│              │                                       [     Render     ]     │
└──────────────┴──────────────────────────────────────────────────────────────┘
```

- **Seznam nahrávek** (QListView): výběr načte nahrávku (na pozadí: index snímků, přepis, špičky zvuku); během načítání je zobrazen stav. Výchozí výběr = první (nejnovější) položka, nebo položka ze `--session`.
- **Náhled**: snímek vybrané obrazovky v čase playheadu, zmenšený s poměrem stran (QLabel/vlastní paint s cache QPixmap). Kurzor se dokresluje jako malá šipka (viz otázka 2). Transport: Play/Pause (mezerník), Stop (návrat na In), skok na In/Out, krok o 1 snímek výstupu (1/24 s, šipky), časový údaj. Přepínač **Subtitles** (výchozí zapnuto); titulek je samostatný popisek pod náhledem (text segmentu, který obsahuje aktuální čas; prázdný, když žádný).
- **Časová osa** (vlastní QWidget s paintEvent):
  - pravítko s časem; playhead jako svislá čára, klik/tažení v pravítku přesune playhead;
  - jedna videostopa na obrazovku (`screen1` nahoře) s miniaturami rozmístěnými po pevném rozestupu v pixelech (cca každých 160 px), aby byly vidět při každém zoomu; miniatury se počítají na pozadí a cachují (LRU, max ~400 miniatur);
  - jedna zvuková stopa (společná – viz otázka 1) s křivkou špiček (min/max po blocích ~20 ms, spočteno jednou po načtení);
  - oblast mimo `[in, out]` ztmavená; **úchyty In/Out** na okrajích výřezu se táhnou myší (kurzor ↔), přichytávají se k playheadu a hranicím; klávesy `I`/`O` nastaví In/Out na playhead; tlačítka In/Out s editovatelným časem; **Reset Crop**;
  - **výběr stopy** kliknutím na hlavičku nebo tělo stopy (zvýrazněný rámeček); výběr ovlivňuje náhled i export;
  - zoom: Ctrl+kolečko (kolem pozice myši), posuvník, tlačítko Fit; vodorovný posun kolečkem/posuvníkem; při přehrávání se osa posouvá tak, aby playhead zůstal vidět.
- **Render**: velké tlačítko vpravo dole (a `Ctrl+R`). Když neběží export: dialog Uložit jako (filtr `*.mp4`), pak `ProgressDialog` s procenty (z `-progress` ffmpeg) a tlačítkem Cancel (ukončí ffmpeg, smaže rozpracovaný soubor). Po dokončení informační dialog s cestami k MP4 a SRT.
- Stavový řádek: název nahrávky, rozlišení vybrané obrazovky, délka výřezu, odhad výstupu.

Klávesy: `Space` play/pause, `Home`/`End` na In/Out, `←`/`→` o 1/24 s, `Shift+←/→` o 1 s, `I`/`O` In/Out, `Ctrl+=`/`Ctrl+-` zoom, `Ctrl+0` Fit, `Ctrl+R` Render, `Esc` zastaví přehrávání.

## 6. Přehrávání

- Zdroj času je **zvuk**: `sounddevice.OutputStream` (výstupní zařízení systému, int16 mono, vzorkovací frekvence z WAV) s callbackem, který čte bloky WAV od pozice playheadu (soubor otevřený přes `wave`, čtení po blocích – 2h WAV se nenačítá do paměti). Playhead = pozice ve streamu (počet odehraných vzorků) + začátek. Přehrávání se zastaví na `out_ms` (přehrává se jen výřez; Play mimo výřez začne od In).
- GUI timer (~30 Hz) čte playhead a překreslí náhled jen tehdy, když se změnil zobrazovaný snímek (snímky se mění po `capture_interval_ms`, dekódování PNG 2560×1600 trvá desítky ms, proto se snímek dekóduje jednou a škáluje do cache). Titulek a časový údaj se aktualizují každý tick. Dekódování snímku probíhá ve worker vlákně s malým předstihem (následující snímek).
- Bez zvukového výstupu (žádné zařízení): přehrávání běží z `time.perf_counter` a zobrazí se upozornění.

## 7. Export (Render)

Vstup: nahrávka, obrazovka `S`, interval `[in, out]`, výstupní cesta. Kroky:

1. **Plán snímků** (`export/plan.py`, čistý Python): z `FrameIndex[S]` vybrat snímky pokrývající interval: první = snímek platný v čase `in`, dále každý snímek s `in < time_ms < out`; každému přiřadit dobu trvání do dalšího snímku (poslednímu do `out`). Výsledek je seznam `(cesta, trvání_s)`.
2. **Concat seznam** pro `ffmpeg -f concat` (slideshow s `duration`; poslední položka zopakována podle pravidel concat demuxeru). Snímky se nekopírují ani nepřekódovávají do mezisouborů.
3. **Kurzor** (pokud odsouhlasen, otázka 2): malý PNG s šipkou se přidá jako druhý vstup a filtr `overlay` se řídí `sendcmd` skriptem: pro každý snímek, jehož sidecar má kurzor na obrazovce `S`, příkaz v čase `t`: `overlay x <px>`, `overlay y <py>`; mimo tyto časy je overlay vypnut (`enable`). Jeden příkaz na snímek, žádné kompozitování v Pythonu.
4. **Příkaz ffmpeg** (vždy CFR 24 fps, rozlišení obrazovky, zvuk z WAV ořezaný na interval):

   ```text
   ffmpeg -nostdin -y -hide_banner -progress pipe:1
     -f concat -safe 0 -i frames.txt            # (-i cursor.png, -filter_complex ... sendcmd/overlay)
     -ss <in_s> -to <out_s> -i audio.wav
     -map 0:v -map 1:a
     -vf "fps=24,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
     -c:v libx264 -preset medium -crf 18 -r 24
     -c:a aac -b:a 192k
     -movflags +faststart -shortest
     output.mp4
   ```

   Protokoly omezeny na `file,pipe`. Délka videa = `out − in`; zvuk kratší/delší o milisekundy se dorovná `-shortest` a `apad` (viz otázka 3).
5. **SRT** (`export/srt.py`): segmenty přepisu protínající interval, časy posunuté o `in` a oříznuté na `[0, out−in]`, číslované od 1, formát `HH:MM:SS,mmm`; uložen vedle MP4 se stejným základem jména (`…/<name>.srt`), UTF‑8 (s BOM? – ne, čisté UTF‑8).
6. **Průběh**: `-progress pipe:1` dává `out_time_ms`; procento = `out_time / (out−in)`. Zrušení = ukončení procesu a smazání částečného souboru.
7. **Ověření výsledku** (v testech): `ffprobe` – 1 video stream h264 24/1 fps v rozlišení obrazovky, 1 audio stream aac, délka ≈ `out−in` (±0,2 s).

Výkon a odhad: dvouhodinová nahrávka při 500 ms = 14 400 PNG snímků na obrazovku; výstup 172 800 snímků 2560×1600 v libx264 `medium` trvá na běžném CPU řádově 20–40 minut (statické duplikáty se kódují rychle). Náhledové hodnoty se ukazují ve stavovém řádku; preset je v configu (`export_preset`, výchozí `medium`) bez UI.

CLI (pro agenta i testy):

```text
video-story export --session <id|dir> [--screen screen1] [--start-ms 0] [--end-ms N] --output out.mp4 [--no-cursor] [--no-srt]
→ {"output": ..., "srt": ..., "duration_ms": ..., "frames_used": ..., "fps": 24, "width": ..., "height": ...}
video-story edit [--detach] [--session <id>]     # otevře editor; --detach vrátí {"pid", "session_id"}
```

MCP: `video_story_edit(session_id?)`, `video_story_export(session_id, screen?, start_ms?, end_ms?, output?)`.

## 8. Skill `video-story-edit`

- `skills/video-story-edit/SKILL.md`: spouští se na „Edit Video Story“, „otevři editor nahrávek“, „udělej z nahrávky video/MP4“. Postup pro agenta: `edit --detach` (volitelně `--session`), jednou větou potvrdit otevření; na přímou žádost o export bez okna použít `export`. Nepoužívat pro nahrávání (to je skill `video-story`).
- `agents/openai.yaml`, `scripts/video-story-edit.cmd|.sh` (stejný mechanismus jako u prvního skillu; oba skripty volají stejný balík).
- `install-hosts.*` a `docs/installation.md` zaregistrují i druhý skill (junction/symlink `~/.claude/skills/video-story-edit`), MCP server nabídne nové nástroje automaticky.
- `.claude-plugin/plugin.json` – přidání druhého skillu.

## 9. Konfigurace

Nové klíče v `config.json` (bez UI v dialogu Nastavení nahrávání): `export_preset` (`medium`), `export_crf` (18), `export_audio_bitrate_k` (192), `editor_draw_cursor` (true). Editor sám žádné nastavení neukládá (geometrie okna se neukládá – otázka 6).

## 10. Testy a ověření

- Unit (offscreen, všechny OS v CI): `TimelineModel` (čas↔px, zoom, crop invarianty), `ExportPlan` (výběr snímků a trvání vč. hranic a prázdných intervalů), concat seznam, sendcmd skript, SRT (ořez a posun), `recordings_model` (řazení, výchozí výběr), `EditorWindow` s falešnou nahrávkou (výběr stopy, In/Out klávesy, Render volá worker s parametry).
- Integrace (kde je FFmpeg – runnery GitHubu i tento stroj): syntetická nahrávka (barevné PNG se známými časy + 2 s WAV tón + přepis) → `export` → `ffprobe` ověří 24 fps, rozlišení, délku, zvuk; obsah ověřen vzorkováním barev v dekódovaných snímcích (`ffmpeg -ss … -frames:v 1`).
- Integrace Windows (ručně a `VIDEO_STORY_INTEGRATION=1`): export skutečné nahrávky `20261107-122147` (obě obrazovky), přehrání v náhledu se zvukem, titulky.
- Akceptační checklist rozšířen o kapitolu editoru; ruční scénář: otevřít editor přes agenta, vybrat nahrávku, přehrát, oříznout, vybrat screen2, Render, otevřít MP4 v přehrávači, zkontrolovat SRT.

## 11. Milníky

| Milník | Obsah | Ověření |
| --- | --- | --- |
| E1 | `export/` (plán, SRT, ffmpeg) + CLI `export` + MCP nástroj | unit + integrační export v CI na 3 OS |
| E2 | Kostra editoru: proces `edit`, seznam nahrávek, model časové osy, časová osa s pravítkem, stopami, zoomem, cropem a výběrem stopy, statický náhled | unit offscreen, ruční na Windows |
| E3 | Přehrávání se zvukem, titulky, kurzor v náhledu, klávesy | ruční na Windows |
| E4 | Miniatury a křivka zvuku na pozadí, výkon u dlouhých nahrávek | ruční (2h syntetická nahrávka) |
| E5 | Render dialog s průběhem a zrušením, stavový řádek | ruční + unit (worker) |
| E6 | Skill `video-story-edit`, instalační skripty, dokumentace, akceptační checklist, memory | registrace v Claude Code, CI zelené |
| E7 (po schválení) | Linux X11 a macOS: ověření na Fedoře (Xvfb + ALSA/PulseAudio výstup) a na GitHub runneru macOS; doladění (`QDesktopServices`, výstupní zařízení, cesty) | stejné sady testů |

Odhad náročnosti: E1 je nejmenší a nejdůležitější (export bez GUI), E2–E5 je většina práce (vlastní kreslení časové osy a synchronizace přehrávání).

## 12. Otevřené otázky

1. **Zvuková stopa.** Zvuk je jeden (mikrofon), společný pro všechny monitory. Navrhuji **jednu** zvukovou stopu pod všemi videostopami (nikoli kopii pod každým monitorem). Souhlasíš, nebo chceš dvojici video+audio pod sebou pro každý monitor?
2. **Kurzor.** Snímky z `mss` kurzor **neobsahují** (sidecar má jen souřadnice). Navrhuji kurzor **dokreslovat** v náhledu i v exportu jako malou šipku (cca 24 px, bílá s černým okrajem) na pozici ze sidecaru, když je kurzor na exportované obrazovce; v exportu přes `overlay` + `sendcmd` (bez překódování snímků), vypnutelné `--no-cursor` / `editor_draw_cursor`. Souhlasíš?
3. **Parametry MP4.** „Vysoká kvalita“ navrhuji jako H.264 `libx264 -crf 18 -preset medium`, `yuv420p` (kompatibilita s přehrávači; liché rozměry se zaokrouhlí na sudé), AAC 192 kb/s mono, `faststart`. Alternativa je CRF 16 (větší soubory) nebo H.265. Souhlasíš s H.264 CRF 18?
4. **Chování skillu.** Má agent po `edit --detach` skončit (editor je pro uživatele; výsledek exportu agent nepotřebuje), nebo má čekat a dostat cestu k vyexportovanému MP4? Navrhuji: **nečeká**; pro export bez okna existuje `export` (agent ho může zavolat na žádost „udělej mi z poslední nahrávky MP4“). Druhá možnost (`edit --wait`) jde doplnit později.
5. **Výchozí jméno a místo výstupu.** Navrhuji `<Recording Folder>/<id>_<screenN>.mp4` a SRT se stejným jménem vedle něj; dialog Uložit jako umožní změnu. Souhlasíš? (Alternativa: `~/Videos` nebo složka nahrávky `<Recording Folder>/<id>/export/`.)
6. **Okno editoru.** Má si pamatovat velikost/pozici a poměr splitterů (QSettings uživatele)? Navrhuji ano (bez položky v configu). A má editor dovolit otevřít jen jednu instanci (druhé spuštění jen zvedne okno), nebo libovolně? Navrhuji jednu instanci.
7. **Miniatury na stopě.** Navrhuji pevný rozestup v pixelech (každých ~160 px) nezávisle na zoomu, výška stopy ~60 px. Chceš jiné (např. jen pár miniatur na celou stopu)?
8. **Ořez uvnitř segmentu přepisu.** Když crop protne titulek, navrhuji segment zachovat s oříznutými časy (text celý). Alternativa: zahodit segmenty, které nejsou celé uvnitř. Souhlasíš s ořezáním?
9. **Rozsah seznamu nahrávek.** Jen zpracované nahrávky (`<id>/manifest.json`). Nahrávky bez přepisu (selhalo zpracování, zůstaly v `.work`) nenabízet. Souhlasíš?
10. **FFmpeg.** Editor i export vyžadují FFmpeg (včetně `libx264`, `aac`); použije se stejná detekce/stažení jako pro přepis (`doctor --fix`). Stažené sestavení BtbN je obsahuje. Je to v pořádku (jiná možnost není rozumná)?

## 13. Stav implementace (7. 11. 2026)

Odpovědi na otázky: všechny návrhy z kapitoly 12 odsouhlaseny beze změn.

| Milník | Stav |
| --- | --- |
| E1 export | hotovo: `export/plan.py`, `srt.py`, `cursor.py`, `ffmpeg_export.py`; CLI `export`, MCP `video_story_export`; testy `tests/unit/test_export.py`, `test_export_ffmpeg.py` (ffprobe, dekódované snímky, kurzor, zrušení). Ověřeno reálným exportem nahrávky `20261107-122147` (2560×1600, h264 24 fps, AAC, 44,08 s, SRT). |
| E2–E5 editor | hotovo: `editor/` (okno, seznam nahrávek, model a pohled časové osy, miniatury, křivka zvuku, náhled, přehrávač, render dialog); testy `tests/unit/test_editor.py`. Okno ověřeno na ploše Windows (dvě videostopy, zvuk, kurzor v náhledu, Render). |
| E6 skill a integrace | hotovo: `skills/video-story-edit`, CLI `edit [--detach] [--session]`, MCP `video_story_edit`, `host/editor_launcher.py` (zámek `.work/editor.lock`, `NO_RECORDINGS`, `ALREADY_RUNNING`), instalační skripty pro oba skilly, dokumentace, akceptační checklist (sekce „Editor and export“). |
| E7 Linux/macOS | neprovedeno (čeká na schválení): jednotkové a exportní testy běží v CI na všech třech systémech, okno editoru tam ověřeno není. |

Odchylky od návrhu:

- **Kurzor v exportu bez `sendcmd`.** Filtr `overlay` v FFmpeg 7.1 nepřijímá změny `x`/`y` za běhu (volby nejsou označené jako runtime), takže `sendcmd` příkazy ignoruje. Místo toho se poloha kurzoru vyjadřuje výrazem nad indexem snímku `n` – vyvážený strom `if(lt(n,N),a,b)` pro každou osu (`cursor_expressions`), vyhodnocovaný s `eval=frame`. Pro dlouhé nahrávky (desetitisíce klíčových snímků) jde graf přes `-filter_complex_script`, kratší inline. Režie je neměřitelná.
- Zvuk se do výstupu bere `-ss`/`-t` jako vstupní volby (`-to` jako vstupní volba je v starších FFmpeg nedostupná); délka výstupu se drží `-t` a `apad`.
- Nastavení editoru (`export_preset`, `export_crf`, `export_audio_bitrate_k`, `editor_draw_cursor`) jsou jen v `config.json`.

## 14. Rozšíření v2 (7. 11. 2026, zadání `video-story-spec-v2.md`, potvrzeno autorem)

| Část | Stav |
| --- | --- |
| Výchozí hodnoty | JPEG, kvalita 70 % (`core/config.py`); dokumentace a testy upraveny. |
| Kreslení | výchozí nástroj obdélník (`DEFAULT_TOOL`), paleta 8 barev (`ui/palette.py`). |
| Zoom náhledu | `editor/preview.py`: kolečko 1,25×/dílek ukotvené ke kurzoru, max 800 %, posun tažením, dvojklik = celý náhled; zoom přežívá změnu snímku, reset při jiném rozlišení; testy `tests/unit/test_editor_preview.py`. |
| Video Editor ve Stop dialogu | `ui/stop_dialog.py` (čtvrtá volba), `host/controller.py` (`process(hand_to_editor=True)` → stav `editing`, `editor_opened`, `editor_failed`), `ui/control_window.py` (po zpracování `launch_editor(..., recording_session=id)`, při selhání `processed` s celým rozsahem). |
| Process v editoru, crop.json | `editor/window.py`: zelené tlačítko (Ctrl+Enter), `crop.json` při každé změně (tažení s 300 ms debounce), obnovení při načtení, událost `process`/`closed` do souboru editor session, `finish_recording_session` / `cancel_recording_session`; připojení k běžícímu editoru přes požadavky v `.work/editor-requests/` (1 s). Testy `tests/unit/test_editor_process.py`. |
| Rozhraní pro agenta | `Status.EDITING`, pole `crop` a `processed_via` ve `state.json`; `host/editor_session.py`; `edit --detach` vrací `editor_session` a `attached`; `wait --editor`; MCP `video_story_wait_editor`; `describe` vrací `crop` a `transcript_in_range`; `active_session` ignoruje `editing`, takže nové nahrávání může začít, zatímco editor čeká. Skilly `video-story` a `video-story-edit` aktualizovány. |
| Ověření | 241 jednotkových testů; integrační `tests/integration/test_editor_flow.py` (Windows: Stop → Video Editor → skutečný proces editoru → zavření křížkem → `cancelled`). |

Rozhodnutí při implementaci: Process stisknutý nad jinou nahrávkou, než ke které patří nahrávací relace, ukončí relaci s cropem z jejího vlastního `crop.json` (crop zobrazené nahrávky jde jen do události editoru). Připojení k běžícímu editoru uzavře předchozí neodeslanou editor session a zruší neukončenou nahrávací relaci, aby dřívější agent nečekal navždy.
