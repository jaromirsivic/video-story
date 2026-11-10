# Video Story – implementační plán: akce myši, prvky pod myší, gesta touchpadu

Stav: **implementováno (M0–M6, 10. 11. 2026)**; platné chování je ve [video-story-spec.md](video-story-spec.md) (kap. 2, 5, 6, 9, 10, 11, 12) a tam, kde se tento plán liší, platí spec. Výsledky M0 jsou v kap. 12. Skutečné gesto prsty na touchpadu a čtení označeného textu v cizích aplikacích ověřuje ruční zkouška ([tests/manual/acceptance.md](tests/manual/acceptance.md)).

## 1. Cíl a rozsah

Během nahrávání zaznamenávat, **co uživatel dělá myší a na čem**, a předat to agentovi jako krátké řádky v `timeline`, aby nemusel otevírat tolik snímků.

| Patří do rozsahu | Nepatří |
| --- | --- |
| Klik, dvojklik (a vícenásobný klik), tažení, dlouhé podržení | Klávesnice (kromě stavu modifikátorů Ctrl/Shift/Alt/Win v okamžiku akce) |
| Tlačítka levé, prostřední, pravé, X1 (zpět), X2 (vpřed) a schéma pro dalších 6 (`b6`–`b11`) | Gesta třemi a čtyřmi prsty |
| Kolečko svisle i vodorovně, včetně jemného (high-res) posunu | Dotyková obrazovka a pero |
| Prvek pod myší: role, název, okno, proces a **obsah prvku** (hodnota pole, text v místě kliku, označený text; nejvýš 4096 znaků) | Obsah polí pro heslo (nikdy) |
| Gesta touchpadu dvěma prsty: posun a pinch | Zobrazení kliků v editoru a v MP4 (navazující práce, kap. 10) |
| Rozhraní připravené pro implementaci na macOS a Linuxu | Samotná implementace pro macOS a Linux (jen popis v kap. 8) |

Vše zapíná a vypíná **jeden checkbox** v Settings (kap. 4.3): když je zaškrtnutý, zaznamenávají se akce myši i všechna data k nim (titulky oken, prvky pod myší, jejich obsah).

## 2. Co jde a co nejde (Windows)

| Požadavek | Proveditelnost | Jak |
| --- | --- | --- |
| Levé, pravé, prostřední, X1, X2 | Ano | `WH_MOUSE_LL` hook: `WM_[LRM]BUTTONDOWN/UP`, `WM_XBUTTONDOWN/UP` |
| Dalších až 6 tlačítek (6.–11.) | **Na Windows ne.** | Windows (hook i Raw Input) zná jen 5 tlačítek. Další tlačítka herních a kancelářských myší přemapuje software výrobce (Logitech Options, G Hub…) na klávesové zkratky nebo na X1/X2; jako „tlačítko 6“ k aplikaci nikdy nedorazí. HID kolekci myši si systém otevírá výhradně, takže ji číst nejde. Schéma `b6`–`b11` zavedeme kvůli Linuxu (X11 posílá tlačítka 8+) a macOS (`buttonNumber` až 31). |
| Dvojklik | Ano, vlastní detekce | Hook `WM_LBUTTONDBLCLK` neposílá; rozhodujeme sami podle `GetDoubleClickTime()` a `SM_CXDOUBLECLK`/`SM_CYDOUBLECLK`. |
| Tažení | Ano | Stisk, pohyby se stisknutým tlačítkem, puštění; práh `SM_CXDRAG`/`SM_CYDRAG`. |
| Kolečko svisle a vodorovně | Ano | `WM_MOUSEWHEEL`, `WM_MOUSEHWHEEL`, `delta` v jednotkách 120 na zářez; menší hodnoty = jemné kolečko nebo touchpad. |
| Posun dvěma prsty (Precision Touchpad) | Ano, přes M5 | Hook ho vidí **jen nad aplikacemi bez DirectManipulation** (v M0 okno Qt: 684 událostí kolečka). Nad VS Code, Chrome, Edge a Poznámkovým blokem Windows 11 hook nedostane nic. Spolehlivě jen z Raw Input touchpadu (M5, kap. 7). |
| Pinch dvěma prsty | Ano, přes M5 | Aplikacím bez DirectManipulation posílá systém Ctrl+kolečko (v M0: 165× `wheel` s Ctrl) → `ZOOM-SCROLL`. V prohlížečích a moderních aplikacích hook nic nevidí, ale Raw Input touchpadu pinch zachytí všude (kap. 7). |
| Prvek pod myší a jeho obsah | Ano | UI Automation přes `comtypes` v samostatném vlákně s časovým limitem (kap. 6). |

## 3. Architektura

```text
platform/ (OS specifické)                  capture/ (bez OS kódu)                core/ (čistá logika, bez Qt a OS)
┌───────────────────────────┐   surové    ┌───────────────────────────┐        ┌──────────────────────────────┐
│ MouseSource               │──události──▶│ InputRecorder             │──zápis─▶ input/mouse.jsonl          │
│  windows_input.py         │             │  - mapování na obrazovku  │        │                              │
│   (hook v pomocném proc.) │             │                           │        │                              │
│  [macos.py, linux.py: 8]  │             │  - slučování kolečka      │        │ input_events.py (schéma, IO) │
├───────────────────────────┤             │  - žádosti o prvek        │        │ mouse_actions.py (klik, …)   │
│ ElementInspector          │◀──bod, čas──│  - detekce zastavení      │        │ gestures.py (pinch, posun)   │
│  windows_uia.py (UIA)     │──prvek─────▶│  - obsah jednou (hash)    │        └──────────────┬───────────────┘
├───────────────────────────┤             └───────────────────────────┘                       │
│ TouchpadSource (M5)       │──kontakty──▶ (stejný recorder → gesture)            host/events.py: řádky timeline
│  windows_touchpad.py      │                                                     host/cli.py: `mouse` (detail)
└───────────────────────────┘
```

