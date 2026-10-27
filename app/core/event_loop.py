import asyncio
import sys


def loop_factory() -> asyncio.AbstractEventLoop:
    # Psycopg async connections require a selector loop on Windows, including reload workers.
    return asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
