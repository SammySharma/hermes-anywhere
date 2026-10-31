# NSE live data MCP for Hermes

The `alpha-desk` skill reads NSE's live market data MCP server (called `nse-live`) from its own scripts.
The skill needs no Hermes setup for that.
Register the server in Hermes only if you also want to ask market questions in chat.

## Register the server (optional)

Add the server to `config.yaml` in the Hermes data folder, then run `/reload-mcp` in a Hermes session.
On the VM that file is `hermes-data/config.yaml`. Inside the container it is `/opt/data/config.yaml`.

```yaml
mcp_servers:
  nse-live:
    url: "https://mcp.nseindia.in/cmmkt/mcp"
```

Hermes names the tools `mcp_<server>_<tool>` and turns hyphens into underscores.
For example `cm_get_stock_quote` becomes `mcp_nse_live_cm_get_stock_quote`.

## What to expect

- NSE runs several server instances without sticky sessions, so about half of the follow-up requests fail with "Session not found".
  The skill's scripts retry automatically. Native tool calls from the agent do not, so ask it to try again.
- NSE says the data runs 1 to 3 minutes behind real time and is for educational and informational use only.
  It is not for real-time trading, investment advice or commercial use.
- The server publishes snapshots (LTP, OHLC, volume, traded value, index values), not intraday bars.
  The skill takes its 15-minute and 1-minute bars from Yahoo Finance.
- No API key is needed.

Source: https://www.nseindia.com/nse-mcp
