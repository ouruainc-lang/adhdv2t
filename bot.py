import os
import logging
import asyncio
import tempfile
import math
import requests
import stripe
from pathlib import Path
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler
import google.generativeai as genai
from dotenv import load_dotenv

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import database

# Load environment variables
load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
STRIPE_PAYMENT_LINK = os.getenv('STRIPE_PAYMENT_LINK')
STRIPE_CUSTOMER_PORTAL = os.getenv('STRIPE_CUSTOMER_PORTAL') # Optional: Direct link if no API usage

# Configure Stripe
stripe.api_key = STRIPE_SECRET_KEY

# Configure Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configure Gemini
genai.configure(api_key=GOOGLE_API_KEY)

# Initialize Database
database.init_db()

# --- CONSTANTS ---
FREE_TIER_MINUTES = 5.0
PRO_TIER_MINUTES = 300.0

# --- HELPER FUNCTIONS ---



def sync_to_todoist(token, tasks_text, title=None):
    if not token: return False
    try:
        url = "https://api.todoist.com/rest/v2/tasks"
        headers = {"Authorization": f"Bearer {token}"}
        
        # 1. Create Parent Task
        if not title:
            title = datetime.now().strftime("Voice Note - %Y-%m-%d %H:%M")
            
        parent_data = {
            "content": title,
            "due_string": "today"
        }
        resp = requests.post(url, headers=headers, json=parent_data)
        if resp.status_code != 200:
            logger.error(f"Todoist Parent Task Error: {resp.text}")
            return False
            
        parent_id = resp.json().get("id")
        
        # 2. Create Subtasks
        lines = tasks_text.split('\n')
        for line in lines:
            line = line.strip()
            if not line: continue
            
            # STRICT FILTER: Only process lines that look like list items
            if not (line.startswith("-") or line.startswith("*")):
                continue
            
            # Cleanup bullets/checkboxes for cleaner tasks
            clean_content = line
            if line.startswith("- [ ] "): clean_content = line[6:]
            elif line.startswith("- [x] "): clean_content = line[6:]
            elif line.startswith("- "): clean_content = line[2:]
            elif line.startswith("* "): clean_content = line[2:]
            
            # Remove bold markdown asterisks
            clean_content = clean_content.replace("*", "").strip()
            
            sub_data = {
                "content": clean_content,
                "parent_id": parent_id
            }
            requests.post(url, headers=headers, json=sub_data)
            
        return True
    except Exception as e:
        logger.error(f"Todoist Sync Error: {e}")
        return False

def sync_to_notion(token, parent_id, tasks_text, title=None):
    if not token or not parent_id: return False
    try:
        # Create a new Page inside the parent page
        url = "https://api.notion.com/v1/pages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }
        
        # Default title if not provided
        if not title:
            title = datetime.now().strftime("Voice Note - %Y-%m-%d %H:%M")

        # Parse text into Notion Blocks
        blocks = []
        lines = tasks_text.split('\n')
        
        for line in lines:
            line = line.strip()
            if not line: continue
            
            # Detect Checkboxes/List Items
            # Matches: "- [ ] task", "- [x] task", "- task", "* task"
            # We strip the marker to get clean content
            content = line
            is_todo = False
            is_checked = False
            
            if line.startswith("- [ ] "):
                content = line[6:]
                is_todo = True
                is_checked = False
            elif line.startswith("- [x] "):
                content = line[6:]
                is_todo = True
                is_checked = True
            elif line.startswith("- "):
                content = line[2:]
                is_todo = True
            elif line.startswith("* "):
                content = line[2:]
                is_todo = True
            
            # CLEANUP: Remove bold markdown (*) from content for Notion
            content = content.replace("*", "").strip()
            
            # Construct Block
            if is_todo:
                blocks.append({
                    "object": "block",
                    "type": "to_do",
                    "to_do": {
                        "rich_text": [{"type": "text", "text": {"content": content}}],
                        "checked": is_checked
                    }
                })
            else:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": line}}]
                    }
                })

        data = {
            "parent": {"page_id": parent_id},
            "properties": {
                "title": [
                    {
                        "text": {
                            "content": title
                        }
                    }
                ]
            },
            "children": blocks
        }
        
        resp = requests.post(url, headers=headers, json=data)
        if resp.status_code != 200:
            logger.error(f"Notion Error {resp.status_code}: {resp.text}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Notion Sync Error: {e}")
        return False

