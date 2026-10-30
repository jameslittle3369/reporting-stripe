#!/usr/bin/env python3
"""
stripe_report.py
----------------
Pulls every reportable metric from the connected Stripe account - balance
activity, payments, payment intents, refunds, disputes, fraud warnings,
and payouts - builds an HTML
email with embedded charts and summary cards, attaches a self-contained
interactive Chart.js HTML report, and sends it via Amazon SES.

Stripe has no "metrics" endpoint, so every series here is derived from
Stripe's list endpoints over the lookback window. Related series share one
chart (e.g. gross / net / fees / refunds). Any object type the API key can't
read, or the account doesn't use, degrades to a "No activity" note instead of
crashing the run.

A 1-day window (the daily run, covering yesterday) is a trimmed report: six
summary cards (payouts, captures, auths, refunds, fees, disputes) plus the
breakdown bar charts - no trend lines and no interactive attachment.

Setup:
  1. copy .env.example .env   and fill in STRIPE_API_KEY and the AWS SES /
     recipient settings.
  2. .venv\\Scripts\\pip.exe install -r requirements.txt
  3. .venv\\Scripts\\python.exe stripe_report.py --preview --debug

Usage:
  python stripe_report.py                  # build and send the report
  python stripe_report.py --preview        # save locally, don't send
  python stripe_report.py --days 30        # override the lookback window (full report)
  python stripe_report.py --date 2026-10-28                # one specific day (condensed)
  python stripe_report.py --range 2026-10-01 2026-10-28    # inclusive date range (full report)
  python stripe_report.py --to a@b.com     # override recipients for this run
  python stripe_report.py --debug          # print a sample raw object per endpoint
"""

import argparse
import base64
import io
import json
import os
import re
import struct
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import email.encoders
import email.mime.base
import email.mime.image
import email.mime.multipart
import email.mime.text

import stripe
import boto3
from dotenv import load_dotenv

load_dotenv()

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# -- CONFIG (loaded from .env) ------------------------------------------------
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY", "")

AWS_REGION            = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID     = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")

FROM_ADDRESS = os.getenv("FROM_ADDRESS", "")
FROM_NAME    = os.getenv("FROM_NAME", "James Little")
TO_ADDRESSES = [a.strip() for a in os.getenv("TO_ADDRESSES", "").split(",") if a.strip()]

REPORT_DAYS = int(os.getenv("REPORT_DAYS", "30"))
TIMEZONE    = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))

DEBUG = False
# -----------------------------------------------------------------------------

ses_kwargs = {"region_name": AWS_REGION}
if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
    ses_kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
    ses_kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
ses = boto3.client("sesv2", **ses_kwargs)

# Currencies Stripe expresses in whole units rather than cents.
# https://docs.stripe.com/currencies#zero-decimal
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg",
                "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}
CURRENCY_SYMBOLS = {"usd": "$", "cad": "CA$", "aud": "A$", "eur": "€", "gbp": "£", "jpy": "¥"}

# Set once the account's settlement currency is known (see main()).
CURRENCY = "usd"


def safe_id(s: str) -> str:
    """Turn a string into a safe CSS/JS identifier (must not start with a digit)."""
    clean = re.sub(r"[^a-zA-Z0-9]", "_", s)
    if clean and clean[0].isdigit():
        clean = "n" + clean
    return clean


def major(amount) -> float:
    """Stripe minor units (cents) -> major units (dollars) for CURRENCY."""
    return (amount or 0) / (1 if CURRENCY in ZERO_DECIMAL else 100)


def cur_symbol() -> str:
    return CURRENCY_SYMBOLS.get(CURRENCY, CURRENCY.upper() + " ")


def fmt_int(v) -> str:
    try:
        return f"{int(round(v)):,}"
    except (TypeError, ValueError):
        return "0"


def fmt_money(v) -> str:
    return f"{'-' if v < 0 else ''}{cur_symbol()}{abs(v):,.2f}"


def fmt_pct(v) -> str:
    return "—" if v is None else f"{v:.1f}%"


def fmt_value(v, fmt):
    if fmt == "money":
        return fmt_money(v)
    if fmt == "pct":
        return fmt_pct(v)
    return fmt_int(v)


def nice_label(s) -> str:
    """'card_present' / 'insufficient_funds' -> 'Card Present' / 'Insufficient Funds'."""
    return str(s).replace("_", " ").title() if s else "Unknown"


# ---------------------------------------------------------------------------
# Stripe API client
# ---------------------------------------------------------------------------

client = None


def fetch_all(label, service, params=None):
    """List every object on an endpoint (auto-paginated), returned as plain
    dicts. Returns None when the endpoint can't be read (missing restricted-key
    scope, feature not enabled, network error) so callers can omit the section
    and its summary card instead of reporting a misleading zero."""
    params = dict(params or {})
    params.setdefault("limit", 100)
    try:
        items = [o.to_dict() for o in service.list(params).auto_paging_iter()]
    except stripe.AuthenticationError:
        raise
    except stripe.StripeError as e:
        print(f"  Stripe: {label} unavailable ({type(e).__name__}: {e.user_message or e}) - skipping")
        return None
    print(f"  {label}: {len(items):,}")
    if DEBUG and items:
        print("  " + json.dumps(items[0], default=str)[:1500])
    return items


def created_window(start_ts, end_ts):
    return {"created": {"gte": start_ts, "lt": end_ts}}


def day_of(ts):
    return datetime.fromtimestamp(ts, TIMEZONE).date() if ts else None