Zásady:

- **Do souboru jdou surové události, význam se počítá až při čtení.** Klik, dvojklik, tažení a scroll vznikají v `core/mouse_actions.py` z `down`/`up`/`move`/`wheel` a systémových parametrů uložených v manifestu. Klasifikace je deterministická, testovatelná bez OS a dá se změnit i pro staré nahrávky.
- **Selhání nikdy nezastaví nahrávání** (stejně jako zvuk počítače): když hook nejde nainstalovat, nahrávka prostě nemá `input/mouse.jsonl` a manifest říká proč.
- **Hook běží v samostatném pomocném procesu** (`python -m video_story.platform.windows_mouse_hook`, priorita `ABOVE_NORMAL`), ne ve vlákně okna Video Story. M0 ukázal, že callback hooku v hlavním procesu čeká na GIL, když se kódují snímky. Pak se zpozdí myš **v celém systému**: medián 16 ms, p95 109 ms, nejhůř 170 ms. Ve vlastním procesu trvá callback pod 0,1 ms a událost dorazí do 2 ms i pod zátěží (kap. 12).
- **Pomocný proces nic nepočítá.** Callback jen orazítkuje událost `time.perf_counter_ns()`, zapíše jeden řádek do roury (stdout) a vrátí `CallNextHookEx`. `perf_counter_ns` je na Windows QPC, které je společné pro všechny procesy; rodič od něj odečte `_start_ns` svých `RecordingClock` a dostane čas nahrávky. (Na macOS a Linuxu je to `CLOCK_MONOTONIC`, také společný pro procesy.) Pomalý hook Windows potichu odpojí (`LowLevelHooksTimeout`).
- **Hlídání:** rodič čte rouru ve vlákně. Když pomocný proces skončí nebo kurzor se 5 s hýbe a nepřijde nic, proces se spustí znovu (nejvýš 3×) a do souboru se zapíše `status`. Když Video Story spadne, pomocný proces skončí sám, protože mu zanikne roura (zápis selže) a rodič je v Job Objectu s `KILL_ON_JOB_CLOSE`.
- **Souřadnice** jsou všude fyzické pixely virtuální plochy, stejné jako `Platform.cursor_position()`, a na obrazovku se mapují existující funkcí `core.cursor.locate_cursor`.

### 3.1 Rozhraní v `platform/base.py` (smlouva pro všechny OS)

```python
class MouseButton(StrEnum):      # core/input_events.py
    LEFT = "left"; MIDDLE = "middle"; RIGHT = "right"; X1 = "x1"; X2 = "x2"
    B6 = "b6"; B7 = "b7"; B8 = "b8"; B9 = "b9"; B10 = "b10"; B11 = "b11"

@dataclass(frozen=True)
class RawMouseEvent:             # co platforma předá; žádná interpretace
    kind: Literal["down", "up", "move", "wheel"]
    t_ms: int                    # hodiny nahrávky
    gx: int; gy: int             # fyzické pixely virtuální plochy
    button: MouseButton | None = None
    axis: Literal["v", "h"] | None = None
    delta: int = 0               # kolečko: 120 = jeden zářez, kladné = nahoru / doprava
    modifiers: frozenset[str] = frozenset()   # {"ctrl","shift","alt","meta"}
    injected: bool = False       # událost vytvořená programem (SendInput, vzdálená plocha)
    window: WindowRef | None = None           # okno pod bodem (u down/up/wheel)

@dataclass(frozen=True)
class MouseCapabilities:
    buttons: tuple[MouseButton, ...]          # Windows: left..x2
    horizontal_wheel: bool
    high_resolution_wheel: bool
    touchpad_gestures: bool                   # pinch a posun dvěma prsty přímo z touchpadu
    elements: bool
    element_content: bool                     # hodnota, text v bodě, označený text

@dataclass(frozen=True)
class MouseSystemParams:          # uloží se do manifestu, klasifikace z nich počítá
    double_click_ms: int; double_click_px: tuple[int, int]; drag_px: tuple[int, int]

class MouseSource(ABC):
    def start(self, sink: Callable[[RawMouseEvent], None], clock: RecordingClock) -> None: ...
    def stop(self) -> None: ...
    def healthy(self) -> bool: ...            # hook stále dostává události (watchdog)
    # Implementace může běžet v jiném procesu (Windows); t_ms musí být na hodinách nahrávky.
    capabilities: MouseCapabilities
    params: MouseSystemParams

class ElementInspector(ABC):
    def element_at(self, gx: int, gy: int, want: ContentRequest, timeout_ms: int) -> ElementInfo | None: ...
    def window_info(self, ref: WindowRef) -> WindowInfo | None: ...   # titulek, proces

class TouchpadSource(ABC):                    # M5, jen dva prsty
    def start(self, sink: Callable[[ContactFrame], None], clock: RecordingClock) -> None: ...
    def stop(self) -> None: ...

class Platform(ABC):                          # nové metody, výchozí implementace None
    def mouse_source(self) -> MouseSource | None: return None
    def element_inspector(self) -> ElementInspector | None: return None
    def touchpad_source(self) -> TouchpadSource | None: return None
```

`ElementInfo` = `role` (normalizovaná: `button`, `edit`, `link`, `menu_item`, `tab`, `list_item`, `tree_item`, `checkbox`, `combobox`, `cell`, `document`, `window`, `other` + původní název role), `name` (nejvýš 200 znaků), `automation_id`, `class_name`, `framework`, `bounds` (v pixelech obrazovky), `is_password`, `ancestors` (nejvýš 3 pojmenovaní předci, jen role + název), `process`, `window_title` (nejvýš 120 znaků) a `content` (kap. 6.1). Normalizované role umožní, aby timeline vypadala stejně na všech OS.

