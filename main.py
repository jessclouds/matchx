from typing import Final
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

import os
from dotenv import load_dotenv

load_dotenv()   # reads .env and loads it into the environment

TOKEN: Final = os.getenv("BOT_TOKEN")
BOT_USERNAME: Final = "@HackathonMatchBot"

if not TOKEN:
    raise RuntimeError("BOT_TOKEN missing — did you create .env?")

EVENTS = {
    "ideate2026": "IDEATE 2026",
    "healthhack2026": "HealthHack 2026"
}

SCHOOLS = [
    "NUS", "NTU", "SMU",
    "SUTD", "SIT", "SUSS",
    "Polytechnic", "Junior College", "Other",
]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args: # Check if there are any arguments passed with the /start command
        user_event_code = context.args[0]
        if user_event_code in EVENTS:
            event_name = EVENTS[user_event_code]
            await update.message.reply_text(f'Welcome to {event_name}! How can I assist you today?')
        else:
            await update.message.reply_text('Sorry, I do not recognize that event code. Please use a valid event code to start the bot.')
    else:
        await update.message.reply_text('Hello! I am your Hackathon Match Bot. How can I assist you today?')

    

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('You can use the following commands:\n'
                                    '/start - Start the bot\n'
                                    '/help - Show this help message\n'
                                    '/match - Find a hackathon match for you')

async def match_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('Finding a hackathon match for you...') 


async def school_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask the user to select their school from a list of options."""
    keyboard = []
    row = []

    for school in SCHOOLS:
        row.append(InlineKeyboardButton(school, callback_data=f'school_{school}'))
        if len(row) == 3:  # Create a new row after every 3 buttons
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)  # Add the last row if it has any buttons

    await update.message.reply_text(
        'Which school are you from?',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_school_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # Acknowledge the callback query
    school = query.data.split('_', 1)[1]  # Extract the school name from the callback
    context.user_data['school'] = school  # Store the selected school in user data
    await query.edit_message_text(text=f'You selected: {school}. Thank you!')

# Responses

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.lower()
    if 'hello' in text or 'hi' in text:
        await update.message.reply_text('Hello! How can I help you today?')
    elif 'hackathon' in text:
        await update.message.reply_text('Are you looking for hackathon matches? Use /match to find one!')
    else:
        await update.message.reply_text('I am not sure how to respond to that. Type /help for assistance.')

async def handle_error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print(f'Update {update} caused error {context.error}')

if __name__ == '__main__':
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler('start', start_command))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('match', match_command))
    application.add_handler(CommandHandler('school', school_command))
    application.add_handler(CallbackQueryHandler(handle_school_choice, pattern='^school_'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(handle_error)

    print("Bot is running...")
    application.run_polling(poll_interval=3)