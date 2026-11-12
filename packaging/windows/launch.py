"""Started by the Windows service. Everything it calls is in the compiled app package."""
import sys

from app.server import main

sys.exit(main())
