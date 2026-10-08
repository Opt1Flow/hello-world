# News bot

Builds a Calgary + world news digest three times a day (06:00, 13:00, 19:00).

## Reading the news

- **`news.html`** is the reading page: open it with `.venv\Scripts\python newsbot.py open`
  (or double-click the file) and keep the tab open or bookmark it. Each edition is a tab,
  weather warnings sit at the top while they're in effect, and the search box looks through
  all editions (Escape clears it). A dot marks an edition you haven't opened yet.
  The page refreshes itself when a new edition is due and says so if one is late.
- **`news.md`** is the editable copy for your notes (for example in Obsidian). Only the
  parts between the edition markers are ever replaced.

## Showing it on the Pi kiosk (or any screen at home)

`kiosk.html` is a TV view of the news: large type, weather warnings on top, and the subjects
taking turns every 20 seconds. Nothing to click. The laptop shares it on your home network:

1. On the laptop, in PowerShell opened with **Run as administrator**, inside this folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File install_server.ps1
   ```

   It starts the server now and at every logon, opens port 8765 for your **Private** (home)
   network only, and prints the address to use, e.g. `http://192.168.1.50:8765/kiosk`.
2. On the Pi, point Chromium (or the dashboard's page list) at that address.
   In Home Assistant, a **Webpage** card with the same address works too, as long as you
   open Home Assistant over plain `http://` on your home network: browsers block an `http`
   page inside an `https` one. Put it in a **panel** (single-card) view so the text is TV-sized.
3. Options go at the end of the address, e.g. `/kiosk?seconds=30&scale=1.3`:
   `seconds` (how long each screen stays), `scale` (story text size), `max` (stories per
   subject), `theme=light`.

Good to know:

- **Laptop asleep:** the kiosk keeps showing the last news it received and says it can't
  reach the laptop; it catches up when the laptop wakes. To keep it current all day, set
  Windows to not sleep while plugged in (Settings > System > Power).
- **Pi starts while the laptop sleeps:** a page loaded from the laptop can't appear until
  the laptop wakes. To avoid that, copy `kiosk.html` onto the Pi and open it as a file with
  the laptop's address added (and the timezone, for the clock before the first news arrives):
  `file:///home/pi/kiosk.html?server=http://192.168.1.50:8765&tz=America/Edmonton`
  Copy it again after you update the bot.
- **The address changes:** give the laptop a fixed address in your router (a "DHCP
  reservation"), so the Pi always finds it.
- The reading page is shared too, at `http://<laptop address>:8765/` (handy on a phone).
  Nothing else in this folder can be reached.
- If the kiosk shows nothing, check `data\server.log` on the laptop; the server restarts
  itself within 15 minutes if it ever stops.

## How it works

- **Short check-ins instead of an always-running loop.** Windows Task Scheduler starts
  `newsbot.py run-due` every 15 minutes and at logon. The script looks in a small database
  (`data/state.db`) to see which editions are already done, produces the one that is due,
  and exits. If the laptop was asleep at 06:00, the Morning edition comes out within about
  15 minutes of waking.
- **Missed editions are not produced late.** Their section gets a one-line note instead,
  so news older than 24 hours never sits there looking current.
- **The last good edition is kept.** If half or more of the feeds fail (no Wi-Fi), the old
  section stays and the next check-in tries again.
- **Your notes are safe.** Only the text between an edition's `<!-- Morning start -->` and
  `<!-- Morning end -->` markers is replaced. If a marker gets deleted or duplicated, the
  bot refuses to touch the file and writes `news.md.ERROR.txt` next to it explaining why.
- **Each story appears once.** Every edition shows only items first seen since the
  previous edition, and never anything published more than 24 hours ago.

## Setup on Windows

1. **Double-click `setup.bat`** in this folder. It creates the Python environment
   (`.venv`), installs the libraries, checks every feed (`OK` / `FAIL`) and runs the
   tests. If any feed says `FAIL`, replace it in `config.toml`.
2. In PowerShell, inside this folder, make one edition now and open the reading page:

   ```powershell
   .venv\Scripts\python newsbot.py run Morning
   .venv\Scripts\python newsbot.py open
   ```

3. Register the scheduled task:

   ```powershell
   powershell -ExecutionPolicy Bypass -File install_task.ps1
   ```

Type each command on its own line and press Enter after each one.

Then open Task Scheduler, select **NewsBot**, and confirm the trigger says
*Repeat task every 15 minutes for a duration of: Indefinitely*. If registering fails with
"Access is denied", run PowerShell as Administrator once.

To use Obsidian, set `output` in `config.toml` to a path inside your vault. The reading
page then goes next to it (`html_output` changes that).

## Day to day

- `newsbot.py status` lists recent runs (`ok`, or `skipped` for a missed edition).
- Logs are in `data/newsbot.log` (and `data/server.log` for the kiosk server).
- Change feeds, subjects, warning feeds (`[alerts]`), short source names (`[sources]`) or
  edition times in `config.toml`. Delete one line under `[editions]` to go to two
  editions a day.

## Not done yet

- Step 2: source health checks over time and conditional downloads.
- Step 3: grouping different headlines about the same story.
- Step 4: neutral wording with the local AI (Qwen3 via Ollama), used only for sorting and
  flagging, never for writing facts.
