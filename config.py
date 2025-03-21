class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    DATABASE_URL = os.getenv("DATABASE_URL")  # Указываем ваш URL
    MANAGER_GROUP_ID = int(os.getenv("MANAGER_GROUP_ID"))

bot = Bot(token=Config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
pool = None
