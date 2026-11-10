"""What Nuitka compiles. Kept separate from app/server.py because a compiled main module reports a different
location, and app/server.py, app/main.py and app/agent/skills.py find static/, db/ and skills/ relative to themselves."""
import sys

from app.server import main

sys.exit(main())
