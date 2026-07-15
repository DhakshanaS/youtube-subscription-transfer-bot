import os
import asyncio
import logging
import zipfile
import csv
import io
import sys
import os.path
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from contextlib import contextmanager
import threading

from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import psycopg2
import paypalrestsdk

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIRECT_URI = os.getenv("REDIRECT_URI")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox")
APP_URL = os.getenv("APP_URL")

# Validate required environment variables
required_vars = {
    "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
    "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
    "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
    "DATABASE_URL": DATABASE_URL,
    "REDIRECT_URI": REDIRECT_URI,
    "PAYPAL_CLIENT_ID": PAYPAL_CLIENT_ID,
    "PAYPAL_CLIENT_SECRET": PAYPAL_CLIENT_SECRET,
    "APP_URL": APP_URL
}
for name, value in required_vars.items():
    if not value:
        logger.error(f"{name} environment variable not set")
        sys.exit(1)

# Google OAuth 2.0 configuration
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
CLIENT_CONFIG = {
    "web": {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uris": [REDIRECT_URI],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token"
    }
}

# Configure PayPal SDK
paypalrestsdk.configure({
    "mode": PAYPAL_MODE,
    "client_id": PAYPAL_CLIENT_ID,
    "client_secret": PAYPAL_CLIENT_SECRET
})

# Flask app setup
app = Flask(__name__)
app.bot = None  # Initialized later
update_queue = asyncio.Queue()
application = None  # Global application object

# Create and set the global event loop
global_loop = asyncio.new_event_loop()
asyncio.set_event_loop(global_loop)

# Database helpers
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

@contextmanager
def db_cursor():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        cursor.close()
        conn.close()

def init_db():
    with db_cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                google_id VARCHAR(255),
                access_token TEXT,
                refresh_token TEXT,
                token_expiry TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS channels (
                telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
                channel_ids TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
                state VARCHAR(255) PRIMARY KEY,
                telegram_id BIGINT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
                payment_id VARCHAR(255),
                status VARCHAR(50),
                amount VARCHAR(10),
                max_channels INTEGER,
                successful_count INTEGER DEFAULT 0,
                payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS transfer_summaries (
                telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
                successful TEXT,
                failed TEXT,
                already_subscribed TEXT,
                transfer_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
    logger.info("Database initialized")

# Setup Telegram bot application
async def ensure_webhook_set(bot):
    desired_url = f"{APP_URL}/webhook"
    webhook_info = await bot.get_webhook_info()
    if webhook_info.url != desired_url or webhook_info.allowed_updates != ["message", "callback_query"]:
        # Set webhook with explicit allowed updates including callback_query
        await bot.set_webhook(url=desired_url, allowed_updates=["message", "callback_query"])
        logger.info(f"Webhook set to {desired_url} with allowed_updates=['message', 'callback_query']")
    else:
        logger.info("Webhook already set correctly with required allowed_updates")

def setup_application():
    global application
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("signin", signin))
    application.add_handler(CommandHandler("tutorial", tutorial))
    application.add_handler(CommandHandler("upload", upload))
    application.add_handler(CommandHandler("selectplan", selectplan))
    application.add_handler(CallbackQueryHandler(handle_plan_selection))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    application.add_handler(CommandHandler("starttransfer", starttransfer))
    application.add_handler(CommandHandler("summary", summary))
    
    global_loop.run_until_complete(application.initialize())
    global_loop.run_until_complete(application.start())
    global_loop.run_until_complete(ensure_webhook_set(application.bot))
    app.bot = application.bot
    task = global_loop.create_task(update_processor(application))
    logger.info("Update processor task scheduled")
    app.update_processor_task = task

def run_loop():
    init_db()
    setup_application()
    try:
        global_loop.run_forever()
        # For local testing, comment out above and uncomment below for polling
        # global_loop.run_until_complete(application.run_polling())
    except Exception as e:
        logger.error(f"Event loop crashed: {e}")
        global_loop.stop()

# Start the event loop in a daemon thread
thread = threading.Thread(target=run_loop, daemon=True)
thread.start()

# Health check endpoint
@app.route('/')
def health_check():
    return "OK", 200

# Webhook endpoint
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        logger.info("Received POST request from Telegram")
        update_data = request.get_json()
        logger.info(f"Raw update data: {update_data}")
        update = Update.de_json(update_data, app.bot)
        if update:
            logger.info(f"Update queued: {update.update_id}, Type: {'callback_query' if update.callback_query else 'message'}")
            asyncio.run_coroutine_threadsafe(update_queue.put(update), global_loop)
            return 'OK'
        else:
            logger.error("Failed to parse update from JSON")
            return 'Invalid update', 400
    logger.warning("Non-POST request received")
    return 'Method not allowed', 405

# OAuth callback endpoint
@app.route("/callback")
def oauth_callback():
    state = request.args.get("state")
    code = request.args.get("code")
    with db_cursor() as cursor:
        cursor.execute("SELECT telegram_id FROM oauth_states WHERE state = %s", (state,))
        result = cursor.fetchone()
        if not result:
            return "Error: Invalid state", 400
        telegram_id = result[0]
        cursor.execute("DELETE FROM oauth_states WHERE state = %s", (state,))
    flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES, redirect_uri=REDIRECT_URI)
    flow.fetch_token(code=code)
    credentials = flow.credentials
    token_expiry = datetime.fromtimestamp(credentials.expiry.timestamp()) if credentials.expiry else None

    with db_cursor() as cursor:
        cursor.execute("""
            INSERT INTO users (telegram_id, google_id, access_token, refresh_token, token_expiry)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (telegram_id) DO UPDATE
            SET google_id = EXCLUDED.google_id,
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                token_expiry = EXCLUDED.token_expiry;
        """, (telegram_id, credentials.id_token, credentials.token, credentials.refresh_token, token_expiry))
    asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=telegram_id, text="Signed in successfully!✅. Use /tutorial to continue."),
        global_loop
    )
    return "Authentication successful!✅ \nYou can close this window and return to Telegram."