# --- COMMANDS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_name = update.effective_user.first_name
    
    # Init user
    database.check_user_status(user_id)
    
    msg = (
        f"Hello {user_name}! 👋\n\n"
        "I am your ADHD Voice-to-Task Assistant.\n"
        "Just talk to me, and I'll organize your life.\n\n"
        f"👇 *Available Commands:*\n"
        "/help - Integration Setup Guide 📘\n"
        "/settings - Configure Timezone & Integrations (Todoist/Notion)\n"
        "/status - Check your usage and plan\n"
        "/managesub - Manage your subscription\n\n"
        f"💎 *Pro Plan ($5.99/mo):*\n"
        "• 300 Minutes/mo\n"
        "• Daily Digest\n"
        "• Todoist & Notion Sync\n\n"
        f"🆓 *Free Tier:*\n"
        "• 5 Minutes/mo\n"
        "• Daily Digest\n"
        "• Todoist & Notion Sync\n\n"
        "📧 *Support:* ouruainc@gmail.com"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = database.check_user_status(user_id)
    
    plan = user_data.get('plan_tier', 'free').upper()
    used = user_data.get('minutes_used', 0.0)
    limit = PRO_TIER_MINUTES if plan == 'PRO' else FREE_TIER_MINUTES
    
    msg = (
        f"📊 *Usage Stats*\n"
        f"Plan: {plan}\n"
        f"Usage: {used:.1f} / {limit} minutes\n\n"
        f"🔗 *Integrations:*\n"
        f"Todoist: {'✅' if user_data.get('todoist_token') else '❌'}\n"
        f"Notion: {'✅' if user_data.get('notion_token') else '❌'}\n\n"
        "Use /managesub to upgrade or change plan."
    )
    await update.message.reply_markdown(msg)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📘 *Integration Setup Guide*\n\n"
        "*Notion Setup:*\n"
        "1. Go to [My Integrations](https://www.notion.so/my-integrations) & create one.\n"
        "2. Copy 'Internal Secret' -> `/set_notion <token>`.\n"
        "3. **Crucial:** Open your Notion Page -> '...' (top right) -> Connections -> Add your interaction.\n"
        "4. Copy Page ID from URL -> `/set_notion_page <id>`.\n\n"
        "*Todoist Setup:*\n"
        "1. Go to [Todoist Settings](https://todoist.com/app/settings/integrations/developer).\n"
        "2. Copy 'API token' -> `/set_todoist <token>`.\n\n"
        "*Timezone:*\n"
        "Use `/set_timezone <Region/City>` (e.g. Asia/Singapore) to set your local time for the Daily Digest.\n\n"
        "📧 *Support:* Any issues? Email us at ouruainc@gmail.com"
    )
    await update.message.reply_markdown(msg)

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚙️ *Settings*\n\n"
        "To configure integrations, send commands like this:\n\n"
        "*Set Todoist Token:*\n"
        "`/set_todoist <your_token>`\n\n"
        "*Set Notion Token:*\n"
        "`/set_notion <your_token>`\n\n"
        "*Set Notion Page ID:*\n"
        "`/set_notion_page <page_id>`\n\n"
        "*Set Digest Time (24h format):*\n"
        "`/set_digest 18:00`\n\n"
        "*Set Timezone:*\n"
        "`/set_timezone Asia/Singapore`"
    )
    await update.message.reply_markdown(msg)

async def manage_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = database.check_user_status(user_id)
    stripe_id = user_data.get('stripe_customer_id')
    
    if not stripe_id:
        # Not a customer yet, send payment link
        await update.message.reply_text(
            f"You don't have an active subscription record.\n\n"
            f"Upgrade here: {STRIPE_PAYMENT_LINK}?client_reference_id={user_id}"
        )
        return

    try:
        # Create Portal Session
        session = stripe.billing_portal.Session.create(
            customer=stripe_id,
            return_url=f"https://t.me/{context.bot.username}"
        )
        await update.message.reply_text(
            f"Manage your subscription here:\n{session.url}"
        )
    except Exception as e:
        logger.error(f"Stripe Portal Error: {e}")
        await update.message.reply_text("Could not generate portal link. Please check /status.")

# --- SETTINGS HANDLERS ---
async def set_todoist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = context.args[0] if context.args else None
    if not token:
        await update.message.reply_text("Usage: /set_todoist <token>")
        return
    database.update_user(update.effective_user.id, todoist_token=token)
    await update.message.reply_text("✅ Todoist token saved!")

