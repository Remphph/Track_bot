import asyncio
import logging
import asyncpg
import os
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv("Bot.env")


class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    DATABASE_URL = os.getenv("DATABASE_URL")
    MANAGER_GROUP_ID = int(os.getenv("MANAGER_GROUP_ID"))


bot = Bot(token=Config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
pool = None


# FSM States
class DriverStates(StatesGroup):
    REG_COMPANY = State()
    REG_FULL_NAME = State()
    REG_PHONE = State()
    REG_TRUCK = State()
    EDIT_COMPANY = State()
    EDIT_FULL_NAME = State()
    EDIT_PHONE = State()
    EDIT_TRUCK = State()
    SEND_TASK_ID = State()
    SEND_BOL = State()
    SEND_TRAILER = State()


# Validation patterns
PHONE_PATTERN = re.compile(r'^\+?[1-9]\d{9,14}$')
BOL_PATTERN = re.compile(r'^\d{8,12}$')


async def init_db():
    global pool
    try:
        pool = await asyncpg.create_pool(dsn=Config.DATABASE_URL)
        logger.info("Database connected")
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        raise


async def execute(query: str, *args):
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)


async def fetch(query: str, *args):
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args):
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def setup_db():
    await execute(r'''
            CREATE TABLE IF NOT EXISTS drivers (
                id SERIAL PRIMARY KEY,
                driver_id BIGINT UNIQUE NOT NULL,
                company TEXT NOT NULL,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL CHECK (phone ~ '^\+?[1-9]\d{9,14}$'),
                truck_number TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
    await execute(r'''
            CREATE TABLE IF NOT EXISTS tasks (
                task_id SERIAL PRIMARY KEY,
                driver_id BIGINT NOT NULL,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                manager_id BIGINT,
                bol_number TEXT,
                trailer_number TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        ''')
    logger.info("Tables created")


async def update_task(task_id, **kwargs):
    set_clauses = ', '.join(f"{k} = ${i + 1}" for i, k in enumerate(kwargs.keys()))
    await execute(
        f"UPDATE tasks SET {set_clauses}, updated_at = NOW() WHERE task_id = ${len(kwargs) + 1}",
        *kwargs.values(),
        task_id
    )


def get_main_menu():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="New Shift"), types.KeyboardButton(text="New Cycle")],
            [types.KeyboardButton(text="Reset Break"), types.KeyboardButton(text="Add Time")],
            [types.KeyboardButton(text="Check"), types.KeyboardButton(text="Load")],
            [types.KeyboardButton(text="Contact Me"), types.KeyboardButton(text="⚙️ Settings")]
        ],
        resize_keyboard=True
    )


def settings_menu():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Edit Profile")],
            [types.KeyboardButton(text="Back")]
        ],
        resize_keyboard=True
    )


@dp.message(F.text.in_(["New Shift", "New Cycle", "Reset Break", "Add Time", "Check", "Load", "Contact Me"]))
async def create_task(message: types.Message):
    driver = await fetchrow("SELECT * FROM drivers WHERE driver_id = $1", message.from_user.id)
    if not driver:
        await message.answer("Please register first!")
        return

    task_type = message.text
    try:
        await execute("INSERT INTO tasks (driver_id, task_type) VALUES ($1, $2)", message.from_user.id, task_type)
        task_id = (await fetchrow("SELECT task_id FROM tasks ORDER BY task_id DESC LIMIT 1"))['task_id']

        await bot.send_message(
            Config.MANAGER_GROUP_ID,
            f"📩 New Task from {driver['full_name']} ({driver['company']}):\n"
            f"Type: {task_type}",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text="Take Task", callback_data=f"take_{task_id}")]]
            )
        )

        await message.answer("We are working on your log book. Please wait.")
    except Exception as e:
        logger.error(f"Task creation error: {e}")
        await message.answer("An error occurred. Please try again later.")


@dp.callback_query(F.data.startswith("take_"))
async def take_task(callback: types.CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    manager_id = callback.from_user.id
    try:
            async with pool.acquire() as conn:
                task = await conn.fetchrow("SELECT task_type, status, bol_number, trailer_number FROM tasks WHERE task_id = $1", task_id)
                if not task:
                    await callback.message.answer("Task not found.")
                    return
                text_fields = [task['task_type'], task['status'], task['bol_number'] or '', task['trailer_number'] or '']
                if any(x in field.lower() for field in text_fields for x in ["vpn", "http", "arturshi", "🔒", "🔥"]):
                    logger.warning(f"Ignored task with potential spam: {text_fields}")
                    await callback.message.answer("This task is not available.")
                    return
                if task['status'] != 'created':
                    await callback.message.answer("Task is already taken or completed.")
                    return
                await conn.execute(
                    "UPDATE tasks SET status = 'in_progress', manager_id = $1 WHERE task_id = $2",
                    manager_id, task_id
                )
                task = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
                driver_info = await conn.fetchrow("SELECT full_name, company FROM drivers WHERE driver_id = $1", task['driver_id'])

                await bot.edit_message_text(
                    chat_id=Config.MANAGER_GROUP_ID,
                    message_id=callback.message.message_id,
                    text=f"📩 Task taken by {callback.from_user.full_name}:\n"
                         f"Type: {task['task_type']}\n"
                         f"Driver: {driver_info['full_name']} ({driver_info['company']})",
                    reply_markup=types.InlineKeyboardMarkup(
                        inline_keyboard=[[types.InlineKeyboardButton(text="Complete", callback_data=f"finish_{task_id}")]]
                    )
                )

                await bot.send_message(
                    task['driver_id'],
                    f"Your task ({task['task_type']}) has been taken by {callback.from_user.full_name}!"
                )
                await callback.answer("Task taken!")
    except Exception as e:
        logger.error(f"Task take error: {e}")
        await callback.answer("An error occurred", show_alert=True)


@dp.callback_query(F.data.startswith("finish_"))
async def finish_task(callback: types.CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    manager_id = callback.from_user.id
    try:
        task = await fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
        if task['manager_id'] != manager_id:
            await callback.answer("You cannot complete someone else's task")
            return
        driver_info = await fetchrow("SELECT full_name, company FROM drivers WHERE driver_id = $1", task['driver_id'])

        await update_task(task_id, status="completed")

        await bot.edit_message_text(
            chat_id=Config.MANAGER_GROUP_ID,
            message_id=callback.message.message_id,
            text=f"📩 Task completed by {callback.from_user.full_name}:\n"
                 f"Type: {task['task_type']}\n"
                 f"Driver: {driver_info['full_name']} ({driver_info['company']})",
            reply_markup=None
        )

        await bot.send_message(
            task['driver_id'],
            f"Your task ({task['task_type']}) has been completed by {callback.from_user.full_name}. Have a safe trip!"
        )
        await callback.answer("Task completed!", show_alert=True)
    except Exception as e:
        logger.error(f"Task completion error: {e}")
        await callback.answer("An error occurred", show_alert=True)


@dp.message(F.text == "Send Data")
async def start_send_data(message: types.Message, state: FSMContext):
    driver = await fetchrow("SELECT * FROM drivers WHERE driver_id = $1", message.from_user.id)
    if not driver:
        await message.answer("Please register first!")
        return

    await message.answer("Enter task number:")
    await state.set_state(DriverStates.SEND_TASK_ID)


@dp.message(DriverStates.SEND_TASK_ID)
async def process_task_id(message: types.Message, state: FSMContext):
    task_id_str = message.text.strip()
    if not task_id_str.isdigit():
        await message.answer("Task number must be a number. Please try again.")
        return

    task_id = int(task_id_str)
    task = await fetchrow(
        "SELECT * FROM tasks WHERE task_id = $1 AND driver_id = $2 AND status = 'in_progress'",
        task_id, message.from_user.id
    )

    if not task:
        await message.answer("Task not found or not assigned to you.")
        return

    await state.update_data(task_id=task_id)
    await message.answer("Enter BOL number:")
    await state.set_state(DriverStates.SEND_BOL)


@dp.message(DriverStates.SEND_BOL)
async def process_bol(message: types.Message, state: FSMContext):
    bol = message.text.strip()
    if not BOL_PATTERN.match(bol):
        await message.answer("Invalid BOL format (must be 8-12 digits). Please try again.")
        return

    await state.update_data(bol=bol)
    await message.answer("Enter trailer number:")
    await state.set_state(DriverStates.SEND_TRAILER)


@dp.message(DriverStates.SEND_TRAILER)
async def process_trailer(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        task_id = data["task_id"]
        bol = data["bol"]
        trailer = message.text.strip()
        if any(x in trailer.lower() for x in ["vpn", "http", "arturshi", "🔒", "🔥"]):
            logger.warning(f"Blocked spam in trailer: {trailer}")
            await message.answer("Invalid trailer number. Please try again.")
            return

        await update_task(task_id, bol_number=bol, trailer_number=trailer)
        await state.clear()

        await message.answer("Data sent to manager!", reply_markup=get_main_menu())

        task = await fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
        manager_info = await fetchrow("SELECT manager_id FROM tasks WHERE task_id = $1", task_id)

        if manager_info and manager_info['manager_id']:
            await bot.send_message(
                manager_info['manager_id'],
                f"📩 Task update:\n"
                f"Type: {task['task_type']}\n"
                f"BOL: {bol}\n"
                f"Trailer: {trailer}"
            )
    except Exception as e:
        logger.error(f"Task data update error: {e}")
        await message.answer("An error occurred. Please try again later.")


@dp.message(F.text == "Check Status")
async def check_task_status(message: types.Message):
    driver_id = message.from_user.id
    tasks = await fetch(
        "SELECT * FROM tasks WHERE driver_id = $1 ORDER BY updated_at DESC LIMIT 5",
        driver_id
    )

    if not tasks:
        await message.answer("You have no active tasks.")
        return

    for task in tasks:
        status_emoji = "⏳" if task['status'] == 'in_progress' else "✅"
        await message.answer(
            f"Task:\n"
            f"Type: {task['task_type']}\n"
            f"Status: {status_emoji} {task['status']}\n"
            f"BOL: {task['bol_number'] or 'Not provided'}\n"
            f"Trailer: {task['trailer_number'] or 'Not provided'}"
        )


@dp.message(F.text == "/start")
async def cmd_start(message: types.Message, state: FSMContext):
    await message.answer(
        "Welcome to our team!\n"
        "We work 24/7 🕐\n"
        "Let us know if you have any questions or need some help with ELD.\n"
        "We are always glad to help you! 📲",
        reply_markup=get_main_menu()
    )

    current_state = await state.get_state()
    if current_state:
        await message.answer("Please complete the current action or type /cancel")
        return

    driver = await fetchrow("SELECT * FROM drivers WHERE driver_id = $1", message.from_user.id)

    if driver:
        await message.answer("Welcome back!", reply_markup=get_main_menu())
    else:
        await message.answer("Registration:\n\nEnter company name:", reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(DriverStates.REG_COMPANY)


@dp.message(F.text == "/cancel")
async def cancel_registration(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        await message.answer("Action cancelled", reply_markup=get_main_menu())
    else:
        await message.answer("No active actions", reply_markup=get_main_menu())


@dp.message(F.text == "/menu")
async def cmd_menu(message: types.Message):
    await message.answer("Main menu", reply_markup=get_main_menu())


@dp.message(F.text == "⚙️ Settings")
async def settings(message: types.Message):
    driver = await fetchrow("SELECT * FROM drivers WHERE driver_id = $1", message.from_user.id)
    if not driver:
        await message.answer("Please register first!")
        return

    await message.answer("Select an action:", reply_markup=settings_menu())


@dp.message(F.text == "Edit Profile")
async def edit_data(message: types.Message, state: FSMContext):
    await message.answer("Enter new company name:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(DriverStates.EDIT_COMPANY)


@dp.message(DriverStates.REG_COMPANY)
async def process_company(message: types.Message, state: FSMContext):
    company = message.text.strip()
    if not company:
        await message.answer("Company name cannot be empty.")
        return

    await state.update_data(company=company)
    await message.answer("Enter your full name:")
    await state.set_state(DriverStates.REG_FULL_NAME)


@dp.message(DriverStates.REG_FULL_NAME)
async def process_full_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    if not full_name:
        await message.answer("Full name cannot be empty.")
        return

    await state.update_data(full_name=full_name)
    await message.answer("Enter phone number (e.g., +71234567890):")
    await state.set_state(DriverStates.REG_PHONE)


@dp.message(DriverStates.REG_PHONE)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    if not PHONE_PATTERN.match(phone):
        await message.answer("❌ Invalid phone format. Example: +71234567890")
        return

    await state.update_data(phone=phone)
    await message.answer("Enter truck number:")
    await state.set_state(DriverStates.REG_TRUCK)


@dp.message(DriverStates.REG_TRUCK)
async def process_truck(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        truck_number = message.text.strip()
        if not truck_number:
            await message.answer("Truck number cannot be empty.")
            return

        await execute(
            "INSERT INTO drivers (driver_id, company, full_name, phone, truck_number) VALUES ($1, $2, $3, $4, $5)",
            message.from_user.id,
            data["company"],
            data["full_name"],
            data["phone"],
            truck_number
        )

        await message.answer("✅ Registration completed!", reply_markup=get_main_menu())
        await state.clear()
    except Exception as e:
        logger.error(f"Registration error: {e}")
        await message.answer("An error occurred. Please try again later.")


@dp.message(DriverStates.EDIT_COMPANY)
async def process_edit_company(message: types.Message, state: FSMContext):
    company = message.text.strip()
    if not company:
        await message.answer("Company name cannot be empty.")
        return

    await state.update_data(company=company)
    await message.answer("Enter new full name:")
    await state.set_state(DriverStates.EDIT_FULL_NAME)


@dp.message(DriverStates.EDIT_FULL_NAME)
async def process_edit_full_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    if not full_name:
        await message.answer("Full name cannot be empty.")
        return

    await state.update_data(full_name=full_name)
    await message.answer("Enter new phone number:")
    await state.set_state(DriverStates.EDIT_PHONE)


@dp.message(DriverStates.EDIT_PHONE)
async def process_edit_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    if not PHONE_PATTERN.match(phone):
        await message.answer("❌ Invalid phone format. Example: +71234567890")
        return

    await state.update_data(phone=phone)
    await message.answer("Enter new truck number:")
    await state.set_state(DriverStates.EDIT_TRUCK)


@dp.message(DriverStates.EDIT_TRUCK)
async def process_edit_truck(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        truck_number = message.text.strip()
        if not truck_number:
            await message.answer("Truck number cannot be empty.")
            return

        await execute(
            "UPDATE drivers SET company = $1, full_name = $2, phone = $3, truck_number = $4 WHERE driver_id = $5",
            data["company"],
            data["full_name"],
            data["phone"],
            truck_number,
            message.from_user.id
        )

        await message.answer("Profile updated!", reply_markup=get_main_menu())
        await state.clear()
    except Exception as e:
        logger.error(f"Profile update error: {e}")
        await message.answer("An error occurred. Please try again later.")


@dp.message(F.text == "Back")
async def back_to_main_menu(message: types.Message):
    await message.answer("Main menu", reply_markup=get_main_menu())


@dp.message()
async def handle_unknown(message: types.Message):
    if any(x in message.text.lower() for x in ["vpn", "http", "arturshi", "🔒", "🔥"]):
        logger.warning(f"Blocked spam from {message.from_user.id}: {message.text}")
        return
    await message.answer("Please use the menu to select an action.", reply_markup=get_main_menu())


async def on_startup():
    await init_db()
    await setup_db()
    await bot.delete_webhook()


async def main():
    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())