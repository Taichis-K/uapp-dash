"""狭いコンソール符号化でも出力で落ちないようにする。

日本語 Windows の既定コードページは **cp932** で、出力をリダイレクトすると
`sys.stdout.encoding` がそれになる（端末へ直接書く場合は Python が UTF-16 API を
使うので表に出ない＝**リダイレクトしたときだけ落ちる**という気づきにくい形になる）。

cp932 に無い文字がひとつでもあると `UnicodeEncodeError` で異常終了し、**診断そのものが
読めなくなる**。これは実際に `uapp-dash doctor > log.txt` で起きた（`―` U+2014）。
とくに痛いのは、隔離やブロックの切り分けが要る環境ほど**素の設定のまま**動かすこと。

文字を選ぶだけでは足りない: 出力には**ユーザー由来の文字列**（プロジェクトのパス、
`--label` に書いた作業名、絵文字を含むこともある）が混ざる。こちらは選べないので、
出せない文字は化けさせて**実行は続ける**。`backslashreplace` を使うのは、
`replace` の `?` と違って元の文字が復元できる形で残るため。
"""
from __future__ import annotations

import sys


def make_output_safe() -> None:
    """標準出力・標準エラーを「出せない文字があっても落ちない」設定にする。

    再構成できないストリーム（差し替え済み・pytest の capture 等）は黙って飛ばす。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            # 既に閉じている／再構成を受け付けない実装。出力の可否はここでは決めない
            pass
