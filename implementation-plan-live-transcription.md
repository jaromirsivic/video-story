# Video Story – implementační plán: přepis řeči už během nahrávání

Stav: **implementováno L0–L5, L6 bez ruční zkoušky (11. 11. 2026)**. Platné chování je ve [video-story-spec.md](video-story-spec.md) (kap. 4, 5, 6, 7, 8, 10, 11, 12) a tam, kde se tento plán liší, platí spec. Rozhodnutí uživatele jsou v kap. 12, výsledky měření L0 a co podle nich změnilo návrh v kap. 13. Ruční zkouška (acceptance.md, bod 17) a ověření na Linuxu a macOS zbývají.

## 1. Cíl a rozsah

Dnes začne přepis až v editoru, takže po delší nahrávce uživatel čeká, než doběhne celý (Render a Process čekají na úplný přepis). Nově se přepis rozběhne **hned po stisku Record**. Editor pak naváže tam, kam se přepis během nahrávání dostal, a přepíše jen zbytek.

Požadavky uživatele:

1. **Nerušit práci.** Výchozí režim nesmí vytížit procesor naplno. Uživatel dál normálně pracuje, kliká a mluví. Snímání obrazovek, záznam myši a zvuk mají vždy přednost.
2. **Vysoká kvalita přepisu.** Přepis po kouscích nesmí být znatelně horší než přepis celé nahrávky v editoru. Stejný model, žádné zkratky na úkor kvality. Rychlost se šetří prioritou a počtem vláken, ne horším dekódováním.
3. **Zpoždění nevadí.** Přepis smí zaostávat (10 s i víc) a může se i vzdalovat. Co se stihne, to v editoru odpadne.
4. **Volba v Settings** (combobox):

| Volba (UI anglicky) | Klíč `live_transcription` | Význam |
| --- | --- | --- |
| **Off – transcribe in the editor** | `off` | Jako dnes: přepis začne až v editoru. |
| **Light – low CPU use (recommended)** | `light` (**výchozí**) | Přepisuje se během nahrávání s nízkou prioritou a malým počtem vláken. Procesor dostane, jen když ho nepotřebuje nic jiného. |
| **Faster – more CPU** | `faster` | Víc vláken, priorita jen mírně snížená. Přepis drží krok i s nepřetržitou řečí. S počítačem jde dál pracovat, ale zatížení je znát. |

Tooltip: *„Transcribes your speech while you record, so the editor has less left to do. Light uses spare CPU only; Faster uses more CPU but still leaves room for your work.“*

| Patří do rozsahu | Nepatří |
| --- | --- |
| Přepis mikrofonu a zvuku počítače (stejný mix jako výchozí stav editoru) | Zobrazení titulků během nahrávání (živé titulky) |
| Navázání editoru na rozpracovaný přepis, s jazykem a kontextem | Přepis na GPU |
| Priority a počet vláken pro Windows, Linux a macOS | Jiný (menší) model pro živý přepis |
| Pojistka, která ubere vlákna, když začnou vypadávat snímky | Změna pravidla „agent jedná jen po Process“ |

Pravidla ze specifikace kap. 2 se nemění. Process dál čeká na úplný přepis. Auto process je při otevření editoru dál vždy odškrtnutý. Přepis běží lokálně.

## 2. Na čem plán stojí (stávající kód)