`ContentRequest` říká, co inspektor přečte: `value_and_text` (při stisku, zastavení kurzoru) nebo `selection` (po tažení a vícenásobném kliku). Konstanta `MAX_ELEMENT_TEXT_CHARS = 4096` a oříznutí se třemi tečkami jsou v `core/`, takže platí stejně na všech OS; zobecní se k tomu dnešní `truncate_clipboard` (`ui/clipboard.py`) do `core/`.

`linux.py` a `macos.py` v tomto plánu vrátí `None`; checkbox Mouse je tam zašedlý s tooltipem „Windows only for now“ (stejně jako Computer Audio).

## 4. Data

### 4.1 `input/mouse.jsonl`

Nová složka `input/` vedle `audio/`, `frames/`, `frames_info/`. Jeden JSON na řádek, zapisuje se průběžně (flush nejpozději po 1 s), takže pád okna ztratí nejvýš poslední sekundu; čtení toleruje useknutý poslední řádek. Archivace ji **ponechává** (je malá, stejně jako `frames_info/`).

```json
{"seq":41,"t":12345,"type":"down","button":"left","screen":"screen1","x":812,"y":440,"mods":[],"target":"app","win":17}
{"seq":42,"t":12346,"type":"element","ref":41,"role":"edit","role_raw":"Edit","name":"Search","automation_id":"searchBox","bounds":[780,428,300,26],"ancestors":[{"role":"window","name":"Settings"}],"content":{"kind":"value","hash":"9f2c…","chars":37},"ms":18}
{"seq":43,"t":12346,"type":"content","hash":"9f2c…","text":"video-story mouse capture implementation"}
{"seq":44,"t":12401,"type":"up","button":"left","screen":"screen1","x":812,"y":440,"mods":[],"target":"app"}
{"seq":50,"t":14010,"type":"move","screen":"screen1","x":120,"y":205}
{"seq":61,"t":16000,"type":"wheel","axis":"v","delta":-360,"count":3,"until":16120,"screen":"screen2","x":900,"y":500,"mods":[]}
{"seq":70,"t":18000,"type":"hover","screen":"screen1","x":640,"y":400}
{"seq":90,"t":20000,"type":"gesture","kind":"pinch","fingers":2,"until":20800,"scale":1.8,"screen":"screen1","x":640,"y":400}
{"seq":99,"t":30000,"type":"status","source":"mouse","state":"restarted","reason":"no events for 5 s while the pointer moved"}
```

- Řádky s bodem nesou kromě `screen`/`x`/`y` (pixely snímku té obrazovky, jako kurzor v sidecarech) i `gx`/`gy` (fyzické pixely virtuální plochy). Vzdálenosti pro dvojklik a tažení se měří v nich, takže fungují i přes hranici obrazovek.
- `move` jen když je stisknuté tlačítko (dráha tažení), nejvýš 1× za 16 ms a po posunu aspoň o 2 px; po každých 1000 bodech jednoho tažení se interval zdvojnásobí (`MoveThrottle`). Pohyb bez stisku se neukládá – poloha kurzoru už je v sidecarech.
- `wheel`: události stejné osy a modifikátorů do 40 ms od sebe se sloučí do jednoho řádku (`delta` je součet, `count` počet, `until` poslední). Touchpad posílá 60–100 událostí za sekundu, takže bez slučování by soubor zbytečně rostl.
- `target`: `app` (cizí okno), `video_story` (vlastní okno, pozná se podle PID okna pod bodem) nebo `drawing` (kreslicí vrstva; HWND vrstev předá `DrawingOverlay`). Timeline vlastní okna i kreslení vynechá, protože ty pokrývají KEYPOINT, DRAW a ZOOM. U `video_story` a `drawing` se na prvek neptáme.
- `win` je pořadové číslo okna v rámci nahrávky. Titulek a proces okna se zapíšou jednou do řádku `{"type":"window",...}`, ne ke každé události. Když okno změní titulek, dostane nové číslo, takže každá akce má titulek z té chvíle.
- `status` má stavy `started`, `restarted` (pomocný proces skončil, nebo se kurzor hýbal a 5 s nepřišla žádná událost), `gave_up` (po 3 restartech už se nerestartuje, běží dál ten poslední) a `stopped`.
- Pomocný proces se spouští ve vlákně na pozadí, aby pomalý start Pythonu (antivir, pomalý disk) nezdržel snímky a zvuk; události z prvních desetin sekundy se tak mohou ztratit.
- `element` přichází asynchronně a na svou událost odkazuje přes `ref`. `ms` je, jak dlouho dotaz trval (pro ladění).
- **Obsah se ukládá jednou.** Řádek `content` nese text (nejvýš 4096 znaků) a jeho hash (SHA-1 z textu). Další prvek se stejným obsahem (opakované kliky do téhož pole nebo dokumentu) už má jen odkaz `hash`. Soubor tak neroste a timeline může napsat „stejný text jako v 12345“.

Odhad velikosti: běžná hodina práce zabere několik tisíc řádků a stovky unikátních obsahů, tedy nejvýš desítky MB, proti ~3 GB snímků.

### 4.2 Manifest

```json
"mouse": {
  "file": "input/mouse.jsonl",
  "platform": "windows-ll-hook",
  "capabilities": {"buttons": ["left","middle","right","x1","x2"], "horizontal_wheel": true, "high_resolution_wheel": true, "touchpad_gestures": true, "elements": true, "element_content": true},
  "params": {"double_click_ms": 500, "double_click_px": [4, 4], "drag_px": [4, 4]},
  "max_content_chars": 4096,
  "events": 3812, "elements": 214, "contents": 96, "element_timeouts": 3, "hook_restarts": 0
}
```

