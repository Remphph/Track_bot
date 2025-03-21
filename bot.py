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

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv("Bot.env")

import os

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    DATABASE_URL = os.getenv("DATABASE_URL")  # Указываем ваш URL
    MANAGER_GROUP_ID = int(os.getenv("MANAGER_GROUP_ID"))

bot = Bot(token=Config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
pool = None


# Состояния FSM
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


# Паттерны валидации
PHONE_PATTERN = re.compile(r'^\+?[1-9]\d{9,14}$')  # Минимум 10 цифр (с +)
BOL_PATTERN = re.compile(r'^\d{8,12}$')


async def init_db():
    global pool
    try:
        pool = await asyncpg.create_pool(dsn=Config.DATABASE_URL)
        logger.info("База данных подключена")
    except Exception as e:
        logger.error(f"Ошибка подключения к БД: {e}")
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


# Создание таблиц
async def setup_db():
    # Таблица водителей
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

    # Таблица задач
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
    logger.info("Таблицы созданы")


# Обновление задачи
async def update_task(task_id, **kwargs):
    set_clauses = ', '.join(f"{k} = ${i + 1}" for i, k in enumerate(kwargs.keys()))
    await execute(
        f"UPDATE tasks SET {set_clauses}, updated_at = NOW() WHERE task_id = ${len(kwargs) + 1}",
        *kwargs.values(),
        task_id
    )


# Клавиатуры
def get_main_menu():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Новая смена")],
            [types.KeyboardButton(text="Новый цикл")],
            [types.KeyboardButton(text="Перезапуск перерыва")],
            [types.KeyboardButton(text="Добавить время")],
            [types.KeyboardButton(text="Проверка")],
            [types.KeyboardButton(text="Загрузка")],
            [types.KeyboardButton(text="Связаться со мной")],
            [types.KeyboardButton(text="⚙️ Настройки")]
        ],
        resize_keyboard=True
    )


def settings_menu():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="Изменить данные")],
            [types.KeyboardButton(text="Назад")]
        ],
        resize_keyboard=True
    )


# Обработчик создания задачи
@dp.message(F.text.in_([
    "Новая смена",
    "Новый цикл",
    "Перезапуск перерыва",
    "Добавить время",
    "Проверка",
    "Загрузка",
    "Связаться со мной"
]))
async def create_task(message: types.Message):
    driver = await fetchrow("SELECT * FROM drivers WHERE driver_id = $1", message.from_user.id)
    if not driver:
        await message.answer("Сначала зарегистрируйтесь!")
        return

    task_type = message.text
    try:
        await execute("INSERT INTO tasks (driver_id, task_type) VALUES ($1, $2)", message.from_user.id, task_type)
        task_id = (await fetchrow("SELECT task_id FROM tasks ORDER BY task_id DESC LIMIT 1"))['task_id']

        await bot.send_message(
            Config.MANAGER_GROUP_ID,
            f"📩 Новая задача #{task_id} от {driver['full_name']} ({driver['company']}):\n"
            f"Тип: {task_type}",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text="Взять задачу", callback_data=f"take_{task_id}")]]
            )
        )

        await message.answer(f"Задача #{task_id} создана! Ожидайте менеджера...")
    except Exception as e:
        logger.error(f"Ошибка создания задачи: {e}")
        await message.answer("Произошла ошибка. Попробуйте позже")


# Взятие задачи менеджером
@dp.callback_query(F.data.startswith("take_"))
async def take_task(callback: types.CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    manager_id = callback.from_user.id

    try:
        await update_task(task_id, status="in_progress", manager_id=manager_id)
        task = await fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
        driver_info = await fetchrow("SELECT full_name, company FROM drivers WHERE driver_id = $1", task['driver_id'])

        await bot.edit_message_text(
            chat_id=Config.MANAGER_GROUP_ID,
            message_id=callback.message.message_id,
            text=f"📩 Задача #{task_id} (Взята менеджером {callback.from_user.full_name}):\n"
                 f"Тип: {task['task_type']}\n"
                 f"Водитель: {driver_info['full_name']} ({driver_info['company']})",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text="Завершить", callback_data=f"finish_{task_id}")]]
            )
        )

        await bot.send_message(task['driver_id'],
                               f"Ваша задача #{task_id} взята менеджером {callback.from_user.full_name}!")
        await callback.answer("Задача взята!")
    except Exception as e:
        logger.error(f"Ошибка взятия задачи: {e}")
        await callback.answer("Произошла ошибка", show_alert=True)