- **Editor už umí navázat.** `transcript.json` s `complete: false` a `transcribed_ms` editor načte (`LoadedRecording.transcribed_ms`) a `TranscriptionManager` spustí `transcribe-stream --from-ms <transcribed_ms>` ([editor/transcription.py](src/video_story/editor/transcription.py)). Stačí, aby takový soubor vznikl už v pracovní složce nahrávky. `SessionPaths.finalize()` ho přesune do finální složky spolu s ostatním (kromě `state.json`, `lock`, `ui.log`).
- **Zvuk jde číst průběžně.** `AudioRecorder` zapisuje `audio/audio.wav` modulem `wave` po blocích ~1024 vzorků z vlákna `audio-writer`; `ComputerAudioRecorder` drží `audio/computer.wav` na hodinách nahrávky (stejná vzorkovací frekvence). Obě stopy rostou souběžně.
- **Mix umí `core/audio_mix`.** Funkce `_to_mono_int16` a logika `MixReader` (součet, ořez na int16) jdou použít i pro rostoucí soubory, jen délku je potřeba brát z velikosti souboru, ne z hlavičky.
- **Pomocné procesy už existují.** Hook myši a UI Automation běží v pomocných procesech s `ABOVE_NORMAL_PRIORITY_CLASS`, v Job objektu s `KILL_ON_JOB_CLOSE` a končí, když se jim zavře stdin ([platform/windows_input.py](src/video_story/platform/windows_input.py)). Stejný vzor použije i worker přepisu. Že má záznam myši vyšší prioritu než přepis, je pro požadavek 1 zásadní.
- **pywhispercpp 1.5.1** předává do whisper.cpp `initial_prompt`, `language`, `n_threads` (lze měnit mezi voláními), `offset_ms/duration_ms`, `beam_search`, `vad` + `vad_model_path` (Silero). Pro kvalitu (kap. 5) se použije prompt a jazyk.
- **Výkon na tomto stroji:** large-v3-turbo q5_0 přepsal 76 s zvuku za 23 s s 32 vlákny (3,3× rychleji než reálný čas); 32 vláken je 1,6× rychlejší než 8.

## 3. Architektura

```text
Nahrávací okno (proces UI)                                 Worker (samostatný proces, nízká priorita)
┌──────────────────────────────────────┐   stdin: příkazy  ┌───────────────────────────────────────────┐
│ SessionController                    │ ────────────────▶ │ video-story transcribe-live <work dir>    │
│  start_recording() → LiveTranscriber │                   │   --mode light --from-ms N                │
│  stop_recording()  → kill + wait     │ ◀──────────────── │  1. sníží si prioritu (platform)          │
│  discard()         → kill + wait     │   stdout: JSON    │  2. načte model (jednou)                  │
│                                      │   události        │  3. smyčka: GrowingWav → plán okna →      │
│ LiveTranscriber (host/)              │                   │     resample 16 kHz → whisper → commit    │
│  - čte události ve vlákně            │                   │  4. stdin EOF ⇒ okamžitý konec            │
│  - zapisuje transcript.json/.txt     │                   └───────────────────────────────────────────┘
│    (atomicky, core/fileio)           │                         │ čte (jen pro čtení, bez zámků)
│  - hlídá výpadky snímků → throttle   │                         ▼
└──────────────────────────────────────┘                .work/<id>/audio/audio.wav, computer.wav
```

Proč samostatný proces:

- prioritu, počet jader a power throttling jde spolehlivě nastavit jen celému procesu;
- whisper nesdílí GIL ani paměť s nahráváním;
- pád workeru nahrávání neohrozí;
- je to stejný model jako `transcribe-stream` v editoru.

**Soubory přepisu zapisuje nahrávací okno, ne worker.** Worker jen posílá události. Díky tomu:

- po zabití workeru (Stop, Discard) nezůstane rozepsaný soubor a nic nedrží zámek na Windows;
- `transcript.json` se mění jen po celých oknech, takže je vždy konzistentní;
- když spadne nahrávací okno, worker skončí také (stdin EOF + Job objekt) a obnova (`host/recovery.py`) najde poslední zapsaný stav.

### 3.1 Nové a změněné moduly

