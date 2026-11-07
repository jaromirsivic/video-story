# Video Story

## Účel a rozsah

Video Story je plugin se stejnojmenným skillem pro ChatGPT, Claude a Codex. Uživatel při práci na počítači popisuje, co dělá nebo co chce zadat. Plugin zaznamená zvuk, snímky všech obrazovek a polohu kurzoru. Po zpracování záznamu předá časovaný přepis a obrazové podklady agentovi, který pokračuje v práci podle pokynů uživatele.

Cílové operační systémy jsou Windows, Linux a macOS. Sdílené funkce mají mít společnou implementaci oddělenou od integrací s hostitelskými aplikacemi a od částí závislých na operačním systému. Volba jazyka a členění sdíleného kódu jsou součástí návrhu implementace. Celý plugin včetně skillu, uživatelského rozhraní a hlášek je v angličtině. Přepis zachovává jazyk zaznamenané řeči.

## Spuštění a ovládání

Po aktivaci skillu se na pokyn „Video Story“ nebo na požadavek ke spuštění Video Story zobrazí malé ovládací okno, které zůstává nad ostatními okny. Anglický příklad pokynu je „Start Video Story“. Otevření okna připraví ovládání a záznam začne až po stisku **Record**.

| Ovládací prvek | Chování |
| --- | --- |
| **Record** | Tlačítko s červeným kruhem spustí nahrávání. |
| **Stop** | Během nahrávání otevře dialog s volbou dalšího postupu. |
| **Settings** | Tlačítko s ozubeným kolem otevře nastavení. |
| **Draw on Screen** | Umožní kreslit na obrazovce. Je aktivní pouze během nahrávání. |

## Nastavení

Položky se zobrazují v tomto pořadí:

1. **Recording Folder** určuje adresář pro uložení zvuku, snímků a souvisejících souborů nahrávky.
2. **Capture Interval** určuje interval snímání v rozsahu 1 až 1 000 ms včetně. Výchozí hodnota je 250 ms.
3. **Image Format** nabízí PNG nebo JPEG. Výchozí formát je PNG. Při volbě JPEG se zobrazí nastavení **JPEG Quality** s dolní hranicí 20 %. Horní hranice zůstává k rozhodnutí mezi 90 % a 100 %.
4. **Audio Input** umožní vybrat zvukové zařízení. Výchozí volbou je systémový výchozí zvukový vstup.

Plugin ukládá konfiguraci do `config.json` v instalaci pluginu. Typickým scénářem je globální instalace. Přesná výchozí cesta pro data se určí podle způsobu instalace v jednotlivých hostitelských prostředích.

## Záznam zvuku, obrazovek a kurzoru

Po stisku **Record** se spustí nahrávání zvuku z vybraného vstupu a v nastavených intervalech se snímají všechny připojené obrazovky. Během nahrávání vznikají pracovní soubory v dočasné složce pluginu oddělené pro každou nahrávku. Čas snímků se odvozuje od začátku nahrávání, aby šly snímky později přiřadit k odpovídajícím částem zvuku a přepisu.

Název snímku obsahuje sekundy od začátku nahrávky, tři číslice milisekund a identifikátor obrazovky ve tvaru `screen1`, `screen2` a dále. Jako oddělovač slouží podtržítko. Přípona odpovídá zvolenému obrazovému formátu. Každý snímek má samostatný doprovodný JSON se stejným základem názvu.

| Snímek | Doprovodný soubor |
| --- | --- |
| `0_000_screen1.png` | `0_000_screen1.json` |
| `0_000_screen2.png` | `0_000_screen2.json` |
| `0_250_screen1.png` | `0_250_screen1.json` |

JSON obsahuje identifikátor obrazovky, na které se v okamžiku snímání nacházel kurzor, a jeho souřadnice `x` a `y` v pixelech jejího snímku. Počátek `(0, 0)` je v levém horním rohu, `x` roste doprava a `y` dolů. Identifikátor v názvu souboru označuje snímanou obrazovku. Údaj o poloze kurzoru může odkazovat na jinou obrazovku.

## Zastavení a rozhodnutí o záznamu

Po stisku **Stop** se otevře dialog, který nabídne tři možnosti.

