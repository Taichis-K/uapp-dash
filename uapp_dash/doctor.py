"""`uapp-dash doctor` — 導入状況の自己診断。

導入した AI が「入ったつもり」で終わらないよう、**実際に読み書きして確かめる**。
判定は次の 3 種類に絞る:

- `ok`    … [済]
- `ng`    … [未]（必須。1 件でもあれば終了コード 1）
- `info`  … [--]（環境によって要否が変わるもの。終了コードには影響しない）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, agents as agents_mod, aggregate, protocol as P
from .store import StatusStore

OK = "ok"
NG = "ng"
INFO = "info"

_LABEL = {OK: "[済]", NG: "[未]", INFO: "[--]"}


def _check(status: str, title: str, detail: str = "", hint: str = "") -> dict:
    return {"status": status, "title": title, "detail": detail, "hint": hint}


INSTALL_HINT = ("リポジトリ直下で `pip install -e .`（または `pip install .`）を実行する。"
                "入れずに使うなら `python -m uapp_dash` / `python -m uapp_dash.emit`")

VERSION_LINE = f"uapp-dash {__version__}"


def _child_env() -> dict:
    """子プロセスがソースツリーへフォールバックしないようにする。

    `PYTHONPATH` にリポジトリが入っていると、PATH 上のコマンドが壊れていても
    作業ツリーの実装を読んで動いてしまい、「入っているか」の確認にならない。
    """
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    return env


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """**ソースツリーの外**を作業ディレクトリにして起動する。

    `PYTHONPATH` を外すだけでは足りない。`python -m uapp_dash "$@"` 型の古い shim を
    リポジトリ直下から起動すると、カレントディレクトリ経由で作業ツリーを import してしまい、
    壊れたインストールでも [済] になる。
    """
    kwargs.setdefault("env", _child_env())
    with tempfile.TemporaryDirectory() as outside:
        kwargs.setdefault("cwd", outside)
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, **kwargs)


def _responds(exe: str, expected: str) -> tuple[bool, str]:
    """**実際に起動して**自分のコマンドかを確かめる。

    `shutil.which` は名前が見つかったことしか言わない（古い shim・import に失敗する
    スクリプト・同名の別コマンドでも [済] になってしまう）。usage だけでは
    「パーサーは起動するが中身は別物/別版」を弾けないので、版まで突き合わせる。
    """
    try:
        result = _run([exe, "--help"])
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"起動できない: {exc}"
    if result.returncode != 0:
        return False, f"--help が終了コード {result.returncode}: {(result.stderr or '').strip()[:200]}"
    if expected not in (result.stdout or ""):
        return False, f"別のコマンドの応答に見える（'{expected}' が出力に無い）"
    try:
        version = _run([exe, "--version"])
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"--version を実行できない: {exc}"
    reported = ((version.stdout or "") + (version.stderr or "")).strip()
    # 完全一致で見る（部分一致だと `uapp-dash 0.1.0.dev0-old` のような別物を通す）
    if reported != VERSION_LINE:
        return False, (f"別のインストールを指している（この診断は {VERSION_LINE}、"
                       f"PATH のコマンドは {reported or '版を答えない'}）")
    return True, ""


def _round_trip(exe: str) -> tuple[bool, str]:
    """PATH のコマンドで実際に `.agent-status` を作れるかまで見る。

    パーサーが起動するだけの壊れたインストール（本体の import に失敗する等）を通さないため。
    一時ディレクトリの中だけで完結し、後片付けまで行う。
    """
    try:
        with tempfile.TemporaryDirectory() as workdir:
            home = Path(workdir) / "home"          # 実運用のレジストリを汚さない
            env = _child_env()
            env["UAPP_DASH_HOME"] = str(home)
            target = Path(workdir) / "probe"
            target.mkdir()
            result = _run([exe, "--project", str(target), "init"], env=env)
            if result.returncode != 0:
                return False, f"init が終了コード {result.returncode}: {(result.stderr or '').strip()[:200]}"
            if not (target / P.STATUS_DIR_NAME / "units").is_dir():
                return False, "init しても .agent-status/units が作られない"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"実行できない: {exc}"
    return True, ""


def _check_commands() -> list[dict]:
    checks = []
    for name in ("uapp-dash", "uapp-dash-emit"):
        exe = shutil.which(name)
        if not exe:
            checks.append(_check(NG, f"コマンド {name} が使える", "PATH に見つからない", INSTALL_HINT))
            continue
        ok, detail = _responds(exe, f"usage: {name}")
        if not ok:
            checks.append(_check(NG, f"コマンド {name} が使える", f"{exe} — {detail}", INSTALL_HINT))
            continue
        if name == "uapp-dash":
            works, why = _round_trip(exe)
            if not works:
                checks.append(_check(NG, "コマンド uapp-dash が実際に動く", f"{exe} — {why}", INSTALL_HINT))
                continue
        checks.append(_check(OK, f"コマンド {name} が使える", exe))
    return checks


def _check_status_dir(store: StatusStore) -> list[dict]:
    if not store.exists():
        return [_check(NG, ".agent-status/ がある", str(store.root),
                       f"`uapp-dash --project {store.project_root} init` を実行する")]
    checks = [_check(OK, ".agent-status/ がある", str(store.root))]
    missing = [d.name for d in (store.units_dir, store.resources_dir) if not d.is_dir()]
    if missing:
        checks.append(_check(NG, ".agent-status/ の構造が正しい", f"欠けている: {', '.join(missing)}",
                             "`uapp-dash init` を再実行すると作り直される（既存の記録は消さない）"))
    # 読めるだけでなく書けるかを実際に確かめる（権限・ウイルス対策ソフトで書けないことがある）
    probe = store.root / f".doctor-probe-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(_check(OK, ".agent-status/ へ書き込める"))
    except OSError as exc:
        checks.append(_check(NG, ".agent-status/ へ書き込める", str(exc),
                             "フォルダの権限を確認する（書けないと申告もエビデンスも記録されない）"))
    return checks


def _check_gitignore(project_root: Path) -> list[dict]:
    if not (project_root / ".git").exists():
        return [_check(INFO, ".gitignore に .agent-status/ がある", "git リポジトリではない（対象外）")]
    gitignore = project_root / ".gitignore"
    entry = f"{P.STATUS_DIR_NAME}/"
    if gitignore.exists():
        lines = [line.strip() for line in gitignore.read_text(encoding="utf-8", errors="replace").splitlines()]
        if entry in lines or P.STATUS_DIR_NAME in lines:
            return [_check(OK, ".gitignore に .agent-status/ がある")]
    return [_check(NG, ".gitignore に .agent-status/ がある", str(gitignore),
                   "`uapp-dash init` が追記する（ホスト名・絶対パス・pid を含むのでコミットしない）")]


def _check_registry(project_root: Path) -> list[dict]:
    known = {str(Path(p).resolve()) for p in aggregate.load_registry()}
    if str(Path(project_root).resolve()) in known:
        return [_check(OK, "レジストリに登録済み（uapp-dash view の走査対象）", str(aggregate.registry_path()))]
    return [_check(NG, "レジストリに登録済み（uapp-dash view の走査対象）", str(aggregate.registry_path()),
                   "`uapp-dash init` か `uapp-dash begin` を一度実行すると自動登録される。"
                   "登録しなくても `uapp-dash view --project <path>` なら表示できる")]


def _agent_hint(project_root: Path, agent: str, path: Path) -> str:
    hint = f"`uapp-dash --project {project_root} init --agents {agent}` を実行する"
    if path.exists():
        hint += ("（既存ファイルは自動で書き換えないので、表示されるスニペットを begin/end マーカーごと"
                 "そのまま統合する。古い規約ブロックが残っていれば消す）")
    return hint


def _check_agent_rules(project_root: Path, status_dir: Path) -> list[dict]:
    """申告規約の配置。**`--agents both` と要求したなら両方が揃って初めて [済]**。

    片方でも [済] にすると、既存の AGENTS.md があるプロジェクト（Codex 利用者の普通の状態）で
    「Claude 側だけ入って Codex 側は未統合」を見逃し、ダッシュボードが空のまま導入成功に見える。
    """
    found = agents_mod.convention_present(project_root)
    paths = {agent: project_root / relpath for agent, relpath in agents_mod.RELPATHS.items()}
    requested = agents_mod.requested_agents(status_dir)
    state = agents_mod.manifest_state(status_dir)
    extra: list[dict] = []
    if state != agents_mod.MANIFEST_OK and any(found.values()):
        # `.agent-status` は丸ごと gitignore される一時状態なので記録は消えうる。
        # **要求状態が分からないまま [済] にすると**「both で入れた片方が消えている」を
        # 見逃す（第2周・第3周のレビュー指摘）。不明は未了として扱う
        broken = state == agents_mod.MANIFEST_BROKEN
        extra.append(_check(NG, "申告規約の配置記録が読める",
                            (f"壊れている: {status_dir / agents_mod.MANIFEST_NAME}" if broken else
                             f"記録が無い（{status_dir / agents_mod.MANIFEST_NAME}）。規約は在るが、"
                             "どの種別を要求したのか分からない＝欠落を検出できない"),
                            f"`uapp-dash --project {project_root} init --agents <claude|codex|both>` で"
                            "作り直す（既存ファイルは書き換えない）"))
    if requested:
        checks = list(extra)
        for agent in requested:
            if found[agent]:
                checks.append(_check(OK, f"申告規約が配置済み（{agent}）", str(paths[agent])))
            else:
                checks.append(_check(NG, f"申告規約が配置済み（{agent}）",
                                     f"未配置: {paths[agent]}（要求済みなのに置かれていない）",
                                     _agent_hint(project_root, agent, paths[agent])))
        for agent in agents_mod.AGENT_NAMES:
            if agent not in requested and found[agent]:
                checks.append(_check(INFO, f"申告規約（{agent}）", f"配置あり（未要求）: {paths[agent]}"))
        return checks

    # まだ一度も --agents を指定していない場合
    placed = [agent for agent, ok in found.items() if ok]
    if placed:
        checks = [*extra, _check(OK, "申告規約が配置済み（AI が uapp-dash を打つ導線）",
                                 "配置先: " + ", ".join(str(paths[a]) for a in placed))]
        for agent in agents_mod.AGENT_NAMES:
            if agent not in placed:
                checks.append(_check(INFO, f"申告規約（{agent}）", f"未配置: {paths[agent]}",
                                     _agent_hint(project_root, agent, paths[agent])))
        return checks
    return [*extra, _check(NG, "申告規約が配置済み（AI が uapp-dash を打つ導線）",
                           "これが無いと AI は申告すべきだと知らず、ダッシュボードは埋まらない",
                           _agent_hint(project_root, "both", paths["codex"]))]


def _check_emitter(project_root: Path) -> list[dict]:
    # 導入先レイアウト（uapp_e2e\scripts\）と、キット開発リポのレイアウト（scripts\）の両方を見る。
    # 前者だけ見ていると、キット自身のリポジトリで「該当なし」と誤って表示される
    for relative in (Path("uapp_e2e") / "scripts" / "emit-status.ps1",
                     Path("scripts") / "emit-status.ps1"):
        emitter = project_root / relative
        if emitter.exists():
            return [_check(OK, "ツール側エミッタの配線（uapp_e2e キット）", str(emitter))]
    return [_check(INFO, "ツール側エミッタの配線（uapp_e2e キット）",
                   "このプロジェクトに uapp_e2e キットは無い（該当なし）",
                   "自前のラッパーから記録するなら `uapp-dash-emit evidence.test --set passed=… --set exitCode=…`")]


def _check_records(store: StatusStore) -> list[dict]:
    if not store.exists():
        return []
    units = store.list_units(include_done=True)
    orphans = store.orphan_journals()
    if units or orphans:
        return [_check(INFO, "記録がある",
                       f"単位 {len(units)} 件・スナップショット無しのジャーナル {len(orphans)} 件")]
    return [_check(INFO, "記録がある", "まだ 1 件も無い",
                   "`uapp-dash begin --label 疎通確認` → `uapp-dash end --result success` で疎通を確かめられる")]


def run_checks(project: Path | None = None) -> list[dict]:
    store = StatusStore.for_project(Path(project) if project else None)
    project_root = store.project_root
    checks = [_check(INFO, "パッケージ", f"uapp_dash {Path(__file__).parent} / Python {sys.version.split()[0]}")]
    checks += _check_commands()
    checks += _check_status_dir(store)
    checks += _check_gitignore(project_root)
    checks += _check_registry(project_root)
    checks += _check_agent_rules(project_root, store.root)
    checks += _check_emitter(project_root)
    checks += _check_records(store)
    return checks


def format_checks(project_root: Path, checks: list[dict]) -> str:
    lines = [f"診断対象: {project_root}", ""]
    for check in checks:
        line = f"{_LABEL[check['status']]} {check['title']}"
        if check["detail"]:
            line += f" — {check['detail']}"
        lines.append(line)
        if check["status"] == NG and check["hint"]:
            lines.append(f"      → {check['hint']}")
    ng = [c for c in checks if c["status"] == NG]
    lines.append("")
    lines.append("すべて満たしている" if not ng else f"未了 {len(ng)} 件（上の → の手順で解消する）")
    return "\n".join(lines)