| Soubor | Co |
| --- | --- |
| `transcribe/growing_wav.py` (nový) | `GrowingWav`: hlavička se přečte jednou (formát), počet vzorků se počítá z velikosti souboru `(size − data_offset) // block_align`, soubor se otevře jen na dobu čtení (žádný trvalý handle). `GrowingMix`: součet mikrofonu a zvuku počítače po úsecích, dostupná délka = minimum obou stop − 300 ms rezerva (zvuk počítače se dorovnává až o 100 ms). |
| `transcribe/live.py` (nový) | Čistá logika bez whisperu: plánování okna (`plan_window`), hledání pauz (`find_pauses`), test ticha (`has_speech`), pravidlo zápisu (`commit`), skládání promptu (`build_prompt`), výpočet vláken (`live_threads`). Plus smyčka workeru `run_live(...)`, která to spojí s backendem. |
| `transcribe/whisper_backend.py` | `transcribe_samples(samples16k, *, language, threads, initial_prompt)` – přepis pole vzorků bez WAV souboru. `transcribe(...)` dostane volitelný `initial_prompt` (whisper-cli: `--prompt`). |
| `transcribe/ffmpeg.py` | `resample_to_16k(samples, rate)` přes rouru (stdin `s16le` → stdout `s16le`, stejný filtr a `-ar 16000` jako `convert_to_16k_mono`), aby živý přepis i editor dostaly stejně převzorkovaný zvuk. |
| `platform/process.py` (+ `windows.py`, `linux.py`, `macos.py`) | `lower_own_priority(mode)` – kap. 4. |
| `host/cli.py` | Skrytý příkaz `transcribe-live DIR --mode light\|faster --from-ms N`. `transcribe-stream` při navázání převezme jazyk a prompt z existujícího `transcript.json` (kap. 5.6). `record` (headless, testy) dostane `--live-transcription MODE`. |
| `host/live_transcription.py` (nový) | `LiveTranscriber`: spuštění workeru (`console_python()`, Job objekt na Windows, `CREATE_NO_WINDOW`), čtecí vlákno událostí, zápis přepisu, restart po pádu (nejvýš 2×), `stop()` = zavřít stdin + kill + wait (nejvýš 3 s), statistika pro manifest, příkaz `threads N`. |
| `core/transcript.py` | `append_segments(transcript, segments, transcribed_ms, language)` – sloučení bez duplicit. Používá ho `LiveTranscriber` i `LoadedRecording.add_segments` (dnes je ta logika jen v editoru). |
| `core/config.py` | `live_transcription: Literal["off", "light", "faster"] = "light"`, validace jako u ostatních klíčů. |
| `core/manifest.py` | Sekce `live_transcription` (kap. 6.2), jinak `null`. |
| `host/controller.py` | Start workeru po `recorder.start()`, zastavení v `stop_recording()` a `discard()` **před** `finalize()` / `paths.discard()`. |
| `ui/settings_dialog.py` | Combobox **Live Transcription** pod Audio/Mouse. Při backendu `whisper-cli` je volba zašedlá s vysvětlením (kap. 12, bod 6). |
| `ui/control_window.py` | Throttle podle výpadků snímků (kap. 4.3), volitelně stav přepisu ve stavovém řádku (kap. 12, bod 4). |
| `host/doctor.py` | Řádek „live transcription: light, 8 threads, background priority“. |

## 4. Zatížení procesoru: režimy Light a Faster

### 4.1 Priorita procesu (worker si ji nastaví sám hned po startu)

| OS | Light | Faster |
| --- | --- | --- |
| Windows | `SetPriorityClass(GetCurrentProcess(), PROCESS_MODE_BACKGROUND_BEGIN)`: procesor na úrovni Idle a navíc nízká priorita I/O a stránek paměti, takže worker nevytlačí z RAM ani disku nic jiného. Na baterii se chová stejně jako na síti (rozhodnutí 5). | `BELOW_NORMAL_PRIORITY_CLASS` |
| Linux | `SCHED_IDLE` (`os.sched_setscheduler`) + I/O třída idle (`ioprio_set`, ctypes) | `os.nice(10)` |
| macOS | `setpriority(PRIO_DARWIN_PROCESS, 0, PRIO_DARWIN_BG)` (procesor i I/O, na Apple Silicon efektivní jádra) | `os.nice(10)` |

Pořadí priorit při nahrávání: **hook myši a UI Automation (Above normal) > nahrávací okno, snímání, zvuk (Normal) > aplikace uživatele (Normal) > worker (Below normal / Background)**. Uživatelova práce i záznam proto vždy dostanou procesor dřív.

Selhání nastavení priority (politika, sandbox) se jen zaloguje. Light pak běží s Below normal a doctor to ukáže.

### 4.2 Počet vláken (potvrzeno v L0, kap. 13)

| Režim | Vzorec | 32 logických CPU (tento stroj) | 8 logických CPU |
| --- | --- | --- | --- |
| Light | `clamp(cpu // 4, 2, 8)` | 8 vláken (~25 % CPU), naměřeno 1,2× rychleji než reálný čas (RTF 0,81; background mode pouští Windows na úsporná jádra) | 2 vlákna (zaostává, editor dokončí víc) |
| Faster | `clamp(cpu // 2, 2, 16)` | 16 vláken (~50 % CPU), naměřeno 2,7× (RTF 0,36) | 4 vlákna |
| Editor (beze změny) | `whisper_threads` = `min(32, cpu)` | 32 | 8 |