`null`, když je checkbox vypnutý nebo platforma nic nenabízí; s polem `error`, když se hook nepodařilo nainstalovat. Obnova po pádu (`host/recovery.py`) sekci doplní ze souboru, stejně jako u `computer_audio`.

### 4.3 Nastavení

| Položka | Klíč | Výchozí |
| --- | --- | --- |
| Mouse (Record mouse actions and the elements under the mouse) | `mouse_actions` | zapnuto (Windows; jinde zašedlé) |

Jeden checkbox za Computer Audio. Tooltip řekne, co všechno se zaznamená: kliky, tažení, kolečko, gesta dvěma prsty, titulky oken, prvky pod myší a jejich obsah (nejvýš 4096 znaků, nikdy pole pro heslo). Samostatné přepínače pro titulky oken ani obsah prvků nejsou.

## 5. Odvozené akce (`core/mouse_actions.py`)

| Akce | Pravidlo |
| --- | --- |
| `click` | `down` a `up` téhož tlačítka, pohyb mezi nimi do `drag_px` |
| `double_click`, `multi_click` (n) | další stisk téhož tlačítka do `double_click_ms` od předchozího **stisku** a v obdélníku `double_click_px` se středem v něm (tak to rozhoduje Windows); stisk jiného tlačítka mezi tím řadu přeruší, podržený klik ji nezačne |
| `hold` | klik držený déle než 800 ms (v timeline jako `CLICK … (held 1.4 s)`) |
| `drag` | `down` → pohyb za `drag_px` → `up`; dráha zjednodušená Douglasem–Peuckerem (tolerance 3 px), v timeline jen start, konec, délka, prvek na startu a v cíli a označený text |
| `scroll` | souvislá řada `wheel` jedné osy s mezerami do 400 ms; počet zářezů (`Σdelta/120`, na desetiny), směr; s Ctrl = `zoom` |
| `gesture` | dva prsty z řádků `gesture` (M5); posun a pinch, které se kryjí se `scroll`/`zoom` z kolečka, se spojí do jednoho řádku s poznámkou `(touchpad)` |

Klik tlačítkem, které vyvolalo tažení jiného tlačítka (např. pravé během levého tažení), se zaznamená zvlášť. Události s `injected` se v timeline označí `(injected)`.

## 6. Prvky pod myší (UI Automation, Windows)

- **Kdy se ptáme:** při každém `down` (prvek + `value_and_text`), při `up` tažení a dvoj/vícenásobného kliku (cíl a `selection`), na začátku scrollu (jen prvek, bez obsahu: odstavec pod kolečkem nic neřekne) a při **zastavení kurzoru** (`hover`): kurzor stojí aspoň 600 ms nad cizím oknem, od posledního dotazu se posunul o víc než `drag_px` a od posledního `hover` uplynulo aspoň 500 ms. Zastavení se zjišťuje z pohybů, které pomocný proces hooku posílá i bez stisku (nejvýš 20× za s).
- **Proč při `down`:** akce tlačítka proběhne obvykle až při `up`, takže při stisku je dialog ještě otevřený a prvek ještě existuje.
- **Pomocný proces:** UI Automation běží v `python -m video_story.platform.windows_uia_helper` (vlastní MTA, JSON po řádcích přes rouru, Job Object, nejvýš 3 restarty), ne ve vlákně okna. COM tak nekoliduje s apartmentem Qt, pád nebo zaseknutí cizího poskytovatele přístupnosti neshodí Video Story a dotazy nečekají na GIL kódování snímků. V okně běží jen fronta (nejvýš 8 čekajících, při zahlcení se zahazují nejdřív `hover`) a čekání na odpověď (1,5 s; pozdní odpověď se zahodí, pomocný proces přeskočí žádost, na kterou už nikdo nečeká). Jeden `CacheRequest` načte ControlType, Name, AutomationId, ClassName, FrameworkId, BoundingRectangle, IsPassword, ProcessId a dostupnost vzorů Value a Text; předci přes `ControlViewWalker` (nejvýš 6 kroků, 3 pojmenovaní). `ConnectionTimeout` a `TransactionTimeout` jsou 400 ms. Titulek a proces okna se čtou v okně přes `InternalGetWindowText`, která cizímu oknu neposílá zprávu, takže nezamrzne na aplikaci, která neodpovídá.
- **Známá daň:** Chrome, Edge a Electron (VS Code) zapnou svůj strom přístupnosti až na první dotaz UIA; první dotaz trvá déle a prohlížeč může být nepatrně pomalejší. Podle M0 je to snesitelné: `ElementFromPoint` s cache trvá 2–25 ms, první dotaz na okno Firefoxu 95 ms.
- **Závislost:** `comtypes` (čistý Python, `sys_platform == 'win32'`). Wrapper `UIAutomationCore` se vygeneruje při prvním spuštění (na tomto počítači 0,9 s); když `site-packages` není zapisovatelné, `comtypes` použije vlastní složku v profilu uživatele. `doctor` ověří, že se pomocný proces spustí. Když UI Automation nejde, zapíše se `status` `elements_unavailable` a nahrávají se jen události.

### 6.1 Obsah prvku (nejvýš 4096 znaků)

**Pomůže to?** Ano, hlavně ve třech případech:

- **přesný text místo čtení ze snímku:** adresa v adresním řádku, hledaný výraz, hodnota buňky, název souboru, chybová hláška; agent ho nemusí přepisovat ze screenshotu (OCR modelu dělá chyby v diakritice, číslech a dlouhých cestách);
- **„tady to nefunguje“ v dlouhém textu:** odstavec, do kterého uživatel klikl, řekne agentovi, o které místo jde, i když je na snímku malým písmem;
- **označený text:** když uživatel tažením nebo dvojklikem něco označí a řekne „tohle přepiš“, agent dostane přesně to, co je označené.