# PayPal payment callback endpoint
@app.route("/paypal_callback")
def paypal_callback():
    telegram_id = request.args.get("telegram_id")
    payment_id = request.args.get("paymentId")
    payer_id = request.args.get("PayerID")
    
    try:
        payment = paypalrestsdk.Payment.find(payment_id)
        logger.info(f"Payment found: ID={payment_id}, State={payment.state}")
        if payment.execute({"payer_id": payer_id}):
            with db_cursor() as cursor:
                cursor.execute("""
                    UPDATE payments
                    SET status = %s
                    WHERE telegram_id = %s AND payment_id = %s
                """, ("completed", telegram_id, payment_id))
            asyncio.run_coroutine_threadsafe(
                app.bot.send_message(chat_id=telegram_id, text="Payment successful!✅ Use /starttransfer to begin transferring your subscriptions."),
                global_loop
            )
            return "Payment successful!✅ \nReturn to Telegram."
        else:
            logger.error(f"Payment execution failed: {payment.error}")
            asyncio.run_coroutine_threadsafe(
                app.bot.send_message(chat_id=telegram_id, text=f"Payment failed: {payment.error.get('message', 'Unknown error')}"),
                global_loop
            )
            return "Payment failed.", 400
    except paypalrestsdk.exceptions.ResourceNotFound:
        logger.error(f"Payment ID {payment_id} not found")
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=telegram_id, text="Payment not found. Please try again or contact @YTTransferBotSupport."),
            global_loop
        )
        return "Payment not found. Please return to Telegram.", 404
    except Exception as e:
        logger.error(f"Error in paypal_callback: {str(e)}", exc_info=True)
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=telegram_id, text="Error processing payment. Please try again or contact @YTTransferBotSupport."),
            global_loop
        )
        return "Error processing payment.", 500