Nikdy se nepoužijí všechna jádra. Polovina (Faster) až tři čtvrtiny (Light) zůstávají uživateli a nahrávání. Na pomalém stroji Light zaostává. To je v pořádku (požadavek 3): co se nestihne, dodělá editor plnou rychlostí.

### 4.3 Pojistka proti zpomalení nahrávání (throttle)

Paměťová propustnost se sdílí i při nízké prioritě, takže se zátěž hlídá podle skutečného dopadu:

- Nahrávací okno každou sekundu (stávající `_tick`) porovná `recorder.dropped_ticks` a `failed_grabs`.
- Přibude-li za posledních 10 s víc než 2 vynechané snímky, pošle workeru `threads <polovina>` (nejméně 2). Změna platí od dalšího okna, protože `n_threads` jde měnit mezi voláními.
- Po 2 min bez vynechaných snímků vrátí původní počet.
- Každé ubrání se zapíše do logu a do statistiky v manifestu.
- Pojistka platí v obou režimech; ve Faster zasáhne pravděpodobněji.

### 4.4 Paměť

Model large-v3-turbo q5_0 zabere ve workeru ~0,8–1,2 GB RAM po celou dobu nahrávání a načítá se jednou (pár sekund, při nízké prioritě déle). Editor si model načítá sám. Worker je ale v tu chvíli už ukončený, takže dva modely v paměti nejsou nikdy.

## 5. Kvalita přepisu

Přepis po oknech je náchylný ke třem chybám: rozseknutému slovu na hraně okna, ztrátě kontextu (jména, interpunkce, jazyk) a halucinacím v tichu. Proti každé je opatření:

### 5.1 Stejný model a dekódování jako editor

Stejný `ggml-large-v3-turbo-q5_0.bin`, stejný backend, stejné parametry dekódování (greedy, `best_of` 5, prahy entropie a logprob z výchozích hodnot whisper.cpp). Režim mění jen prioritu a vlákna, takže výsledek nezávisí na volbě Light/Faster.

### 5.2 Okna řezaná v pauzách

Whisper zpracovává 30s okna. Plánovač (`plan_window`) dělá okno od bodu `P` (kam je přepis hotový):

1. Čeká, až je k dispozici `P + 30 s` zvuku. (Původně okno smělo skončit už po 12 s v delší pauze. L0 ukázal, že enkodér whisperu počítá vždy celých 30 s, takže krátké okno stojí stejně jako dlouhé; předčasné řezání je proto vypnuté, `LiveParams.early_cut`.)
2. Konec okna `C` je **nejdelší pauza** ≥ 0,4 s, která začíná v rozmezí `P + 20 s … P + 30 s`, jinak nejdelší pauza v `P + 12 s … P + 30 s` (dlouhá pauza je nejspíš konec věty a okno zůstane dlouhé). Pauza se hledá v energii po 20ms rámcích s prahem odvozeným od hladiny šumu: 10. percentil posledních minut + 12 dB.
3. Pauza se hledá nejdřív v mixu. Když mix pauzu nemá (hraje hudba nebo video), hledá se pauza **jen v mikrofonu**, tedy v řeči vypravěče. Když ani tam není, okno se utne natvrdo na `P + 30 s`.

### 5.3 Pravidlo zápisu (commit)

- **Okno končí v pauze:** zapíší se všechny segmenty a `transcribed_ms = C`.
- **Tvrdý řez:** zahodí se poslední segment, který končí později než `C − 1,5 s` (může být useknutý). Zapíše se zbytek. Další okno začne na začátku zahozeného segmentu minus 200 ms. Nové segmenty, jejichž střed leží před `transcribed_ms`, se zahodí jako duplicity.
- **Pojistka proti zacyklení:** když okno dá jediný segment, zapíše se celý.

### 5.4 Ticho se nepřepisuje

Okno, ve kterém `has_speech` nenajde řeč (energie pod prahem po celou dobu), se whisperu nepředá. Jen se posune `transcribed_ms = C`. To šetří procesor a zabraňuje halucinacím typu „Titulky vytvořil…“ v tichu. Stávající filtr `drop_credit_hallucinations` zůstává jako druhá pojistka. Volitelně (kap. 12, bod 7) jde později nasadit Silero VAD z whisper.cpp.