# Завершение задачи менеджером
@dp.callback_query(F.data.startswith("finish_"))
async def finish_task(callback: types.CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    manager_id = callback.from_user.id

    try:
        task = await fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
        if task['manager_id'] != manager_id:
            await callback.answer("Вы не можете завершить чужую задачу")
            return

        await update_task(task_id, status="completed")

        await bot.edit_message_text(
            chat_id=Config.MANAGER_GROUP_ID,
            message_id=callback.message.message_id,
            text=f"📩 Задача #{task_id} завершена менеджером {callback.from_user.full_name}",
            reply_markup=None
        )

        await bot.send_message(
            task['driver_id'],
            f"Ваша задача #{task_id} завершена менеджером {callback.from_user.full_name}!"
        )
        await callback.answer("Задача завершена!", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка завершения задачи: {e}")
        await callback.answer("Произошла ошибка", show_alert=True)


# Отправка данных задачи
@dp.message(F.text == "Отправить данные")
async def start_send_data(message: types.Message, state: FSMContext):
    driver = await fetchrow("SELECT * FROM drivers WHERE driver_id = $1", message.from_user.id)
    if not driver:
        await message.answer("Сначала зарегистрируйтесь!")
        return

    await message.answer("Введите номер задачи:")
    await state.set_state(DriverStates.SEND_TASK_ID)


@dp.message(DriverStates.SEND_TASK_ID)
async def process_task_id(message: types.Message, state: FSMContext):
    task_id_str = message.text.strip()
    if not task_id_str.isdigit():
        await message.answer("Номер задачи должен быть числом. Попробуйте снова")
        return

    task_id = int(task_id_str)
    task = await fetchrow(
        "SELECT * FROM tasks WHERE task_id = $1 AND driver_id = $2 AND status = 'in_progress'",
        task_id,
        message.from_user.id
    )

    if not task:
        await message.answer("Задача не найдена или не назначена на исполнителя")
        return

    await state.update_data(task_id=task_id)
    await message.answer("Введите BOL (номер груза):")
    await state.set_state(DriverStates.SEND_BOL)


@dp.message(DriverStates.SEND_BOL)
async def process_bol(message: types.Message, state: FSMContext):
    bol = message.text.strip()
    if not BOL_PATTERN.match(bol):
        await message.answer("Неверный формат BOL (должно быть 8-12 цифр). Попробуйте снова")
        return

    await state.update_data(bol=bol)
    await message.answer("Введите номер трейлера:")
    await state.set_state(DriverStates.SEND_TRAILER)


@dp.message(DriverStates.SEND_TRAILER)
async def process_trailer(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        task_id = data["task_id"]
        bol = data["bol"]
        trailer = message.text.strip()

        await update_task(task_id, bol_number=bol, trailer_number=trailer)
        await state.clear()

        await message.answer("Данные отправлены менеджеру!", reply_markup=get_main_menu())

        # Уведомление менеджеру
        task = await fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
        manager_info = await fetchrow("SELECT manager_id FROM tasks WHERE task_id = $1", task_id)

        if manager_info and manager_info['manager_id']:
            await bot.send_message(
                manager_info['manager_id'],
                f"📩 Обновление задачи #{task_id}:\n"
                f"BOL: {bol}\n"
                f"Трейлер: {trailer}"
            )
    except Exception as e:
        logger.error(f"Ошибка обновления данных задачи: {e}")
        await message.answer("Произошла ошибка. Попробуйте позже")


# Проверка статуса задачи
@dp.message(F.text == "Проверка статуса")
async def check_task_status(message: types.Message):
    driver_id = message.from_user.id
    tasks = await fetch(
        "SELECT * FROM tasks WHERE driver_id = $1 ORDER BY updated_at DESC LIMIT 5",
        driver_id
    )

    if not tasks:
        await message.answer("У вас нет активных задач")
        return

    for task in tasks:
        status_emoji = "⏳" if task['status'] == 'in_progress' else "✅"
        await message.answer(
            f"Задача #{task['task_id']}:\n"
            f"Тип: {task['task_type']}\n"
            f"Статус: {status_emoji} {task['status']}\n"
            f"BOL: {task['bol_number'] or 'Не указан'}\n"
            f"Трейлер: {task['trailer_number'] or 'Не указан'}"
        )


# Регистрация водителя
@dp.message(F.text == "/start")
async def cmd_start(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await message.answer("Завершите текущее действие или напишите /cancel")
        return

    driver = await fetchrow("SELECT * FROM drivers WHERE driver_id = $1", message.from_user.id)

    if driver:
        await message.answer("Добро пожаловать обратно!", reply_markup=get_main_menu())
    else:
        await message.answer("Регистрация:\n\nВведите название компании", reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(DriverStates.REG_COMPANY)


# Обработчик отмены
@dp.message(F.text == "/cancel")
async def cancel_registration(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        await message.answer("Действие отменено", reply_markup=get_main_menu())
    else:
        await message.answer("Нет активных действий")


# Настройки водителя
@dp.message(F.text == "⚙️ Настройки")
async def settings(message: types.Message):
    driver = await fetchrow("SELECT * FROM drivers WHERE driver_id = $1", message.from_user.id)
    if not driver:
        await message.answer("Сначала зарегистрируйтесь!")
        return

    await message.answer("Выберите действие:", reply_markup=settings_menu())


@dp.message(F.text == "Изменить данные")
async def edit_data(message: types.Message, state: FSMContext):
    await message.answer("Введите новое название компании:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(DriverStates.EDIT_COMPANY)


# Регистрация
@dp.message(DriverStates.REG_COMPANY)
async def process_company(message: types.Message, state: FSMContext):
    company = message.text.strip()
    if not company:
        await message.answer("Название компании не может быть пустым")
        return

    await state.update_data(company=company)
    await message.answer("Введите ваше ФИО:")
    await state.set_state(DriverStates.REG_FULL_NAME)


@dp.message(DriverStates.REG_FULL_NAME)
async def process_full_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    if not full_name:
        await message.answer("ФИО не может быть пустым")
        return

    await state.update_data(full_name=full_name)
    await message.answer("Введите телефон (пример: +71234567890):")
    await state.set_state(DriverStates.REG_PHONE)


@dp.message(DriverStates.REG_PHONE)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    if not PHONE_PATTERN.match(phone):
        await message.answer("❌ Неверный формат телефона. Пример: +71234567890")
        return

    await state.update_data(phone=phone)
    await message.answer("Введите номер транспорта:")
    await state.set_state(DriverStates.REG_TRUCK)


@dp.message(DriverStates.REG_TRUCK)
async def process_truck(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        truck_number = message.text.strip()
        if not truck_number:
            await message.answer("Номер транспорта не может быть пустым")
            return

        await execute(
            "INSERT INTO drivers (driver_id, company, full_name, phone, truck_number) VALUES ($1, $2, $3, $4, $5)",
            message.from_user.id,
            data["company"],
            data["full_name"],
            data["phone"],
            truck_number
        )

        await message.answer("✅ Регистрация завершена!", reply_markup=get_main_menu())
        await state.clear()
    except Exception as e:
        logger.error(f"Ошибка регистрации: {e}")
        await message.answer("Произошла ошибка. Попробуйте позже")


# Обновление данных
@dp.message(DriverStates.EDIT_COMPANY)
async def process_edit_company(message: types.Message, state: FSMContext):
    company = message.text.strip()
    if not company:
        await message.answer("Название компании не может быть пустым")
        return

    await state.update_data(company=company)
    await message.answer("Введите новое ФИО:")
    await state.set_state(DriverStates.EDIT_FULL_NAME)


@dp.message(DriverStates.EDIT_FULL_NAME)
async def process_edit_full_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    if not full_name:
        await message.answer("ФИО не может быть пустым")
        return

    await state.update_data(full_name=full_name)
    await message.answer("Введите новый телефон:")
    await state.set_state(DriverStates.EDIT_PHONE)


@dp.message(DriverStates.EDIT_PHONE)
async def process_edit_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    if not PHONE_PATTERN.match(phone):
        await message.answer("❌ Неверный формат телефона. Пример: +71234567890")
        return

    await state.update_data(phone=phone)
    await message.answer("Введите новый номер транспорта:")
    await state.set_state(DriverStates.EDIT_TRUCK)


@dp.message(DriverStates.EDIT_TRUCK)
async def process_edit_truck(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        truck_number = message.text.strip()
        if not truck_number:
            await message.answer("Номер транспорта не может быть пустым")
            return

        await execute(
            "UPDATE drivers SET company = $1, full_name = $2, phone = $3, truck_number = $4 WHERE driver_id = $5",
            data["company"],
            data["full_name"],
            data["phone"],
            truck_number,
            message.from_user.id
        )

        await message.answer("Данные обновлены!", reply_markup=get_main_menu())
        await state.clear()
    except Exception as e:
        logger.error(f"Ошибка обновления данных: {e}")
        await message.answer("Произошла ошибка. Попробуйте позже")


# Обработчик кнопки "Назад"
@dp.message(F.text == "Назад")
async def back_to_main_menu(message: types.Message):
    await message.answer("Главное меню", reply_markup=get_main_menu())


# Обработчик всех остальных сообщений
@dp.message()
async def handle_unknown(message: types.Message):
    await message.answer("Неизвестная команда. Используйте меню")


# Запуск бота
async def on_startup():
    await init_db()
    await setup_db()
    await bot.delete_webhook()


async def main():
    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())