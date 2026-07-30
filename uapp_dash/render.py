"""フリートビューの出力: 自己完結 HTML と、更新を見たいときだけのローカル配信。

外部 CDN も追加ファイルも使わない（1 ファイルで完結させる）。
"""
from __future__ import annotations

import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TEMPLATE = Path(__file__).with_name("viewer.html")
DATA_TOKEN = "/*__FLEET_DATA__*/null"


MAX_JSON_DEPTH = 40


def json_safe(value, depth: int = 0):
    """`NaN` / `Infinity` を落として**必ず正しい JSON になる**形に直す。

    記録は外部（他のエージェント・自作ラッパー）が書くもので、`json.loads` は `NaN` や
    `Infinity` を受け付けてしまう。そのまま `json.dumps` すると JavaScript の
    `JSON.parse` / `response.json()` が拒否する文字列になり、**その 1 単位が居る間ずっと
    serve モードの自動更新が失敗する**（ページが黙って古いままになる）。

    **深さも切る**。`json.loads`（C 実装）は深さ 1000 のネストを読めるが、Python 側の
    再帰処理と `json.dumps` は `RecursionError` になる＝ビューアーの生成そのものが落ちる。
    表示に使う構造は数段しかないので、それを超える深さは捨てて構わない。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        if depth >= MAX_JSON_DEPTH:
            return None
        return {str(k): json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if depth >= MAX_JSON_DEPTH:
            return None
        return [json_safe(v, depth + 1) for v in value]
    return value


def dumps(payload) -> str:
    """ビューアーへ渡す JSON 文字列（非有限の数値を除去したうえで直列化する）。"""
    return json.dumps(json_safe(payload), ensure_ascii=False, allow_nan=False)


def render_html(fleet: dict, *, template: Path | None = None) -> str:
    text = (template or TEMPLATE).read_text(encoding="utf-8")
    if DATA_TOKEN not in text:
        raise RuntimeError(f"テンプレートに差し込み口 {DATA_TOKEN} が無い")
    payload = dumps(fleet)
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
                body = dumps(fleet_builder()).encode("utf-8")
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
