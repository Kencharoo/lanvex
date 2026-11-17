"""TEST DOUBLE: two switches answering SNMP on localhost, for end-to-end tests and trying the UI.

    uv run python -m tests.snmp_lab            # sw-access on UDP 16101, sw-core on UDP 16102

Both answer SNMPv2c (community lanvex-test) and SNMPv3 (user lanvex, SHA auth-pass-123, AES
priv-pass-456); see tests/switch_tables.py for what they hold. A TCP port (default 16100) answers once
they are up, for test runners that wait for a port.
"""

import argparse
import asyncio

from .snmp_agent import Agent
from .switch_tables import access_switch, core_switch


async def main(access: int, core: int, ready: int) -> None:
    agents = [Agent(access_switch(), port=access), Agent(core_switch(), port=core)]

    async def answer(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", ready)
    try:
        async with server:
            await server.serve_forever()
    finally:
        for a in agents:
            a.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--access", type=int, default=16101)
    p.add_argument("--core", type=int, default=16102)
    p.add_argument("--ready", type=int, default=16100)
    a = p.parse_args()
    asyncio.run(main(a.access, a.core, a.ready))