### 5.5 Kontext a jazyk mezi okny

- **Prompt: vypnutý (změna proti návrhu na základě L0).** Návrh byl předat každému oknu konec dosavadního textu jako `initial_prompt`. Měření na třech nahrávkách (kap. 13) ukázalo, že prompt slova nezlepší (shoda s přepisem celé nahrávky stejná nebo horší), ale whisper pak vrací celé okno jako jeden či dva dlouhé segmenty (průměr 12–13 s místo 3–10 s). Titulky v editoru, stopa narration i párování řeči s kurzorem a snímky v `timeline` by tím utrpěly. Navíc prompt přenášel jednou zvolený tvar slova dál. Kód prompt umí (`LiveParams.use_prompt`, `scripts/live_benchmark.py --prompt`), výchozí je vypnuto.
- **Jazyk:** první okno s řečí (≥ 20 znaků textu) běží s `language="auto"`. Zjištěný jazyk se pak **zafixuje** pro další okna i pro navázání v editoru. Krátká okna tak nepřeskakují mezi jazyky, třeba čeština a slovenština u krátké věty. Zapíše se do `transcript.json` (`language`).



### 5.6 Navázání v editoru

`transcribe-stream --from-ms N` (editor) při `N > 0` přečte `transcript.json` a použije z něj `language` (místo `auto`), takže navázání nepřeskočí do jiného jazyka. Prompt se nepředává (viz 5.5). Platí to i pro dnešní navazování po zavření editoru.

### 5.7 Měřitelné kritérium kvality

Referenční přepis: uživatel v editoru přes **Edit** opraví přepis 2–3 vlastních nahrávek (5–15 min, čeština, aspoň jedna se zvukem počítače). Skript `scripts/live_benchmark.py` přehraje zvuk těch nahrávek jako rostoucí WAV v reálném čase (nebo zrychleně) přes stejný worker a porovná:

| Metrika | Požadavek |
| --- | --- |
| WER/CER živého přepisu proti referenci | nejvýš o 1 procentní bod horší než přepis celé nahrávky (dnešní cesta) |
| Rozseknutá slova na hranách oken | 0 v testovacích nahrávkách |
| Halucinace v tichu | 0 |

## 6. Průběh a data

### 6.1 Životní cyklus

| Událost | Co se stane |
| --- | --- |
| **Record** | Po `recorder.start()` (zvuk už běží) `LiveTranscriber.start(mode)`, jen když `live_transcription != off`, preflight nic nehlásí (model, FFmpeg) a backend je `pywhispercpp`. Když uživatel nahrává „i tak“ bez modelu, živý přepis neběží. |
| Nahrávání | Worker čeká na zvuk, přepisuje okna a posílá `committed`. Okno zapíše `transcript.json` + `.txt` (`complete: false`, `transcribed_ms`, `language`, `model`, `backend`) atomicky přes `core/fileio`. |
| **Stop → Video Editor**, Auto stop, málo místa, chyba zápisu | `stop_recording()` → `LiveTranscriber.stop()`: zavře stdin, kill, wait ≤ 3 s. Rozpracované okno se zahodí, zapsané zůstává. Pak manifest a `finalize()` jako dnes (přesune i přepis). Editor naváže od `transcribed_ms`. |
| **Continue Recording** | Nic, worker běží dál. |
| **Discard Recording** | Worker se ukončí před `paths.discard()`, přepis se smaže s nahrávkou. |
| Pád workeru | Zaloguje se a do 5 s se spustí znovu od `transcribed_ms` (nejvýš 2×, pak už ne). Nahrávání pokračuje bez hlášky. |
| Pád nahrávacího okna | Worker skončí (stdin EOF, Job objekt). Obnova dokončí nahrávku i s přepisem do posledního okna. Editor naváže. |
| Editor: přepnutí stopy / pero | Jako dnes: přepis se smaže a začne od nuly. Živý přepis vycházel z výchozí volby (obě stopy), takže práce se zahodí. To je očekávané. |

Zabití místo „dokonči okno“ je zvolené, aby se editor otevřel okamžitě. Ztrácí se nejvýš jedno rozpracované okno (≤ 30 s zvuku), které editor plnou rychlostí přepíše za několik sekund (kap. 12, bod 2).

