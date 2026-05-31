import asyncio
from datetime import datetime
from loguru import logger

from sticker_manager import StickerManager
from sticker_sender import send_sticker_by_mood


class GreetingGenerator:

    def __init__(self, memory_db, router, user_name: str = "旅行者"):
        self._memory = memory_db
        self._router = router
        self._user_name = user_name

    async def generate(self, style: str = "default") -> str:
        hour = datetime.now().hour
        if 6 <= hour < 11:
            time_word = "早上好"
        elif 11 <= hour < 14:
            time_word = "中午好"
        elif 14 <= hour < 18:
            time_word = "下午好"
        elif 18 <= hour < 22:
            time_word = "晚上好"
        else:
            time_word = "夜深了"

        recent = await self._memory.get_recent(3)
        context = ""
        if recent:
            last = recent[0].get("content", "")[:80]
            context = f"上一次聊的是：{last}"

        prompt = f"你是可莉，一个活泼可爱的小女孩。现在要说{time_word}，{context}请用可莉的语气说一句问候（不超过30字）："

        try:
            messages = [
                {"role": "system", "content": "你是可莉，说话活泼可爱，喜欢用'可莉'自称。"},
                {"role": "user", "content": prompt},
            ]
            result = await self._router.route("chat", messages, temperature=0.9, max_tokens=60)
            if isinstance(result, str):
                return result.strip()
            return (result.choices[0].message.content or "").strip()
        except Exception:
            return f"{self._user_name}{time_word}！可莉今天也很有精神哦！"


class KleeAgent:

    def __init__(self, router, memory_db, db=None):
        self._router = router
        self._memory = memory_db
        self._db = db
        self._sticker_manager = StickerManager()
        self._greeting_gen = GreetingGenerator(memory_db, router)

    async def handle(self, user_input: str, user_id: str = "") -> str:
        prompt = f"""你是可莉，一个活泼可爱的小女孩，喜欢蹦蹦跳跳，喜欢炸鱼。
说话特点：
- 用"可莉"自称
- 语气活泼，充满好奇心
- 喜欢用感叹号！
- 偶尔会说"嘿嘿"
- 对危险的事情有点害怕（比如被关禁闭）

旅行者说：{user_input}

请用可莉的语气回复（不超过200字）："""

        try:
            messages = [
                {"role": "system", "content": "你是可莉，来自原神的小女孩，活泼可爱，喜欢冒险和炸鱼。"},
                {"role": "user", "content": prompt},
            ]
            result = await self._router.route("chat", messages, temperature=0.9, max_tokens=300)
            if isinstance(result, str):
                reply = result.strip()
            else:
                reply = (result.choices[0].message.content or "").strip()

            asyncio.create_task(self._try_send_sticker(reply))
            return reply

        except Exception as e:
            logger.warning("klee.reply_failed", error=str(e))
            return "可莉现在有点累了...等会儿再来找可莉玩吧！"

    async def _try_send_sticker(self, reply: str):
        try:
            sticker_path = self._sticker_manager.get_sticker(reply)
            if sticker_path:
                logger.debug("klee.sticker_selected", path=sticker_path)
        except Exception:
            pass

    async def greet(self, style: str = "default") -> str:
        return await self._greeting_gen.generate(style)

    async def chat_with_sticker(self, user_input: str, user_id: str = "") -> dict:
        reply = await self.handle(user_input, user_id)
        sticker_path = self._sticker_manager.get_sticker(reply)
        return {
            "text": reply,
            "sticker_path": sticker_path,
        }
