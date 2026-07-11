# Ticker Map

Company name (as it appears in Capillary posts) → resolved exchange ticker used in this
project. Yahoo Finance's chart API convention: `.NS` = NSE, `.BO` = BSE, no suffix = US
exchange (NASDAQ/NYSE).

| Company | Ticker used | Notes |
|---|---|---|
| Caplin Point Laboratories | `CAPLIPOINT.NS` | |
| Kovai Medical Center & Hospital | `KOVAI.NS` | |
| Kalyan Jewellers | `KALYANKJIL.NS` | NOT `KALYANJWL.NS` — that symbol doesn't resolve |
| Titan Company | `TITAN.NS` | |
| Trent Ltd | `TRENT.NS` | |
| KPIT Technologies | `KPITTECH.NS` | |
| Tata Elxsi | `TATAELXSI.NS` | |
| HDFC Bank | `HDFCBANK.NS` | |
| Airfloa Rail Technology | `AIRFLOA.BO` | NSE symbol didn't resolve; BSE did |
| International Gemological Institute (India) | `IGIL.NS` | |
| Sagility India | `SAGILITY.NS` | |
| Palo Alto Networks | `PANW` | NASDAQ, no suffix |
| CrowdStrike | `CRWD` | NASDAQ — did a 4-for-1 split Jul 2, 2026, adjust historical prices accordingly |
| Rubrik | `RBRK` | NYSE |
| Cloudflare | `NET` | NYSE |
| Zscaler | `ZS` | NASDAQ |
| Varonis Systems | `VRNS` | NASDAQ |
| Symbotic | `SYM` | NASDAQ |

When a new company appears in a future post, resolve its ticker manually (search Yahoo
Finance / the exchange listing) and add a row here before letting anything automated
depend on it. Don't trust an LLM's ticker guess unverified — this table exists because
several tickers required trial-and-error the first time.
