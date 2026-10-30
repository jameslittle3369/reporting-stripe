# IMT — Stripe Metrics Report

Pulls **every reportable metric** from the connected Stripe account (balance
activity, payments, payment intents, refunds, disputes, fraud warnings,
payouts), charts them
grouped into sections, builds summary cards, and sends an HTML email with an
interactive Chart.js attachment via Amazon SES.

Stripe has no "metrics" endpoint, so every series is derived from Stripe's
list endpoints over the lookback window, bucketed by day in `TIMEZONE`.
Related series share one chart (e.g. gross / net / fees / refunds).

**Daily mode** (`REPORT_DAYS=1`, the default): reports yesterday with six
summary cards and the breakdown bar charts — no trend lines and no
interactive attachment. Any window longer than one day (`--days 30`) produces
the full report with trend charts and the attachment.

---

## Files

| File | Purpose |
|---|---|
| `stripe_report.py` | Main script — fetches Stripe data, builds charts, sends email |
| `.env` | Configuration (API key, recipients, lookback window) — not committed |
| `.env.example` | Template for `.env` |
| `requirements.txt` | Python dependencies (official `stripe` SDK, boto3, matplotlib, ...) |

---

## Setup

### 1. Create the venv and install dependencies (Python 3.14)

```powershell
py -3.14 -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
```

### 2. Create a Stripe restricted API key

**Stripe Dashboard → Developers → API keys → Create restricted key**, with
**Read** access to:

- Balance, Balance transactions
- Charges, PaymentIntents, Refunds, Disputes
- Payouts
- Radar → Early fraud warnings

A scope you leave out isn't fatal: that section just shows "Not readable with
this API key". A test-mode key (`rk_test_...`) works for trying things out.

### 3. Configure AWS credentials (for SES)

Add your IAM credentials to `C:\Users\James\.aws\credentials`, or set
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` directly in `.env`.

### 4. Configure `.env`

```powershell
copy .env.example .env
```

Fill in `STRIPE_API_KEY`, `FROM_ADDRESS` and `TO_ADDRESSES` (comma-separated).

---

## Running Manually

```powershell
.venv\Scripts\python.exe stripe_report.py
```

Useful flags:

```powershell
# Save the report locally instead of emailing it (good for testing)
.venv\Scripts\python.exe stripe_report.py --preview

# Print a sample raw object from each Stripe endpoint
.venv\Scripts\python.exe stripe_report.py --preview --debug

# Override the lookback window for this run (full report with trends)
.venv\Scripts\python.exe stripe_report.py --days 30

# Report one specific day (condensed daily report) - e.g. to backfill a missed run
.venv\Scripts\python.exe stripe_report.py --date 2026-10-28

# Report an inclusive date range (full report; a one-day range is condensed)
.venv\Scripts\python.exe stripe_report.py --range 2026-10-01 2026-10-28

# Override recipients for this run
.venv\Scripts\python.exe stripe_report.py --to ops@example.com ceo@example.com
```

If `FROM_ADDRESS` or `TO_ADDRESSES` are blank, the script saves the
interactive report and the email body locally instead of sending.

---

## Scheduling (Windows Task Scheduler)

1. **Task Scheduler** → **Create Basic Task**, trigger **Daily**, e.g. 7:00 AM (reports the previous day)
2. Action **Start a Program**:
   - **Program:** `D:\projects\reporting\Stripe\.venv\Scripts\python.exe`
   - **Arguments:** `stripe_report.py`
   - **Start in:** `D:\projects\reporting\Stripe`
3. Under **General**, check **Run whether user is logged on or not**

---

## What's reported

**Daily summary cards:**

| Card | Source |
|---|---|
| Payouts | Payouts arriving yesterday with status `paid` |
| Captures | Charge balance transactions created yesterday — everything captured yesterday, including auths from earlier days |
| Auths | Total authorized amount of yesterday's authorizations, excluding ones released without capture (captured auths count at their full authorized amount). Detail line: auth count, captured (amount actually captured), pending capture, released |
| Refunds | Refund balance transactions created yesterday |
| Fees | Per-transaction Stripe fees on yesterday's balance transactions |
| Disputes | Disputes opened yesterday (count and amount) |

**Multi-day summary cards:** Gross Volume, Net Volume, Stripe Fees, Captures,
Auths, Successful Payments, Payment Success Rate, Average Payment, Refunded,
Disputes, Paid Out, Available Balance, Pending Balance.

A card is left out when its source couldn't be read.

| Section | Charts |
|---|---|
| Revenue | Gross / Net / Fees / Refunds (daily) · Volume by transaction type · Fees by type |
| Payments | Successful vs failed · Success rate % · Average payment · Payment methods · Card brands · Card countries · Digital wallets · Outcomes · Decline reasons · Radar risk levels |
| Payment Intents | Created vs succeeded / canceled / incomplete · Status · Cancellation reasons · Last payment errors |
| Refunds & Disputes | Refund / dispute / early-fraud-warning counts · Refunded vs disputed amount · Refund reasons & status · Dispute reasons & status · Fraud warning types |
| Payouts | Payout amount by arrival date (paid / pending / failed) · Status · Method |

In daily mode only the breakdown (bar) charts are included; the daily trend
lines appear only in multi-day reports.

**Email body:** light theme, one PNG chart per metric group with activity;
empty ones are listed in a compact "No activity in this period" note.

**Interactive attachment** (`IMT_Stripe_YYYYMMDD.html`, multi-day reports
only): every chart, including empty ones, with hover tooltips.

---

## Notes

- **Currency:** amounts are reported in the account's settlement currency
  (taken from the Balance). Objects in other currencies are excluded and
  counted in a footnote.
- **Fees:** the "Fees" line is the per-transaction fee on balance
  transactions. Standalone Stripe fee debits (Billing, Radar, and so on) are
  counted in Net Volume and "Volume by transaction type".
- **Rate limits:** the Stripe SDK retries 429s/network errors
  (`max_network_retries=3`).
