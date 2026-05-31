import os
import json
import asyncio
from dataclasses import dataclass, field
from typing import Any
from openai import AsyncOpenAI
from loguru import logger

from agent_core import AgentCore
from agent_context import AgentContext, ContextManager
from config import load_config
from model_router import ModelRouter


@dataclass
class AgentConfig:
    name: str
    display_name: str
    personality_file: str = ""
    model_tier: str = "standard"
    capabilities: list[str] = field(default_factory=list)
    route_description: str = ""
    max_context: int = 30


class SubAgent:

    def __init__(self, config: AgentConfig, model_router: ModelRouter):
        self.config = config
        self._router = model_router
        self._context = ContextManager()
        self._system_prompt = ""
        self._available = True

        if config.personality_file and os.path.exists(config.personality_file):
            with open(config.personality_file, "r", encoding="utf-8") as f:
                self._system_prompt = f.read()

    @property
    def available(self) -> bool:
        return self._available

    async def chat(self, user_input: str, status_callback=None) -> str:
        ctx = self._context.get_or_create(self.config.name)
        if not ctx.system_prompt and self._system_prompt:
            ctx.system_prompt = self._system_prompt

        ctx.add_message("user", user_input)
        messages = ctx.get_messages()

        try:
            response = await self._router.route(
                "chat" if self.config.model_tier == "standard" else "pro",
                messages,
                temperature=0.7,
            )
            if isinstance(response, str):
                reply = response
            else:
                reply = response.choices[0].message.content or ""

            reasoning = ""
            if not isinstance(response, str):
                reasoning = getattr(response.choices[0].message, "reasoning_content", None) or ""

            ctx.add_message("assistant", reply, reasoning_content=reasoning if reasoning else None)
            return reply
        except Exception as e:
            logger.error("sub_agent.chat_failed", agent=self.config.name, error=str(e))
            return f"{self.config.display_name}暂时无法回应……"


class AgentDispatcher:

    def __init__(self, config: dict = None):
        self._config = config or load_config()
        self._model_router = ModelRouter(self._config)
        self._agents: dict[str, SubAgent] = {}
        self._agent_configs: dict[str, dict] = {}

    def register(self, agent_config: AgentConfig):
        agent = SubAgent(agent_config, self._model_router)
        self._agents[agent_config.name] = agent
        self._agent_configs[agent_config.name] = {
            "name": agent_config.name,
            "display_name": agent_config.display_name,
            "capabilities": agent_config.capabilities,
            "route_description": agent_config.route_description,
            "model_tier": agent_config.model_tier,
        }
        logger.info("dispatcher.registered", agent=agent_config.name)

    def get_agent(self, name: str) -> SubAgent | None:
        return self._agents.get(name)

    async def dispatch(self, agent_name: str, user_input: str,
                       status_callback=None) -> str | None:
        agent = self._agents.get(agent_name)
        if not agent:
            return None
        return await agent.chat(user_input, status_callback=status_callback)

    @property
    def agent_configs(self) -> dict:
        return self._agent_configs

    def list_agents(self) -> list[dict]:
        return [
            {"name": name, "available": agent.available, "display_name": agent.config.display_name}
            for name, agent in self._agents.items()
        ]
