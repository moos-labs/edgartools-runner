#!/usr/bin/env python3
"""Fetch headline financials for a US-listed company from SEC EDGAR.

Uses the `edgartools` library (`edgar` package). SEC EDGAR requires every
client to declare an identity, so `set_identity()` is called before any
request is made.

Generic by design: the ticker is a CLI argument (default ``AAPL``) and the
forms to fetch default to the latest 10-K and latest 10-Q. Nothing else is
hardcoded — the same script works for any US ticker with XBRL filings.

Output: ``results/<ticker>_financials.json`` with, per filing, the form,
filing date, period end, and every headline figure the library returned,
each with its exact value and the label/method it came from. Figures the
library does not expose are omitted, never estimated.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path

# SEC EDGAR declared identity (also honoured via the EDGAR_IDENTITY env var).
IDENTITY = "Moos Labs research moos@sherbeeny.com"
os.environ.setdefault("EDGAR_IDENTITY", IDENTITY)

# Standardised getters on the edgartools Financials object:
# (method name, human label). Only methods that exist and return a
# non-None value are included in the output.
GETTERS = [
    ("get_revenue", "Revenue"),
    ("get_net_income", "Net income"),
    ("get_operating_income", "Operating income"),
    ("get_gross_profit", "Gross profit"),
    ("get_total_assets", "Total assets"),
    ("get_total_liabilities", "Total liabilities"),
    ("get_stockholders_equity", "Stockholders' equity"),
    ("get_current_assets", "Current assets"),
    ("get_current_liabilities", "Current liabilities"),
]

# Period columns in a statement DataFrame are headed by an ISO date,
# optionally suffixed, e.g. "2026-09-26" or "2026-09-26 (FY)".
PERIOD_COLUMN = re.compile(r"^\d{4}-\d{2}-\d{2}")

# Headline concepts to fill from statement rows when no standardised
# getter exists (edgartools 6.x has no get_gross_profit(), but the income
# statement carries a GrossProfit row). Matched on the row's
# standard_concept or its exact label; the figure keeps the row's label.
ROW_HEADLINES = [
    # (canonical key, standard concepts, label fragments)
    ("gross_profit", {"GrossProfit"}, ("gross margin", "gross profit")),
]


def json_safe(value):
    """Convert a library value into something json.dumps can handle."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, Decimal):
        return float(value) if value != value.to_integral_value() else int(value)
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    # numpy / pandas scalars expose .item()
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return json_safe(item())
        except Exception:
            pass
    # dates / datetimes
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    return str(value)


def statement_rows(statement):
    """Dump a statement object as rows of {label, concept, values-by-period}.

    Prefers the "summary" view (non-dimensional totals — the headline
    figures) and falls back to the default view.
    """
    df = None
    last_error = None
    for kwargs in ({"view": "summary"}, {}):
        try:
            df = statement.to_dataframe(**kwargs)
            if df is not None and len(df) > 0:
                break
        except Exception as exc:  # noqa: BLE001 - record and try next view
            last_error = exc
            df = None
    if df is None:
        if last_error is not None:
            print(f"  ! to_dataframe failed: {last_error}", file=sys.stderr)
        return [], []
    period_cols = [c for c in df.columns if PERIOD_COLUMN.match(str(c))]
    rows = []
    for _, row in df.iterrows():
        values = {}
        for col in period_cols:
            v = json_safe(row.get(col))
            if v is not None:
                values[str(col)] = v
        std = row.get("standard_concept")
        rows.append({
            "label": None if row.get("label") is None else str(row.get("label")),
            "concept": None if row.get("concept") is None else str(row.get("concept")),
            "standard_concept": None if std is None or (isinstance(std, float) and math.isnan(std)) else str(std),
            "values": values,
        })
    return rows, [str(c) for c in period_cols]


def extract_from_financials(financials):
    """Pull headline figures + statement rows from a Financials object."""
    figures = []
    metrics = None
    income_rows, income_periods = [], []
    balance_rows, balance_periods = [], []

    if financials is None:
        return figures, metrics, income_rows, balance_rows, income_periods

    for method_name, label in GETTERS:
        method = getattr(financials, method_name, None)
        if not callable(method):
            continue
        try:
            value = method()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {method_name}() failed: {exc}", file=sys.stderr)
            continue
        value = json_safe(value)
        if value is not None:
            figures.append({
                "label": label,
                "value": value,
                "source": f"financials.{method_name}()",
            })

    get_metrics = getattr(financials, "get_financial_metrics", None)
    if callable(get_metrics):
        try:
            raw_metrics = get_metrics()
            if isinstance(raw_metrics, dict):
                metrics = {str(k): json_safe(v) for k, v in raw_metrics.items()}
        except Exception as exc:  # noqa: BLE001
            print(f"  ! get_financial_metrics() failed: {exc}", file=sys.stderr)

    for stmt_name, target in (("income_statement", "income"),
                              ("balance_sheet", "balance")):
        method = getattr(financials, stmt_name, None)
        if not callable(method):
            continue
        try:
            statement = method()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {stmt_name}() failed: {exc}", file=sys.stderr)
            continue
        if statement is None:
            continue
        rows, periods = statement_rows(statement)
        if target == "income":
            income_rows, income_periods = rows, periods
        else:
            balance_rows, balance_periods = rows, periods

    # Fill headline figures that have no getter from the statement rows,
    # keeping the row's exact label and most-recent-period value.
    have = {f["label"].lower() for f in figures}
    for _key, std_concepts, label_fragments in ROW_HEADLINES:
        if any(fragment in label for label in have for fragment in label_fragments):
            continue
        for row in income_rows:
            label = (row.get("label") or "").strip()
            std = row.get("standard_concept") or ""
            if std in std_concepts or label.lower() in label_fragments:
                if row["values"]:
                    period = next(iter(row["values"]))
                    figures.append({
                        "label": label,
                        "value": row["values"][period],
                        "period": period,
                        "source": f"income_statement row "
                                  f"(standard_concept={std})",
                    })
                break

    return figures, metrics, income_rows, balance_rows, income_periods or balance_periods


