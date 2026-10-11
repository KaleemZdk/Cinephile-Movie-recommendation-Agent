"""Terminal chat with the agent: python -m cinephile.agent"""

import asyncio

from cinephile.agent.cli import main

if __name__ == "__main__":
    asyncio.run(main())
