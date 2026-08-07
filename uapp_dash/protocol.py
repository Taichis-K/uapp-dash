"""プロトコル v0 の語彙・時刻・既定値（正本は docs/protocol-v0.md）。

ここに定数を集約し、CLI・エミッタ・集約側が同じ語彙を共有する。
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

SCHEMA_UNIT = "uapp-dash/status/0"
SCHEMA_EVENT = "uapp-dash/event/0"

STATUS_DIR_NAME = ".agent-status"

# 宣言できる状態（エージェントが自分で名乗れるもの）
DECLARABLE_STATES = ("running", "waiting-approval", "blocked", "review", "done")
# 読み手（集約）だけが付けられる状態。自己申告できる停滞は停滞ではない
DERIVED_STATES = ("stalled", "crashed", "failed", "aborted", "idle")

# 要注意ファーストの表示順（小さいほど上）
ATTENTION_ORDER = (
    "waiting-approval",
    "blocked",
    "crashed",
    "failed",
    "aborted",
    "stalled",
    "review",
    "running",
    "done",
    "dropped",
    "idle",
)

# 終端状態（もう自分では動かない）。表示側が独自判定しないよう集約が配る
TERMINAL_STATES = ("done", "failed", "aborted", "dropped")

# 要注意の種類。人が取るべき行動が違うので分ける（表示の並びもこの順）
#   human    … 人が動かないと進まない（承認・入力・調整）
#   incident … 壊れている（失敗・中断・プロセス消失・取り残し資源）
#   watch    … 様子がおかしい（停滞・過負荷・データの不整合）
ATTENTION_CATEGORIES = ("human", "incident", "watch")
CATEGORY_OF_STATE = {
    "waiting-approval": "human",
    "blocked": "human",
    "review": "human",
    "crashed": "incident",
    "failed": "incident",
    "aborted": "incident",
    "stalled": "watch",
}

# dropped は「**再開しない**と決めた打ち切り」（意図的な取りやめ・目的自体が不要になった）。
# aborted（外的要因で切れた＝宿題が残りうる → 要注意に出す）と使い分ける。
# 再開されない中断が要注意欄・一覧に赤く残り続けると、本当に手が要るものが埋もれる（実運用の指摘）
RESULTS = ("success", "failure", "aborted", "dropped")
NEEDS = ("approval", "input", "resource")
TASK_STATUSES = ("todo", "done", "dropped")

CLAIM_KINDS = (
    "claim.begin",
    "claim.heartbeat",
    "claim.task",
    "claim.blocked",
    "claim.note",
    "claim.resource",  # 実装時に追加: エージェント自身による資源の取得/解放宣言
    "claim.ack",       # 実装時に追加: 終了済みの失敗・中断を人が「確認した」と記録する
    "claim.supersede",  # 0.1.7 で追加: 後続の begin --supersedes が旧単位へ引き継ぎ先を記録する
    "claim.end",
)
EVIDENCE_KINDS = (
    "evidence.test",
    "evidence.e2e",
    "evidence.build",
    "evidence.git",
    "evidence.resource",
    "evidence.device",
)

PRODUCER_AGENT = "agent"
PRODUCER_TOOL = "tool"

DEFAULT_TTL_SEC = 300
STALL_GRACE_SEC = 60
# TTL の上限（30 日）。これ以上は「事実上無期限」であり、丸めても運用上の意味は変わらない。
# 上限を設けるのは、桁の大きい値をそのまま timedelta に渡すと読み手が例外で落ちるため
MAX_TTL_SEC = 86_400 * 30

# 排他資源 ID の接頭辞（実際に衝突した資源だけ。増やさない）
RESOURCE_PREFIXES = ("editor-play", "build", "device", "host-port")

# デバイス負荷の警告しきい値（これを超えると E2E がコールドスタート待ちで偽陽性全滅する）
DEVICE_LOAD_WARN = 10.0

_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def now() -> datetime:
    """オフセット付きのローカル現在時刻。naive な datetime を作らないための唯一の入口。"""
    return datetime.now().astimezone()


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


def now_iso() -> str:
    return to_iso(now())


def parse_iso(value: str) -> datetime:
    """オフセット付き ISO 8601 を厳格に解釈する。naive なら ValueError。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"オフセットのない時刻は受け付けない: {value!r}")
    return dt


def parse_iso_safe(value) -> tuple[datetime | None, str | None]:
    """壊れた／naive な時刻でも集約を止めないための寛容な解釈。

    戻り値は (時刻, 警告)。naive はローカル時刻とみなしつつ警告を返す。
    """
    if not isinstance(value, str) or not value:
        return None, None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None, f"時刻を解釈できない: {value!r}"
    if dt.tzinfo is None:
        return dt.astimezone(), f"オフセットのない時刻（ローカルとして解釈）: {value!r}"
    return dt, None


