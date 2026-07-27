"""フリートビューの出力: 自己完結 HTML と、更新を見たいときだけのローカル配信。

外部 CDN も追加ファイルも使わない（1 ファイルで完結させる）。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TEMPLATE = Path(__file__).with_name("viewer.html")
DATA_TOKEN = "/*__FLEET_DATA__*/null"


def render_html(fleet: dict, *, template: Path | None = None) -> str:
    text = (template or TEMPLATE).read_text(encoding="utf-8")
    if DATA_TOKEN not in text:
        raise RuntimeError(f"テンプレートに差し込み口 {DATA_TOKEN} が無い")
    payload = json.dumps(fleet, ensure_ascii=False)
    # </script> がデータ中に現れると HTML が壊れるので分割する
    payload = payload.replace("</", "<\\/")
    return text.replace(DATA_TOKEN, payload)


def write_html(fleet: dict, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(fleet), encoding="utf-8")
    return out_path


def serve(*, fleet_builder, port: int = 8788, host: str = "127.0.0.1", open_browser: bool = False) -> int:
    """`fleet.json` を毎回作り直して返す軽量サーバー（ページ側が5秒ごとに取りに来る）。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/fleet.json"):
                body = json.dumps(fleet_builder(), ensure_ascii=False).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            elif self.path in ("/", "/index.html", "/fleet.html"):
                body = render_html(fleet_builder()).encode("utf-8")
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # アクセスログは出さない
            return

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"配信中: {url}（Ctrl+C で終了）")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("終了")
    finally:
        server.server_close()
    return 0
