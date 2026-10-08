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
- Logs are in `data/newsbot.log`.
- Change feeds, subjects, warning feeds (`[alerts]`), short source names (`[sources]`) or
  edition times in `config.toml`. Delete one line under `[editions]` to go to two
  editions a day.

## Not done yet

- Step 2: source health checks over time and conditional downloads.
- Step 3: grouping different headlines about the same story.
- Step 4: neutral wording with the local AI (Qwen3 via Ollama), used only for sorting and
  flagging, never for writing facts.
