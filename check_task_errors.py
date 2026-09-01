import httpx
import json
from dotenv import load_dotenv
load_dotenv(override=True)
from config import Config

auth = (Config.PC_USERNAME, Config.PC_PASSWORD)
base_url = f"https://{Config.PC_HOST}:{Config.PC_PORT}/api"

with httpx.Client(verify=False, timeout=15.0, auth=auth) as client:
    r = client.get(f"{base_url}/prism/v4.3/config/tasks?$limit=5&$orderby=createdTime%20desc")
    for t in r.json().get("data", []):
        print(f"Task: {t.get('extId')} | desc: {t.get('description')} | status: {t.get('status')}")
        print("  Errors:", t.get("errorMessages"))