Limit 4096 znaků stačí na adresu, pole formuláře i jeden odstavec a zároveň zastaví celé dokumenty. Text delší než limit se ořízne a skončí `...` (stejně jako schránka).

| `kind` | Kdy | Windows (UIA) |
| --- | --- | --- |
| `value` | při stisku a zastavení kurzoru, když prvek má Value | `ValuePattern.CurrentValue` (pole, combobox, adresní řádek, buňka tabulky). Hodnota delší než 1000 znaků je celý dokument (např. Poznámkový blok): pak se místo ní vezme odstavec v bodě (`text`). |
| `text` | při stisku a zastavení kurzoru, když prvek nemá Value, ale má Text | `TextPattern.RangeFromPoint(bod)` → `ExpandToEnclosingUnit(Paragraph)` → `GetText(4096)`: odstavec v místě myši, ne celý dokument |
| `selection` | po `up` tažení a dvoj/vícenásobného kliku | `TextPattern.GetSelection()` → `GetText(4096)`; prázdný výběr se neukládá |

- **Pole pro heslo nikdy:** `IsPassword` → obsah se nečte vůbec a název se nahradí `(password field)`. Stejně se zachází s prvky, jejichž role je na macOS `AXSecureTextField` a na Linuxu `password text`.
- Obsah se čte jen u cizích oken (`target: app`).
- Vše ostatní z obsahu (Value, Text) se čte jen tímto způsobem; žádné procházení celého stromu ani dokumentu.
- **Výsledky M0:** hodnota pole i odstavec v bodě fungují ve VS Code, Chrome, Teams, Signalu, Poznámkovém bloku a Thunderbirdu, typicky do 25 ms. Firefox vrací pro `RangeFromPoint` chybu `E_INVALIDARG`; tam se použije `value` a místo odstavce `TextPattern.GetVisibleRanges()` (viditelná část, oříznutá na 4096; ve Firefoxu to ještě ověří M3). Sublime Text přes UIA nevystavuje nic, takže se uloží jen okno a proces. Ve Wordu je první nalezené pole Edit na pásu karet, proto se hledá jen prvek přímo pod bodem, nikdy první pole v okně.
- **Zjištění oříznutí:** `GetText(4096)` vrátí přesně 4096 znaků a neřekne, jestli je text delší. Inspektor proto žádá `GetText(4097)` a u `ValuePattern` porovná délku; když je text delší než 4096, ořízne ho na 4096 a přidá `...`.
- Když čtení obsahu v některé aplikaci selže, uloží se jen `value` nebo nic.

Spec v kap. 2 (pravidlo 4) a kap. 6 doplní: při zapnutém Mouse se zaznamenávají titulky oken, prvky pod myší a jejich obsah (do 4096 znaků, nikdy hesla) a to, co si agent přečte, jde k modelu stejně jako snímky a schránka.

## 7. Gesta touchpadu dvěma prsty (M5)

Gesta třemi a čtyřmi prsty se nezaznamenávají.

- **Proč M5 není volitelný:** M0 ukázal, že hook vidí posun a pinch jen nad aplikacemi bez DirectManipulation. Nad VS Code, Chrome, Edge a Poznámkovým blokem Windows 11, tedy nad většinou toho, co uživatel ukazuje, hook nedostane nic. Bez M5 by se gesta touchpadu ve většině nahrávek neobjevila.
- **Co dá M2 bez práce navíc:** posun a pinch v aplikacích bez DirectManipulation jako SCROLL a ZOOM-SCROLL (z kolečka). M5 je pak sloučí s gestem do jednoho řádku s `(touchpad)`.
- **Zdroj:** Raw Input Precision Touchpadu (`RegisterRawInputDevices`, `UsagePage 0x0D`, `Usage 0x05`, `RIDEV_INPUTSINK`) ve vlákně se skrytým oknem `HWND_MESSAGE`. Report se rozloží přes `hid.dll` (`HidP_GetCaps`, `HidP_GetValueCaps`, `HidP_GetUsageValue`, `HidP_GetUsages`): Contact Count `0x54`, Contact ID `0x51`, Tip Switch `0x42`, X `0x30`, Y `0x31`. Systémová gesta to neruší (bez `RIDEV_NOLEGACY`). Reporty přicházejí i na pozadí, když je vpředu Chrome (ověřeno v M0).
- **Hybridní hlášení:** touchpad posílá prsty jednoho snímku v několika reportech. Contact Count je jen v prvním, ostatní mají 0. Snímek se proto skládá podle Contact ID, dokud nepřijde tolik kontaktů, kolik říkal první report (v M0 tak vzniklo 98 % snímků se dvěma prsty, bez toho žádný).
- **Kde běží:** ve stejném pomocném procesu jako hook (kap. 3), ve vlastním vlákně. V hlavním procesu by události pod zátěží dorazily o 30–380 ms později a časy gest by neseděly se snímky. Zpracování reportu trvá zlomek milisekundy a pak vlákno čeká v `GetMessageW` bez GIL. Pokud integrační test M5 ukáže, že callback hooku tím vzroste nad p95 1 ms, dostane touchpad vlastní pomocný proces.
- **Do souboru jdou jen hotová gesta, ne kontakty** (touchpad posílá ~125 reportů/s). Kontakty jednoho doteku se drží v paměti a po zvednutí prstů je `core/gestures.py` klasifikuje. Jen doteky se dvěma prsty; jeden prst se ignoruje, tři a více prstů jen krátce (palec, dlaň) nevadí.
- **Pravidlo klasifikace** (z dat M0, vzdálenosti v jednotkách touchpadu přepočtené na podíl šířky touchpadu z `LogicalMax` osy X):
  - `rozptyl` = největší − nejmenší vzdálenost prstů během doteku, `dráha` = celková dráha středu mezi prsty (ne čistý posun, protože posun nahoru a zpět má čistý posun skoro nulový);
  - **pinch**, když `dráha ≤ 2 × rozptyl`; `in`/`out` a `scale` podle poměru konečné a počáteční vzdálenosti (při změně směru uprostřed doteku podle největší změny);
  - **scroll**, když `dráha ≥ 3 × rozptyl`; směr podle převažující osy, při obratu víc řádků;
  - jinak, nebo když je rozptyl i dráha pod 5 % šířky touchpadu, se dotek zahodí.
  - Samotný poměr vzdáleností nestačí: jeden posun v M0 měl poměr 1,42 (prsty se při posunu rozjely), ale dráha středu byla 4× větší než rozptyl. V datech M0 měly posuny `dráha/rozptyl` ≥ 4,3 a pinche ≤ 0,70, takže pravidlo rozdělilo správně všech 6 posunů i všech 16 pinchů a jeden krátký dotek (94 ms) zahodilo.
  - **Ruční zkouška po implementaci (10. 11. 2026, Chrome):** 12 posunů nad stránkou se zaznamenalo správně. Pinch zpočátku ne: dotek se zahazoval celý, jakmile měl jediný snímek se třemi kontakty (palec nebo dlaň se při pinchi o touchpad otře). Teď se zahodí, až když má tři a více kontaktů víc než 3 snímky a víc než pětinu doteku. Dva pinche se ztratily i proto, že se prsty při nich posunuly (`dráha/rozptyl` 1,03 a 1,67), proto je hranice pinche 2 a posunu 3. Po úpravě se z 9 doteků stalo 6 pinchů. Zbylé 3 doteky z této zkoušky byly krátký dotyk a dva pinche, které by nová hranice zachytila.