async def set_notion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = context.args[0] if context.args else None
    if not token:
        await update.message.reply_text("Usage: /set_notion <token>")
        return
    database.update_user(update.effective_user.id, notion_token=token)
    await update.message.reply_text("✅ Notion token saved! Now set the page ID too.")

async def set_notion_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page_id = context.args[0] if context.args else None
    if not page_id:
        await update.message.reply_text("Usage: /set_notion_page <page_id>")
        return
    database.update_user(update.effective_user.id, notion_page_id=page_id)
    await update.message.reply_text("✅ Notion page ID saved!")

async def set_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = context.args[0] if context.args else None
    # Validate format HH:MM
    try:
        datetime.strptime(time_str, "%H:%M")
        database.update_user(update.effective_user.id, digest_time=time_str)
        await update.message.reply_text(f"✅ Daily Digest set for {time_str}!")
    except:
        await update.message.reply_text("Invalid format. Use HH:MM (e.g., 18:00)")

async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz_str = context.args[0] if context.args else None
    if not tz_str:
        await update.message.reply_text("Usage: /set_timezone <Area/City>\nExample: /set_timezone Asia/Singapore")
        return
    
    try:
        import pytz
        pytz.timezone(tz_str) # Validate
        database.update_user(update.effective_user.id, timezone=tz_str)
        await update.message.reply_text(f"✅ Timezone set to {tz_str}!")
    except Exception:
        await update.message.reply_text("❌ Invalid timezone. Try 'US/Pacific', 'UTC', 'Asia/Singapore', etc.")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = database.check_user_status(user_id)
    
    # 1. Check Limits
    plan = user_data.get('plan_tier', 'free')
    used = user_data.get('minutes_used', 0.0)
    limit = PRO_TIER_MINUTES if plan == 'pro' else FREE_TIER_MINUTES
    
    if used >= limit:
        await update.message.reply_text(
            f"🚫 *Limit Reached*\n"
            f"You have used {used:.1f}/{limit} minutes.\n"
            f"Upgrade to Pro for 300 minutes: /managesub"
        )
        return

    status_msg = await update.message.reply_text("🎧 Processing...")

    ogg_path = None

    try:
        # 2. Download & Measure Duration
        # Optimization: Use Telegram metadata for duration (seconds) -> No ffmpeg needed!
        voice = update.message.voice
        duration_seconds = voice.duration if voice.duration else 0
        duration_mins = duration_seconds / 60.0
        
        # Check limit again with new duration
        if used + duration_mins > limit:
             await status_msg.edit_text(f"❌ This voice note ({duration_mins:.1f}m) is too long for your remaining quota.\n\nUpgrade to send more: /managesub")
             return

        # Download file to /tmp (Vercel Requirement for Write Access)
        # Note: python-telegram-bot's download_to_drive handles path
        voice_file = await voice.get_file()
        ogg_path = f"/tmp/voice_{user_id}_{int(duration_seconds)}.oga"
        await voice_file.download_to_drive(ogg_path)
        
        # 3. Gemini Process (Direct OGG Upload)
        # Gemini supports audio/ogg (Opus)
        gemini_file = genai.upload_file(ogg_path, mime_type="audio/ogg")
        model = genai.GenerativeModel("gemini-3-flash-preview")
        
        prompt = (
            "Extract actionable tasks, deadlines, and events from this audio. "
            "Format them as a clean Markdown checklist using single asterisks (*) for bold text instead of double asterisks (**). "
            "Ignore filler phrases."
        )
        response = model.generate_content([prompt, gemini_file])
        
        if response.text:
            content = response.text
            
            # 5. Update Usage & Save Task
            new_total = used + duration_mins
            database.update_user(user_id, minutes_used=new_total)
            database.add_task(user_id, content)
            
            # 6. Integrations
            sync_msg = []
            
            # Create a shared title for the new page/task
            # e.g. "Voice Note - January 12, 03:00 PM"
            page_title = datetime.now().strftime("Voice Note - %B %d, %I:%M %p")

            if user_data.get('todoist_token'):
                if sync_to_todoist(user_data['todoist_token'], content, title=page_title):
                    sync_msg.append("Todoist ✅")
                else:
                    sync_msg.append("Todoist ❌")
            
            if user_data.get('notion_token') and user_data.get('notion_page_id'):
                # Use the same title
                if sync_to_notion(user_data['notion_token'], user_data['notion_page_id'], content, title=page_title):
                    sync_msg.append("Notion ✅")
                else:
                    sync_msg.append("Notion ❌")

            footer_sync = " | ".join(sync_msg)
            if footer_sync: footer_sync = f"\nSync: {footer_sync}"
            
            # Header with Date/Time
            now_fmt = datetime.now().strftime("%B %d, %Y - %I:%M %p")
            header = f"📝 *Voice Note Tasks*\n📅 {now_fmt}\n\n"
            
            footer = f"\n\n---\nUsage: {new_total:.1f}/{limit} min | Status: {plan.title()}{footer_sync}"
            
            await status_msg.edit_text("✅ Done!")
            await update.message.reply_markdown(header + content + footer)
            
        else:
            await status_msg.edit_text("⚠️ No tasks found.")

    except Exception as e:
        logger.error(f"Error: {e}")
        await status_msg.edit_text("❌ Error processing request.")
        
    finally:
        if ogg_path and os.path.exists(ogg_path): os.remove(ogg_path)

