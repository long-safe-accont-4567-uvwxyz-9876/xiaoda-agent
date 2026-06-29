"""Thompson Sampling belief-based agent routing."""
import asyncio
import math
import random
import time
import sqlite3
from dataclasses import dataclass
from loguru import logger
from utils.atomic_write import atomic_json_write


@dataclass
class AgentBelief:
    """Beta distribution belief for a single agent."""
    alpha: float = 1.0  # success count + 1
    beta: float = 1.0   # failure count + 1

    def sample(self) -> float:
        """Sample from Beta(alpha, beta) distribution using numpy-free method."""
        # Use the relation: if X ~ Gamma(alpha, 1) and Y ~ Gamma(beta, 1),
        # then X/(X+Y) ~ Beta(alpha, beta)
        x = self._gamma_sample(self.alpha)
        y = self._gamma_sample(self.beta)
        if x + y == 0:
            return 0.5
        return x / (x + y)

    def _gamma_sample(self, shape: float) -> float:
        """Simple gamma distribution sampling (Marsaglia and Tsang method)."""
        if shape < 1.0:
            # Use the relation: if X ~ Gamma(shape+1, 1) * U^(1/shape), then X ~ Gamma(shape, 1)
            return self._gamma_sample(shape + 1.0) * (random.random() ** (1.0 / shape))

        d = shape - 1.0 / 3.0
        c = 1.0 / math.sqrt(9.0 * d)
        while True:
            x = random.gauss(0, 1)
            v = (1.0 + c * x) ** 3
            if v > 0:
                u = random.random()
                if u < 1.0 - 0.0331 * (x * x) * (x * x):
                    return d * v
                if math.log(u) < 0.5 * x * x + d * (1.0 - v + math.log(v)):
                    return d * v

    def update(self, success: bool) -> None:
        """Update belief based on observation."""
        if success:
            self.alpha += 1.0
        else:
            self.beta += 1.0

    def to_dict(self) -> dict:
        return {"alpha": self.alpha, "beta": self.beta}

    @classmethod
    def from_dict(cls, d: dict) -> "AgentBelief":
        return cls(alpha=d.get("alpha", 1.0), beta=d.get("beta", 1.0))


class BeliefRouter:
    """Thompson Sampling router that selects agents based on historical performance."""

    VALID_AGENTS = ["xilian", "yinlang", "nike", "nahida"]

    def __init__(self, db_path: str = "") -> None:
        self._beliefs: dict[str, AgentBelief] = {name: AgentBelief() for name in self.VALID_AGENTS}
        self._db_path = db_path
        if db_path:
            self._load_from_db()

    def select_agent(self, exclude: set[str] | None = None) -> str:
        """Select the best agent using Thompson Sampling.

        Args:
            exclude: Set of agent names to exclude from selection.

        Returns:
            The name of the selected agent.
        """
        candidates = [a for a in self.VALID_AGENTS if a not in (exclude or set())]
        if not candidates:
            return "nahida"

        samples = {a: self._beliefs[a].sample() for a in candidates}
        selected = max(samples, key=samples.get)

        logger.debug("belief_router.sampled",
                     samples={k: round(v, 3) for k, v in samples.items()},
                     selected=selected)
        return selected

    def update_belief(self, agent_name: str, success: bool) -> None:
        """Update belief for an agent based on task result."""
        if agent_name in self._beliefs:
            self._beliefs[agent_name].update(success)
            if self._db_path:
                self._save_to_db()
            logger.debug("belief_router.updated", agent=agent_name, success=success,
                         alpha=self._beliefs[agent_name].alpha, beta=self._beliefs[agent_name].beta)

    def get_beliefs(self) -> dict[str, dict]:
        """Get current belief parameters for all agents."""
        return {name: belief.to_dict() for name, belief in self._beliefs.items()}

    def _load_from_db(self) -> None:
        """Load beliefs from database."""
        try:
            conn = sqlite3.connect(self._db_path)
            cur = conn.execute("SELECT agent_name, alpha, beta FROM agent_beliefs")
            for row in cur:
                name, alpha, beta = row
                # 防止除零：确保 alpha/beta > 0
                alpha = max(alpha, 0.01)
                beta = max(beta, 0.01)
                if name in self._beliefs:
                    self._beliefs[name] = AgentBelief(alpha=alpha, beta=beta)
            conn.close()
            logger.info("belief_router.loaded", beliefs=self.get_beliefs())
        except Exception as e:
            logger.warning("belief_router.load_failed", error=str(e))

    def _save_to_db(self) -> None:
        """Save beliefs to database (non-blocking via thread pool)."""
        try:
            import concurrent.futures
            beliefs_snapshot = {name: b.to_dict() for name, b in self._beliefs.items()}
            db_path = self._db_path

            def _do_save() -> None:
                conn = sqlite3.connect(db_path)
                conn.execute("""CREATE TABLE IF NOT EXISTS agent_beliefs (
                    agent_name TEXT PRIMARY KEY,
                    alpha REAL NOT NULL DEFAULT 1.0,
                    beta REAL NOT NULL DEFAULT 1.0,
                    updated_at REAL NOT NULL
                )""")
                for name, data in beliefs_snapshot.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO agent_beliefs (agent_name, alpha, beta, updated_at) VALUES (?, ?, ?, ?)",
                        (name, data["alpha"], data["beta"], time.time())
                    )
                conn.commit()
                conn.close()

            # 使用线程池避免阻塞事件循环
            try:
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, _do_save)
            except RuntimeError:
                # 没有运行中的事件循环，直接执行
                _do_save()
        except Exception as e:
            logger.warning("belief_router.save_failed", error=str(e))

        # 原子写入 JSON 状态文件
        self._save_to_json()

    def _save_to_json(self) -> None:
        """原子写入信念状态到 JSON 文件"""
        if not self._db_path:
            return
        try:
            json_path = self._db_path.rsplit(".", 1)[0] + "_beliefs.json"
            data = {
                name: belief.to_dict()
                for name, belief in self._beliefs.items()
            }
            atomic_json_write(json_path, data)
        except Exception as e:
            logger.warning("belief_router.json_save_failed", error=str(e))
