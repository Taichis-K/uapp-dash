"""`uapp-dash-emit` — ツールラッパー専用の客観エビデンス出力。

エージェントの自己申告 CLI（`uapp-dash`）とはエントリポイントを分けている（判断 E）。
ラッパーに組み込む前提のため、**失敗しても終了コードを汚さない**（既定で常に 0）。
`.agent-status/` が無ければ完全な no-op。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, protocol as P
from .proc import hostname
from .store import StatusStore

AMBIENT_PREFIX = "ambient-"


def resolve_unit_id(explicit: str | None = None, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    for candidate in (explicit, env.get("UAPP_DASH_UNIT_ID"), env.get("UAPP_E2E_UNIT_ID")):
        if candidate and P.valid_unit_id(candidate):
            return candidate
    # 誰の作業か分からなくても、プロジェクト単位では見えるようにする
    return f"{AMBIENT_PREFIX}{hostname()}"


def _coerce(value: str):
    lowered = value.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def parse_data(json_text: str | None, pairs: list[str] | None) -> dict:
    """`--json` と `--set k=v` を合成する（Windows のクォート事故を避けるため両方用意する）。"""
    data: dict = {}
    if json_text:
        parsed = json.loads(json_text)
        if not isinstance(parsed, dict):
            raise ValueError("--json はオブジェクトであること")
        data.update(parsed)
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--set は key=value 形式: {pair!r}")
        key, value = pair.split("=", 1)
        data[key.strip()] = _coerce(value)
    return data


summarize = P.summarize_evidence  # 集約側と同じ文言を使う


def emit(kind: str, data: dict, *, project: Path | None = None, unit_id: str | None = None,
         create: bool = False) -> dict | None:
    """エビデンスを 1 行**追記するだけ**。書けない状況なら None を返す（例外は出さない）。

    **スナップショットは書かない**。書き手が別プロセスで並行するため、共有ファイルの
    read-modify-write は他コマンド（heartbeat / task / end）の更新を巻き戻す。
    最新エビデンスとスナップショットの無い単位は、集約側がジャーナルから導出する。
    """
    if kind not in P.EVIDENCE_KINDS:
        raise ValueError(f"evidence 系の種別ではない: {kind}（{P.EVIDENCE_KINDS}）")
    store = StatusStore.for_project(project)
    if not store.exists():
        if not create:
            return None  # ダッシュボード未導入環境では完全な no-op
    store.ensure()

    unit_id = resolve_unit_id(unit_id)
    # seq は採番しない（0＝不明）。並行追記では通し番号を保証できず、読み手も順序に依存しない
    return store.append_event(unit_id, kind, data, P.PRODUCER_TOOL, seq=0)


class _QuietParser(argparse.ArgumentParser):
    """引数エラーを例外にするパーサー。

    既定の argparse は usage を stderr に出して SystemExit(2) で落ちる。ラッパーに
    組み込む道具なので、**引数の誤りでも呼び出し元の終了コードと出力を汚さない**。
    """

    def error(self, message):
        raise ValueError(f"引数エラー: {message}")

    def exit(self, status=0, message=None):
        if status:
            raise ValueError(message or f"引数エラー (status={status})")
        raise SystemExit(0)   # --help は従来どおり


def build_parser() -> argparse.ArgumentParser:
    parser = _QuietParser(
        prog="uapp-dash-emit", description="ツールが書く客観エビデンス（エージェントからは使わない）"
    )
    parser.add_argument("--version", action="version", version=f"uapp-dash {__version__}")
    parser.add_argument("kind", choices=list(P.EVIDENCE_KINDS))
    parser.add_argument("--json", dest="json_text", help="data 部分の JSON オブジェクト")
    parser.add_argument("--set", dest="pairs", action="append", help="key=value（--json の代わりに使える）")
    parser.add_argument("--project", help="プロジェクトルート or .agent-status のパス")
    parser.add_argument("--unit-id", help="単位 ID（既定: 環境変数 → ambient-<host>）")
    parser.add_argument("--create", action="store_true", help=".agent-status が無い場合に作成する")
    parser.add_argument("--verbose", action="store_true", help="失敗理由を stderr に出す")
    parser.add_argument("--strict", action="store_true", help="失敗時に非 0 で終わる（テスト用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    verbose = bool(argv and ("--verbose" in argv or "--strict" in argv))
    strict = bool(argv and "--strict" in argv)
    try:
        args = build_parser().parse_args(argv)   # 引数エラーもここで捕まえる
        verbose, strict = args.verbose or args.strict, args.strict
        data = parse_data(args.json_text, args.pairs)
        event = emit(
            args.kind, data,
            project=Path(args.project) if args.project else None,
            unit_id=args.unit_id, create=args.create,
        )
        if event is None and verbose:
            print("no-op: .agent-status が無い", file=sys.stderr)
    except SystemExit:
        raise                      # --help のみ
    except BaseException as exc:   # ラッパーの終了コードを汚さないため握りつぶす
        if verbose:
            print(f"uapp-dash-emit 失敗: {exc}", file=sys.stderr)
        return 1 if strict else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
