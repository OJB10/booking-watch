# Booking Watch

Get a Telegram message when a SevenRooms restaurant date opens for your party size.

The watcher checks three levels:

1. **Not released:** the date has no published shifts yet.
2. **Released but full:** the date is published, but no instant booking times are available.
3. **Bookable:** at least one time can be booked.

You get one alert per step up, with a booking link. If the first check finds tables,
you get just the bookable alert. Moving backwards and forwards does not repeat an
alert. After the bookable alert is delivered, the watch stops checking. Failed
deliveries are retried on the next run.

If the page cannot be read for two hours, you get one "can't read the page" alarm.
Checks continue, and a recovery message follows when reading works again. These
messages also require working Telegram credentials and delivery. The watcher
never books or pays. You make the booking yourself.

## Setup

Use Python 3.9 or newer on macOS or Linux. There are no third-party packages and
no AI at run time. Open a terminal in your downloaded or cloned repository.

1. In Telegram, message **@BotFather**, use `/newbot`, and save the bot token.
   Open your new bot's chat and press Start or send it a message.

2. Get your chat ID from Telegram's `getUpdates` response. Run this command and
   enter your token at the hidden prompt. It prints chat IDs, never the token:

   ```sh
   python3 - <<'PY'
   import getpass
   import json
   import urllib.request

   token = getpass.getpass("Bot token: ")
   try:
       with urllib.request.urlopen(
           "https://api.telegram.org/bot" + token + "/getUpdates", timeout=60
       ) as response:
           updates = json.load(response)
       ids = {item["message"]["chat"]["id"]
              for item in updates.get("result", []) if "message" in item}
       for chat_id in sorted(ids):
           print(chat_id)
       if not ids:
           print("Send the bot a message, then try again.")
   except Exception:
       print("Could not read updates. Check the token and connection.")
   PY
   ```

   For a group, add your bot to the group, send it a command there, and use that
   group's chat ID. The bot needs permission to send messages to the chosen chat.

3. Create the private settings file and open it in a text editor:

   ```sh
   mkdir -p ~/.config/booking-watch
   touch ~/.config/booking-watch/env
   chmod 600 ~/.config/booking-watch/env
   nano ~/.config/booking-watch/env
   ```

   Save these two lines with your real values:

   ```text
   TELEGRAM_BOT_TOKEN=your-bot-token
   TELEGRAM_CHAT_ID=your-chat-id
   ```

   A forum topic can also use `TELEGRAM_THREAD_ID`. The engine reads this file
   on every run, including scheduled runs. Existing environment variables take
   precedence over the file. Blank lines and comments beginning with `#` are
   allowed. Values may have matching single or double quotes. This is a settings
   file, not a shell script: do not use `export` or shell expressions.
   Comments after a value are allowed only after a space and `#`; the file must stay `chmod 600`.
   Keep the token private. It is never written to a watch config or LaunchAgent.

4. Find the venue slug in its SevenRooms booking URL. For example, in
   `https://www.sevenrooms.com/explore/your-venue-slug/reservations/create/search`,
   the slug is `your-venue-slug`. Older URLs may use `/reservations/your-venue-slug`.

5. Create a watch, replacing the venue and date with your own choices. Choose
   today or a future date, and give each watch a unique ID:

   ```sh
   python3 watchctl.py new --id dinner --label Dinner --adapter sevenrooms \
     --venue your-venue-slug --date 2099-01-01 --party-size 2 --preferred-time 19:30
   ```

   This creates `watches/dinner.json` and builds its booking link. The date shown
   is a placeholder. `examples/example-watch.json` shows the config format.
   The preferred time guides the query; alerts can include other times that day.

6. Try a dry run:

   ```sh
   python3 engine.py watches/dinner.json --dry-run
   ```

   This reads the live booking endpoint and prints any proposed alert. It sends
   no Telegram messages and uses separate dry-run state, so it cannot consume
   your real alert. It does not test Telegram credentials.

7. Schedule checks every five minutes.

   On macOS:

   ```sh
   python3 watchctl.py start dinner
   python3 watchctl.py status
   ```

   This checks immediately, then every five minutes while you are logged in.
   The LaunchAgent label is `local.booking-watch.dinner`. To stop it, run
   `python3 watchctl.py stop dinner`. Start and stop accept `--dry-run` to print
   their proposed actions.

   On Linux:

   ```sh
   python3 watchctl.py cron dinner
   crontab -e
   ```

   Copy the printed line into your crontab and save it. The command only prints;
   it installs nothing. Cron must be installed and running. Remove the line to
   stop the schedule. Start, stop and status use macOS launchd; use your crontab
   and saved state on Linux.

## Operation and limits

Be polite: one small request every 5 minutes per watch. Do not shorten it.
Avoid scheduling the same watch twice. Telegram requests happen only when an
alert or retry is needed.

Requests identify the tool with the honest User-Agent
`booking-watch/1.0 (+https://github.com/OJB10/booking-watch)`.
HTTP 403 or 429 responses trigger a one-hour back-off with no booking requests.
That time still counts towards the "can't read the page" alarm.

A python.org install on macOS may need its `Install Certificates.command` run
once; otherwise both booking checks and Telegram alerts can fail TLS verification.

State is saved in `~/.local/state/booking-watch`. Set `BOOKING_WATCH_STATE_DIR`
to choose another directory before starting a watch or printing its cron line.
On macOS, each watch's log is in that directory as `<id>.log`. Configs stay in
`watches/`. Stopping a schedule keeps configs, state and logs. Watches become
inactive after their date has passed in UTC, or after a bookable alert succeeds.
The scheduled job remains installed until you remove it.

Optional `--tg-chat` and `--tg-thread` values override the environment for that
watch. Without them, the environment supplies the destination. Missing token or
chat ID makes delivery fail and retry on the next run. `--booking-url` overrides
the generated link; `--broken-after-minutes` changes the alarm threshold from 120.

SevenRooms is the only supported service today. It reads an unofficial endpoint
that can change without notice. Some sites block automated reads. Availability
can disappear before you open the link, and five-minute polling can miss brief
openings. A network failure just after Telegram accepts a message can cause a
duplicate on retry.

The computer must be on, awake and connected. On macOS, a restart stops the
watcher until you log in. Use it for your own bookings, not resale. Check the
booking site's terms.

Adding OpenTable, Resy or Tock means writing one adapter file,
`adapters/<name>.py`, that provides `check(cfg) -> (state, times)`. Return one of
`NOT_RELEASED`, `REQUEST_ONLY` or `BOOKABLE`, plus a list of bookable time strings.
Raise an exception if the page cannot be read. Use only the standard library.
Set `adapter` to the module name in a watch JSON file; the current `new` command
offers SevenRooms only. A new service also needs its own `booking_url` in that
config. Finding a reliable way to read each service is the adapter author's work.

## Tests

Run the offline tests from the repository directory:

```sh
python3 -m unittest -v test_booking_watch test_watchctl
```

They use saved public availability fixtures, fake delivery and fake scheduling
commands. They make no network requests. Temporary files stay inside the
repository and are cleaned up afterwards.
