"""ORM 模型包。

★★★ 关键：这里必须把每个模型都 import 一次 ★★★

原因：SQLAlchemy 只有在「模型类被真正导入执行」时，才会把表定义注册到 Base.metadata。
如果某个模型没被导入，Base.metadata.create_all() 就建不出对应的表，
而且不会报任何错 —— 这是新手最容易踩、也最难排查的坑之一。

因此建表脚本（scripts/init_db.py）里会先 `import app.db.models`，
靠本文件把这些模型全部加载进来。
"""

from app.db.models.character_card import CharacterCard
from app.db.models.llm_provider import LLMProvider
from app.db.models.message import Message
from app.db.models.narrative import NarrativeSession
from app.db.models.plugin import Plugin
from app.db.models.prompt_preset import PromptPreset
from app.db.models.user import User
from app.db.models.world_book import WorldBook

__all__ = [
    "User",
    "LLMProvider",
    "CharacterCard",
    "WorldBook",
    "NarrativeSession",
    "Message",
    "PromptPreset",
    "Plugin",
]