# PayPal cancel endpoint
@app.route("/cancel")
def paypal_cancel():
    telegram_id = request.args.get("telegram_id")
    with db_cursor() as cursor:
        cursor.execute("""
            UPDATE payments
            SET status = %s
            WHERE telegram_id = %s
        """, ("canceled", telegram_id))
    asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=telegram_id, text="Payment cancelled❌. Return to Telegram and try again with /selectplan."),
        global_loop
    )
    return "Payment cancelled❌. Return to Telegram and try again with /selectplan."

# Helper function: check if payment is completed
async def is_payment_completed(telegram_id: int) -> bool:
    with db_cursor() as cursor:
        cursor.execute("SELECT status FROM payments WHERE telegram_id = %s", (telegram_id,))
        payment = cursor.fetchone()
        return payment is not None and payment[0] == "completed"

# Decorator for commands that require no completed payment
def require_no_payment(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        telegram_id = update.effective_user.id
        if await is_payment_completed(telegram_id):
            await update.message.reply_text("You’ve already paid for a transfer. Use /starttransfer to proceed.")
            return
        await func(update, context)
    return wrapper

# Telegram Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    if await is_payment_completed(telegram_id):
        await update.message.reply_text("You’ve already paid for a transfer. Use /starttransfer to proceed.")
        return
    user_first = update.effective_user.first_name
    welcome_message = f"Hello, {user_first}! Welcome to the YouTube Subscription Transfer Bot.\nUse /signin to authenticate with Google."
    await update.message.reply_text(welcome_message)

@require_no_payment
async def signin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES, redirect_uri=REDIRECT_URI)
    auth_url, state = flow.authorization_url(prompt="consent")
    with db_cursor() as cursor:
        cursor.execute("INSERT INTO oauth_states (state, telegram_id) VALUES (%s, %s)", (state, telegram_id))
    keyboard = [[InlineKeyboardButton("Sign in with Google", url=auth_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Please click the button below to sign in with Google:\nCan't Sign in? @YTTransferBotSupport\nNote: New YouTube Account.", reply_markup=reply_markup)

async def tutorial(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    instructions = (
        "Here's how to export your YouTube subscriptions:\n"
        "1. Watch this video: https://youtu.be/f9so492w3Dk\n"
        "2. Go to https://takeout.google.com\n"
        "3. Select 'YouTube and YouTube Music' > 'Subscriptions'\n"
        "4. Export and download the ZIP file.\n\n"
        "Then use /upload to send the file."
    )
    await update.message.reply_text(instructions)

@require_no_payment
async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    with db_cursor() as cursor:
        cursor.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
        user = cursor.fetchone()
    if not user:
        await update.message.reply_text("Please sign in with Google first using /signin.")
        return
    await update.message.reply_text("Please upload your Google Takeout file (ZIP or CSV) containing your YouTube subscriptions.")

async def process_file(file, telegram_id: int, context: ContextTypes.DEFAULT_TYPE) -> Tuple[Optional[List[str]], Optional[str]]:
    file_name = file.file_name
    if not (file_name.lower().endswith(".zip") or file_name.lower().endswith(".csv")):
        return None, "Please upload a ZIP or CSV file."

    if file.file_size and file.file_size > 10 * 1024 * 1024:
        return None, "File too large. Please upload a file smaller than 10MB."

    uploads_dir = "uploads"
    os.makedirs(uploads_dir, exist_ok=True)
    file_path = os.path.join(uploads_dir, f"{telegram_id}_{file_name}")

    file_obj = await file.get_file()
    await file_obj.download_to_drive(file_path)
    logger.info(f"File saved to: {file_path}")
    
    channel_ids: List[str] = []
    try:
        if file_name.lower().endswith(".zip"):
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                for info in zip_ref.namelist():
                    if "subscriptions.csv" in info.lower():
                        with zip_ref.open(info) as csv_file:
                            csv_content = csv_file.read().decode("utf-8")
                            reader = csv.DictReader(io.StringIO(csv_content))
                            if "Channel Id" not in reader.fieldnames:
                                os.remove(file_path)
                                return None, "Invalid CSV format: 'Channel Id' column not found❌."
                            channel_ids = [row["Channel Id"] for row in reader]
                        break
                else:
                    os.remove(file_path)
                    return None, "No 'subscriptions.csv' found in the ZIP file❌."
        elif file_name.lower().endswith(".csv"):
            with open(file_path, newline='', encoding="utf-8") as csv_file:
                reader = csv.DictReader(csv_file)
                if "Channel Id" not in reader.fieldnames:
                    os.remove(file_path)
                    return None, "Invalid CSV format: 'Channel Id' column not found❌."
                channel_ids = [row["Channel Id"] for row in reader]
    except Exception as e:
        os.remove(file_path)
        logger.error(f"Error processing file: {e}")
        return None, f"Error processing file: {e}"
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
    
    return channel_ids, None

@require_no_payment
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    file = update.message.document
    if not file:
        await update.message.reply_text("Please upload a valid file.")
        return
    wait_message = await update.message.reply_text("Please wait, processing your file...⏳")
    chat_id = update.message.chat_id
    message_id = wait_message.message_id

    channel_ids, error = await process_file(file, telegram_id, context)
    if error:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=error)
        return
    if not channel_ids:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="No channels found in the file❌.")
        return

    channel_ids_str = ",".join(channel_ids)
    with db_cursor() as cursor:
        cursor.execute("""
            INSERT INTO channels (telegram_id, channel_ids)
            VALUES (%s, %s)
            ON CONFLICT (telegram_id) DO UPDATE SET channel_ids = EXCLUDED.channel_ids;
        """, (telegram_id, channel_ids_str))
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"Found {len(channel_ids)} channels✅. You are ready for transfer. Use /selectplan to continue.")

@require_no_payment
async def selectplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    logger.info(f"User {telegram_id} issued /selectplan")
    with db_cursor() as cursor:
        cursor.execute("SELECT channel_ids FROM channels WHERE telegram_id = %s", (telegram_id,))
        result = cursor.fetchone()
    if not result:
        await update.message.reply_text("No channels found❌. Please upload your file with /upload first.")
        return
    channel_ids = result[0].split(",")
    channel_count = len(channel_ids)
    if channel_count == 0:
        await update.message.reply_text("No channels found in your uploaded file❌.")
        return
    keyboard = [
        [InlineKeyboardButton("$7.99 - Transfer up to 250 channels", callback_data="plan_250")],
        [InlineKeyboardButton("$9.99 - Transfer up to 500 channels", callback_data="plan_500")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(f"You have {channel_count} channels to transfer.\nSelect a plan to continue:", reply_markup=reply_markup)

async def handle_plan_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    logger.info(f"Callback query received: {query.data} from user {query.from_user.id}")
    await query.answer()
    plan = query.data
    telegram_id = query.from_user.id
    logger.info(f"User {telegram_id} selected plan: {plan}")
    if plan == "plan_250":
        amount = "7.99"
        max_channels = 250
    elif plan == "plan_500":
        amount = "9.99"
        max_channels = 500
    else:
        logger.warning(f"Invalid plan selected by user {telegram_id}: {plan}")
        await query.edit_message_text("Invalid plan selected.")
        return

    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT channel_ids FROM channels WHERE telegram_id = %s", (telegram_id,))
            result = cursor.fetchone()
        logger.info(f"Database query result for user {telegram_id}: {result}")
        if not result:
            logger.info(f"No channels found for user {telegram_id}")
            await query.edit_message_text("No channels found❌. Please upload your file with /upload first.")
            return
        channel_ids = result[0].split(",")
        logger.info(f"User {telegram_id} has {len(channel_ids)} channels")
        if len(channel_ids) > max_channels:
            await query.edit_message_text(f"Your {len(channel_ids)} channels exceed the {max_channels} limit for this plan. Please select a higher plan or reduce your channels.")
            return

        payment = paypalrestsdk.Payment({
            "intent": "sale",
            "payer": {"payment_method": "paypal"},
            "transactions": [{
                "amount": {"total": amount, "currency": "USD"},
                "description": f"Transfer up to {max_channels} YouTube subscriptions"
            }],
            "redirect_urls": {
                "return_url": f"{APP_URL}/paypal_callback?telegram_id={telegram_id}",
                "cancel_url": f"{APP_URL}/cancel?telegram_id={telegram_id}"
            }
        })
        logger.info(f"Creating PayPal payment for user {telegram_id}")
        if payment.create():
            approval_url = next(link.href for link in payment.links if link.rel == "approval_url")
            logger.info(f"Payment created successfully for user {telegram_id}, approval URL: {approval_url}")
            keyboard = [[InlineKeyboardButton("Pay with PayPal", url=approval_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(f"You selected the ${amount} plan for {max_channels} channels.\nClick below to complete payment:", reply_markup=reply_markup)
            with db_cursor() as cursor:
                cursor.execute("""
                    INSERT INTO payments (telegram_id, payment_id, status, amount, max_channels)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (telegram_id) DO UPDATE SET payment_id = EXCLUDED.payment_id, status = EXCLUDED.status, amount = EXCLUDED.amount, max_channels = EXCLUDED.max_channels;
                """, (telegram_id, payment.id, "pending", amount, max_channels))
                logger.info(f"Payment record updated for user {telegram_id}, payment ID: {payment.id}")
        else:
            logger.error(f"Payment creation failed for user {telegram_id}: {payment.error}")
            await query.edit_message_text("Failed to initiate payment. Please try again later.")
    except Exception as e:
        logger.error(f"Error in handle_plan_selection for user {telegram_id}: {e}", exc_info=True)
        await query.edit_message_text("An error occurred. Please try again later or contact @YTTransferBotSupport.")

async def starttransfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    with db_cursor() as cursor:
        cursor.execute("SELECT status, max_channels, successful_count FROM payments WHERE telegram_id = %s", (telegram_id,))
        payment = cursor.fetchone()
        if not payment or payment[0] != "completed":
            await update.message.reply_text("🔵Please complete payment first using /selectplan.")
            return
        max_channels, successful_count = payment[1], payment[2] or 0
        cursor.execute("SELECT access_token, refresh_token, token_expiry FROM users WHERE telegram_id = %s", (telegram_id,))
        user = cursor.fetchone()
        cursor.execute("SELECT channel_ids FROM channels WHERE telegram_id = %s", (telegram_id,))
        channel_result = cursor.fetchone()
    if not user or not channel_result:
        await update.message.reply_text("Missing data. Please sign in with /signin and upload a file with /upload.")
        return

    channel_ids = channel_result[0].split(",")
    remaining_capacity = max_channels - successful_count
    if remaining_capacity <= 0:
        await update.message.reply_text("🔴Your max limit reached.")
        return
    channel_ids = channel_ids[:remaining_capacity]

    wait_message = await update.message.reply_text("Transferring 0% ⏳")
    chat_id = update.message.chat_id
    message_id = wait_message.message_id

    if not channel_ids:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="No channels to transfer.")
        return

    credentials = Credentials(
        token=user[0],
        refresh_token=user[1],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET
    )
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        with db_cursor() as cursor:
            cursor.execute("UPDATE users SET access_token = %s, token_expiry = %s WHERE telegram_id = %s", 
                           (credentials.token, datetime.fromtimestamp(credentials.expiry.timestamp()), telegram_id))
    
    youtube = build("youtube", "v3", credentials=credentials)

    successful = []
    failed = []
    already_subscribed = []
    total_channels = len(channel_ids)
    
    for i, channel_id in enumerate(channel_ids, 1):
        try:
            youtube.subscriptions().insert(
                part="snippet",
                body={
                    "snippet": {
                        "resourceId": {
                            "kind": "youtube#channel",
                            "channelId": channel_id.strip()
                        }
                    }
                }
            ).execute()
            successful.append(channel_id)
        except HttpError as e:
            error_reason = e.error_details[0]["reason"] if e.error_details else "unknown"
            if error_reason == "subscriptionDuplicate":
                already_subscribed.append(channel_id)
            else:
                failed.append(channel_id)
            logger.error(f"Error subscribing to {channel_id}: {e}")
        
        progress = (i / total_channels) * 100
        if progress >= 25 and i == int(total_channels * 0.25) + 1:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="Transferring 25% ⏳")
        elif progress >= 50 and i == int(total_channels * 0.5) + 1:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="Transferring 50% ⏳")
        elif progress >= 75 and i == int(total_channels * 0.75) + 1:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="Transferring 75% ⏳")

        await asyncio.sleep(0.6)  # Throttle to stay within API limits
    
    new_successful_count = successful_count + len(successful)
    with db_cursor() as cursor:
        cursor.execute("UPDATE payments SET successful_count = %s WHERE telegram_id = %s", (new_successful_count, telegram_id))
        cursor.execute("""
            INSERT INTO transfer_summaries (telegram_id, successful, failed, already_subscribed)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (telegram_id) DO UPDATE
            SET successful = EXCLUDED.successful,
                failed = EXCLUDED.failed,
                already_subscribed = EXCLUDED.already_subscribed,
                transfer_date = CURRENT_TIMESTAMP
        """, (
            telegram_id,
            ",".join(successful) if successful else "",
            ",".join(failed) if failed else "",
            ",".join(already_subscribed) if already_subscribed else ""
        ))
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=f"Transfers completed! ✅ Successful: {len(successful)}/{total_channels}\nUse /summary for details."
    )

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    with db_cursor() as cursor:
        cursor.execute("SELECT status FROM payments WHERE telegram_id = %s", (telegram_id,))
        payment = cursor.fetchone()
        if not payment or payment[0] != "completed":
            await update.message.reply_text("🔵Please complete payment using /selectplan to use this command.")
            return
        cursor.execute("""
            SELECT successful, failed, already_subscribed, transfer_date
            FROM transfer_summaries
            WHERE telegram_id = %s
        """, (telegram_id,))
        result = cursor.fetchone()
    if not result:
        await update.message.reply_text("No transfer results found. Please run /starttransfer first.")
        return
    successful_list = result[0].split(",") if result[0] else []
    failed_list = result[1].split(",") if result[1] else []
    already_list = result[2].split(",") if result[2] else []
    transfer_date = result[3]
    if datetime.now() - transfer_date > timedelta(days=30):
        await update.message.reply_text("Your transfer results have expired (older than 30 days). Run /starttransfer to generate new results.")
        return
    message = (
        "Transfer Summary:\n"
        f"✅ Successful subscriptions: {len(successful_list)}\n"
        f"❌ Failed subscriptions: {len(failed_list)}\n"
        f"⚠️ Already subscribed channels: {len(already_list)}\n"
        "Contact: @YTTransferBotSupport"
    )
    await update.message.reply_text(message)

async def update_processor(application: Application):
    while True:
        update = await update_queue.get()
        logger.info(f"Processing update {update.update_id}, Type: {'callback_query' if update.callback_query else 'message'}")
        try:
            await application.process_update(update)
            logger.info(f"Update {update.update_id} processed successfully")
        except Exception as e:
            logger.error(f"Error processing update {update.update_id}: {e}", exc_info=True)

if __name__ == "__main__":
    app.run()