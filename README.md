# PayLess Mobile — Stripe Metrics Report

Pulls **every reportable metric** from the connected Stripe account (balance
activity, payments, payment intents, refunds, disputes, fraud warnings,
payouts, customers, subscriptions/MRR, invoices, Checkout), charts them
grouped into sections, builds summary cards, and sends an HTML email with an
interactive Chart.js attachment via Amazon SES.

Stripe has no "metrics" endpoint, so every series is derived from Stripe's
list endpoints over the lookback window, bucketed by day in `TIMEZONE`.
Related series share one chart (e.g. gross / net / fees / refunds).

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
- Customers
- Subscriptions, Invoices, Products
- Checkout Sessions
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

# Override the lookback window for this run
.venv\Scripts\python.exe stripe_report.py --days 7

# Override recipients for this run
.venv\Scripts\python.exe stripe_report.py --to ops@example.com ceo@example.com
```

If `FROM_ADDRESS` or `TO_ADDRESSES` are blank, the script saves the
interactive report and the email body locally instead of sending.

---

## Scheduling (Windows Task Scheduler)

1. **Task Scheduler** → **Create Basic Task**, trigger e.g. **Weekly**, Monday 7:00 AM
2. Action **Start a Program**:
   - **Program:** `D:\projects\reporting\Stripe\.venv\Scripts\python.exe`
   - **Arguments:** `stripe_report.py`
   - **Start in:** `D:\projects\reporting\Stripe`
3. Under **General**, check **Run whether user is logged on or not**

---

## What's reported

**Summary cards:** Gross Volume, Net Volume, Stripe Fees, Successful Payments,
Payment Success Rate, Average Payment, Refunded, Disputes, New Customers,
Active Subscriptions, MRR, Open Invoices, Checkout Conversion, Paid Out,
Available Balance, Pending Balance. A card is left out when its source
couldn't be read.

| Section | Charts |
|---|---|
| Revenue | Gross / Net / Fees / Refunds (daily) · Volume by transaction type · Fees by type |
| Payments | Successful vs failed · Success rate % · Average payment · Payment methods · Card brands · Card countries · Digital wallets · Outcomes · Decline reasons · Radar risk levels |
| Payment Intents | Created vs succeeded / canceled / incomplete · Status · Cancellation reasons · Last payment errors |
| Refunds & Disputes | Refund / dispute / early-fraud-warning counts · Refunded vs disputed amount · Refund reasons & status · Dispute reasons & status · Fraud warning types |
| Payouts | Payout amount by arrival date (paid / pending / failed) · Status · Method |
| Customers | New per day · Cumulative · By country |
| Subscriptions | New vs canceled vs ended · Active (and in-trial) count · MRR · Status · Top plans · Cancellation reasons |
| Invoices | Created vs paid · Due / paid / remaining amounts · Status · Billing reasons · Collection methods · Top products by invoiced revenue |
| Checkout | Completed / expired / open sessions · Conversion rate · Modes · Payment status |

**Email body:** light theme, one PNG chart per metric group with activity;
empty ones are listed in a compact "No activity in this period" note.

**Interactive attachment** (`PayLess_Stripe_YYYYMMDD.html`): every chart,
including empty ones, with hover tooltips.

---

## Notes

- **Currency:** amounts are reported in the account's settlement currency
  (taken from the Balance). Objects in other currencies are excluded and
  counted in a footnote.
- **Fees:** the "Fees" line is the per-transaction fee on balance
  transactions. Standalone Stripe fee debits (Billing, Radar, and so on) are
  counted in Net Volume and "Volume by transaction type".
- **MRR** is rebuilt for each day from subscription start/end dates at each
  subscription's *current* prices. It includes licensed (non-metered) items,
  leaves out discounts and trial periods, and normalizes yearly, weekly and
  daily intervals to a monthly amount. Treat it as an estimate, not Stripe
  Billing's official MRR figure.
- **Subscriptions** are fetched in full (not just the window) to rebuild the
  active-count trend, so large accounts take longer on that step.
- **Rate limits:** the Stripe SDK retries 429s/network errors
  (`max_network_retries=3`).