def g(d, *path, default=None):
    """Nested dict get: g(charge, "outcome", "reason")."""
    for key in path:
        if not isinstance(d, dict):
            return default
        d = d.get(key)
    return default if d is None else d


# ---------------------------------------------------------------------------
# Chart specs - each metric group becomes one spec, rendered twice (matplotlib
# PNG for the email body, Chart.js for the interactive attachment).
# ---------------------------------------------------------------------------

class Report:
    def __init__(self, days_list):
        self.days = days_list                      # [date, ...] in TIMEZONE
        self.sections = {}                         # {section: [spec, ...]}
        self.notes = []                            # footnotes (e.g. other currencies)
        self.trends = len(days_list) > 1           # a 1-day window has no trend to plot

    def zeros(self):
        return {d: 0 for d in self.days}

    def line(self, section, title, series, fmt="int"):
        """series: list of (label, {date: value}) - dates outside the window are
        dropped, missing dates gap-filled with 0 (pct charts keep None gaps).
        No-op for a single-day report."""
        if not self.trends:
            return
        rows = []
        for label, by_day in series:
            fill = None if fmt == "pct" else 0
            rows.append((label, [by_day.get(d, fill) for d in self.days]))
        if fmt == "pct":
            has_data = any(v is not None for _, vals in rows for v in vals)
        else:
            has_data = any(v for _, vals in rows for v in vals)
        self._add(section, {"kind": "line", "title": title, "series": rows,
                            "fmt": fmt, "has_data": has_data})

    def bar(self, section, title, counter, fmt="int", top=12, labeler=nice_label):
        rows = [(labeler(k), v) for k, v in sorted(counter.items(), key=lambda kv: kv[1], reverse=True) if v]
        self._add(section, {"kind": "bar", "title": title, "rows": rows[:top],
                            "fmt": fmt, "has_data": bool(rows)})

    def unavailable(self, section, what):
        self._add(section, {"kind": "note", "title": what, "has_data": False})

    def _add(self, section, spec):
        spec["id"] = safe_id(f"{section}_{spec['title']}")
        self.sections.setdefault(section, []).append(spec)


def daily_sum(items, ts_key, value=lambda o: 1, where=lambda o: True):
    out = defaultdict(float)
    for o in items:
        if where(o):
            d = day_of(o.get(ts_key))
            if d:
                out[d] += value(o)
    return out


def ratio_by_day(num, den):
    return {d: (100 * num.get(d, 0) / den[d]) for d in den if den[d]}


# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------

PAYOUT_TYPES = {"payout", "payout_cancel", "payout_failure", "payout_minimum_balance_hold",
                "payout_minimum_balance_release"}
CHARGE_TYPES = {"charge", "payment"}
REFUND_TYPES = {"refund", "payment_refund"}
DISPUTE_TYPES = {"adjustment", "issuing_dispute"}  # dispute debits post as "adjustment"


