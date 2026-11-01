# alpha-desk cron jobs

The alpha-desk skill only works as designed if Hermes runs it on a schedule.
Open positions are watched by `portfolio.py monitor`, so these jobs are what protects a trade after it is taken.
Times are UTC, which is what `hermes cron list` shows for a default profile (next-run times end in +00:00).

## 1. Check what exists

```bash
docker exec -it hermes-intraday hermes cron list
```

A complete setup has the six jobs below plus the existing `alpha-desk-squareoff` job at `45 9 * * 1-5` (15:15 IST).
A job like `alpha-desk-morning-scan` at `20 5 * * 1-5` runs once a day at 10:50 IST. It is not the 15-minute scan, so remove it:

```bash
docker exec -it hermes-intraday hermes cron remove alpha-desk-morning-scan
```

## 2. Create the agent jobs

Each one starts an agent turn with the skill loaded and sends the result to Telegram.

```bash
docker exec -it hermes-intraday hermes cron create "50 3 * * 1-5" "Run the alpha-desk pre-session job: market_data.py status (stop if the market is closed), then market_data.py screen, then send the pre-session brief." --name alpha-desk-presession --skill alpha-desk --deliver telegram
```

```bash
docker exec -it hermes-intraday hermes cron create "*/15 4-8 * * 1-5" "Run the alpha-desk scan turn checklist." --name alpha-desk-scan --skill alpha-desk --deliver telegram
```

```bash
docker exec -it hermes-intraday hermes cron create "0 9 * * 1-5" "Run the alpha-desk scan turn checklist." --name alpha-desk-scan-1430 --skill alpha-desk --deliver telegram
```

```bash
docker exec -it hermes-intraday hermes cron create "15 10 * * 1-5" "Run performance.py daily-review and send the alpha-desk end-of-day message." --name alpha-desk-eod --skill alpha-desk --deliver telegram
```

```bash
docker exec -it hermes-intraday hermes cron create "30 4 * * 0" "Run the alpha-desk weekly review. If today is the first Sunday of the month, also run the monthly review." --name alpha-desk-weekly --skill alpha-desk --deliver telegram
```

If Hermes rejects a flag, run `hermes cron create --help` and adjust.

## 3. Create the silent monitor

This is a script-only job. It runs every minute without an agent turn, so it uses no model requests.
It prints nothing, and so sends nothing, unless a position closed, the day hit the loss limit, or a price is missing.

First write the script into the Hermes scripts folder, which is where Hermes looks for script-only jobs:

```bash
docker exec -i hermes-intraday sh -c 'mkdir -p "$HERMES_HOME/scripts" && cat > "$HERMES_HOME/scripts/alpha-desk-monitor.sh" && chmod +x "$HERMES_HOME/scripts/alpha-desk-monitor.sh"' <<'EOF'
#!/bin/sh
# Hermes script-only cron job: manage open alpha-desk positions. Prints nothing unless something happened.
portfolio=$(find "${HERMES_HOME:-/opt/data}/skills" -path '*/alpha-desk/scripts/portfolio.py' -not -path '*/.hub/*' 2>/dev/null | head -n 1)
[ -n "$portfolio" ] || { echo "alpha-desk monitor: skill not found"; exit 1; }
exec python3 "$portfolio" monitor --brief
EOF
```

Then schedule it for market hours (09:30 to 15:29 IST):

```bash
docker exec -it hermes-intraday hermes cron create "* 4-9 * * 1-5" --no-agent --script alpha-desk-monitor.sh --name alpha-desk-monitor --deliver telegram
```

Run it once by hand. With no open positions it prints nothing:

```bash
docker exec --user hermes hermes-intraday sh /opt/data/scripts/alpha-desk-monitor.sh
```

## What each job does

| Job | When (IST) | What happens |
|-----|-----------|--------------|
| `alpha-desk-presession` | 09:20 | Checks the market is open, builds the day's shortlist, sends the brief |
| `alpha-desk-scan`, `alpha-desk-scan-1430` | 09:30 to 14:30, every 15 minutes | Checks positions, scans for setups, proposes a trade for your approval |
| `alpha-desk-monitor` | 09:30 to 15:29, every minute | Books stop, target and breakeven exits, halts the day at -2%, sends a message only when something happened |
| `alpha-desk-squareoff` | 15:15 | Closes anything still open |
| `alpha-desk-eod` | 15:45 | Daily review and end-of-day message |
| `alpha-desk-weekly` | Sunday 10:00 | Weekly review, plus the monthly review on the first Sunday |
