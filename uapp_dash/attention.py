"""派生状態の計算と「要注意ファースト」の並び替え（判断 B / C）。

stalled / crashed は宣言できない。ここでだけ付与する。
"""
from __future__ import annotations

from datetime import datetime

from . import protocol as P
from .proc import alive_on_this_host


def _derived(state: str, declared: str, reasons: list, warnings: list,
             overdue_sec: int, alive, acknowledged: bool = False) -> dict:
    """派生情報の形を 1 か所で決める。

    途中 return ごとに手で組み立てていたため、**終了済みの単位に種別が付かず、
    失敗が要注意の件数から漏れていた**（実運用で発覚）。
    """
    terminal = state in P.TERMINAL_STATES
    # 人が「確認した」と記録した終了済みの失敗・中断は要注意から外す。
    # これが無いと、終わった失敗が永久に要注意欄へ居座り、本当に手を貸すべきものが埋もれる
    # （実運用で発覚。掃除のためにファイルごと消す羽目になった）
    category = None if (terminal and acknowledged) else category_of(state)
    return {
        "state": state, "declaredState": declared, "reasons": reasons, "warnings": warnings,
        "overdueSec": overdue_sec, "alive": alive,
        "terminal": terminal,
        "acknowledged": acknowledged,
        "attentionCategory": category,
        "attentionRank": rank(state),
        # 生存を判定できたか。**pid を持たない単位では常に False**（AI はコマンドごとに
        # 別プロセスなので pid を書けない）＝ crashed は付かず stalled 止まりになる。
        # 表示側がこれを見て「停滞と消失を区別できない」ことを明示できるようにする
        "livenessKnown": alive is not None,
    }


def heartbeat_window(unit: dict, *, now: datetime | None = None,
                     grace: int = P.STALL_GRACE_SEC) -> dict:
    """ハートビートの残り時間と期限切れ。**エミッタの自動結びつけと同じ判定**を使う。

    `state: running` のままでも期限が切れていることがあり、そうなるとツール側の記録は
    ambient に落ちる（設計どおり）。ただし運用では**切れていることに気づけなかった**
    （`lastHeartbeat` と `ttlSec` を人が突き合わせる必要があった）ので、
    残り秒と期限切れフラグを一次情報として出せるようにする。

    **この関数が「期限切れかどうか」の唯一の判定**。表示（`units` / `doctor`）と
    エミッタの自動結びつけが別々に計算していると、同じ単位が「残り 240 秒」なのに
    エビデンスだけ ambient に落ちる、という食い違いが起きる（実際に起きた）。

    - `ttlSec` の欠損・`0` は**既定値として扱う**（0 を「即座に期限切れ」と読むと、
      ジャーナルから合成した単位や古い記録が一斉に切れた扱いになる）
    - **時刻が読めない単位は期限切れ扱い**（`unknown: True`）。新鮮だと証明できないものに
      ツールの実測値を結びつけてはならない
    """
    now = now or P.now()
    ttl = P.ttl_of(unit)
    heartbeat, _ = P.parse_iso_safe(unit.get("lastHeartbeat") or unit.get("startedAt"))
    if heartbeat is None:
        return {"ttlSec": ttl, "remainingSec": None, "overdue": True, "unknown": True,
                "expiresAt": None}
    remaining = P.seconds_until_overdue(heartbeat, ttl, now, grace)
    return {"ttlSec": ttl, "remainingSec": remaining, "overdue": remaining < 0,
            "unknown": False, "expiresAt": P.to_iso(P.overdue_after(heartbeat, ttl, grace))}


def derive(unit: dict, *, now: datetime | None = None, grace: int = P.STALL_GRACE_SEC) -> dict:
    """単位に派生状態を付ける。戻り値は表示用の追加フィールド。"""
    now = now or P.now()
    declared = unit.get("state") or "running"
    reasons: list[str] = []
    warnings: list[str] = []
    acknowledged = bool(unit.get("acknowledgedAt"))

    if unit.get("kind") == "ambient":
        # 作業単位の宣言が無いエビデンスの入れ物。止まっていて当然なので停滞判定しない
        return _derived("idle", declared, reasons, warnings, 0, None)

    if declared == "done":
        result = unit.get("result") or "success"
        state = {"success": "done", "failure": "failed", "aborted": "aborted",
                 "dropped": "dropped"}.get(result, "done")
        superseded_by = unit.get("supersededBy")
        if state == "dropped":
            # 取りやめ＝再開しない意図的な打ち切り。宿題が無いので要注意には出さない
            # （CATEGORY_OF_STATE に無い＝category None）。理由だけ残す
            reasons.append("取りやめで終了（再開しない）")
            return _derived(state, declared, reasons, warnings, 0, None, acknowledged)
        if state != "done":
            reasons.append(f"{'失敗' if state == 'failed' else '中断'}で終了: "
                           f"{(unit.get('result') or '')}")
            if superseded_by:
                # 「打ち切ったが目的は別単位で達成した」― 宿題は残っていないので、
                # ack と同じ扱いで要注意から外す（導入先報告: 消去法の aborted が
                # 要注意欄に居座り、未処理の宿題に見えていた）
                reasons.append(f"引き継ぎ済み（→ {superseded_by}）")
            if acknowledged:
                reasons.append(f"確認済み（{unit.get('acknowledgedAt')}）")
        return _derived(state, declared, reasons, warnings, 0, None,
                        acknowledged or bool(superseded_by))

    heartbeat, warn = P.parse_iso_safe(unit.get("lastHeartbeat") or unit.get("startedAt"))
    if warn:
        warnings.append(warn)
    # TTL の正規化は P.ttl_of に一本化する（表示・停滞判定・エミッタで揃える）
    ttl = P.ttl_of(unit)

    overdue_sec = 0
    state = declared
    alive = alive_on_this_host(unit.get("owner"))

    # 人待ちの状態（blocked / waiting-approval / review）は、止まっているのが正常なので
    # 停滞判定の対象にしない。もともと要注意順の上位に居るため見落とさない
    human_wait = declared in ("blocked", "waiting-approval", "review")

    if heartbeat is None:
        warnings.append("lastHeartbeat も startedAt も読めない")
    elif not human_wait:
        # 残り秒で判定する（日時への加算は極端な lastHeartbeat で OverflowError になる）
        remaining = P.seconds_until_overdue(heartbeat, ttl, now, grace)
        if remaining < 0:
            overdue_sec = -remaining
            if alive is False:
                state = "crashed"
                reasons.append(f"ハートビート途絶（{overdue_sec}秒超過）＋プロセス消失")
            else:
                state = "stalled"
                note = "生存確認不能" if alive is None else "プロセスは生存"
                reasons.append(f"ハートビート途絶（{overdue_sec}秒超過・{note}）")

    if declared in ("blocked", "waiting-approval"):
        blocked = unit.get("blocked") or {}
        if declared == "blocked" and blocked.get("needs") == "approval":
            state = "waiting-approval"
        reasons.append(blocked.get("reason") or ("承認待ち" if state == "waiting-approval" else "ブロック中"))

    return _derived(state, declared, reasons, warnings, overdue_sec, alive)