def collect(report, start_ts, end_ts, summary, details):
    win = created_window(start_ts, end_ts)
    other_currency = Counter()

    def in_cur(o, key="currency"):
        c = (o.get(key) or CURRENCY).lower()
        if c != CURRENCY:
            other_currency[c.upper()] += 1
            return False
        return True

    # -- Revenue (balance transactions - already in settlement currency) ------
    S = "Revenue"
    bts = fetch_all("Balance transactions", client.v1.balance_transactions, win)
    if bts is None:
        report.unavailable(S, "Balance transactions")
    else:
        activity = [b for b in bts if b["type"] not in PAYOUT_TYPES and in_cur(b)]
        gross = daily_sum(activity, "created", lambda b: major(b["amount"]), lambda b: b["type"] in CHARGE_TYPES)
        net = daily_sum(activity, "created", lambda b: major(b["net"]))
        fees = daily_sum(activity, "created", lambda b: major(b["fee"]))
        refunds = daily_sum(activity, "created", lambda b: -major(b["amount"]), lambda b: b["type"] in REFUND_TYPES)
        report.line(S, "Gross Volume, Net Volume, Fees & Refunds",
                    [("Gross", gross), ("Net", net), ("Fees", fees), ("Refunds", refunds)], "money")
        by_type = Counter()
        for b in activity:
            by_type[b["type"]] += abs(major(b["amount"]))
        report.bar(S, "Volume by Transaction Type", by_type, "money")
        fee_types = Counter()
        for b in activity:
            for fd in b.get("fee_details") or []:
                fee_types[fd.get("type")] += major(fd.get("amount"))
        report.bar(S, "Fees by Type", fee_types, "money")
        # Charge balance transactions post at capture, so this includes captures
        # of authorizations made on earlier days.
        captures = [b for b in activity if b["type"] in CHARGE_TYPES]
        summary["Captures"] = fmt_money(sum(gross.values()))
        details["Captures"] = f"{len(captures):,} captured"
        summary["Gross Volume"] = fmt_money(sum(gross.values()))
        summary["Net Volume"] = fmt_money(sum(net.values()))
        summary["Stripe Fees"] = fmt_money(sum(fees.values()))
        summary["Refunded"] = fmt_money(sum(refunds.values()))

    # -- Payments (charges) ---------------------------------------------------
    S = "Payments"
    charges = fetch_all("Charges", client.v1.charges, win)
    if charges is None:
        report.unavailable(S, "Charges")
    else:
        charges = [c for c in charges if in_cur(c)]
        ok = [c for c in charges if c["status"] == "succeeded"]
        bad = [c for c in charges if c["status"] == "failed"]
        n_ok = daily_sum(ok, "created")
        n_bad = daily_sum(bad, "created")
        report.line(S, "Successful vs Failed Payments", [("Successful", n_ok), ("Failed", n_bad)])
        attempts = {d: n_ok.get(d, 0) + n_bad.get(d, 0) for d in set(n_ok) | set(n_bad)}
        report.line(S, "Payment Success Rate", [("Success rate", ratio_by_day(n_ok, attempts))], "pct")
        amt_ok = daily_sum(ok, "created", lambda c: major(c["amount"]))
        report.line(S, "Average Payment Amount",
                    [("Average", {d: amt_ok[d] / n_ok[d] for d in n_ok if n_ok[d]})], "money")

        report.bar(S, "Payment Methods", Counter(g(c, "payment_method_details", "type") for c in ok))
        brands = Counter()
        countries = Counter()
        wallets = Counter()
        for c in ok:
            pmd = c.get("payment_method_details") or {}
            card = pmd.get("card") or pmd.get("card_present") or {}
            if card:
                brands[card.get("brand")] += 1
                countries[card.get("country")] += 1
                if g(card, "wallet", "type"):
                    wallets[g(card, "wallet", "type")] += 1
        report.bar(S, "Card Brands", brands)
        report.bar(S, "Card Issuing Countries", countries, top=10, labeler=lambda k: k or "Unknown")
        report.bar(S, "Digital Wallets", wallets)
        report.bar(S, "Payment Outcomes", Counter(g(c, "outcome", "type") for c in charges))
        report.bar(S, "Decline Reasons",
                   Counter(g(c, "outcome", "reason") or c.get("failure_code") for c in bad))
        report.bar(S, "Radar Risk Levels", Counter(g(c, "outcome", "risk_level") for c in charges))

        # A succeeded charge is an authorization; one released without capture
        # shows as refunded and isn't counted (matches the Dashboard).
        auths = [c for c in ok if c.get("captured") or not c.get("refunded")]
        captured = [c for c in auths if c.get("captured")]
        pending = [c for c in auths if not c.get("captured")]
        released = len(ok) - len(auths)
        summary["Auths"] = fmt_money(sum(major(c["amount"]) for c in auths))  # full authorized amount
        details["Auths"] = (f"{len(auths):,} auths &bull; {len(captured):,} captured ({fmt_money(sum(major(c['amount_captured']) for c in captured))})"
                            f" &bull; {len(pending):,} pending ({fmt_money(sum(major(c['amount']) for c in pending))})"
                            + (f" &bull; {released:,} released" if released else ""))

        total_attempts = len(ok) + len(bad)
        summary["Successful Payments"] = fmt_int(len(ok))
        summary["Payment Success Rate"] = fmt_pct(100 * len(ok) / total_attempts if total_attempts else None)
        if ok:
            summary["Average Payment"] = fmt_money(sum(amt_ok.values()) / len(ok))

    # -- Payment intents ------------------------------------------------------
    S = "Payment Intents"
    pis = fetch_all("Payment intents", client.v1.payment_intents, win)
    if pis is None:
        report.unavailable(S, "Payment intents")
    else:
        incomplete = {"requires_payment_method", "requires_confirmation", "requires_action", "processing"}
        report.line(S, "Payment Intents Created vs Outcome", [
            ("Created", daily_sum(pis, "created")),
            ("Succeeded", daily_sum(pis, "created", where=lambda p: p["status"] == "succeeded")),
            ("Canceled", daily_sum(pis, "created", where=lambda p: p["status"] == "canceled")),
            ("Incomplete", daily_sum(pis, "created", where=lambda p: p["status"] in incomplete)),
        ])
        report.bar(S, "Payment Intent Status", Counter(p["status"] for p in pis))
        report.bar(S, "Payment Intent Cancellation Reasons",
                   Counter(p.get("cancellation_reason") for p in pis if p["status"] == "canceled"))
        report.bar(S, "Last Payment Errors",
                   Counter(g(p, "last_payment_error", "decline_code") or g(p, "last_payment_error", "code")
                           for p in pis if p.get("last_payment_error")))

    # -- Refunds, disputes & fraud warnings ------------------------------------
    S = "Refunds & Disputes"
    refunds = fetch_all("Refunds", client.v1.refunds, win)
    disputes = fetch_all("Disputes", client.v1.disputes, win)
    efws = fetch_all("Early fraud warnings", client.v1.radar.early_fraud_warnings, win)
    count_series, amount_series = [], []
    if refunds is not None:
        refunds = [r for r in refunds if in_cur(r)]
        count_series.append(("Refunds", daily_sum(refunds, "created")))
        amount_series.append(("Refunded", daily_sum(refunds, "created", lambda r: major(r["amount"]))))
    if disputes is not None:
        disputes = [d for d in disputes if in_cur(d)]
        count_series.append(("Disputes", daily_sum(disputes, "created")))
        amount_series.append(("Disputed", daily_sum(disputes, "created", lambda d: major(d["amount"]))))
        summary["Disputes"] = fmt_int(len(disputes))
        details["Disputes"] = fmt_money(sum(major(d["amount"]) for d in disputes)) + " disputed"
    if efws is not None:
        count_series.append(("Early fraud warnings", daily_sum(efws, "created")))
    if count_series:
        report.line(S, "Refunds, Disputes & Fraud Warnings", count_series)
    if amount_series:
        report.line(S, "Refunded vs Disputed Amount", amount_series, "money")
    if refunds is not None:
        report.bar(S, "Refund Reasons", Counter(r.get("reason") or "not_specified" for r in refunds))
        report.bar(S, "Refund Status", Counter(r.get("status") for r in refunds))
    if disputes is not None:
        report.bar(S, "Dispute Reasons", Counter(d.get("reason") for d in disputes))
        report.bar(S, "Dispute Status", Counter(d.get("status") for d in disputes))
    if efws is not None:
        report.bar(S, "Fraud Warning Types", Counter(e.get("fraud_type") for e in efws))
    for name, items in (("Refunds", refunds), ("Disputes", disputes), ("Early fraud warnings", efws)):
        if items is None:
            report.unavailable(S, name)

    # -- Payouts --------------------------------------------------------------
    S = "Payouts"
    payouts = fetch_all("Payouts", client.v1.payouts, {"arrival_date": {"gte": start_ts, "lt": end_ts}})
    if payouts is None:
        report.unavailable(S, "Payouts")
    else:
        payouts = [p for p in payouts if in_cur(p)]
        report.line(S, "Payout Amount by Arrival Date", [
            ("Paid", daily_sum(payouts, "arrival_date", lambda p: major(p["amount"]), lambda p: p["status"] == "paid")),
            ("Pending / In transit", daily_sum(payouts, "arrival_date", lambda p: major(p["amount"]),
                                               lambda p: p["status"] in ("pending", "in_transit"))),
            ("Failed / Canceled", daily_sum(payouts, "arrival_date", lambda p: major(p["amount"]),
                                            lambda p: p["status"] in ("failed", "canceled"))),
        ], "money")
        report.bar(S, "Payout Status", Counter(p["status"] for p in payouts))
        report.bar(S, "Payout Methods", Counter(p.get("method") for p in payouts))
        summary["Paid Out"] = fmt_money(sum(major(p["amount"]) for p in payouts if p["status"] == "paid"))

    if other_currency:
        report.notes.append(
            f"Amounts are in {CURRENCY.upper()} (the account's settlement currency). Excluded "
            + ", ".join(f"{n:,} {c} object(s)" for c, n in other_currency.most_common()) + "."
        )