def make_unit_id(prefix: str = "u") -> str:
    return f"{prefix}-{now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def valid_unit_id(unit_id: str) -> bool:
    """パス要素として安全か（.. や区切り文字の混入を防ぐ）。"""
    return bool(_UNIT_ID_RE.match(unit_id or "")) and unit_id not in (".", "..")


def valid_resource_id(resource_id: str) -> bool:
    if not resource_id or ":" not in resource_id:
        return False
    return resource_id.split(":", 1)[0] in RESOURCE_PREFIXES


def evidence_ok(data: dict | None):
    """エビデンスの成否。判断できないときは None を返す。"""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("ok"), bool):
        return data["ok"]
    exit_code = data.get("exitCode")
    failed = data.get("failed")
    if exit_code is None and failed is None:
        return None
    if exit_code is not None and exit_code != 0:
        return False
    if isinstance(failed, int) and failed > 0:
        return False
    return True


def summarize_evidence(kind: str, data: dict) -> str:
    """エビデンスの1行要約（書き手側と集約側で同じ文言を使う）。"""
    data = data or {}
    if kind in ("evidence.test", "evidence.e2e"):
        suite = data.get("suite") or kind.split(".")[-1]
        passed, failed = data.get("passed"), data.get("failed")
        if passed is not None or failed is not None:
            total = (passed or 0) + (failed or 0)
            return f"{suite} {passed or 0}/{total}" + (f" 失敗{failed}" if failed else "")
        return f"{suite} exit={data.get('exitCode')}"
    if kind == "evidence.build":
        return f"{data.get('target') or 'build'} exit={data.get('exitCode')}"
    if kind == "evidence.git":
        return f"{data.get('action') or 'commit'} {str(data.get('sha') or '')[:8]} {data.get('subject') or ''}".strip()
    if kind == "evidence.resource":
        return f"{data.get('action')} {data.get('resource')}"
    if kind == "evidence.device":
        return f"{data.get('serial')} load={data.get('load1')}"
    return kind


def seconds_until_overdue(last_heartbeat: datetime, ttl_sec: int, now: datetime,
                          grace: int = STALL_GRACE_SEC) -> int:
    """期限までの残り秒（マイナスなら超過）。**日時への加算を避ける**のが要点。

    `lastHeartbeat` は外部が書く値で、`9999-12-31T23:59:59+00:00` のような極端な日時でも
    ISO として正しく読める。それに TTL を足すと `datetime` の上限を超えて `OverflowError` になり、
    **1 単位で集約全体（view / units / 自動結びつけ）が死ぬ**。経過時間の側で比べれば、
    どんな日時でも計算できる。
    """
    elapsed = (now - last_heartbeat).total_seconds()
    return int(int(ttl_sec) + int(grace) - elapsed)


def overdue_after(last_heartbeat: datetime, ttl_sec: int, grace: int = STALL_GRACE_SEC) -> datetime:
    """期限の時刻。**極端な日時では飽和させる**（呼び手を例外で落とさない）。

    残り秒の判定には `seconds_until_overdue` を使うこと。こちらは表示用。
    """
    try:
        return last_heartbeat + timedelta(seconds=int(ttl_sec) + int(grace))
    except (OverflowError, ValueError):
        return datetime.max.replace(tzinfo=last_heartbeat.tzinfo)


def ttl_of(unit: dict) -> int:
    """単位の TTL（秒）。**欠損・0・負数・非数はすべて既定値**、巨大値は上限で丸める。

    読み手（表示・停滞判定・エミッタの自動結びつけ）は必ずここを通す。別々に正規化すると、
    同じ単位が「TTL 残り 240 秒」なのに「停滞」かつ「エビデンスは ambient」になる
    （実際に食い違った）。`0` を「即座に期限切れ」と読まないのは、ジャーナルから合成した単位や
    TTL を書かない書き手の記録が一斉に切れた扱いになるため。

    **上限で丸めるのは読み手を落とさないため**。`ttlSec` は外部が書く値で、`--ttl` も
    任意精度の整数を受け取る。桁の大きい値をそのまま `timedelta` に渡すと `OverflowError` になり、
    **たった 1 単位で `view` / `units` の集約全体と自動結びつけが死ぬ**
    （「読み手は壊れた記録で止まらない」という v0 の約束に反する）。
    """
    value = unit.get("ttlSec")
    if isinstance(value, bool):
        return DEFAULT_TTL_SEC
    number = None
    if isinstance(value, (int, float)):
        try:
            number = int(value)          # inf は OverflowError、nan は ValueError
        except (OverflowError, ValueError):
            number = None
    elif isinstance(value, str):
        try:
            number = int(value.strip())  # 全角数字や 上付き 2 (U+00B2) は ValueError（isdigit は真になる）
        except ValueError:
            number = None
    if number is None or number <= 0:
        return DEFAULT_TTL_SEC
    return min(number, MAX_TTL_SEC)
