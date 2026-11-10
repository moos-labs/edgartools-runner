# edgartools-runner

Runs [**edgartools**](https://pypi.org/project/edgartools/) SEC EDGAR
extractions on GitHub Actions, where the commands to execute live in a
repository **Actions variable** — not in code.

edgartools is a Python library for SEC EDGAR filings: it turns 10-K, 10-Q and
other US filings into structured Python objects and standardised financial
statements (income statement, balance sheet, cash flow) parsed from XBRL.
EDGAR covers **US-listed companies only**, so this runner is US-only by
design — tickers are SEC tickers and there is no other market to select.

## The pattern (one repo per tool)

1. **Edit the commands**: repo *Settings → Secrets and variables → Actions →
   Variables* → set the `COMMANDS` variable to whatever shell commands this
   run should execute (multi-line works). The same can be done through the
   GitHub API (`PUT /repos/{owner}/{repo}/actions/variables/COMMANDS`).
2. **Trigger manually**: *Actions* tab → “Run analysis” → *Run workflow*
   (or `POST /repos/{owner}/{repo}/actions/workflows/run.yml/dispatches`).
   The workflow has **no other triggers** — it runs only when dispatched.
3. **Collect results**: the workflow uploads everything under `results/` as
   the `results` build artifact (`if: always()`, so partial results survive
   a failing step). Download it from the run page or via the API.

The workflow itself is generic: checkout → Python 3.11 → `pip install -r
requirements.txt` → run `${{ vars.COMMANDS }}` → upload `results/`.
To run a different extraction, change the variable — not the workflow.

## The script

`scripts/fetch.py` is generic: the ticker is a CLI argument
(`--ticker`, default `AAPL`) and the forms default to the latest **10-K**
and latest **10-Q** (`--forms`). Nothing is hardcoded beyond those pilot
defaults, so the same repo can extract any US ticker's filings.

SEC EDGAR requires a declared identity: the script calls
`edgar.set_identity("Moos Labs research moos@sherbeeny.com")` (and sets
`EDGAR_IDENTITY` to the same string) **before** any request.

For each filing it records the form, filing date and period end, then
extracts the headline figures the library exposes for the most recent
period — revenue / net sales, net income, operating income, gross profit
where available, plus total assets and total liabilities from the balance
sheet — each with its exact value and the label/method it came from.
Figures the library does not return are omitted, never estimated. The full
standardised income-statement and balance-sheet rows are included alongside
the headline figures.

## Pilot: Apple (AAPL)

Pilot `COMMANDS` value:

```sh
mkdir -p results
python scripts/fetch.py 2>&1 | tee results/run.log
```

Output: `results/aapl_financials.json` — latest 10-K and latest 10-Q
headline financials for Apple Inc., plus `results/run.log`.
