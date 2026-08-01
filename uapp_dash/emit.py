"""`uapp-dash-emit` ― ツールラッパー専用の客観エビデンス出力。

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

from . import __version__, attention, protocol as P
from . import console
from .proc import hostname
from .store import StatusStore

AMBIENT_PREFIX = "ambient-"


SOURCE_EXPLICIT = "explicit"
SOURCE_ENV = "env"
SOURCE_ACTIVE = "active-unit"
SOURCE_AMBIENT = "ambient"
# ambient に落ちた理由が「進行中の単位が TTL 切れだった」場合。後から
# 「なぜこの記録は単位に付かなかったのか」を辿れるようにする（気づけないのが実害だった）
SOURCE_AMBIENT_OVERDUE = "ambient-unit-overdue"


def sole_active_unit(store: StatusStore | None) -> str | None:
    """後方互換の薄いラッパー（unitId だけ返す）。"""
    return sole_active_unit_with_reason(store)[0]


def sole_active_unit_with_reason(store: StatusStore | None) -> tuple[str | None, str | None]:
    """進行中の単位が**ちょうど 1 件**ならその unitId。それ以外は None。

    ラッパー（run-e2e.ps1 / verify.ps1 等）は AI から別プロセスで起動されるため、
    環境変数も unitId も届かない。その結果、申告している最中の作業でもエビデンスだけが
    `ambient-<host>` に落ち、**単位レベルで申告と実測を突き合わせられなかった**
    （二層化の目的そのものが成立しない）。

    候補を絞る条件（**推測で結びつけない**ための線引き）:

    - **このホストが所有する単位だけ**。`.agent-status` を共有している場合、
      別マシンで動いている他人の作業にこちらの実測値を付けてしまう
    - **ハートビートが切れていない単位だけ**。放置された単位に後日の実測値が付くと、
      その単位の申告と実測がまるで同時のものに見える
    - 該当が 0 件（誰の作業でもない）／2 件以上（どちらか決められない）なら ambient のまま
    """
    if store is None or not store.exists():
        return None, None
    here = hostname()
    found: list[str] = []
    overdue: list[str] = []
    for unit in store.list_units(include_done=False):
        unit_id = unit.get("unitId")
        if not unit_id or unit.get("state") in P.TERMINAL_STATES:
            continue
        owner_host = (unit.get("owner") or {}).get("host")
        if owner_host and owner_host != here:
            continue                       # 別マシンの作業に付けない
        # **期限の判定は attention.heartbeat_window に一本化する**（表示と食い違わせない。
        # 時刻が読めない単位も「新鮮だと証明できない」ので候補から外れる）
        if attention.heartbeat_window(unit)["overdue"]:
            overdue.append(unit_id)        # 停滞している単位には付けない
            continue
        found.append(unit_id)
        if len(found) > 1:
            return None, None              # 2 件見つかった時点で決められない（走査も打ち切る）
    if found:
        return found[0], None
    # **なぜ結びつかなかったのか**を呼び手が記録できるようにする。TTL 切れの単位が
    # ちょうど 1 件なら、それが原因だと後から突き合わせられる（気づけないのが実害だった）
    return None, "unit-overdue" if len(overdue) == 1 else None


def resolve_unit_id(explicit: str | None = None, env: dict | None = None,
                    store: StatusStore | None = None) -> str:
    """記録先の unitId を決める。**戻り値は従来どおり文字列**（外部の呼び出しを壊さない）。"""
    return resolve_unit_id_with_source(explicit, env, store)[0]


def resolve_unit_id_with_source(explicit: str | None = None, env: dict | None = None,
                                store: StatusStore | None = None) -> tuple[str, str]:
    """(unitId, 解決根拠) を返す。根拠は記録に残して**後から辿れる**ようにする。"""
    env = os.environ if env is None else env
    if explicit and P.valid_unit_id(explicit):
        return explicit, SOURCE_EXPLICIT
    for key in ("UAPP_DASH_UNIT_ID", "UAPP_E2E_UNIT_ID"):
        candidate = env.get(key)
        if candidate and P.valid_unit_id(candidate):
            return candidate, SOURCE_ENV
    active, reason = sole_active_unit_with_reason(store)
    if active:
        return active, SOURCE_ACTIVE
    # 誰の作業か分からなくても、プロジェクト単位では見えるようにする
    if reason == "unit-overdue":
        return f"{AMBIENT_PREFIX}{hostname()}", SOURCE_AMBIENT_OVERDUE
    return f"{AMBIENT_PREFIX}{hostname()}", SOURCE_AMBIENT


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

    unit_id, source = resolve_unit_id_with_source(unit_id, store=store)
    if source in (SOURCE_ACTIVE, SOURCE_AMBIENT_OVERDUE):
        # 自動で結びつけた／結びつけられなかった理由を記録に残す
        # （後から「なぜこの単位に入ったのか / 入らなかったのか」を辿れるように）
        data = {**(data or {}), "unitIdSource": source}
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
    console.make_output_safe()
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