- Poloha gesta na obrazovce je poloha kurzoru na začátku doteku (souřadnice touchpadu s obrazovkou nesouvisí).
- Když počítač Precision Touchpad nemá, `touchpad_source()` vrátí `None` a v manifestu je `touchpad_gestures: false`.

## 8. Přenositelnost na macOS a Linux

Smlouva z kap. 3.1 je jediné, co zbytek kódu vidí. Nová platforma implementuje `MouseSource`, `ElementInspector` a volitelně `TouchpadSource`, vrátí je z `Platform` a projde sdílenými testy smlouvy (kap. 9). Nic v `capture/`, `core/` ani `host/` se měnit nemá. Limit 4096 znaků, oříznutí, hash obsahu a vyřazení hesel jsou v `core/`, takže se na nových platformách nepíšou znovu.

| | macOS (13+) | Linux X11 |
| --- | --- | --- |
| Tlačítka a pohyb | `CGEventTapCreate(kCGSessionEventTap, …, kCGEventTapOptionListenOnly)` na vlastním `CFRunLoop`; `kCGMouseEventButtonNumber` (0–31 → až `b11`) | XInput2 `XI_RawButtonPress/Release`, `XI_RawMotion` na root okně (`python-xlib`, `Xlib.ext.xinput`); tlačítka 1–3, 8–9 = X1/X2, 10–13 = `b6`–`b9`… |
| Kolečko | `kCGScrollWheelEventDeltaAxis1/2`, `…IsContinuous`, fáze (trackpad) | tlačítka 4/5 (svisle) a 6/7 (vodorovně), nebo XI2 valuátory pro jemný posun |
| Souřadnice | body × backing scale (jako dnešní `cursor_position` v `macos.py`) | fyzické pixely přímo |
| Systémové parametry | `NSEvent.doubleClickInterval`; práh tažení pevně 4 px | `XSettings` / GTK `gtk-double-click-time`, výchozí 400 ms |
| Prvek pod myší | `AXUIElementCopyElementAtPosition` (systémový prvek), atributy `AXRole`, `AXTitle`/`AXDescription`, `AXFrame`; `AXUIElementSetMessagingTimeout` | AT-SPI2 přes D-Bus: `Component.GetAccessibleAtPoint` z aktivního okna; aplikace musí mít zapnutou přístupnost (GTK, Qt ano) |
| Obsah `value` | `AXValue` (jen když je to řetězec) | rozhraní `Text` u editovatelných polí (`GetText(0, 4096)`), číselné `Value` jako text |
| Obsah `text` (odstavec v bodě) | `AXRangeForPosition` + `AXStyleRangeForIndex`/`AXLineForIndex` → `AXStringForRange` | `Text.GetOffsetAtPoint` + `GetTextAtOffset(…, SENTENCE/PARAGRAPH)` |
| Obsah `selection` | `AXSelectedText` | `Text.GetSelection` + `GetText` |
| Hesla | role `AXSecureTextField` | role `password text`, stav `PROTECTED` |
| Gesta dvěma prsty | `NSEventTypeMagnify` (pinch) a `scrollWheel` s fázemi a `hasPreciseScrollingDeltas` (posun) přes globální monitor `NSEvent` | XI 2.4 `XI_GesturePinch*` (X.Org 21.1+, libinput), posun dvěma prsty jako jemné kolečko |
| Oprávnění | Input Monitoring a Accessibility (TCC); bez nich `MouseSource.start` vyhodí `PlatformError` s návodem a nahrávání pokračuje bez myši | žádná (X11); Wayland nepodporujeme |
| Závislosti | `pyobjc-framework-Quartz`, `pyobjc-framework-ApplicationServices` (jen darwin) nebo ctypes | `python-xlib` (už je), `dbus-next` nebo `pyatspi` |

Obsah prvku tedy půjde na všech třech systémech: Windows (UIA), macOS (Accessibility API) i Linux (AT-SPI2) mají hodnotu pole, text v bodě i označený text. Na Linuxu záleží na tom, jestli aplikace přístupnost podporuje; GTK a Qt ano, Electron jen se zapnutou přístupností.