def rank(state: str) -> int:
    try:
        return P.ATTENTION_ORDER.index(state)
    except ValueError:
        return len(P.ATTENTION_ORDER)


def category_of(state: str) -> str | None:
    """要注意の種類（human / incident / watch）。要注意でなければ None。"""
    return P.CATEGORY_OF_STATE.get(state)


def last_update(unit: dict) -> datetime | None:
    for key in ("endedAt", "lastHeartbeat", "startedAt"):
        parsed, _ = P.parse_iso_safe(unit.get(key))
        if parsed is not None:
            return parsed
    return None


def sort_units(units: list[dict], *, now: datetime | None = None) -> list[dict]:
    """要注意ファースト。同順位内は最終更新が古い順（放置されているものを上に）。"""
    now = now or P.now()

    def key(unit: dict):
        state = (unit.get("derived") or {}).get("state") or unit.get("state") or "running"
        updated = last_update(unit)
        age = (now - updated).total_seconds() if updated else float("inf")
        return (rank(state), -age)

    return sorted(units, key=key)


def progress(unit: dict) -> dict:
    tasks = unit.get("tasks") or []
    done = sum(1 for t in tasks if t.get("status") == "done")
    dropped = sum(1 for t in tasks if t.get("status") == "dropped")
    total = len(tasks)
    return {"done": done, "dropped": dropped, "total": total,
            "ratio": (done / (total - dropped)) if total - dropped > 0 else None}


def detect_mismatch(unit: dict, events: list[dict]) -> list[dict]:
    """自己申告とエビデンスの食い違いを拾う（二層化の実利）。

    - タスク完了申告の後に、同じ単位で赤いエビデンスが出ている
    - 成功で終了したのに、直前のエビデンスが赤い
    """
    findings: list[dict] = []
    last_claim_done: dict | None = None
    last_evidence_red: dict | None = None

    for event in events:
        kind = event.get("kind") or ""
        data = event.get("data") or {}
        if kind == "claim.task" and data.get("status") == "done":
            last_claim_done = event
            last_evidence_red = None
            continue
        if kind.startswith("evidence."):
            ok = P.evidence_ok(data)
            if ok is False:
                last_evidence_red = event
                if last_claim_done is not None:
                    findings.append(
                        {
                            "severity": "mismatch",
                            "at": event.get("at"),
                            "message": (
                                f"タスク「{(last_claim_done.get('data') or {}).get('taskId')}」を完了と申告した後に "
                                f"{kind} が失敗している"
                            ),
                        }
                    )
                    last_claim_done = None
            elif ok is True:
                last_evidence_red = None
            continue
        if kind == "claim.end":
            if data.get("result") == "success" and last_evidence_red is not None:
                findings.append(
                    {
                        "severity": "mismatch",
                        "at": event.get("at"),
                        "message": f"成功として終了したが、直前の {last_evidence_red.get('kind')} が失敗している",
                    }
                )
            continue

    # **終了した後に届いた失敗**も拾う（ラッパーの記録は終了申告と競合しうる。
    # 解決とジャーナル追記の間に end が入ると、成功で閉じた単位の後ろに赤が並ぶ）
    ended_at, ended_success = None, False
    for event in events:
        if (event.get("kind") or "") == "claim.end":
            ended_at = event.get("at")
            ended_success = (event.get("data") or {}).get("result") == "success"
    if ended_at and ended_success:
        for event in events:
            kind = event.get("kind") or ""
            if not kind.startswith("evidence."):
                continue
            if str(event.get("at") or "") <= str(ended_at):
                continue
            if P.evidence_ok(event.get("data") or {}) is False:
                findings.append(
                    {
                        "severity": "mismatch",
                        "at": event.get("at"),
                        "message": f"成功として終了した後に {kind} が失敗している（終了申告 {ended_at} より後の記録）",
                    }
                )
    return findings
