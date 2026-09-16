# Stock Drop Alerts

Watches all 61 tickers from your Robinhood margin account and sends a free,
immediate push notification to your iPhone the first time any of them drops
more than 1% in a day (threshold is configurable).

Runs entirely for free on GitHub Actions - no server, no always-on machine.

## One-time setup (about 10 minutes)

### 1. Get a free Finnhub API key
Go to https://finnhub.io/register, sign up (free), and copy your API key
from the dashboard.

### 2. Install ntfy on your iPhone
- Download **ntfy** from the App Store (it's free, open-source).
- Open it and tap "+" to subscribe to a new topic.
- Pick a topic name only you would guess - anyone who knows your topic
  name can see your alerts, since ntfy topics aren't private by default.
  Something like `fazal-stockdrops-8f2k91` works well. Write it down.

### 3. Create a GitHub repository
- Create a new **private** repository (e.g. `stock-alerts`).
- Upload all the files in this folder to it (check_alerts.py,
  requirements.txt, requirements.txt, README.md, and the
  `.github/workflows/stock-alerts.yml` file - keep that folder structure
  intact).

### 4. Add your secrets
In the repo, go to **Settings -> Secrets and variables -> Actions -> New
repository secret** and add two secrets:
- `FINNHUB_API_KEY` - the key from step 1
- `NTFY_TOPIC` - the topic name you picked in step 2

### 5. Enable the workflow
- Go to the **Actions** tab in your repo. GitHub sometimes disables
  workflows on new repos by default - click "I understand my workflows,
  go ahead and enable them" if you see that prompt.
- Click on "Stock Drop Alerts" in the left sidebar, then "Run workflow"
  to trigger a manual test run.
- Check the run's logs - you should see each ticker's current % change
  printed out. If a ticker shows "no data", double check the symbol is
  correct/still trading under that ticker.

That's it. From here it runs automatically every 15 minutes during market
hours (Mon-Fri, 9:30am-4pm ET) with no further action needed.

## Notes and limitations

- **Free tier realities**: GitHub Actions' free scheduled runs are
  reliable but not millisecond-precise - under heavy platform load,
  runs can occasionally be delayed a few minutes. For a 1% daily-drop
  alert this is a non-issue in practice.
- **Once-per-day alerts**: each ticker only alerts once per day, even if
  it keeps dropping further or bounces and re-drops. Delete
  `alert_state.json` (or just let it roll over at midnight UTC) to reset.
- **Threshold**: change `DROP_THRESHOLD` by adding it as another repo
  secret (e.g. `-2.0` for a 2% trigger) - the script defaults to -1.0 if
  it's not set.
- **Excluded tickers**: the Opendoor warrants (OPENL/OPENW/OPENZ) from
  your statement aren't included - they're not standard equities and
  most free quote APIs, including Finnhub, don't carry them.
- **Rate limits**: Finnhub's free tier allows 60 calls/minute. With 61
  tickers and a 1-second pause between each, one full run takes about a
  minute - comfortably within the limit.
