# YouTube Subscription Transfer Bot

A production Telegram bot that lets users bulk transfer their YouTube channel subscriptions from one account to another. Users authenticate with Google, upload their Google Takeout subscriptions file, pay for a plan through PayPal, and the bot subscribes their new account to every channel automatically, with live progress updates and a final summary.

Built as a real, deployed product with paying users, integrating four external services (Telegram, Google/YouTube, Google OAuth2, and PayPal) behind a single Flask webhook backend backed by PostgreSQL.

## What it does

Migrating hundreds of YouTube subscriptions to a new account by hand is slow and tedious. This bot automates the whole flow end to end:

1. The user starts the bot on Telegram and signs in to their new YouTube account through Google OAuth2.
2. They follow a short tutorial to export their old subscriptions from Google Takeout, then upload the ZIP or CSV file.
3. The bot parses the file, counts the channels, and offers tiered paid plans.
4. After the user pays through PayPal, the bot subscribes their new account to each channel through the YouTube Data API, showing progress at 25, 50, 75, and 100 percent.
5. The user can pull a summary of successful, failed, and already-subscribed channels at any time.

## Key features

- **End-to-end automation** across four external services with no manual steps for the user.
- **Google OAuth2 sign-in** with secure state handling and automatic access-token refresh when tokens expire.
- **PayPal payments** with tiered plans, a payment lifecycle (pending, completed, cancelled), and enforcement so users cannot transfer more channels than their plan allows.
- **Robust file processing** that accepts both the Google Takeout ZIP and a raw CSV, validates the format, enforces a size limit, and cleans up uploaded files afterwards.
- **Live progress updates** edited into a single Telegram message during the transfer.
- **API rate-limit throttling** with a deliberate delay between subscription calls to stay within YouTube Data API limits.
- **Resilient transfers** that categorise each channel as successful, failed, or already subscribed, and persist a resumable successful-count so a user's remaining capacity is respected across runs.
- **Persistent state** in PostgreSQL across users, channels, OAuth states, payments, and transfer summaries.

## Architecture

The app runs as a Flask web service that receives Telegram updates through a webhook, rather than long polling, so it can be deployed as a standard web process.

- **Flask** exposes the webhook, the OAuth callback, the PayPal callback and cancel routes, and a health check.
- **python-telegram-bot** handles commands, inline keyboards, callback queries, and file uploads.
- An **asyncio event loop** runs in a daemon thread, and incoming updates are placed on a queue and processed by a background worker, keeping the webhook responsive.
- **PostgreSQL** (via psycopg2) stores all state, accessed through a context-managed cursor that commits or rolls back automatically.
- **Google OAuth2** and the **YouTube Data API v3** handle authentication and the subscription inserts.
- **PayPal REST SDK** handles payment creation and execution.

```
Telegram  ->  Flask /webhook  ->  update queue  ->  async worker  ->  command handlers
                                                                          |
                        Google OAuth2  <----  /callback  <---------------- |
                        PayPal         <----  /paypal_callback  <--------- |
                        YouTube Data API  <---- subscription inserts  <---- |
                        PostgreSQL  <----  state for every step  <-------- |
```

## Tech stack

Python, Flask, python-telegram-bot, Google OAuth2, YouTube Data API v3, PayPal REST SDK, PostgreSQL (psycopg2), asyncio, Gunicorn. Deployed on Railway.

## Command reference

| Command | Purpose |
| --- | --- |
| `/start` | Welcome message and entry point |
| `/signin` | Authenticate the target YouTube account through Google |
| `/tutorial` | How to export subscriptions from Google Takeout |
| `/upload` | Upload the Takeout ZIP or CSV of subscriptions |
| `/selectplan` | Choose a paid plan and pay through PayPal |
| `/starttransfer` | Run the subscription transfer with live progress |
| `/summary` | View successful, failed, and already-subscribed counts |

## Running it yourself

This is a real deployed service, so running your own copy requires credentials for each integrated platform.

1. Clone the repository and install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Create a `.env` file based on `.env.example` and fill in your own values:
   - `TELEGRAM_BOT_TOKEN` from BotFather
   - `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` from a Google Cloud OAuth client, with the YouTube Data API enabled
   - `REDIRECT_URI` pointing at your deployed `/callback` route
   - `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, and `PAYPAL_MODE` (`sandbox` or `live`)
   - `DATABASE_URL` for your PostgreSQL instance
   - `APP_URL`, the public base URL of your deployment
3. Deploy as a web service. The included `Procfile` runs the app under Gunicorn:
   ```
   web: gunicorn -w 1 -b 0.0.0.0:$PORT app:app
   ```
   The database tables are created automatically on first start, and the Telegram webhook is registered on startup.

Note: the app uses a single worker by design, because it maintains one shared asyncio event loop and in-process update queue.

## Deployment (Railway)

This bot was deployed on [Railway](https://railway.app) as a web service. The flow was:

1. Create a new Railway project from the repository. Railway detects the Python app and installs from `requirements.txt`.
2. Add a **PostgreSQL** plugin. Railway provisions the database and exposes a `DATABASE_URL`, which the app reads directly.
3. Set the remaining environment variables (from `.env.example`) in the Railway project settings: the Telegram, Google, and PayPal credentials, plus `APP_URL` and `REDIRECT_URI` pointing at the public Railway domain.
4. Railway runs the process defined in the `Procfile`:
   ```
   web: gunicorn -w 1 -b 0.0.0.0:$PORT app:app
   ```
5. On startup the app creates its database tables and registers the Telegram webhook against `APP_URL/webhook`, so no manual webhook setup is needed.

Because Telegram, Google OAuth, and PayPal all call back into the service over HTTPS, deploying to a public URL (rather than local polling) was necessary, which is why the app is built around a Flask webhook instead of long polling.

## A note on the PayPal SDK

This project uses `paypalrestsdk`, PayPal's classic REST SDK. It is functional but has since been deprecated by PayPal in favour of their newer Server SDK and v2 Orders API. A current redeployment would migrate the payment layer to the newer API. The rest of the stack (Telegram, Google OAuth2, YouTube Data API, PostgreSQL) remains current.

## Notes on security

No secrets are stored in the code. Every credential is read from environment variables and validated on startup, and the app exits immediately if any required variable is missing. Uploaded files are removed after parsing.

## Status

This bot was deployed and used by real end users. It is shared here as a portfolio project to demonstrate production integration work across authentication, payments, third-party APIs, and persistent state.