Tím, že se v souboru drží surové události a klasifikace je v `core/`, budou dvojklik, tažení i formát timeline na všech OS stejné; liší se jen `params` a `capabilities` v manifestu.

## 9. Testy

- **Unit (všechny OS, bez hardwaru):** `mouse_actions` (klik vs. tažení na prahu, dvojklik na hraně času a vzdálenosti, trojklik, hold, X1/X2, souběh dvou tlačítek, scroll bursty a Ctrl+zoom, vodorovný scroll, jemné delta), `gestures` (syntetické kontakty dvou prstů: pinch in/out, posun, šum; jeden a tři prsty se ignorují), obsah (4096 znaků projde, 4097 se ořízne na 4096 + `...`, hash a jediný zápis opakovaného obsahu, heslo se nečte), čtení JSONL s useknutým řádkem, filtrace `video_story`/`drawing`, rozsah cropu, formátování a `counts` v timeline, manifest a config tam a zpět, obnova po pádu, Settings zašedlé mimo Windows.
- **Testy smlouvy:** `tests/unit/test_mouse_contract.py` s `FakeMouseSource` a `FakeElementInspector` projede celou cestu platforma → `InputRecorder` → JSONL → timeline. Každá implementace platformy pak spouští stejné scénáře jako integrační test (níže).
- **Integrace Windows (`@pytest.mark.integration`):** `SendInput` vygeneruje klik, dvojklik, tažení, kolečko svisle i vodorovně, X1 a X2 na obou monitorech s různým DPI; ověří se souřadnice, `injected: true` a odvozené akce. UIA proti vlastnímu Qt testovacímu oknu s tlačítkem `Save`, polem s textem a polem pro heslo (role, název, bounds, `value`, `selection` po dvojkliku, heslo bez obsahu). Zátěž: 10 s kódování snímků naplno, latence callbacku pod 5 ms, `hook_restarts == 0`.
- **Ruční (`tests/manual/acceptance.md`):** skutečná myš, touchpad (posun, pinch), Průzkumník, Chrome (adresní řádek, odstavec stránky), VS Code, Word (odstavec, označený text), Excel (buňka), dialog UAC (prvek nedostupný, nahrávání běží), kreslicí vrstva (kliky se v timeline neopakují), přihlašovací pole s heslem.

## 10. Milníky

| # | Obsah | Výstup |
| --- | --- | --- |
| **M0** | Spike (1–2 dny): LL hook v Pythonu pod zátěží, souřadnice na 150 % DPI, jestli touchpad posílá wheel a Ctrl+wheel, čtení PTP Raw Input na pozadí, latence UIA a `RangeFromPoint`/`GetSelection` v Chrome, VS Code, Průzkumníku a Office, potvrzení, že tlačítka 6+ nepřicházejí | krátká zpráva v tomto souboru (kap. 12), rozhodnutí go/no-go pro M5 |
| **M1** | `core/input_events.py`, `core/mouse_actions.py`, obsah (limit, oříznutí, hash), rozhraní v `platform/base.py`, config (`mouse_actions`), manifest (`mouse`), `session.py` (`input/`), fakes a unit testy | čistá logika se zelenými testy na všech OS |
| **M2** | `platform/windows_mouse_hook.py` (pomocný proces: hook a `GetMessageW`, řádky do stdout, konec při zavřeném stdin nebo rouře), `platform/windows_input.py` (spuštění s `ABOVE_NORMAL`, Job Object, čtení roury, převod `perf_counter_ns` na čas nahrávky, watchdog: proces skončil nebo se kurzor hýbe a 5 s nepřišla žádná událost → spustit znovu a zapsat `status`), `capture/input_recorder.py`, napojení do `Recorder.start/stop`, PID a HWND vrstev z `control_window.py`/`overlay.py`, checkbox v Settings, `recovery.py`, `doctor` | `input/mouse.jsonl` v každé nahrávce na Windows |
| **M3** | `platform/windows_uia.py`, fronta dotazů, zastavení kurzoru (`hover`), okna a procesy, obsah `value`/`text`/`selection`, `comtypes` v `pyproject.toml` | řádky `element`, `content` a `window` |
| **M4** | `host/events.py`: řádky `CLICK`, `DOUBLE-CLICK`, `DRAG`, `SCROLL`, `ZOOM-SCROLL`, `GESTURE` a prvek v řádcích `SAY`; volba `timeline --mouse full|summary|off`; nový příkaz `mouse --session <id> [--from --to] [--at <ms>]` (detail: dráhy, předci, bounds, celý obsah do 4096 znaků) a nástroj MCP `video_story_mouse`; LEGEND, `skills/video-story/SKILL.md`, instrukce v `mcp_server.py` | agent vidí akce myši v timeline |
| **M5** | `platform/windows_touchpad.py` (vlákno v pomocném procesu, skládání hybridních snímků), `core/gestures.py` pro dva prsty, spojení s kolečkem, unit testy klasifikace na datech z M0 | řádky `gesture` i ve VS Code, Edge a Chrome |
| **M6** | Spec (kap. 2, 5, 6, 9, 11, 12), `acceptance.md`, README | dokumentace odpovídá kódu |

Navazující práce mimo tento plán: značky kliků na časové ose editoru a „vlnka“ kliku v náhledu a v Render (MP4), implementace pro macOS a Linux podle kap. 8.

## 11. Jak to bude vypadat v timeline

