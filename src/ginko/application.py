"""Compose the dedicated-session application from explicitly configured components."""

from ginko.adapters.onebot import send_text
from ginko.config import RuntimeConfig
from ginko.decision import TextDecider
from ginko.gateway import Gateway
from ginko.providers.chat import ChatClient
from ginko.runtime import Runtime
from ginko.storage.budget import BudgetLedger
from ginko.storage.messages import Delivery, EventClaim


class Application:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.runtime = Runtime(config, self._decide, self._send)
        self.gateway = Gateway(self.runtime)

    async def _decide(self, claim: EventClaim) -> str | None:
        # A client belongs to one activity, so cancellation closes its transport before
        # runtime shutdown releases storage. There are no hidden persistent HTTP tasks.
        if self.runtime.database is None:
            raise RuntimeError("application storage is not open")
        ledger = BudgetLedger(self.runtime.database, self.config.settings.budget.limits)
        async with ChatClient(
            self.config.settings.model, self.config.model_api_key, ledger
        ) as model:
            return await TextDecider(self.config, model)(claim)

    async def _send(self, delivery: Delivery) -> str:
        return await send_text(self.gateway.adapter, delivery)

    async def serve(self) -> None:
        await self.gateway.serve()
