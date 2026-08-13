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


# build keyboard template for questions 1-4
def build_keyboard(prefix: str, options: list[tuple[str, str]], per_row: int = 2):
    keyboard = []
    row = []

    for value, label in options:
        row.append(InlineKeyboardButton(label, callback_data=f'{prefix}_{value}'))
        if len(row) == per_row:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    return InlineKeyboardMarkup(keyboard)


SCHOOLS = [
    ("NUS", "NUS"), ("NTU", "NTU"), ("SMU", "SMU"),
    ("SUTD", "SUTD"), ("SIT", "SIT"), ("SUSS", "SUSS"),
    ("Polytechnic", "Polytechnic"), ("Junior College", "Junior College"), ("Other", "Other"),
]


async def school_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask the user to select their school from a list of options."""
    keyboard = build_keyboard("school", SCHOOLS, per_row=3)
    await update.message.reply_text("Please select your school:", reply_markup=keyboard
    )


async def handle_school_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # Acknowledge the callback query
    school = query.data.split('_', 1)[1]  # Extract the school name from the callback
    context.user_data['school'] = school  # Store the selected school in user data
    await query.edit_message_text(text=f'You selected: {school}. Thank you!')
    await ask_school_preference(update, context)  # Chain to the next question

# Select school preference options

SCHOOL_PREFERENCES = [
    ("same", "Prefer my school"),
    ("different", "Prefer a different school"),
    ("none", "No preference"),
]


async def ask_school_preference(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Question 2 — asked right after the school is chosen."""
    await update.effective_chat.send_message(
        "Would you prefer teammates from your own school?",
        reply_markup=build_keyboard("pref", SCHOOL_PREFERENCES, per_row=1),
    )


async def handle_preference_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    preference = query.data.split("_", 1)[1]        # "pref_same" -> "same"
    context.user_data["school_preference"] = preference

    await query.edit_message_text(f"School preference: {preference}")
    await ask_discipline(update, context)           # ← chain to question 3

# DISCIPLINE
DISCIPLINES = [
    ("med", "Medicine/Health"),
    ("comp", "Computing/AI/Data"),
    ("eng", "Engineering"),
    ("design", "Design"),
    ("business", "Business"),
    ("science", "Science"),
    ("law", "Law"),
    ("humanities", "Humanities/Social Sciences/Psychology"),
    ("other", "Other")
]

async def ask_discipline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Question 3 — asked right after the school preference is chosen."""
    keyboard = build_keyboard("discipline", DISCIPLINES, per_row=2)
    await  update.effective_chat.send_message(
        "What is your primary discipline or field of study?",
        reply_markup=keyboard,
    )

async def handle_discipline_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    discipline = query.data.split("_", 1)[1]  # "discipline_cs" -> "cs"
    context.user_data["discipline"] = discipline

    await query.edit_message_text(f"Discipline: {discipline}")
    # You can chain to the next question or action here, if needed.
    await ask_team_status(update, context)  # Chain to the next question


# TEAM STATUS

STATUSES = [
    ("looking", "Looking for a team"),
    ("has_team", "Have a team, need more teammates"),
]

async def ask_team_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Question 4 — asked right after the discipline is chosen."""
    keyboard = build_keyboard("status", STATUSES, per_row=1)
    await update.effective_chat.send_message(
        "What is your current team status?",
        reply_markup=keyboard,
    )

async def handle_team_status_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    team_status = query.data.split("_", 1)[1]  # "status_looking" -> "looking"
    context.user_data["team_status"] = team_status

    await query.edit_message_text(f"Team status: {team_status}")
    # You can chain to the next question or action here, if needed.
    await update.effective_chat.send_message("Next, skills. (coming soon!)")  # Placeholder for the next step


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
    application.add_handler(CallbackQueryHandler(handle_preference_choice, pattern='^pref_'))
    application.add_handler(CallbackQueryHandler(handle_discipline_choice, pattern='^discipline_'))
    application.add_handler(CallbackQueryHandler(handle_team_status_choice, pattern='^status_'))
    application.add_error_handler(handle_error)

    print("Bot is running...")
    application.run_polling(poll_interval=3)