```text
12345 CLICK left screen1 812,440 on edit "Search" in window "Settings" [notepad.exe] value "video-story mouse capture implementation" (frames before 12000, after 12500)
13010 DOUBLE-CLICK left screen1 300,220 on list_item "report.pdf" [explorer.exe "Downloads"]
13900 CLICK left screen1 640,380 on document "Návrh.docx" [WINWORD.EXE] text "Nahrávání se nikdy nepozastavuje. Časová osa je souvislá od stisku…" (1840 chars, `mouse --at 13900`)
14000-15200 DRAG left screen1 100,200 -> 600,220 (1200 ms) on document "Návrh.docx" selected "souvislá od stisku Record do konce"
16000-17500 SCROLL down 6 notches screen2 900,500 over document "Pull requests" [msedge.exe]
18000-18600 ZOOM-SCROLL in 3 notches (Ctrl+wheel) screen1 640,400 [Code.exe]
19000 CLICK x1 (back) screen1 500,300 [msedge.exe]
19500 CLICK left screen1 812,440 on edit "Search" value same as 12345
20000-20800 GESTURE pinch out 2 fingers x1.8 screen1 640,400 (touchpad)
22000-24500 SAY Tohle tlačítko nefunguje. [cursor screen1 812,440 over button "Save" (notepad.exe)]
```

Pravidla proti zahlcení kontextu:

- Jeden řádek na akci, název prvku nejvýš 60 znaků a jen jeden pojmenovaný předek (ne okno: to říká část `in "titulek" [proces]`), titulek okna jen při změně okna oproti předchozí akci. Řádky myši mají i `frames before/after` (snímek před akcí a po ní).
- **Obsah v timeline nejvýš 200 znaků** (stejně jako `CLIPBOARD`), za ním počet znaků a odkaz na `mouse --at <ms>`, kde je celý (do 4096). Opakovaný obsah se nevypisuje znovu, jen `same as <ms>`. Označený text (`selected`) se vypisuje celý až do 1000 znaků, protože ho uživatel vybral záměrně.
- Zastavení kurzoru (`hover`) nemá vlastní řádky; jen doplní `over …` do `SAY`, bez obsahu.
- `--mouse summary` (výchozí `auto` ho zapne, když je v rozsahu přes 400 akcí myši): kliky, dvojkliky a tažení zůstávají, scrolly a gesta se sloučí po 10 s. Výsledek to oznámí v `mouse_note`.
- Vše ostatní (dráhy tažení, bounds, předci, `automation_id`, celý obsah) je jen v příkazu `mouse`.

Odhad: řádek s prvkem má 25–40 tokenů, s obsahem do ~100 tokenů. 100 akcí tedy vyjde zhruba na 4–8 tisíc tokenů, řádově jako dva snímky obrazovky, a agent jich díky tomu většinu nemusí otevírat.

## 12. Rozhodnutí a výsledky M0

1. **Výchozí stav:** záznam myši je ve výchozím stavu zapnutý. V Settings je jeden checkbox `mouse_actions`; když je zaškrtnutý, zaznamenávají se akce myši i prvky pod myší (rozhodnuto 10. 11. 2026).
2. **Titulky oken a obsah:** samostatný přepínač není. Zaškrtnutý checkbox znamená, že se zaznamenávají i titulky oken, prvky pod myší a jejich obsah do 4096 znaků (nikdy hesla); spec uvede, že to jde k modelu (rozhodnuto 10. 11. 2026).
3. **Gesta:** jen dva prsty (posun, pinch); tři a čtyři prsty se nezaznamenávají (rozhodnuto 10. 11. 2026).
4. **Výsledky M0** (10. 11. 2026, notebook uživatele, Windows 11 23H2, dvě obrazovky 150 % a 100 % DPI, ELAN Precision Touchpad, dva přijímače Logitech):

| Oblast | Výsledek | Důsledek |
| --- | --- | --- |
| LL hook v hlavním procesu pod zátěží (2× JPEG + Python, 4× Python) | callback p95 109 ms, max 170 ms; myš v celém systému zpožděná | hook nikdy v hlavním procesu |
| LL hook v pomocném procesu (`ABOVE_NORMAL`) pod stejnou zátěží | callback p95 < 0,1 ms, doručení p50 0,5 ms, p95 2 ms | **hook v pomocném procesu** (kap. 3) |
| Raw Input v hlavním procesu pod zátěží | systém nebrzdí, ale dorazí o p95 220 ms, max 380 ms později | i touchpad v pomocném procesu (kap. 7) |
| Souřadnice | přesné na obrazovce 150 % i 100 % (PMv2) | `locate_cursor` beze změny |
| Tlačítka | L, M, R, X1, X2, kolečko svisle a vodorovně, jemné delta; `injected` a vlastní PID poznáme | podle plánu |
| Myš Logitech v ručním testu | přišla jen L, P, M a kolečko; žádné X1/X2, náklon kolečka ani další tlačítka, ani jako klávesy. Oba přijímače hlásí v popisu zařízení 16 tlačítek. | tlačítka 6+ na Windows nepřicházejí, jak plán předpokládal. Myš uživatele boční tlačítka nemá (potvrzeno 10. 11. 2026), X1/X2 ověřil `SendInput`. |
| Systémové parametry | dvojklik 480 ms, oblast dvojkliku 4×4 px, práh tažení 4×4 px | do manifestu |
| Posun a pinch na touchpadu přes hook | nad VS Code, Chrome a Poznámkovým blokem nic; nad oknem Qt 684 událostí kolečka a 165× Ctrl+kolečko | M5 povinný (kap. 7) |
| Raw Input touchpadu | reporty chodí i na pozadí; po složení hybridních snímků 16/16 pinchů a 6/6 posunů rozpoznáno správně (i nad Chrome) | **M5: go** |
| UI Automation | `ElementFromPoint` s cache 2–25 ms; první dotaz na Firefox 95 ms | podle plánu, limity 400 ms stačí |
| Obsah prvků | VS Code, Chrome, Teams, Signal, Poznámkový blok, Thunderbird: hodnota i odstavec v bodě. Firefox: `RangeFromPoint` → `E_INVALIDARG`. Sublime Text: nic. `GetText(4096)` nepozná delší text. | náhrady v kap. 6.1, žádá se 4097 znaků |