### 6.2 Události workeru (stdout, JSON na řádek) a příkazy (stdin)

Události: `ready` (model načten, `threads`, `priority`), `window` (`start_ms`, `end_ms`, `cut`: `pause|mic_pause|hard|silence`), `committed` (`segments[]`, `transcribed_ms`, `language`, `rtf`), `error` (`message`). Příkazy: `threads N`, `stop`. EOF stdin = okamžitý konec.

Manifest `live_transcription` (zapíše controller při uložení):

```json
{ "mode": "light", "threads": 8, "priority": "background", "throttled": 0,
  "windows": 41, "silent_windows": 9, "restarts": 0,
  "transcribed_ms_at_stop": 1180400, "recorded_ms": 1203100, "rtf": 0.46, "error": null }
```

Přepis samotný nemá nová pole, formát `transcript.json` zůstává stejný.

## 7. Poruchy

| Situace | Chování |
| --- | --- |
| Chybí model nebo FFmpeg | Živý přepis neběží, ohlásí to stávající preflight. Editor přepíše vše jako dnes. |
| Worker nejde spustit nebo opakovaně padá | Nahrávání pokračuje, manifest `live_transcription.error`. Editor přepíše zbytek. |
| Priorita nejde snížit | Light běží s Below normal, poznámka v logu a v doctor. |
| Málo RAM (model se nenačte) | Jako pád workeru, bez restartu. |
| Uživatelova aplikace vytěžuje CPU | Worker v Light skoro stojí (Background priorita). Nic se nerozbije, editor dodělá víc. |
| Vypadávají snímky | Throttle ubere vlákna (kap. 4.3). |

## 8. Testy

**Unit (bez whisperu):**

- `GrowingWav`/`GrowingMix`: čtení souboru, do kterého jiné vlákno právě zapisuje; zastaralá hlavička; neúplný poslední rámec; chybějící `computer.wav`; různá délka stop.
- `plan_window`, `find_pauses`, `has_speech`: syntetické signály (řeč = šum s obálkou, pauzy, hudba v jedné stopě).
- `commit`: řez v pauze, tvrdý řez, jediný segment, duplicity.
- `build_prompt`, fixace jazyka, `live_threads` pro různé počty CPU.
- `append_segments` (sdílené s editorem).
- Config: výchozí `light`, neplatná hodnota → chyba. Settings dialog: combobox, zašedlý při `whisper-cli`.
- Controller s falešným workerem (monkeypatch; žádný skutečný editor ani whisper): start, stop před `finalize`, discard, restart po pádu, manifest. Testy, které dojdou k `launch_editor`, ho musí nahradit (`cw.launch_editor`).

**Integrační (pomalé, se skutečným modelem):**

- Vlákno zapisuje `tests/samples/czech.wav` (a delší složený vzorek) po 100 ms do rostoucího WAV, worker běží v Light a výsledek se porovná s přepisem celého souboru.
- Headless `record --live-transcription light` → finální složka má `transcript.json` s `complete: false` a editor (`TranscriptionManager` s testovacím příkazem) ho dokončí.
- Obnova: zabitý proces okna uprostřed → recovery → přepis zachován.

**Ruční (acceptance.md, Windows):**

- 10 min nahrávky v Light při normální práci: v manifestu `dropped_ticks` ani `failed_grabs` nenarostou proti Off, žádný restart hooku myši, okno i myš reagují, CPU workeru ≤ ~25 %.
- Po Stopu editor dokončí přepis do ~20 s.
- Totéž ve Faster.
- Pozdější ověření na Linuxu (VM) a macOS (CI).

## 9. Milníky

