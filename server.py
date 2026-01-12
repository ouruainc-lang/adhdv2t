import os
import stripe
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import database

# Load environment variables
load_dotenv()

# Setup Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
endpoint_secret = os.getenv('STRIPE_WEBHOOK_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

import asyncio
from flask import Flask, request, jsonify
from telegram import Update
import bot  # Import the bot module

# Setup Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
endpoint_secret = os.getenv('STRIPE_WEBHOOK_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CRON_SECRET = os.getenv('CRON_SECRET', 'my-secret-key')

app = Flask(__name__)

# --- TELEGRAM WEBHOOK ---
@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    """
    Receives updates from Telegram via Webhook.
    """
    # Create a fresh app instance for this request to avoid "Event loop is closed"
    # because asyncio.run() creates a new loop each time.
    ptb_app = bot.create_app()
    if not ptb_app:
        return jsonify(error="Bot config missing"), 500

    # Decouple update from the global bot if possible, or just use the fresh one
    update = Update.de_json(request.get_json(force=True), ptb_app.bot)
    
    async def process_update_async():
        await ptb_app.initialize()
        await ptb_app.process_update(update)
        await ptb_app.shutdown()

    asyncio.run(process_update_async())
    
    return jsonify(status="ok")

# --- CRON JOB (For Vercel) ---
@app.route('/cron/digest', methods=['GET'])
def cron_digest():
    """
    Triggered by Vercel Cron every minute/hour
    to send daily digests.
    """
    # Simple security check
    auth_header = request.headers.get('Authorization')
    if auth_header != f"Bearer {CRON_SECRET}":
        return jsonify(error="Unauthorized"), 401
    
    if not bot.application:
        return jsonify(error="Bot not initialized"), 500

    # Run the digest job manually
    # We pass a dummy context or modify the job to not need one, 
    # but since daily_digest_job uses `context.bot`, we can mock it or access bot directly.
    # Refactor: daily_digest_job currently takes (context).
    # We probably need to refactor daily_digest_job to accept 'bot' instead or wrap it.
    
    # Let's inspect bot.py's daily_digest_job signature again
    # It takes context. We can create a simple object to mimic it.
    
    class MockContext:
        def __init__(self, bot_instance):
            self.bot = bot_instance
            
    ctx = MockContext(bot.application.bot)
    
    # Create fresh app
    ptb_app = bot.create_app()
    if not ptb_app:
        return jsonify(error="Bot config missing"), 500

    ctx = MockContext(ptb_app.bot)
    
    try:
        async def run_digest():
            await ptb_app.initialize()
            await bot.daily_digest_job(ctx)
            await ptb_app.shutdown()

        asyncio.run(run_digest())
        return jsonify(status="digest run complete")
    except Exception as e:
        return jsonify(error=str(e)), 500

def notify_user(user_id, message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": user_id, "text": message, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"Failed to notify user {user_id}: {e}")

# Configure basic logging to file
import logging
logging.basicConfig(filename='server.log', level=logging.INFO, format='%(asctime)s %(message)s')

@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError as e:
        logging.error("Invalid payload")
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError as e:
        logging.error("Invalid signature")
        return 'Invalid signature', 400

    # Type handling
    event_type = event['type']
    data = event['data']['object']
    logging.info(f"Received event: {event_type}")
    
    # 1. New Subscription / Checkout
    if event_type == 'checkout.session.completed':
        client_reference_id = data.get('client_reference_id')
        customer_id = data.get('customer')
        logging.info(f"Checkout completed. Ref: {client_reference_id}, Cust: {customer_id}")
        
        if client_reference_id:
            # Upgrade user to PRO
            database.update_user(
                client_reference_id, 
                plan_tier='pro', 
                stripe_customer_id=customer_id
            )
            logging.info(f"User {client_reference_id} upgraded to PRO.")
            
            # Notify
            msg = (
                "🎉 *Congratulations!*\n\n"
                "Your *Pro Plan* is active.\n"
                "✅ 300 Minutes/month\n"
                "✅ Daily Digest at 6 PM\n"
                "✅ Todoist & Notion Sync\n\n"
                "Use `/settings` to configure your features!"
            )
            notify_user(client_reference_id, msg)
        else:
            logging.warning("Missing client_reference_id in checkout session.")

    # 2. Subscription Deleted (Expired/Cancelled)
    elif event_type == 'customer.subscription.deleted':
        customer_id = data.get('customer')
        logging.info(f"Subscription deleted for customer: {customer_id}")
        
        user = database.get_user_by_stripe_id(customer_id)
        
        if user:
            logging.info(f"Found user {user['user_id']} for customer {customer_id}. Downgrading...")
            database.update_user(user['user_id'], plan_tier='free')
            msg = (
                "📉 *Subscription Ended*\n\n"
                "Your subscription has expired or was cancelled.\n"
                "You have been downgraded to the *Free Tier* (5 mins/mo).\n"
                "Use `/managesub` to resubscribe at any time."
            )
            notify_user(user['user_id'], msg)
        else:
            logging.warning(f"No user found for customer_id: {customer_id}")

    # 3. Subscription Updated (e.g. Cancel at period end)
    elif event_type == 'customer.subscription.updated':
        customer_id = data.get('customer')
        cancel_at_period_end = data.get('cancel_at_period_end')
        cancel_at = data.get('cancel_at')
        
        logging.info(f"Subscription updated for {customer_id}. CancelAtPeriodEnd: {cancel_at_period_end}, CancelAt: {cancel_at}")
        
        # Check either the boolean flag OR if a specific cancellation date is set
        is_cancelling = cancel_at_period_end or cancel_at
        
        # CHECK PREVIOUS ATTRIBUTES TO PREVENT SPAM
        # Only notify if this specific event CHANGED the status to cancelling
        prev_attrs = event['data'].get('previous_attributes', {})
        was_already_cancelling = prev_attrs.get('cancel_at_period_end') or prev_attrs.get('cancel_at')
        
        # If we are cancelling NOW, and we weren't cancelling BEFORE in this update diff
        # (Or if it's a fresh update that sets it)
        # Actually safer: Check if 'cancel_at_period_end' or 'cancel_at' is IN previous_attributes
        
        should_notify = False
        if is_cancelling:
             # If 'cancel_at_period_end' changed to True
             if 'cancel_at_period_end' in prev_attrs and cancel_at_period_end:
                 should_notify = True
             # OR if 'cancel_at' changed (check if key exists in diff)
             elif 'cancel_at' in prev_attrs and cancel_at:
                 should_notify = True

        if should_notify:
            user = database.get_user_by_stripe_id(customer_id)
            if user:
                # Determine the end date
                end_ts = data.get('current_period_end')
                if cancel_at:
                    end_ts = cancel_at # Prefer the explicit cancel date if set
                
                end_date = "the end of the billing period"
                if end_ts:
                    from datetime import datetime
                    end_date = datetime.fromtimestamp(end_ts).strftime('%Y-%m-%d')
                
                msg = (
                    f"⚠️ *Subscription Cancellation Scheduled*\n\n"
                    f"Your access will remain Pro until {end_date}.\n"
                    "After that, you will be downgraded to the Free Tier."
                )
                notify_user(user['user_id'], msg)
            else:
                logging.warning(f"No user found for customer_id: {customer_id}")

    return jsonify(success=True)

if __name__ == '__main__':
    # Ensure DB is initialized
    database.init_db()
    print("Starting Flask Webhook Server on port 4242...")
    app.run(port=4242)
