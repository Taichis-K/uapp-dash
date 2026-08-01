"""`uapp-dash` ― エージェントの自己申告（claim 系）だけを扱う CLI。

**evidence 系のサブコマンドはここに置かない**（判断 E）。ツールからの客観エビデンスは
`uapp-dash-emit`（uapp_dash.emit）から書く。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import (__version__, agents as agents_mod, aggregate, attention, claims as claims_mod,
               console, doctor as doctor_mod, protocol as P, render, shims)
from .store import (RELEASE_BUSY, RELEASE_NOT_OWNER, RELEASE_SETTLED, StatusStore, default_owner,
                    git_exclude_path, gitignore_candidates, has_status_dir_entry)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFLICT = 3


def _store(args) -> StatusStore:
    project = Path(args.project) if getattr(args, "project", None) else None
    return StatusStore.for_project(project)


def _resolve_unit_id(args) -> str:
    unit_id = getattr(args, "unit_id", None) or os.environ.get("UAPP_DASH_UNIT_ID")
    if not unit_id:
        raise SystemExit(
            "単位 ID が分からない。--unit-id を渡すか、環境変数 UAPP_DASH_UNIT_ID を設定すること"
            "（uapp-dash begin が stdout に出力する）"
        )
    if not P.valid_unit_id(unit_id):
        raise SystemExit(f"不正な単位 ID: {unit_id!r}")
    return unit_id


def _load_unit(store: StatusStore, unit_id: str) -> dict:
    unit = store.read_unit(unit_id)
    if unit is None:
        raise SystemExit(f"単位が見つからない: {unit_id}（{store.root}）")
    return unit


def _normalize_ttl(raw) -> int:
    """`--ttl` を**読み手と同じ規則**へ正規化して保存する（保存値と表示をズラさない）。

    書いた値をそのまま入れると、桁の大きい `--ttl` が読み手側で丸められて
    「宣言した TTL と表示される TTL が違う」ことになる。ここで揃えて、丸めたことも伝える。
    """
    ttl = P.ttl_of({"ttlSec": raw})
    if raw is not None and raw != ttl:
        print(f"--ttl {raw} は {ttl} 秒として扱います"
              f"（0 以下・非数は既定 {P.DEFAULT_TTL_SEC} 秒 / 上限 {P.MAX_TTL_SEC} 秒）", file=sys.stderr)
    return ttl


def _touch(unit: dict, *, ttl: int | None = None) -> dict:
    unit["lastHeartbeat"] = P.now_iso()
    if ttl is not None:
        unit["ttlSec"] = _normalize_ttl(ttl)
    return unit


def _bump_event_count(store: StatusStore, unit: dict, kind: str, data: dict) -> None:
    seq = int(unit.get("eventCount") or 0) + 1
    store.append_event(unit["unitId"], kind, data, P.PRODUCER_AGENT, seq=seq)
    unit["eventCount"] = seq


# 何をしたかを呼び手が言えるようにする（「追記したのか、元から在ったのか」が出力から分からず、
# 導入した AI が確認のために .gitignore を開き直していた）
GITIGNORE_ADDED = "added"
GITIGNORE_PRESENT = "present"
GITIGNORE_NO_REPO = "not-a-git-repo"


def ensure_gitignore(project_root: Path, *, use_exclude: bool = False) -> tuple[str, Path | None]:
    """`.agent-status/` の除外記述を保証し、(結果, 対象ファイル) を返す。

    git ルートは上位へ辿って探す（`<repo>/<unity-project>` 構成でプロジェクト直下に
    `.git` が無くても「リポジトリではない」と誤判定しない）。既に書かれていれば触らない。
    追記先は git ルートの .gitignore、`use_exclude=True` なら `.git/info/exclude`
    （コミットされないローカル除外。リリースブランチの diff を汚したくない場合に使う）。
    """
    project_root = Path(project_root).resolve()
    git_root, candidates = gitignore_candidates(project_root)
    if git_root is None:
        return GITIGNORE_NO_REPO, None
    for file in candidates:
        if has_status_dir_entry(file):
            return GITIGNORE_PRESENT, file
    entry = f"{P.STATUS_DIR_NAME}/"
    if use_exclude:
        target = git_exclude_path(git_root)
        if target is None:
            # gitdir ポインタが読めない形式。壊すよりは .gitignore へ倒す
            target = git_root / ".gitignore"
    else:
        target = git_root / ".gitignore"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        text = target.read_text(encoding="utf-8", errors="replace")
        prefix = "" if text.endswith("\n") or not text else "\n"
        with target.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{prefix}\n# エージェント開発ステータス（ホスト固有の一時状態）\n{entry}\n")
    else:
        target.write_text(
            f"# エージェント開発ステータス（ホスト固有の一時状態）\n{entry}\n", encoding="utf-8"
        )
    return GITIGNORE_ADDED, target


# --- サブコマンド -------------------------------------------------------


def _install_agent_rules(project_root: Path, agents: str, status_dir: Path,
                         *, replace_block: bool = False) -> None:
    """申告規約を配置し、結果を人に分かる形で出す（既存ファイルは書き換えない）。"""
    for result in agents_mod.install(project_root, agents, status_dir=status_dir,
                                     replace_block=replace_block):
        path, status = result["path"], result["status"]
        if status == agents_mod.CREATED:
            print(f"作成: {path}（{result['agent']} 向けの申告規約）")
        elif status == agents_mod.UPDATED:
            print(f"更新: {path}（{result['agent']} 向けの申告規約）")
        elif status == agents_mod.REPLACED_BLOCK:
            print(f"更新: {path}（マーカー間の規約だけ差し替え。マーカー外の記述はそのまま）")
        elif status == agents_mod.UNCHANGED:
            print(f"変更なし: {path}")
        else:
            if status == agents_mod.SKIPPED_MODIFIED:
                print(f"変更しなかった: {path}（このツールが書いた後で編集されているため上書きしない）")
            else:   # SKIPPED_FOREIGN
                print(f"変更しなかった: {path}（このツールが作ったファイルではないため自動では書き換えない）")
            # **手で 100 行貼る作業を勧めない**（導入先で 3 版連続の手作業になり、事故のもとだった）。
            # マーカーがあるなら機械的に差し替えられる
            if agents_mod.has_managed_block(path):
                print("マーカー間だけ差し替えるなら（マーカー外の記述は触りません）:")
                print(f"  uapp-dash --project {agents_mod.quote_for_cmd(project_root)} init "
                      f"--agents {result['agent']} --replace-marker-block")
            else:
                print(f"規約ブロックだけをファイルへ書き出すなら（手で差し込む用）:")
                print(f"  uapp-dash --project {agents_mod.quote_for_cmd(project_root)} init "
                      f"--agents {result['agent']} --emit-convention <出力先>")
            print("手で統合する場合は以下を begin/end マーカー行ごと一字一句そのまま貼る"
                  "（古い規約ブロックが残っていれば消す ― 併存していると診断は未了のまま）:")
            print("-" * 60)
            print(agents_mod.render(result["agent"]))
            print("-" * 60)


def cmd_init(args) -> int:
    store = _store(args)
    # 規約ブロックの書き出しは**配置とは別の関心事**（`.agent-status` も作らず、何も変更しない）。
    # 自分の手順で差し込みたい人が、標準出力からのコピペで崩さずに済むようにする
    if getattr(args, "emit_convention", None):
        if not args.agents or args.agents == "both":
            raise SystemExit("--emit-convention は書き出す種別が要る: --agents claude | codex")
        out = Path(args.emit_convention)
        # **既存ファイルは絶対に切り詰めない**。出力先に AGENTS.md のような実ファイルを
        # 指定されたら、守るべきユーザーの記述を丸ごと消してしまう
        # （規約を守らせるための機能が規約を消す）。マーカーの有無で「自分の物」と
        # 判断しないのはこのモジュール全体の原則でもある ― マーカーは統合用スニペットにも
        # 入るので、所有の証拠にならない。
        # **`exists()` で確かめてから書く形にはしない**（確認と書き込みの間に別プロセスが
        # 作ったファイルを切り詰める）。OS の排他的作成（"x"）に判定そのものを任せる
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            with out.open("x", encoding="utf-8", newline="\n") as fh:
                fh.write(agents_mod.render(args.agents))
        except FileExistsError:
            raise SystemExit(f"--emit-convention の出力先が既にある: {out}"
                             "（既存ファイルは上書きしない。別のパスを指定するか、"
                             "不要なら消してから実行する）") from None
        print(f"書き出し: {out}（{args.agents} 向けの規約ブロック。begin/end マーカーを含む）")
        return EXIT_OK
    store.ensure()
    project_root = store.project_root
    gitignore, gitignore_path = ensure_gitignore(project_root, use_exclude=bool(args.git_exclude))
    print(f"作成: {store.root}")
    # 「やらなかった」ことも言う（黙って何も出さないと、追記漏れなのか既存なのか分からない）
    if gitignore == GITIGNORE_ADDED:
        print(f"追記: {gitignore_path} に {P.STATUS_DIR_NAME}/")
    elif gitignore == GITIGNORE_PRESENT:
        print(f"確認: {gitignore_path} には {P.STATUS_DIR_NAME}/ が既にある（追記なし）")
    else:
        print(f"対象外: git リポジトリではない（上位にも .git が無い）ので除外記述は触らない（{project_root}）")
    aggregate.register_project(project_root)
    if args.agents:
        _install_agent_rules(project_root, args.agents, store.root,
                             replace_block=bool(getattr(args, "replace_marker_block", False)))
    else:
        print("ヒント: `uapp-dash init --agents both` で AI 向けの申告規約を配置できる"
              "（これが無いと AI は uapp-dash を打つべきだと知らない）")
    return EXIT_OK


def cmd_install_shims(args) -> int:
    """exe を作れない環境向けに、同じコマンド名で呼べる `.cmd` を置く。

    **`.agent-status` は作らない**（監視対象の設定ではなく、この端末の実行環境の話）。
    """
    try:
        written = shims.install(Path(args.dir), force=bool(args.force))
    except shims.ShimError as exc:
        raise SystemExit(str(exc)) from exc
    for path in written:
        print(f"作成: {path}")
    if not any(p.suffix == ".ps1" for p in written):
        # **`.ps1` は PowerShell から呼んだときに引数が壊れない安全な経路**。実行ポリシーで
        # 動かない環境では置かない（置くと `.cmd` より優先されて、動いていたものが壊れる）
        print("注意: `.ps1` シムは置きませんでした（この環境の PowerShell では実行ポリシーにより"
              "動かないため）。`.cmd` だけになるので、下の「壊れる引数」が **PowerShell から"
              "呼んだ場合にも当てはまります**")
    directory = Path(args.dir).resolve()
    # **`quote_for_cmd` は使えない**。あれは「引数の位置」用で、安全な文字だけなら裸で出す。
    # ここは `$env:PATH = … + ";" + …` という**式**なので、裸のパスは構文エラーになる
    # （`D:\tools\bin + ";" + $env:PATH` は「そんなコマンドは無い」で落ちる。実測）。
    # 式に埋めるものは常に引用する。単引用符にするのは `$` を展開させないため
    quoted = "'" + str(directory).replace("'", "''") + "'"
    print(f"PATH に {directory} を**前方**へ追加すると `uapp-dash` / `uapp-dash-emit` として呼べる"
          "（exe が別の場所にあると、PATH で先に見つかった方が使われる）:")
    print(f'  $env:PATH = {quoted} + ";" + $env:PATH        # 現在のセッションだけ')
    # **PATH を書き換えるワンライナーは案内しない**。どれも既存の PATH を壊しうる:
    #   - `setx` は 1024 文字で切り捨てて保存する（長い環境では実行した瞬間に後半が消える）。
    #     さらに PowerShell の `$env:PATH` は Machine と User の合成値なので、
    #     それを User へ書き戻すと Machine 側の全項目が User へ複製される
    #   - `[Environment]::SetEnvironmentVariable(…, "User")` も安全ではない。
    #     User PATH が REG_EXPAND_SZ で `%USERPROFILE%\…` を含む場合、
    #     GetEnvironmentVariable は展開済みの値を返し、Set は REG_SZ で書き戻すため、
    #     **参照が固定値に化けて将来の変更に追従しなくなる**（重複追加も防げない）
    # 導入を助けるコマンドが開発環境を壊してはならないので、GUI を案内する
    print("永続化は **GUI で行う**（PATH を書き換えるワンライナーは、"
          "切り捨て・Machine 項目の複製・`%USERPROFILE%` 等の参照が固定値に化ける、"
          "のいずれかを踏む）:")
    print('  rundll32 sysdm.cpl,EditEnvironmentVariables    # 「ユーザー環境変数」の Path を編集し、')
    print(f'                                                # 先頭に {directory} を追加する')
    print("確認: `uapp-dash --version` が版を返し、`uapp-dash doctor` の"
          "「コマンドが使える」が [済] になること")
    print("注意: `.cmd` は cmd に解釈されるため、**引数の値に `&` `|` `<` `>` `^` `%` が入ると"
          "壊れる**（`--label \"A&B\"` はラベルが \"A\" になり \"B\" が実行される＝"
          "自由テキスト経由のコマンド実行）。**PowerShell からは併設した `.ps1` が優先される**"
          "ので実運用の経路は安全だが、cmd.exe や Python の subprocess から呼ぶ場合は"
          "この制限が生きる（`.ps1` を置けなかった場合は PowerShell も同じ）")
    return EXIT_OK


def cmd_doctor(args) -> int:
    store = _store(args)
    checks = doctor_mod.run_checks(Path(args.project) if getattr(args, "project", None) else None)
    print(doctor_mod.format_checks(store.project_root, checks))
    return EXIT_OK if not any(c["status"] == doctor_mod.NG for c in checks) else 1


# 申告に「自分で書いたテスト件数」が混ざるのを見つける正規表現。
# 二層化（自己申告とツールのエビデンスを分ける）の前提は、申告側に数字が無いこと。
# 申告に数字があると、エビデンスと食い違っていても人が申告を読んで納得してしまう。
# **止めない**（作業を邪魔しない）が、1 行警告するだけで抑止になる（導入先の AI が
# 「規約を承知していたのに --summary に件数を書いた」と自己報告した）
_SELF_REPORTED_NUMBERS = re.compile(
    # 39/39・15 / 15（日付 2026/07/30 と issue 番号 #20/#21 は拾わない）
    r"(?<![\d/.#])(?!\d{4}\s*/)\d{1,4}\s*/\s*\d{1,4}(?![\d/.])"
    r"|\d+\s*(?:passed|failed|tests?\b)"                 # 39 passed・2 failed
    r"|(?:passed|failed)\s*[:=]\s*\d+"
    r"|\d+\s*件?\s*(?:通過|パス|成功|失敗)"                # 15 件通過・8 通過
    r"|(?:EditMode|PlayMode|E2E|pytest)\s*\d+",          # EditMode 39 / PlayMode 19
    re.IGNORECASE,
)


def warn_self_reported_numbers(text: str | None, field: str) -> bool:
    """申告テキストにテスト件数らしき数字があれば警告する（真偽を返すのはテスト用）。"""
    if not text or not _SELF_REPORTED_NUMBERS.search(text):
        return False
    print(f"規約に反しています: {field} にテスト件数らしき数字があります"
          "（数字はツール側のエミッタが書きます。申告には「何をやり遂げたか」だけを書く）",
          file=sys.stderr)
    return True


def _warn_claim_separators(offenders: list[str]) -> None:
    """`;` 区切りで書かれた claims を「ほどいた」ことを知らせる（黙って直さない）。"""
    if not offenders:
        return
    print(f"--claims は空白区切りです（`;` 区切りは --tasks だけ）。"
          f"`;` を含む値をほどいて登録しました: {', '.join(offenders)}。"
          '正しくは --claims "a/**" "b/**" のように値ごとに分ける'
          "（本当に `;` を含むパスなら `\\;` とエスケープする）", file=sys.stderr)


def _parse_tasks(spec: str | None) -> list[dict]:
    if not spec:
        return []
    tasks = []
    for index, raw in enumerate([part.strip() for part in spec.split(";") if part.strip()], start=1):
        if "=" in raw:
            task_id, title = raw.split("=", 1)
            task_id, title = task_id.strip(), title.strip()
        else:
            task_id, title = f"t{index}", raw
        tasks.append({"id": task_id, "title": title, "status": "todo"})
    return tasks


def cmd_begin(args) -> int:
    store = _store(args)
    store.ensure()
    project_root = store.project_root
    ensure_gitignore(project_root)  # 戻り値は見ない（begin の本流は申告。除外は最善努力）

    unit_id = args.unit_id or P.make_unit_id()
    if not P.valid_unit_id(unit_id):
        raise SystemExit(f"不正な単位 ID: {unit_id!r}")
    owner = default_owner(agent=args.agent, session=args.session, unit_id=unit_id, pid=args.pid)
    claim_values, claim_offenders = claims_mod.split_separators(args.claims or [])
    unit = {
        "schema": P.SCHEMA_UNIT,
        "unitId": unit_id,
        "project": {"path": str(project_root), "name": project_root.name},
        "label": args.label,
        "owner": owner,
        "state": "running",
        "activity": args.activity or "",
        "startedAt": P.now_iso(),
        "lastHeartbeat": P.now_iso(),
        "ttlSec": _normalize_ttl(args.ttl),
        "tasks": _parse_tasks(args.tasks),
        "claims": claims_mod.normalize_claims(claim_values),
        "resources": [],
        "lastEvidence": None,
        "eventCount": 0,
        "endedAt": None,
        "result": None,
    }
    _bump_event_count(
        store,
        unit,
        "claim.begin",
        {"label": unit["label"], "tasks": unit["tasks"], "claims": unit["claims"], "owner": owner},
    )
    store.write_unit(unit)
    aggregate.register_project(project_root)
    _warn_claim_separators(claim_offenders)
    warn_self_reported_numbers(args.activity, "--activity")
    print(unit_id)
    # 使い方は **stderr** に出す（stdout は unitId だけ。`$u = uapp-dash begin …` を壊さない）
    print(f"以後のコマンドに --unit-id {unit_id} を付ける。"
          "テスト/ビルドのラッパーからツール側の記録を撃つときも同じ値を渡すと確実に結びつく"
          "（渡さない場合、このプロジェクトで進行中の単位が 1 件だけなら自動で結びつく）",
          file=sys.stderr)
    return EXIT_OK


def cmd_heartbeat(args) -> int:
    store = _store(args)
    unit_id = _resolve_unit_id(args)
    unit = _load_unit(store, unit_id)
    changed = False
    if args.state:
        if args.state not in P.DECLARABLE_STATES or args.state == "done":
            raise SystemExit(
                f"宣言できない状態: {args.state}（{[s for s in P.DECLARABLE_STATES if s != 'done']} のいずれか。"
                "done は uapp-dash end を使う。stalled/crashed は集約が付ける）"
            )
        changed = changed or unit.get("state") != args.state
        unit["state"] = args.state
        if args.state != "blocked":
            unit.pop("blocked", None)
    if args.activity is not None:
        changed = changed or unit.get("activity") != args.activity
        unit["activity"] = args.activity
        warn_self_reported_numbers(args.activity, "--activity")
    _touch(unit, ttl=args.ttl)
    # 状態も作業内容も変わらないハートビートはジャーナルに書かない（肥大化防止）
    if changed:
        _bump_event_count(
            store, unit, "claim.heartbeat",
            {"state": unit.get("state"), "activity": unit.get("activity"), "ttlSec": unit.get("ttlSec")},
        )
    store.write_unit(unit)
    return EXIT_OK


def cmd_task(args) -> int:
    store = _store(args)
    unit_id = _resolve_unit_id(args)
    unit = _load_unit(store, unit_id)
    status = "dropped" if args.drop else "done"
    tasks = unit.setdefault("tasks", [])
    target = next((t for t in tasks if t.get("id") == args.task_id), None)
    if target is None:
        if not args.title:
            known = ", ".join(t.get("id", "?") for t in tasks) or "（登録なし）"
            raise SystemExit(f"タスク {args.task_id} が無い。既知のID: {known}。新規なら --title を渡す")
        target = {"id": args.task_id, "title": args.title, "status": "todo"}
        tasks.append(target)
    target["status"] = status
    if args.note:
        target["note"] = args.note
    _touch(unit)
    _bump_event_count(store, unit, "claim.task", {"taskId": args.task_id, "status": status, "note": args.note})
    store.write_unit(unit)
    return EXIT_OK


def cmd_blocked(args) -> int:
    store = _store(args)
    unit_id = _resolve_unit_id(args)
    unit = _load_unit(store, unit_id)
    if args.needs not in P.NEEDS:
        raise SystemExit(f"--needs は {P.NEEDS} のいずれか")
    unit["state"] = "blocked"
    unit["blocked"] = {"reason": args.reason, "needs": args.needs, "resource": args.resource}
    _touch(unit)
    _bump_event_count(store, unit, "claim.blocked", dict(unit["blocked"]))
    store.write_unit(unit)
    return EXIT_OK


def cmd_ack(args) -> int:
    """終了済みの失敗・中断を「人が確認した」と記録する。

    確認するまで要注意欄に残り続けるのは正しい（見落とさないため）が、**外す手段が無い**と
    終わった失敗が永久に居座り、本当に手を貸すべきものが埋もれる（実運用で発覚し、
    掃除のために記録ファイルごと消す羽目になった）。
    """
    store = _store(args)
    unit_id = _resolve_unit_id(args)
    unit = _load_unit(store, unit_id)
    if unit.get("state") not in P.TERMINAL_STATES:
        raise SystemExit(f"確認済みにできるのは終了した単位だけ: {unit_id} は {unit.get('state')}"
                         "（進行中の単位は end で閉じる）")
    unit["acknowledgedAt"] = P.now_iso()
    if args.note:
        unit["acknowledgedNote"] = args.note
    _bump_event_count(store, unit, "claim.ack", {"note": args.note} if args.note else {})
    store.write_unit(unit)
    print(f"確認済みにした: {unit_id}")
    return EXIT_OK


def cmd_end(args) -> int:
    store = _store(args)
    unit_id = _resolve_unit_id(args)
    unit = _load_unit(store, unit_id)
    if args.result not in P.RESULTS:
        raise SystemExit(f"--result は {P.RESULTS} のいずれか")
    warn_self_reported_numbers(args.summary, "--summary")
    # 掴んだままの排他資源を解放してから終わる（取り残しロックを作らない）。
    # 「今は触れない（busy）」だけを未解決として残す。「記録が無い」「別の単位が取り直した」は
    # もう自分のものではないので、いつまでも抱えない（end が永久に成功しなくなる）
    locks = dict(unit.get("resourceLocks") or {})
    busy = []
    for resource_id in list(unit.get("resources") or []):
        status = store.release_resource(resource_id, {"unitId": unit_id}, lock_id=locks.get(resource_id))
        if status in RELEASE_SETTLED:
            locks.pop(resource_id, None)
        else:
            busy.append(resource_id)
    unit["resources"] = busy
    unit["resourceLocks"] = {k: v for k, v in locks.items() if k in busy}

    if busy:
        # **done にはしない**（done にすると集約でも表示でも「終わったもの」として畳まれ、
        # 塞がったままの資源が視界から消える）。人の対処が要る状態として上位に出す
        unit["state"] = "blocked"
        unit["blocked"] = {"reason": f"終了処理で排他資源を解放できない: {', '.join(busy)}",
                           "needs": "resource", "resource": busy[0]}
        unit["pendingEnd"] = {"result": args.result, "summary": args.summary}
        _touch(unit)
        _bump_event_count(store, unit, "claim.blocked", dict(unit["blocked"]))
        store.write_unit(unit)
        print(f"排他資源を解放できなかった: {', '.join(busy)}"
              "（他プロセスが操作中。時間をおいて uapp-dash end をやり直すか、"
              "保持者が居ないと確認できたら uapp-dash resource release --force）", file=sys.stderr)
        return EXIT_CONFLICT

    unit["state"] = "done"
    unit["result"] = args.result
    unit["endedAt"] = P.now_iso()
    unit.pop("blocked", None)
    unit.pop("pendingEnd", None)
    _touch(unit)
    _bump_event_count(store, unit, "claim.end", {"result": args.result, "summary": args.summary})
    store.write_unit(unit)
    store.archive_unit(unit_id)
    return EXIT_OK


def cmd_resource(args) -> int:
    store = _store(args)
    store.ensure()
    unit_id = getattr(args, "unit_id", None) or os.environ.get("UAPP_DASH_UNIT_ID")
    holder = default_owner(unit_id=unit_id)
    unit = store.read_unit(unit_id) if unit_id else None
    if unit is not None:
        holder["label"] = unit.get("label")

    if args.action == "acquire":
        ok, current, lock_id = store.acquire_resource(args.resource_id, holder)
        if not ok:
            who = (current or {}).get("label") or (current or {}).get("unitId") or "不明な保持者"
            print(f"取得できない: {args.resource_id} は {who} が保持中", file=sys.stderr)
            if unit is not None:
                _bump_event_count(store, unit, "claim.resource",
                                  {"resource": args.resource_id, "action": "denied", "holder": current})
                store.write_unit(unit)
            return EXIT_CONFLICT
        if unit is not None:
            resources = unit.setdefault("resources", [])
            if args.resource_id not in resources:
                resources.append(args.resource_id)
            # 自分のロックだけを解放できるよう lockId を控える（他者が取り直したロックを消さない）
            unit.setdefault("resourceLocks", {})[args.resource_id] = lock_id
            _touch(unit)
            _bump_event_count(store, unit, "claim.resource", {"resource": args.resource_id, "action": "acquire"})
            store.write_unit(unit)
        else:
            # 単位に紐付けられないと lockId の保管先が無い。解放できるよう呼び手へ渡す
            print(lock_id)
        return EXIT_OK

    lock_id = args.lock_id or ((unit or {}).get("resourceLocks") or {}).get(args.resource_id)
    status = store.release_resource(args.resource_id, holder, force=args.force, lock_id=lock_id)
    if unit is not None and status in RELEASE_SETTLED:
        unit["resources"] = [r for r in (unit.get("resources") or []) if r != args.resource_id]
        (unit.get("resourceLocks") or {}).pop(args.resource_id, None)
        _touch(unit)
        _bump_event_count(store, unit, "claim.resource",
                          {"resource": args.resource_id, "action": "release", "status": status})
        store.write_unit(unit)
    if status == RELEASE_BUSY:
        print(f"解放しなかった: {args.resource_id}（他プロセスが操作中。時間をおいて再試行）", file=sys.stderr)
        return EXIT_CONFLICT
    if status == RELEASE_NOT_OWNER:
        print(f"解放しなかった: {args.resource_id}（別の保持者のロック。"
              "自分のものなら --lock-id、保持者が居ないと確認できたなら --force）", file=sys.stderr)
        return EXIT_CONFLICT
    return EXIT_OK


def cmd_view(args) -> int:
    fleet = aggregate.build_fleet(projects=[Path(p) for p in args.project] if args.project else None)
    if args.serve:
        return render.serve(fleet_builder=lambda: aggregate.build_fleet(
            projects=[Path(p) for p in args.project] if args.project else None),
            port=args.port, open_browser=args.open)
    out = Path(args.out) if args.out else Path.cwd() / "fleet.html"
    render.write_html(fleet, out)
    print(str(out))
    if args.open:
        import webbrowser

        webbrowser.open(out.as_uri())
    return EXIT_OK


def cmd_units(args) -> int:
    """このプロジェクトの単位を一覧する。

    unitId は以後のコマンドすべてに必要なのに、控え忘れると探す手段が無かった
    （実運用で困った）。ここで「自分の作業がどれか」を label と経過時間から選べるようにする。
    """
    import json

    store = _store(args)
    project = aggregate.build_project(store.project_root)
    units = project["units"]
    if not args.all:
        units = [u for u in units if (u.get("derived") or {}).get("state") not in ("done", "failed", "aborted", "idle")]
    if args.json:
        print(json.dumps([{k: u.get(k) for k in ("unitId", "label", "state", "activity", "owner",
                                                 "startedAt", "lastHeartbeat", "claims", "resources")}
                          | {"derivedState": (u.get("derived") or {}).get("state"),
                             # **`state: running` と期限切れは両立する**。切れているとツール側の
                             # 記録は ambient に落ちるので、残り秒と期限切れを一次情報として出す
                             "heartbeat": attention.heartbeat_window(u)}
                          for u in units],
                         ensure_ascii=False, indent=1))
        return EXIT_OK
    if not units:
        print(f"進行中の単位はない（{store.root}）。--all で完了分も表示する")
        return EXIT_OK
    for unit in units:
        derived = unit.get("derived") or {}
        owner = unit.get("owner") or {}
        window = attention.heartbeat_window(unit)
        if window["unknown"]:
            ttl_text = "**時刻が読めないため期限切れ扱い（ツールの記録は ambient に落ちる）**"
        elif window["overdue"]:
            ttl_text = (f"**TTL 切れ（{-window['remainingSec']}秒超過）"
                        "→ ツールの記録は ambient に落ちる。heartbeat --ttl を伸ばす**")
        else:
            ttl_text = f"TTL 残り {window['remainingSec']}秒"
        print("{:<26} {:<16} {:<34} {}".format(
            unit.get("unitId", "?"), derived.get("state", "?"),
            (unit.get("label") or "")[:32], (unit.get("activity") or "")[:40]))
        print("{:<26} {} / 最終更新 {} / {}".format(
            "", owner.get("agent") or "?", unit.get("lastHeartbeat") or "?", ttl_text))
    return EXIT_OK


def cmd_show(args) -> int:
    """デバッグ用: 単位のスナップショットをそのまま出す。"""
    import json

    store = _store(args)
    unit_id = _resolve_unit_id(args)
    print(json.dumps(_load_unit(store, unit_id), ensure_ascii=False, indent=1))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uapp-dash", description="エージェント開発ステータス（自己申告側）")
    # doctor が「PATH のコマンドが本当にこの実装か」を突き合わせるのに使う
    parser.add_argument("--version", action="version", version=f"uapp-dash {__version__}")
    parser.add_argument("--project", help="プロジェクトルート or .agent-status のパス（既定: 上位探索）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help=".agent-status を作成し .gitignore に追記する")
    p_init.add_argument("--git-exclude", action="store_true",
                        help=".gitignore でなく git のローカル除外（.git/info/exclude）へ追記する"
                             "（コミットされないのでリポジトリの diff を汚さない。他の作業者には共有されない）")
    p_init.add_argument("--replace-marker-block", action="store_true",
                        help="既存ファイルの begin/end マーカー間だけを現行の規約へ差し替える"
                             "（マーカー外の記述は触らない）。マーカー外に自作の規約を足している"
                             "構成で、更新のたびに全文を手で貼り直す作業をなくすためのもの")
    p_init.add_argument("--emit-convention", metavar="PATH",
                        help="規約ブロックをこのファイルへ書き出して終了する（配置はしない）。"
                             "自分の手順で差し込みたい場合に使う")
    p_init.add_argument("--agents", choices=list(agents_mod.AGENT_CHOICES),
                        help="AI 向けの申告規約を配置する（claude=.claude/rules/agent-dash.md / "
                             "codex=ルートの AGENTS.md。既存の AGENTS.md は書き換えずスニペットを表示する）")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", help="導入状況を自己診断する（[済]/[未] 表示・未了があれば終了コード 1）")
    p_doctor.set_defaults(func=cmd_doctor)

    p_shims = sub.add_parser(
        "install-shims",
        help="exe を使わずに uapp-dash / uapp-dash-emit を名乗る .cmd を作る（Windows 専用）")
    p_shims.add_argument("--dir", required=True, metavar="PATH",
                         help="出力先ディレクトリ。**exe のある場所とは別**にして PATH の前方に入れる")
    p_shims.add_argument("--force", action="store_true", help="既存の同名ファイルを上書きする")
    p_shims.set_defaults(func=cmd_install_shims)

    p_begin = sub.add_parser("begin", help="開発単位を開始し unitId を出力する")
    p_begin.add_argument("--label", required=True, help="人が読む単位名")
    p_begin.add_argument("--tasks", help='";" 区切り。"id=タイトル" 形式も可')
    p_begin.add_argument("--claims", nargs="*", help="編集予定のパス/グロブ")
    p_begin.add_argument("--ttl", type=int, default=P.DEFAULT_TTL_SEC, help="ハートビートの有効期間（秒）")
    p_begin.add_argument("--activity", help="開始時の作業内容")
    p_begin.add_argument("--agent", help="エージェント名")
    p_begin.add_argument("--session", help="セッション識別子")
    p_begin.add_argument("--pid", type=int, help="持続プロセスの pid（生存判定に使う。無ければ判定しない）")
    p_begin.add_argument("--unit-id", help="単位 ID を明示指定する")
    p_begin.set_defaults(func=cmd_begin)

    p_hb = sub.add_parser("heartbeat", help="生存と現在の作業を更新する")
    p_hb.add_argument("--state", choices=[s for s in P.DECLARABLE_STATES if s != "done"])
    p_hb.add_argument("--activity")
    p_hb.add_argument("--ttl", type=int)
    p_hb.add_argument("--unit-id")
    p_hb.set_defaults(func=cmd_heartbeat)

    p_task = sub.add_parser("task", help="タスクの消化を申告する")
    p_task.add_argument("task_id")
    group = p_task.add_mutually_exclusive_group(required=True)
    group.add_argument("--done", action="store_true")
    group.add_argument("--drop", action="store_true")
    p_task.add_argument("--note")
    p_task.add_argument("--title", help="未登録のタスクを新規追加するときのタイトル")
    p_task.add_argument("--unit-id")
    p_task.set_defaults(func=cmd_task)

    p_blocked = sub.add_parser("blocked", help="ブロック状態を申告する")
    p_blocked.add_argument("--reason", required=True)
    p_blocked.add_argument("--needs", required=True, choices=P.NEEDS)
    p_blocked.add_argument("--resource")
    p_blocked.add_argument("--unit-id")
    p_blocked.set_defaults(func=cmd_blocked)

    p_end = sub.add_parser("end", help="開発単位を終了する")
    p_end.add_argument("--result", required=True, choices=P.RESULTS)
    p_end.add_argument("--summary")
    p_end.add_argument("--unit-id")
    p_end.set_defaults(func=cmd_end)

    p_ack = sub.add_parser("ack", help="終了済みの失敗・中断を確認済みにする（要注意欄から外す）")
    p_ack.add_argument("--unit-id")
    p_ack.add_argument("--note", help="確認した内容（対応済み・既知の問題 等）")
    p_ack.set_defaults(func=cmd_ack)

    p_res = sub.add_parser("resource", help="排他資源の取得/解放")
    p_res.add_argument("action", choices=["acquire", "release"])
    p_res.add_argument("resource_id", help=f"例: editor-play:<path> / device:<serial>:<port>（接頭辞 {P.RESOURCE_PREFIXES}）")
    p_res.add_argument("--force", action="store_true", help="保持者が違っても解放する")
    p_res.add_argument("--lock-id", help="解放時に使うロックID（単位に紐付けずに取得した場合、acquire が出力する）")
    p_res.add_argument("--unit-id")
    p_res.set_defaults(func=cmd_resource)

    p_view = sub.add_parser("view", help="フリートビュー HTML を生成/配信する")
    p_view.add_argument("--out", help="出力先 HTML（既定: ./fleet.html）")
    p_view.add_argument("--serve", action="store_true", help="ローカルサーバーで配信し自動更新する")
    p_view.add_argument("--port", type=int, default=8788)
    p_view.add_argument("--open", action="store_true", help="ブラウザで開く")
    p_view.set_defaults(func=cmd_view)

    p_units = sub.add_parser("units", help="このプロジェクトの単位を一覧する（unitId を思い出すため）")
    p_units.add_argument("--all", action="store_true", help="完了した単位も含める")
    p_units.add_argument("--json", action="store_true", help="機械可読な JSON で出す")
    p_units.set_defaults(func=cmd_units)

    p_show = sub.add_parser("show", help="単位のスナップショットを表示する（デバッグ用）")
    p_show.add_argument("--unit-id")
    p_show.set_defaults(func=cmd_show)

    # `uapp-dash view --project X` のようにサブコマンドの後ろへ書いても通るようにする
    # （前にしか置けないと、素直に書いた側が「unrecognized arguments」で弾かれる）。
    # default=SUPPRESS ＝ 指定が無ければグローバル側の値を上書きしない
    for subparser in (p_init, p_doctor, p_begin, p_hb, p_task, p_blocked, p_end, p_res, p_view,
                      p_units, p_show):
        subparser.add_argument("--project", default=argparse.SUPPRESS,
                               help="対象プロジェクト（uapp-dash --project と同じ。どちらの位置でもよい）")
    return parser


def main(argv: list[str] | None = None) -> int:
    console.make_output_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    # view は複数プロジェクトを受け取れるようにする
    if args.command == "view":
        args.project = [args.project] if args.project else None
    try:
        return args.func(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
