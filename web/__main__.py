"""``python -m web`` 或 ``uv run roleswap-web`` 入口。"""

import os

from web.app import app


def main() -> None:
    port = int(os.getenv("ROLESWAP_WEB_PORT", "7860"))
    host = os.getenv("ROLESWAP_WEB_HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
