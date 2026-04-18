import asyncio
from collections import defaultdict

class EventBus:
    def __init__(self):
        self.listeners = defaultdict(list)

    def subscribe(self, event_type, callback):
        self.listeners[event_type].append(callback)

    async def emit(self, event_type, data):
        tasks = []
        for callback in self.listeners.get(event_type, []):
            if asyncio.iscoroutinefunction(callback):
                tasks.append(asyncio.create_task(callback(data)))
            else:
                callback(data)
        if tasks:
            await asyncio.gather(*tasks)