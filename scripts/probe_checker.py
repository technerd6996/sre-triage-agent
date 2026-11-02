import sys
from dotenv import load_dotenv
load_dotenv()
from app.ports import registry

number = sys.argv[1]
tk = registry.get_ticketing(role="reviewer")
print("state as checker:", tk.get(number).state.value)


def attempt(label, fn):
    try:
        fn()
        print(f"OK     {label}")
    except Exception as e:
        print(f"DENIED {label}: {str(e)[:160]}")


attempt("work note only", lambda: tk.add_note(number, "probe: checker write test"))
if "--close" in sys.argv:
    attempt("state to Closed", lambda: tk.close(number, "probe: close test"))