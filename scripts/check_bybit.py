import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.exchanges.bybit import BybitClient, BybitError


async def main() -> None:
    client = BybitClient()
    try:
        data = await client.api_key_info()
        result = data["result"]
        api_key = result.get("apiKey", "")
        print(json.dumps({
            "ok": True,
            "api_key": f"{api_key[:4]}…{api_key[-4:]}" if len(api_key) >= 8 else "configured",
            "read_only": result.get("readOnly") == 1,
            "spot_permissions": result.get("permissions", {}).get("Spot", []),
            "ips": result.get("ips", []),
            "uta": result.get("uta"),
            "note": result.get("note", ""),
        }, ensure_ascii=False, indent=2))
    except BybitError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