| # | Obsah | Výstup |
| --- | --- | --- |
| **L0** | Měření před implementací: `scripts/live_benchmark.py` (přehrání nahrávky jako rostoucí WAV, prototyp oken), rychlost pro 2/4/8/16 vláken při Background a Below normal, dopad na `dropped_ticks` a latenci hooku při souběžné nahrávce, kvalita proti referenci (kap. 5.7). | Potvrzené počty vláken, délky oken a prahy pauz; tabulka výsledků do kap. 13. **Brána:** když kvalita nesplní kap. 5.7, upraví se strategie oken dřív, než se píše zbytek. |
| **L1** | `growing_wav.py`, čistá logika `live.py`, `append_segments`. | Unit testy. |
| **L2** | Backend `transcribe_samples` + prompt, `resample_to_16k`, `lower_own_priority`, příkaz `transcribe-live` se smyčkou, stdin lifeline a Job objektem. | Integrační test s rostoucím WAV. |
| **L3** | `LiveTranscriber`, napojení na controller (start, stop, discard, restart), manifest, recovery. | Unit testy controlleru, test obnovy. |
| **L4** | Config, Settings combobox, doctor, throttle, (volitelně) stav v nahrávacím okně. | Unit testy UI, offscreen screenshot Settings. |
| **L5** | Navázání editoru s jazykem a promptem (`transcribe-stream`). | Unit + integrační test navázání. |
| **L6** | Dokumentace (kap. 11), acceptance.md, ruční zkouška na Windows, Linux VM, CI. | Aktualizovaná spec a stav v tomto plánu. |

## 10. Odhad dopadu na uživatele

S Light na tomto stroji (8 vláken, naměřeno 1,2× rychleji než reálný čas) drží přepis krok s řečí a zaostává o jedno až dvě okna (30–60 s). Po Stopu tedy editoru zbývá zhruba poslední půlminuta až minuta zvuku, kterou přepíše za ~10–30 s (podle `whisper_threads`; RTF 0,27 s 32 vlákny) místo ~4 min u 15min nahrávky. Na slabším notebooku s Light se zbytek zkrátí méně, ale nikdy nebude delší než dnes.

## 11. Dokumentace ke změně

- **video-story-spec.md:**
  - kap. 5: řádek *Live Transcription* v tabulce Settings;
  - kap. 6: nový bod „Přepis během nahrávání“;
  - kap. 7: Stop ukončí živý přepis, hotová část zůstává;
  - kap. 8 *Přepis*: editor naváže na přepis z nahrávání s jazykem a kontextem;
  - kap. 10: řádek pro selhání workeru;
  - kap. 11: `transcript.*` může vzniknout už v `.work/<id>`, manifest `live_transcription`;
  - kap. 12: paměť ~1 GB během nahrávání, živý přepis vychází z obou stop.
- **README.md**, **docs/installation.md** (požadavky na RAM), **tests/manual/acceptance.md**.
- **skills/video-story/SKILL.md** se nemění: agent čeká na Process stejně jako dnes.

## 12. Rozhodnutí k odsouhlasení

| # | Otázka | Stav (11. 11. 2026) |
| --- | --- | --- |
| 1 | Názvy voleb v comboboxu | **Schváleno:** *Off – transcribe in the editor* / *Light – low CPU use (recommended)* / *Faster – more CPU*; popisek řádku **Live Transcription** |
| 2 | Co při Stopu s rozpracovaným oknem | **Schváleno:** zabít hned (editor se otevře okamžitě, ztratí se ≤ 30 s práce) |
| 3 | Zafixovat jazyk po prvním okně s řečí | **Schváleno:** zafixovat (varianta A, kap. 5.5). Chová se jako dnešní přepis v editoru: delší pasáže v jiném jazyce se mohou přeložit do prvního jazyka. Detekce po oknech (s prahem jistoty) zamítnuta |
| 4 | Ukazovat stav přepisu v nahrávacím okně | **Schváleno:** krátce ve stavovém řádku, např. `text −0:25` (o kolik přepis zaostává) |
| 5 | Baterie | **Rozhodnuto:** baterie se neřeší, na baterii se vše chová stejně jako na síti (žádné EcoQoS) |
| 6 | Backend `whisper-cli` | **Schváleno:** živý přepis nenabízet (volba zašedlá s vysvětlením); `pywhispercpp` je výchozí |
| 7 | Silero VAD místo energetického detektoru | **Schváleno:** zatím ne. Pokud by ho měření L0 ukázalo jako potřebný, nejdřív se to uživateli řekne a nasadí se **jen po jeho potvrzení** |
| 8 | Beam search pro vyšší kvalitu | **Schváleno (původní návrh):** v první verzi ne, živý přepis i editor dekódují greedy jako dnes; beam search se případně změří v L0 |
| 9 | Backend a GPU | **Rozhodnuto:** zůstává `pywhispercpp` na CPU; přepis na GPU (např. whisper-cli s CUDA) je mimo rozsah |
| 10 | Prompt (kap. 5.5) | **Změněno podle L0:** vypnutý, protože slil titulky do dlouhých bloků bez zlepšení slov (kap. 13). Předčasné řezání oken také vypnuto (5.2). Uživateli oznámeno |

