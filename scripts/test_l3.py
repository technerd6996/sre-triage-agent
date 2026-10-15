from dotenv import load_dotenv
load_dotenv()
from app.tools.aws_tools import escalate_l3

print(escalate_l3.invoke({"number": "INC0010028", "reason": "Test page, please ignore"}))