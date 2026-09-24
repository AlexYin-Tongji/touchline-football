"""Portable server entrypoint. One worker owns SQLite and the scheduler."""
import os

import uvicorn

from .build_info import listen_port


if __name__ == "__main__":
    os.environ.setdefault("TOUCHLINE_SERVER_MODE", "true")
    uvicorn.run("server.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", listen_port())),
                workers=1, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))