## 13. Výsledky měření (L0)

Windows 11, 32 logických CPU (hybridní procesor), model large-v3-turbo q5_0, 11. 11. 2026. Skript `scripts/live_benchmark.py` na třech skutečných českých nahrávkách: 8,4 min, 7,8 min a 5,0 min (poslední se zvukem počítače). Referenční přepisy opravené ručně zatím nejsou (kap. 5.7). Porovnává se proto s přepisem celé nahrávky tak, jak ho dělá editor, a rozdíly se posuzovaly čtením.

**Kvalita** (16 vláken, normální priorita; „rozdíl“ = WER živého přepisu proti přepisu celé nahrávky):

| Nahrávka | Celá nahrávka: segmentů / průměr | S promptem: segmentů / průměr, rozdíl | Bez promptu: segmentů / průměr, rozdíl |
| --- | --- | --- | --- |
| 8,4 min | 142 / 2,8 s | 34 / 13,4 s, 1,7 % | 93 / 4,7 s, 1,9 % |
| 7,8 min | 43 / 10,0 s | 36 / 12,2 s, 7,3 % | 90 / 4,6 s, 6,8 % |
| 5,0 min + zvuk PC | 32 / 8,3 s | 24 / 11,8 s, 5,9 % | 38 / 7,2 s, 4,5 % |

- Rozdíly jsou hlavně v zaváhání a opravách vypravěče („teď jsem ho dokreslil“, „moment“), v pravopisných variantách („text box“ / „textbox“, „obdelníčky“ / „obdelničky“) a ve vynechaných slovech výplně, na obou stranách. Přepis celé nahrávky si na jednom místě vymyslel „www hradeckralove org“, živý přepis ne. Ztráta slov na hranách oken se nenašla.
- Na 8,4min nahrávce: 20 oken, z toho 15 v pauze, 5 tvrdým řezem a jedno zbytkové; na 7,8min nahrávce jedno okno ticha přeskočeno.
- Prompt: viz 5.5. Bez promptu je shoda stejná nebo lepší a titulky krátké, proto je výchozí vypnutý.
- První prototyp s předčasnými okny (od 12 s) byl na krátké nahrávce 1,5× dražší než dlouhá okna. Proto je vypnutý (5.2).

**Rychlost a dopad na nahrávání** (8,4min nahrávka přehrávaná co nejrychleji, souběžně headless `record` 120 s, dvě obrazovky 2560×1600 + 1920×1080, 500 ms, mikrofon + zvuk počítače + myš):

| Režim | Vlákna, priorita | RTF (čas přepisu / délka zvuku) | Nahrávání 120 s: snímků / vynechaných ticků / nepořízených snímků / ztracených bloků zvuku |
| --- | --- | --- | --- |
| bez přepisu | – | – | 480 / 1 / 0 / 0 |
| Light | 8, background mode | 0,81 | 482 / 0 / 0 / 0 |
| Faster | 16, below normal | 0,36 | 482 / 0 / 0 / 0 |
| editor (pro srovnání) | 32, normální | 0,27 | – |

- Light je pomalejší, než naznačoval výkon na 8 vláknech s normální prioritou (RTF ~0,35). Windows totiž vlákna v background mode plánuje na úsporná jádra hybridního procesoru. Přesto stíhá rychleji než řeč, takže po Stopu zbývá editoru zhruba poslední okno (≤ 30 s zvuku a rozpracovaná část).
- Background mode se projevil i při načítání modelu: když počítač naplno pracoval (souběžné měření), worker v Light za 40 s model nenačetl. To je zamýšlené chování („jen volný procesor“).
- Výpadky snímků se v žádném režimu neobjevily, takže pojistka (4.3) při měření nezasáhla. Latence hooku myši se automaticky neměřila: hook běží v pomocném procesu s prioritou Above normal. Ověří ji ruční zkouška (acceptance.md, bod 17).
- Beam search (kap. 12, bod 8) se neměřil, první verze zůstává u greedy.