def filing_financials(filing, company, form):
    """Get a Financials object for one filing, trying the documented routes."""
    # Route 1: typed filing object -> .financials
    try:
        obj = filing.obj()
        fin = getattr(obj, "financials", None)
        if fin is not None:
            return fin, "filing.obj().financials"
    except Exception as exc:  # noqa: BLE001
        print(f"  ! filing.obj() failed: {exc}", file=sys.stderr)

    # Route 2: company-level helpers (10-K -> get_financials,
    # 10-Q -> get_quarterly_financials)
    try:
        if form == "10-Q":
            fin = company.get_quarterly_financials()
        else:
            fin = company.get_financials()
        if fin is not None:
            return fin, "company.get_financials()/get_quarterly_financials()"
    except Exception as exc:  # noqa: BLE001
        print(f"  ! company financials failed: {exc}", file=sys.stderr)

    return None, None


def process_filing(company, form):
    """Fetch the latest filing of `form` and extract its headline figures."""
    print(f"Fetching latest {form} ...")
    filings = company.get_filings(form=form)
    filing = None
    latest = getattr(filings, "latest", None)
    if callable(latest):
        try:
            filing = latest()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! filings.latest() failed: {exc}", file=sys.stderr)
    if filing is None:
        filing = filings[0]

    def attr(obj, *names):
        for name in names:
            value = getattr(obj, name, None)
            if value is not None:
                return json_safe(value() if callable(value) else value)
        return None

    record = {
        "form": attr(filing, "form") or form,
        "filing_date": attr(filing, "filing_date", "filed"),
        "period_end": attr(filing, "period_of_report", "period_end",
                           "report_date"),
        "accession_number": attr(filing, "accession_number", "accession_no",
                                 "accession"),
        "company_name": attr(filing, "company"),
    }

    financials, source = filing_financials(filing, company, form)
    record["financials_source"] = source

    figures, metrics, income_rows, balance_rows, periods = \
        extract_from_financials(financials)
    record["figures"] = figures
    if metrics:
        record["financial_metrics"] = metrics
    if income_rows:
        record["income_statement_rows"] = income_rows
    if balance_rows:
        record["balance_sheet_rows"] = balance_rows

    # Period end fallback: most recent period column in the statements,
    # then the XBRL reporting periods.
    if not record["period_end"] and periods:
        record["period_end"] = periods[0]
    if not record["period_end"]:
        try:
            xbrl = filing.xbrl()
            rps = getattr(xbrl, "reporting_periods", None) if xbrl else None
            if rps:
                record["period_end"] = json_safe(rps[0])
        except Exception as exc:  # noqa: BLE001
            print(f"  ! filing.xbrl() failed: {exc}", file=sys.stderr)

    print(f"  {record['form']} filed {record['filing_date']} "
          f"period_end={record['period_end']} figures={len(figures)}")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AAPL",
                        help="US ticker symbol (default: AAPL)")
    parser.add_argument("--forms", default="10-K,10-Q",
                        help="Comma-separated forms, latest of each "
                             "(default: 10-K,10-Q)")
    parser.add_argument("--output", default=None,
                        help="Output JSON path (default: "
                             "results/<ticker>_financials.json)")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    output = Path(args.output) if args.output else \
        Path("results") / f"{ticker.lower()}_financials.json"

    # Identity MUST be declared before any EDGAR request.
    from edgar import Company, set_identity
    set_identity(IDENTITY)
    print(f"Identity: {IDENTITY}")
    print(f"Ticker: {ticker}")

    company = Company(ticker)

    def safe_company_attr(name):
        try:
            return json_safe(getattr(company, name, None))
        except Exception as exc:  # noqa: BLE001 - header info is optional
            print(f"  ! company.{name} failed: {exc}", file=sys.stderr)
            return None

    result = {
        "ticker": ticker,
        "company_name": safe_company_attr("name"),
        "cik": safe_company_attr("cik"),
        "identity": IDENTITY,
        "filings": [],
        "errors": [],
    }

    for form in [f.strip() for f in args.forms.split(",") if f.strip()]:
        # One retry per form: EDGAR occasionally drops a connection even
        # though the library already retries individual requests.
        last_exc = None
        for attempt in (1, 2):
            try:
                result["filings"].append(process_filing(company, form))
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 - record, maybe retry
                last_exc = exc
                print(f"  ! attempt {attempt} for {form} failed: {exc}",
                      file=sys.stderr)
                if attempt == 1:
                    time.sleep(5)
        if last_exc is not None:
            message = f"{form}: {type(last_exc).__name__}: {last_exc}"
            print(f"ERROR {message}", file=sys.stderr)
            result["errors"].append(message)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {output} ({len(result['filings'])} filings, "
          f"{len(result['errors'])} errors)")

    # Fail the run only if nothing at all was extracted.
    if not result["filings"]:
        return 1
    if all(not f.get("figures") for f in result["filings"]):
        print("WARNING: no headline figures extracted from any filing",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