def fetch_balance():
    try:
        return client.v1.balance.retrieve().to_dict()
    except stripe.AuthenticationError as e:
        raise SystemExit(f"Stripe rejected STRIPE_API_KEY - check .env ({e.user_message or e})")
    except stripe.StripeError as e:
        print(f"  Stripe: balance unavailable ({type(e).__name__}) - skipping")
        return None


# ---------------------------------------------------------------------------
# Chart generation (matplotlib, Agg backend, CID-embedded PNG images)
# ---------------------------------------------------------------------------

PALETTE = ["#4A90D9", "#E8704A", "#50C878", "#9B59B6", "#F1C40F", "#1ABC9C", "#F39C12", "#EC7063"]


def chart_cid(spec) -> str:
    return "chart_" + spec["id"]


def chart_to_png(fig):
    """Renders a matplotlib figure to raw PNG bytes (not base64) - these get
    attached as inline MIMEImage/Content-ID parts rather than embedded as a
    data: URI, since Outlook desktop's Word-based rendering engine mishandles
    large base64 image data (crops it, shows a broken linked-image icon)."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    buf.seek(0)
    data = buf.read()
    plt.close(fig)
    native_w = struct.unpack(">I", data[16:20])[0]
    native_h = struct.unpack(">I", data[20:24])[0]
    return data, native_w, native_h


def axis_formatter(fmt):
    sym = cur_symbol()

    def money(x, _):
        a = abs(x)
        s = f"{a / 1e6:.1f}M" if a >= 1e6 else f"{a / 1e3:.1f}k" if a >= 1e4 else f"{a:,.0f}"
        return ("-" if x < 0 else "") + sym + s

    if fmt == "money":
        return money
    if fmt == "pct":
        return lambda x, _: f"{x:.0f}%"
    return lambda x, _: f"{int(x):,}"


def make_line_chart(spec, timestamps, days):
    fig, ax = plt.subplots(figsize=(7, 3))
    fig.patch.set_facecolor("#F8F9FA")
    ax.set_facecolor("#F8F9FA")

    for i, (label, vals) in enumerate(spec["series"]):
        ax.plot(timestamps, [float("nan") if v is None else v for v in vals], marker="o", markersize=3,
                label=label, color=PALETTE[i % len(PALETTE)], linewidth=2)

    ax.set_title(spec["title"], fontsize=13, fontweight="bold", pad=10)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=TIMEZONE))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, days // 8), tz=TIMEZONE))
    ax.tick_params(axis="x", rotation=30, labelsize=8)
    ax.tick_params(axis="y", labelsize=8)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(axis_formatter(spec["fmt"])))
    if spec["fmt"] == "pct":
        ax.set_ylim(0, 105)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.spines[["top", "right"]].set_visible(False)
    if len(spec["series"]) > 1:
        ax.legend(fontsize=8, framealpha=0.5)
    fig.tight_layout()
    return chart_to_png(fig)


def make_bar_chart(spec):
    """Horizontal bar chart of (label, value) rows."""
    labels = [r[0] for r in spec["rows"]]
    values = [r[1] for r in spec["rows"]]
    maxv = max(values) if values else 1

    fig, ax = plt.subplots(figsize=(7, max(2.5, len(labels) * 0.4 + 1)))
    fig.patch.set_facecolor("#F8F9FA")
    ax.set_facecolor("#F8F9FA")
    bars = ax.barh(labels, values, color=PALETTE[0], height=0.6)
    for bar, v in zip(bars, values):
        ax.text(bar.get_width() + maxv * 0.015, bar.get_y() + bar.get_height() / 2,
                fmt_value(v, spec["fmt"]), va="center", fontsize=8, color="#333")
    ax.set_ylim(max(len(labels), 3) - 0.5, -0.5)  # top-down order; keeps 1-2 bars from filling the plot
    ax.set_xlim(0, maxv * 1.18)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(axis_formatter(spec["fmt"])))
    ax.set_title(spec["title"], fontsize=13, fontweight="bold", pad=10)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", labelsize=8)
    ax.tick_params(axis="x", labelsize=8)
    fig.tight_layout()
    return chart_to_png(fig)


def img_tag(src, native_w, native_h, alt="chart", display_w=600):
    """Outlook-safe image tag wrapped in a 1x1 presentation table - explicit
    width/height attributes, not CSS, are required because Outlook's
    Word-based rendering engine mishandles CSS image sizing. `src` is a full
    src attribute value, e.g. "cid:chart_xxx" for the real email (resolved
    against an inline MIMEImage part) or a data: URI for local preview."""
    display_h = round(native_h * display_w / native_w)
    dw, dh = str(display_w), str(display_h)
    return (
        '<table width="' + dw + '" cellpadding="0" cellspacing="0" border="0" role="presentation" '
        'style="border:0;mso-table-lspace:0;mso-table-rspace:0;">'
        '<tr><td>'
        '<img src="' + src + '" alt="' + alt + '" '
        'width="' + dw + '" height="' + dh + '" '
        'style="display:block;border-radius:6px;">'
        '</td></tr></table>'
    )


def inline_images_for_preview(html, images):
    """Substitutes cid: references back to self-contained data: URIs for the
    local --preview save path, since cid: only resolves inside a real MIME
    container (an opened email) - a plain saved .html file needs the image
    bytes inline to be viewable standalone in a browser."""
    for cid, png_bytes in images.items():
        b64 = base64.b64encode(png_bytes).decode()
        html = html.replace(
            'src="cid:' + cid + '"',
            'src="data:image/png;base64,' + b64 + '"',
        )
    return html


def stat_box(label, value, color="#4A90D9", detail=None):
    return (
        '<div style="flex:1;min-width:140px;background:#fff;border-radius:8px;'
        'padding:16px 12px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.08);">'
        '<div style="font-size:22px;font-weight:700;color:' + color + ';">' + str(value) + '</div>'
        '<div style="font-size:11px;color:#666;margin-top:4px;">' + label + '</div>'
        + ('<div style="font-size:10px;color:#999;margin-top:2px;">' + detail + '</div>' if detail else '')
        + '</div>'
    )


# ---------------------------------------------------------------------------
# Email body (light theme, CID-embedded PNG charts - see img_tag/send_email)
# ---------------------------------------------------------------------------

def build_group_email_section(section_name, specs, pngs):
    """One section: a chart per spec with data, plus a compact 'no activity'
    note for specs that were empty all period (keeps the email from
    ballooning with flat-zero chart images)."""
    parts = [
        '<h2 style="margin:28px 0 10px;font-size:16px;color:#333;'
        'border-top:2px solid #f0f0f0;padding-top:20px;">' + section_name + '</h2>'
    ]
    no_activity, unavailable = [], []
    for spec in specs:
        if spec["kind"] == "note":
            unavailable.append(spec["title"])
        elif spec["id"] in pngs:
            png, w, h = pngs[spec["id"]]
            parts.append('<div style="margin-bottom:14px;">'
                         + img_tag("cid:" + chart_cid(spec), w, h, spec["title"], display_w=560) + '</div>')
        else:
            no_activity.append(spec["title"])
    if no_activity:
        parts.append('<p style="font-size:11px;color:#aaa;margin:6px 0 0;">No activity in this period: '
                     + ", ".join(no_activity) + '</p>')
    if unavailable:
        parts.append('<p style="font-size:11px;color:#c77;margin:6px 0 0;">Not readable with this API key: '
                     + ", ".join(unavailable) + '</p>')
    return "\n".join(parts)


def build_email_html(report_date, days, boxes, group_sections_html, notes, attach_name=None):
    """attach_name=None means no interactive attachment (the daily report)."""
    ts_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")
    notes_html = "".join(f'<p style="margin:16px 0 0;font-size:11px;color:#888;">{n}</p>' for n in notes)
    period = "Daily report" if days == 1 else f"Last {days} days"
    footer = f"Generated {ts_str} &bull; IMT Stripe Report &bull; {period}"
    if attach_name:
        footer += f" &bull; See attached {attach_name} for interactive version"

    lines = [
        "<!DOCTYPE html>",
        '<html xmlns:v="urn:schemas-microsoft-com:vml">',
        "<head>",
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>IMT Stripe Report | " + report_date + "</title>",
        "</head>",
        '<body style="margin:0;padding:0;background:#F0F2F5;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Arial,sans-serif;">',
        '<div style="max-width:700px;margin:32px auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1);">',
        '<!--[if gte mso 9]><v:rect xmlns:v="urn:schemas-microsoft-com:vml" fill="true" stroke="false" style="mso-width-percent:1000;"><v:fill type="gradient" color="#8B8CF6" color2="#4B45C6" angle="135"/><v:textbox style="mso-fit-shape-to-text:true" inset="0,0,0,0"><![endif]-->',
        '<div style="background-color:#8B8CF6;background:linear-gradient(135deg,#8B8CF6 0%,#4B45C6 100%);padding:28px 32px;">',
        '<div style="color:#fff;font-size:20px;font-weight:700;">IMT Stripe Report | ' + report_date + '</div>',
        "</div>",
        '<!--[if gte mso 9]></v:textbox></v:rect><![endif]-->',
        '<div style="padding:24px 32px;">',
        '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px;">',
        boxes,
        "</div>",
        group_sections_html,
        notes_html,
        '<p style="margin:32px 0 0;font-size:11px;color:#aaa;text-align:center;">',
        footer,
        "</p>",
        "</div>",
        "</div>",
        "</body>",
        "</html>",
    ]
    return "\n".join(l for l in lines if l)


# ---------------------------------------------------------------------------
# Interactive HTML attachment (Chart.js with hover tooltips)
# ---------------------------------------------------------------------------

def jlist(x):
    return json.dumps(x)


def build_interactive_html(report_date, days, report, day_labels, cards, notes):
    """Every spec, including zero-activity ones - this file isn't image-size
    constrained the way the email body is, so it stays fully comprehensive."""

    def line_cfg(spec):
        datasets = []
        single = len(spec["series"]) == 1
        for i, (label, vals) in enumerate(spec["series"]):
            color = PALETTE[i % len(PALETTE)]
            vals = [None if v is None else round(v, 2) for v in vals]
            datasets.append(
                f'{{label:{json.dumps(label)},data:{jlist(vals)},borderColor:"{color}",'
                f'backgroundColor:"{color}33",pointRadius:2,pointHoverRadius:6,tension:.3,'
                f'fill:{"true" if single else "false"},spanGaps:true}}'
            )
        legend = "false" if single else "true"
        return (
            f'<div class="chart-card"><h3>{spec["title"]}</h3>'
            f'<div class="chart-wrap"><canvas id="{spec["id"]}"></canvas></div></div>\n'
            f'<script>mk("{spec["id"]}","line",{jlist(day_labels)},[{",".join(datasets)}],'
            f'{{plugins:{{legend:{{display:{legend}}}}}}},"{spec["fmt"]}");</script>'
        )

    def bar_cfg(spec):
        if not spec["rows"]:
            return f'<div class="chart-card"><h3>{spec["title"]}</h3><p class="muted">No activity in this period</p></div>'
        labels = [r[0] for r in spec["rows"]]
        values = [round(r[1], 2) for r in spec["rows"]]
        return (
            f'<div class="chart-card"><h3>{spec["title"]}</h3>'
            f'<div class="chart-wrap"><canvas id="{spec["id"]}"></canvas></div></div>\n'
            f'<script>mk("{spec["id"]}","bar",{jlist(labels)},'
            f'[{{label:{json.dumps(spec["title"])},data:{jlist(values)},backgroundColor:"{PALETTE[0]}",borderRadius:4}}],'
            f'{{indexAxis:"y",plugins:{{legend:{{display:false}}}}}},"{spec["fmt"]}");</script>'
        )

    group_blocks = []
    for section_name, specs in report.sections.items():
        cards_html = []
        for spec in specs:
            if spec["kind"] == "line":
                cards_html.append(line_cfg(spec))
            elif spec["kind"] == "bar":
                cards_html.append(bar_cfg(spec))
            else:
                cards_html.append(f'<div class="chart-card"><h3>{spec["title"]}</h3>'
                                  f'<p class="muted">Not readable with this API key</p></div>')
        group_blocks.append(
            f'<div class="link-section"><h2>{section_name}</h2><div class="charts-grid">'
            + "\n".join(cards_html) + "</div></div>"
        )

    stat_html = '<div class="cards">' + "".join(
        f'<div class="card" style="border-left-color:{PALETTE[i % len(PALETTE)]}">'
        f'<label>{label}</label><div class="val">{value}</div></div>'
        for i, (label, value) in enumerate(cards)
    ) + '</div>'
    notes_html = "".join(f'<p class="muted" style="margin-top:16px">{n}</p>' for n in notes)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IMT Stripe Report | {report_date}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
const CUR = {json.dumps(cur_symbol())};
const FMT = {{
  int:   v => Math.round(v).toLocaleString(),
  money: v => (v < 0 ? '-' : '') + CUR + Math.abs(v).toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}}),
  pct:   v => v.toFixed(1) + '%'
}};
const BASE_OPTS = {{
  responsive: true, maintainAspectRatio: false,
  interaction: {{ mode: 'index', intersect: false }},
  plugins: {{ legend: {{ display: false, labels: {{ boxWidth: 12, font: {{size:11}} }} }},
    tooltip: {{ backgroundColor: '#111827', padding: 10, cornerRadius: 6,
               titleFont: {{size:12}}, bodyFont: {{size:12}} }} }},
  scales: {{
    x: {{ grid: {{ display: false }}, ticks: {{ color: '#6b7280', font: {{size:11}}, maxRotation: 45, autoSkip: true, maxTicksLimit: 10 }} }},
    y: {{ grid: {{ color: '#e5e7eb' }}, ticks: {{ color: '#6b7280', font: {{size:11}} }} }}
  }}
}};
function mk(id, type, labels, datasets, extra, fmt) {{
  extra = extra || {{}};
  const el = document.getElementById(id);
  if (!el) return;
  const f = FMT[fmt] || FMT.int;
  const opts = JSON.parse(JSON.stringify(BASE_OPTS));
  Object.assign(opts, extra);
  opts.scales = JSON.parse(JSON.stringify(BASE_OPTS.scales));
  if (extra.scales) Object.assign(opts.scales, extra.scales);
  opts.plugins = Object.assign(JSON.parse(JSON.stringify(BASE_OPTS.plugins)), extra.plugins || {{}});
  opts.plugins.legend = Object.assign({{}}, BASE_OPTS.plugins.legend, (extra.plugins || {{}}).legend);
  const valueAxis = extra.indexAxis === 'y' ? 'x' : 'y';
  opts.scales[valueAxis].ticks.callback = v => f(v);
  if (fmt === 'pct') {{ opts.scales[valueAxis].min = 0; opts.scales[valueAxis].max = 100; }}
  opts.plugins.tooltip.callbacks = {{
    label: ctx => ' ' + ctx.dataset.label + ': ' + (ctx.parsed[valueAxis] == null ? '—' : f(ctx.parsed[valueAxis]))
  }};
  new Chart(el, {{ type: type, data: {{ labels: labels, datasets: datasets }}, options: opts }});
}}
</script>
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#F0F2F5;color:#1a1a1a;padding:24px 16px}}
  .wrap{{max-width:1100px;margin:0 auto}}
  header{{background:linear-gradient(135deg,#8B8CF6 0%,#4B45C6 100%);color:#fff;border-radius:10px 10px 0 0;padding:28px 32px}}
  header h1{{font-size:22px}}
  header p{{margin-top:4px;color:#e6e6fd;font-size:13px}}
  .body{{background:#fff;border-radius:0 0 10px 10px;padding:24px 32px;margin-bottom:24px;box-shadow:0 1px 6px rgba(0,0,0,.08)}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}}
  .card{{background:#fff;border:1px solid #e5e7eb;border-left:3px solid;border-radius:8px;padding:14px 16px}}
  .card label{{font-size:.68rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#9ca3af}}
  .card .val{{font-size:1.3rem;font-weight:700;margin-top:4px;color:#111827}}
  .charts-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-bottom:8px}}
  .chart-card{{background:#F8F9FA;border-radius:10px;padding:16px 18px;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
  .chart-card h3{{font-size:13px;color:#333;margin-bottom:10px;font-weight:600}}
  .chart-wrap{{position:relative;height:260px}}
  .muted{{color:#888;font-size:12px}}
  .link-section{{margin-top:28px;padding-top:20px;border-top:2px solid #eee}}
  .link-section h2{{font-size:16px;color:#333;margin-bottom:12px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>IMT Stripe Report | {report_date}</h1>
    <p>Last {days} days &bull; Amounts in {CURRENCY.upper()} &bull; Hover over any chart for exact values</p>
  </header>
  <div class="body">
    {stat_html}
    {''.join(group_blocks)}
    {notes_html}
  </div>
</div>
</body>
</html>"""
    return page


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_email(subject, html_body, to_addresses, images=None, attachment_html=None, attachment_filename=None):
    """images: optional {cid: png_bytes} dict - each is attached as an inline
    MIMEImage part with a matching Content-ID, nested inside a multipart/related
    part alongside the HTML (mixed -> alternative -> related), so <img src="cid:...">
    references resolve reliably in every mail client including Windows desktop
    Outlook (which mishandles base64 data: URIs embedded directly in <img src>)."""
    sender = f"{FROM_NAME} <{FROM_ADDRESS}>"
    msg = email.mime.multipart.MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_addresses)

    body_part = email.mime.multipart.MIMEMultipart("alternative")
    if images:
        related = email.mime.multipart.MIMEMultipart("related")
        related.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))
        for cid, png_bytes in images.items():
            img = email.mime.image.MIMEImage(png_bytes, "png")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
            related.attach(img)
        body_part.attach(related)
    else:
        body_part.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))
    msg.attach(body_part)

    if attachment_html:
        att = email.mime.base.MIMEBase("text", "html")
        att.set_payload(attachment_html.encode("utf-8"))
        email.encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment",
                       filename=attachment_filename or "stripe_interactive_report.html")
        msg.attach(att)

    ses.send_email(
        FromEmailAddress=sender,
        Destination={"ToAddresses": to_addresses},
        Content={"Raw": {"Data": msg.as_bytes()}},
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    """Also resolves the report period into args.start_dt (midnight, TIMEZONE)
    and an exclusive args.end_dt."""
    p = argparse.ArgumentParser(description="Stripe metrics report emailed via AWS SES")
    period = p.add_mutually_exclusive_group()
    period.add_argument("--days", type=int, default=None,
                        help="Last N full days ending yesterday (overrides REPORT_DAYS)")
    period.add_argument("--date", metavar="YYYY-MM-DD", default=None,
                        help="Report a single day (condensed report)")
    period.add_argument("--range", nargs=2, metavar=("START", "END"), default=None,
                        help="Report an inclusive date range (full report)")
    p.add_argument("--preview", action="store_true", help="Save the report locally instead of emailing")
    p.add_argument("--to", nargs="+", default=None, help="Override recipients for this run")
    p.add_argument("--debug", action="store_true", help="Print a sample raw object from each Stripe endpoint")
    args = p.parse_args()

    def midnight(s):
        try:
            d = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            p.error(f"invalid date {s!r} - use YYYY-MM-DD")
        return datetime(d.year, d.month, d.day, tzinfo=TIMEZONE)

    today = datetime.now(TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    if args.date or args.range:
        start_s, end_s = (args.date, args.date) if args.date else args.range
        args.start_dt, args.end_dt = midnight(start_s), midnight(end_s) + timedelta(days=1)
        if args.end_dt <= args.start_dt:
            p.error("--range END is before START")
        if args.end_dt > today:
            p.error("report dates must be before today")
    else:
        days = args.days or REPORT_DAYS
        if days < 1:
            p.error("--days must be at least 1")
        args.end_dt = today
        args.start_dt = today - timedelta(days=days)
    return args


# Summary card order - cards whose source was unreadable are simply absent.
CARD_ORDER = [
    "Gross Volume", "Net Volume", "Stripe Fees", "Captures", "Auths", "Successful Payments", "Payment Success Rate",
    "Average Payment", "Refunded", "Disputes", "Paid Out", "Available Balance", "Pending Balance",
]

# Single-day report cards: (display label, summary key).
DAILY_CARDS = [
    ("Payouts", "Paid Out"), ("Captures", "Captures"), ("Auths", "Auths"),
    ("Refunds", "Refunded"), ("Fees", "Stripe Fees"), ("Disputes", "Disputes"),
]


def main():
    global DEBUG, client, CURRENCY
    args = parse_args()
    DEBUG = args.debug

    if not STRIPE_API_KEY:
        raise ValueError("STRIPE_API_KEY must be set in .env")
    client = stripe.StripeClient(STRIPE_API_KEY, max_network_retries=3)

    to_addresses = args.to or TO_ADDRESSES

    start_dt, end_dt = args.start_dt, args.end_dt
    days = (end_dt.date() - start_dt.date()).days
    start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())
    daily = days == 1
    if daily:
        report_date = start_dt.strftime("%b %d, %Y")
    else:
        report_date = start_dt.strftime("%b %d") + " - " + (end_dt - timedelta(days=1)).strftime("%b %d, %Y")
    day_list = [(start_dt + timedelta(days=i)).date() for i in range(days)]

    summary, details = {}, {}
    print("Fetching Stripe balance ...")
    bal = fetch_balance()
    if bal:
        avail = bal.get("available") or []
        if avail:
            CURRENCY = avail[0]["currency"].lower()
        summary["Available Balance"] = fmt_money(sum(major(b["amount"]) for b in avail if b["currency"] == CURRENCY))
        summary["Pending Balance"] = fmt_money(sum(major(b["amount"]) for b in bal.get("pending") or []
                                                   if b["currency"] == CURRENCY))
    print(f"Settlement currency: {CURRENCY.upper()}")

    print(f"Fetching Stripe objects ({days}-day window) ...")
    report = Report(day_list)
    collect(report, start_ts, end_ts, summary, details)

    print("Building charts ...")
    timestamps = [datetime(d.year, d.month, d.day, tzinfo=TIMEZONE) for d in day_list]
    pngs = {}
    for specs in report.sections.values():
        for spec in specs:
            if not spec["has_data"]:
                continue
            if spec["kind"] == "line":
                pngs[spec["id"]] = make_line_chart(spec, timestamps, days)
            elif spec["kind"] == "bar":
                pngs[spec["id"]] = make_bar_chart(spec)

    specs_by_id = {s["id"]: s for specs in report.sections.values() for s in specs}
    email_images = {chart_cid(specs_by_id[sid]): png for sid, (png, w, h) in pngs.items()}

    card_keys = DAILY_CARDS if daily else [(k, k) for k in CARD_ORDER]
    cards = [(label, summary[k], details.get(k)) for label, k in card_keys if k in summary]
    boxes = "".join(stat_box(label, value, PALETTE[i % len(PALETTE)], detail)
                    for i, (label, value, detail) in enumerate(cards))

    group_sections_html = "".join(
        build_group_email_section(name, specs, pngs) for name, specs in report.sections.items()
    )

    attach_name = "IMT_Stripe_" + (end_dt - timedelta(days=1)).strftime("%Y%m%d") + ".html"

    print("Composing email ...")
    html_body = build_email_html(report_date, days, boxes, group_sections_html, report.notes,
                                 None if daily else attach_name)
    interactive_html = None if daily else build_interactive_html(
        report_date, days, report, [d.strftime("%b %d") for d in day_list],
        [(label, value) for label, value, _ in cards], report.notes
    )

    subject = "IMT Stripe Report | " + report_date

    if args.preview or not FROM_ADDRESS or not to_addresses:
        if not (FROM_ADDRESS and to_addresses) and not args.preview:
            print("FROM_ADDRESS or TO_ADDRESSES not set in .env - saving locally instead of sending.")
        if interactive_html:
            with open(attach_name, "w", encoding="utf-8") as f:
                f.write(interactive_html)
            print(f"Saved interactive report to {attach_name}")
        with open(attach_name.replace(".html", "_email.html"), "w", encoding="utf-8") as f:
            f.write(inline_images_for_preview(html_body, email_images))
        print(f"Saved email body preview to {attach_name.replace('.html', '_email.html')}")
    else:
        print(f"Sending to {', '.join(to_addresses)} ...")
        send_email(subject, html_body, to_addresses, images=email_images,
                   attachment_html=interactive_html, attachment_filename=attach_name)
        print("Done! Report sent successfully.")


if __name__ == "__main__":
    main()