# --- DAILY DIGEST ---
async def daily_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Runs periodically to check if digest needs to be sent for any user.
    """
    import pytz
    
    # 1. Get consistent UTC "Now"
    utc_now = datetime.now(pytz.utc)
    
    users = database.get_all_users()
    
    for user in users:
        try:
            # 2. Convert UTC to User's Timezone
            user_tz_str = user.get('timezone', 'UTC')
            if not user_tz_str: user_tz_str = 'UTC'
            
            user_tz = pytz.timezone(user_tz_str)
            user_local_time = utc_now.astimezone(user_tz)
            
            # 3. Check if formatted HH:MM matches preference
            user_time_str = user_local_time.strftime("%H:%M")
            target_time = user.get('digest_time', '18:00')
            
            if user_time_str == target_time:
                tasks = database.get_unsent_tasks(user['user_id'])
                if tasks:
                    # Compile Digest
                    digest_body = f"🌅 *Your Daily Digest*\n\n"
                    for t in tasks:
                        digest_body += f"• {t['task_content'][:100]}...\n" # Truncated preview
                    
                    digest_body += "\nCheck your task manager for details!"
                    
                    try:
                        await context.bot.send_message(chat_id=user['user_id'], text=digest_body, parse_mode='Markdown')
                        # Mark sent
                        task_ids = [t['id'] for t in tasks]
                        database.mark_tasks_sent(task_ids)
                    except Exception as e:
                        logger.error(f"Failed to send digest to {user['user_id']}: {e}")
        except Exception as e:
            logger.error(f"Digest error for user {user.get('user_id')}: {e}")



# --- GLOBAL ---
application = None

async def post_init(application: ApplicationBuilder):
    await application.bot.set_my_commands([
        ('start', 'Start the bot'),
        ('help', 'Integration Setup Guide 📘'),
        ('status', 'Check usage and plan'),
        ('settings', 'Settings Overview'),
        ('managesub', 'Manage Subscription'),
        ('set_notion', 'Set Notion integration token'),
        ('set_notion_page', 'Set Notion page ID'),
        ('set_todoist', 'Set Todoist API token'),
        ('set_digest', 'Set Daily Digest time (HH:MM)'),
        ('set_timezone', 'Set Timezone (e.g. Asia/Singapore)')
    ])

def create_app():
    global application
    if not TELEGRAM_TOKEN:
        print("❌ Error: TELEGRAM_TOKEN not found in .env")
        return None

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('status', status))
    application.add_handler(CommandHandler('settings', settings))
    application.add_handler(CommandHandler('managesub', manage_sub))
    
    # Settings sub-commands
    application.add_handler(CommandHandler('set_todoist', set_todoist))
    application.add_handler(CommandHandler('set_notion', set_notion))
    application.add_handler(CommandHandler('set_notion_page', set_notion_page))
    application.add_handler(CommandHandler('set_digest', set_digest))
    application.add_handler(CommandHandler('set_timezone', set_timezone))
    
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    return application

# Initialize immediately for import usage
application = create_app()

if __name__ == '__main__':
    # Startup Checks
    if not GOOGLE_API_KEY:
        print("❌ Error: GOOGLE_API_KEY not found in .env")
        exit(1)
    if not STRIPE_SECRET_KEY:
        print("❌ Error: STRIPE_SECRET_KEY not found in .env")
        exit(1)

    # Local Polling Mode
    print("🤖 Bot is running in POLLING mode...")
    
    # Local Scheduler for Digest (Only for local polling)
    # In Vercel, this is replaced by the /cron/digest endpoint
    job_queue = application.job_queue
    job_queue.run_repeating(daily_digest_job, interval=60, first=10)
    
    application.run_polling()