| Volba | Výsledek |
| --- | --- |
| **Process Recording** | Ukončí nahrávání a spustí přepis celého zaznamenaného zvuku. |
| **Continue Recording** | Vrátí uživatele k téže nahrávce a zachová dosud pořízené soubory. |
| **Discard Recording** | Zobrazí druhý dialog s upozorněním, že potvrzením budou smazány všechny soubory této nahrávky. |

Po potvrzení volby **Discard Recording** se smažou soubory aktuální nahrávky a ovládací okno se vrátí do výchozího stavu s tlačítkem **Record**. Bez potvrzení se soubory zachovají.

## Místní přepis a předání agentovi

1. Po volbě **Process Recording** plugin zpracuje celý uložený zvuk lokálně. FFmpeg připraví dočasný monofonní zvuk PCM s rozlišením 16 bitů a vzorkovací frekvencí 16 kHz. Přepis provede vícejazyčný model Whisper small přes `whisper.cpp` na CPU s automatickým rozpoznáním jazyka. Přepis běží offline a používá běhové součásti a model dostupné místně na dané platformě.
2. Plugin uloží k nahrávce čitelný textový přepis v UTF-8 a strojově čitelné časované segmenty v JSONu. Čitelný přepis uvádí text každého segmentu a časový rozsah od začátku záznamu ve formátu `HH:MM:SS.mmm`. JSON obsahuje text a začátek i konec segmentu v milisekundách. Časy vyjadřují odhad hranic rozpoznaných segmentů.
3. Během přepisu může plugin zobrazit průběh v anglickém dialogu, například s textem **Transcribing Audio** a indikátorem průběhu.
4. Po dokončení přepisu plugin předá řízení zpět agentovi v hostitelském prostředí. Agent dostane přístup k přepisu, všem snímkům a jejich doprovodným JSONům včetně identifikátorů obrazovek a časů. Podklady používají společnou časovou osu nahrávky, aby agent mohl spojit výrok uživatele s tím, co bylo na příslušné obrazovce a kde se nacházel kurzor.
5. Po dokončení přepisu a předání podkladů se ovládací okno zavře a automatizovaná část skillu skončí. Agent využije přepis a vizuální kontext k vyřízení požadavků, které uživatel během nahrávání vyslovil.

## Ověření splnění

- Po vyvolání skillu se otevře anglické okno nad ostatními okny. Nahrávání se spustí stiskem **Record** a kreslení je dostupné během aktivního záznamu.
- Nastavení je ve stanoveném pořadí. Výchozí interval je 250 ms, výchozí formát je PNG a rozhraní přijme krajní hodnoty intervalu 1 a 1 000 ms. Uložené hodnoty se načtou po novém spuštění.
- Při záznamu se dvěma obrazovkami vzniknou snímky obou obrazovek, zvuk a JSON ke každému snímku. Při přesunu kurzoru na druhou obrazovku metadata uvedou její identifikátor a polohu v pixelech od levého horního rohu jejího snímku.
- Po **Stop** lze pokračovat ve stejné nahrávce. Odmítnutí smazání zachová soubory. Potvrzené smazání odstraní soubory aktuální nahrávky a vrátí ovládání do stavu připraveného k novému záznamu.
- Po **Process Recording** vznikne bez sítě čitelný přepis a časované segmenty. Agent má přístup k podkladům i po zavření ovládacího okna a v ukázce dokáže spojit vyslovený pokyn se správnou obrazovkou a polohou kurzoru.
- Ověření pokryje cílové hostitele a operační systémy v kombinacích, které budou potvrzeny při návrhu integrace.

## Otevřená rozhodnutí

- Horní hranice a výchozí hodnota kvality JPEG a formát uloženého zdrojového zvuku.
- Přesná výchozí cesta pro data a životní cyklus pracovních souborů ve vztahu k adresáři zvolenému v nastavení.
- Chování zvuku a snímání při otevřeném dialogu **Stop**, případné promítnutí pauzy do společné časové osy a přesné chování při odmítnutí smazání.
- Nástroje pro kreslení a zachycení kresby ve snímcích.
- Způsob spuštění lokálního ovládacího okna a předání souborů agentovi v jednotlivých hostitelských prostředích.
