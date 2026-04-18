import asyncio
from datetime import datetime, timedelta

class SessionManager:
    def __init__(self):
        self.sessions = {}  # user_id -> session data
        self._cleanup_task = None

    async def start_periodic_cleanup(self):
        """Startet die Cleanup-Schleife und speichert den Task-Handle"""
        if self._cleanup_task is None:
            # Wir starten die Endlosschleife hier
            await self._periodic_cleanup()

    async def _periodic_cleanup(self):
        """Alle 5 Minuten alte Sessions löschen"""
        print("🕒 Session-Cleanup Loop gestartet...")
        while True:
            try:
                now = datetime.now()
                expired = [
                    uid for uid, sess in self.sessions.items()
                    if now - sess.get("last_active", now) > timedelta(minutes=30)
                ]
                for uid in expired:
                    del self.sessions[uid]
                    print(f"🧹 Session für User {uid} gelöscht.")
                
                await asyncio.sleep(300) 
            except asyncio.CancelledError:
                print("🛑 Cleanup-Task sauber beendet.")
                break
            except Exception as e:
                print(f"⚠️ Session cleanup error: {e}")
                await asyncio.sleep(10)

    def get(self, user_id):
        sess = self.sessions.get(user_id)
        if sess:
            sess["last_active"] = datetime.now()
        return sess

    def set(self, user_id, data):
        self.sessions[user_id] = {**data, "last_active": datetime.now()}

    async def shutdown(self):
        """Wird von bot.py aufgerufen, um die Warnung zu verhindern"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
