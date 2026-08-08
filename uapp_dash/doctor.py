"""`uapp-dash doctor` ― 導入状況の自己診断。

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
import sysconfig
import tempfile
from pathlib import Path

from . import (__version__, agents as agents_mod, aggregate, attention,
               claims as claims_mod, protocol as P)
from .store import StatusStore, gitignore_candidates, has_status_dir_entry

OK = "ok"
NG = "ng"
INFO = "info"

_LABEL = {OK: "[済]", NG: "[未]", INFO: "[--]"}


def _check(status: str, title: str, detail: str = "", hint: str = "") -> dict:
    return {"status": status, "title": title, "detail": detail, "hint": hint}


INSTALL_HINT = ("リポジトリ直下で `pip install -e .`（または `pip install .`）を実行する。"
                "入れずに使うなら `python -m uapp_dash` / `python -m uapp_dash.emit`")

# **「入れたはずなのに無い / 動かない」の典型はウイルス対策**。pip が作る launcher exe は
# 未署名で、隔離・実行ブロック・解析待ちのどれもが「pip install は成功したのに使えない」形で出る。
# 原因に見当が付かないと `pip install` を何度も繰り返すことになるので、候補として出す。
# ただし **Windows 限定**（launcher が exe になるのは Windows だけ。POSIX の launcher は
# 素の Python スクリプトで隔離対象にならず、この案内は誤った調査へ誘導するノイズになる ―
# 実際に macOS で「PATH に無いだけ」の状況をウイルス対策の方向へ誤誘導した。issue #14）
ANTIVIRUS_HINT = (
    "`pip install` が成功しているのにこうなる場合、**ウイルス対策が launcher exe を隔離/ブロック**"
    "している可能性がある（未署名かつレピュテーションが無いため。隔離ログを確認する）。"
    "対処は 3 つ: (1) インストール先を除外に登録して `pip install --force-reinstall --no-deps .`"
    "（再生成でハッシュが変わり判定が覆ることがある） "
    "(2) 除外に登録できる場所へ venv を作り直してそこへ入れる "
    "(3) exe を使わず `python -m uapp_dash` / `python -m uapp_dash.emit` で運用する"
    "（申告規約の `uapp-dash …` を読み替える）")

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
    kwargs.setdefault("timeout", 60)
    with tempfile.TemporaryDirectory() as outside:
        kwargs.setdefault("cwd", outside)
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", **kwargs)


def _responds(exe: str, expected: str, *, timeout: int = 60) -> tuple[bool, str]:
    """**実際に起動して**自分のコマンドかを確かめる。

    `shutil.which` は名前が見つかったことしか言わない（古い shim・import に失敗する
    スクリプト・同名の別コマンドでも [済] になってしまう）。usage だけでは
    「パーサーは起動するが中身は別物/別版」を弾けないので、版まで突き合わせる。
    """
    try:
        result = _run([exe, "--help"], timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"起動できない: {exc}"
    if result.returncode != 0:
        return False, f"--help が終了コード {result.returncode}: {(result.stderr or '').strip()[:200]}"
    if expected not in (result.stdout or ""):
        return False, f"別のコマンドの応答に見える（'{expected}' が出力に無い）"
    try:
        version = _run([exe, "--version"], timeout=timeout)
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


def _script_dirs() -> list[Path]:
    """launcher exe が置かれうる場所（除外登録の宛先として出す）。

    **`sysconfig.get_path("scripts")` 1 本では足りない** ― 既定 scheme の Scripts しか返さず、
    SETUP が失敗時の手として案内している `pip install --user .` の launcher は
    `nt_user` scheme の側（`%APPDATA%\\Python\\PythonXY\\Scripts`）に置かれる。
    片方だけ見ると、正しく入っているのに「インストール先に存在しない＝隔離された」と誤診し、
    利用者を無関係なウイルス対策の調査へ送り込むことになる。
    """
    dirs: list[Path] = []
    schemes: list[str | None] = [None]
    try:
        schemes.append(sysconfig.get_preferred_scheme("user"))
    except (KeyError, ValueError, AttributeError):
        pass
    for scheme in schemes:
        try:
            raw = (sysconfig.get_path("scripts") if scheme is None
                   else sysconfig.get_path("scripts", scheme))
        except (KeyError, ValueError):
            continue
        if raw and Path(raw) not in dirs:
            dirs.append(Path(raw))
    return dirs


def _expected_script_dir() -> Path | None:
    """既定 scheme の Scripts（後方互換。判定には `_script_dirs` を使うこと）。"""
    dirs = _script_dirs()
    return dirs[0] if dirs else None


def _launcher_filename(name: str) -> str:
    """pip が置く launcher のファイル名。**Windows だけ exe になる**。

    POSIX の launcher は拡張子なしの Python スクリプト。ここを `.exe` 固定にすると
    POSIX では実在確認が必ず空振りし、「在るのに無い扱い」で誤った案内に流れる（issue #14）。
    """
    return f"{name}.exe" if os.name == "nt" else name


def _module_form(name: str) -> str:
    """launcher を使わない読み替え先（`python -m …`）。"""
    return "python -m uapp_dash.emit" if name == "uapp-dash-emit" else "python -m uapp_dash"


def _path_add_line(directory: Path) -> str:
    """PATH へ足す 1 行（そのまま貼れる形で。シェルごとに書き方が違う）。

    パスは二重引用符へ埋め込まない（`$` を含むパスが展開され、`"` を含む合法なパスは
    引用から脱出する）。**単引用符で括ってエスケープする** ― `install-shims` と同じ扱い。
    """
    raw = str(directory)
    if os.name == "nt":
        return "$env:PATH = '" + raw.replace("'", "''") + ";' + $env:PATH"
    return "export PATH='" + raw.replace("'", "'\\''") + ":'\"$PATH\""


def _probe_env_ok() -> bool:
    """launcher の実走確認（`_run`）が使う一時ディレクトリを作れる環境か。

    作れないと `_responds` は起動前に失敗し、健全な launcher まで「壊れている」に
    見えてしまう。プローブの土台の故障と launcher の故障を混ぜない。
    """
    try:
        with tempfile.TemporaryDirectory():
            return True
    except OSError:
        return False


def _shebang_of(path: str | None) -> str | None:
    """launcher の 1 行目（shebang）が指すインタープリタ。読めなければ None。"""
    if not path:
        return None
    try:
        first = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first.startswith("#!"):
        return None
    interp = first[2:].strip()
    # pip/distlib は Python パスに空白がある・長すぎる等のとき shebang を `#!/bin/sh` の
    # トランポリンにする。それを Python と誤認すると `/bin/sh -m …` という無効な案内になる。
    # `python` を名乗る実体（`/usr/bin/env python3` 形式を含む）だけを信用する
    if not interp or "python" not in interp.lower():
        return None
    return interp


def _missing_hint(name: str) -> str:
    """`shutil.which` で見つからないときのヒント。

    **「launcher が無い」と「launcher はあるが PATH に無い」を区別する**（issue #14）。
    後者に再インストールやウイルス対策の話をしても対処にならない
    （実際に macOS で誤った方向へ調査を誘導した）。候補ディレクトリを実際に見に行き、
    実体が見つかったら「PATH へ足す 1 行」と「`python -m` へ読み替え」だけを出す。
    """
    launcher = _launcher_filename(name)
    dirs = _script_dirs()
    present = [d / launcher for d in dirs if (d / launcher).exists()]
    if present and not _probe_env_ok():
        # プローブの土台（一時ディレクトリ）が無い環境では、実走できないことを
        # launcher の故障と誤診しない。**確かめられなかったことを正直に言う**
        listed = "、".join(str(p) for p in present)
        return (f"launcher の実体は {listed} に在る（この環境では実走確認ができなかった）。"
                f"PATH に無いだけの可能性が高い。PATH へ足す: `{_path_add_line(present[0].parent)}`。"
                f"足さずに運用するなら `{name} …` を `{_module_form(name)} …` に読み替える")
    # **実在確認だけで「PATH に無いだけ」と言い切らない**。実行権が無い・別版・壊れた
    # インストールでも exists() は通り、その場合 PATH へ足しても直らない。実走で確かめる
    # （タイムアウトは短く。ハングする launcher の診断で doctor 自体を数分止めない）
    probed = [(p, *_responds(str(p), f"usage: {name}", timeout=10)) for p in present]
    working = [p for p, ok, _ in probed if ok]
    if working:
        listed = "、".join(str(p) for p in working)
        return (f"**launcher は {listed} に在って応答する＝PATH に入っていないだけ**。"
                f"PATH へ足す: `{_path_add_line(working[0].parent)}`"
                "（恒久化はシェルの初期化ファイルや環境変数の設定へ）。"
                f"足さずに運用するなら `{name} …` を `{_module_form(name)} …` に読み替える")
    if present:
        # 実体はあるが実走で応答しない＝PATH の問題ではない。**理由は候補ごとに対で示す**
        listed = "、".join(f"{p}（{why}）" for p, _, why in probed)
        parts = [f"{listed} に実体はあるが、実走すると使えない。"
                 "**PATH へ足しても直らない**", INSTALL_HINT]
        if os.name == "nt":
            parts.append(ANTIVIRUS_HINT)
        return " / ".join(parts)
    parts = [INSTALL_HINT]
    if any(d.is_dir() for d in dirs):
        listed = "、".join(str(d / launcher) for d in dirs if d.is_dir())
        # 探索できるのは sysconfig が答える候補まで。**見ていない場所を「無い」と断定しない**
        # （pipx・別 venv・Homebrew prefix 不一致では候補の外に居る）
        parts.append(f"**確認した候補 {listed} には存在しない**（作られていないか、"
                     "消されているか、pipx・別の venv などこの候補以外の場所に入っている）")
    elif dirs:
        parts.append("確認した候補: " + "、".join(str(d) for d in dirs))
    if os.name == "nt":
        parts.append(ANTIVIRUS_HINT)
    return " / ".join(parts)


def _broken_hint(name: str, exe: str | None = None, *, suggest_probe: bool = True) -> str:
    """PATH には在るのに応答しない／動かないときのヒント。

    こちらは「見つからない」ではないので PATH の話はしない。壊れたインストールの
    入れ直しと、Windows なら実行ブロック（ウイルス対策）を疑わせる。

    `suggest_probe=False` は round-trip（init 実走）失敗用 ― そこでは `--help` /
    `--version` が既に通っているので、版切り分けの案内は的外れになる。
    """
    parts = [INSTALL_HINT]
    if os.name == "nt":
        parts.append(ANTIVIRUS_HINT)
    elif suggest_probe:
        # **素の python では切り分けにならない**（別の Python や作業ツリーの本体を拾いうる。
        # SETUP の注意と同じ）。launcher の shebang が読めるなら、その実体を具体的に出す
        mod = _module_form(name).removeprefix("python -m ")
        interp = _shebang_of(exe)
        if interp:
            parts.append(f"切り分け: `{interp} -m {mod} --version`"
                         "（launcher と同じ Python）を試す。動くなら launcher 側だけの問題"
                         "（入れ直しで戻る）")
        else:
            parts.append(f"切り分け: `python -m {mod} --version` を **launcher と同じ Python**"
                         "（launcher 1 行目の shebang が指す実体）で試す。それが動くなら"
                         " launcher 側だけの問題（入れ直しで戻る）。素の `python` は別の環境を"
                         "拾いうるので切り分けには使わない")
    return " / ".join(parts)


# シム（`install-shims` が作る `.cmd`）経由で呼ばれているときに毎回出す注意。
# **SETUP を読んだ導入者ではなく、実際にコマンドを打つ側へ届かせる**ためにここに置く
# （申告規約は版を固定しているので、そこへは書けない）。
SHIM_LIMITATION_CMD = (
    "**`.cmd` は引数の値に `&` `|` `<` `>` `^` `%` が入ると壊れる**"
    "（`--label \"A&B\"` はラベルが `A` になり、`B` がコマンドとして実行される）。"
    "`cmd` がコマンドラインを読む時点で起きるため `.cmd` 側では直せない")
SHIM_HAS_PS1 = (
    "PowerShell からは併設の `.ps1` が優先されるので、実運用の経路（申告規約・"
    "キットのラッパー）では引数は無傷で届く。**cmd.exe や Python の subprocess から"
    "呼ぶ場合はこの制限が生きる**")
SHIM_NO_PS1 = (
    "**`.ps1` が併設されていないので、PowerShell から呼んでもこの制限が生きる**。"
    "自由テキスト（`--label` / `--summary` / `--activity` / `--reason`）に"
    "これらの文字を入れないこと。入りうるなら `python -m uapp_dash …` で運用するか、"
    "`install-shims` をやり直して `.ps1` を置けるようにする")


def _is_shim(path: str) -> bool:
    """その実行ファイルが、このツールの作った `.cmd` シムか。

    拡張子だけで決めない（利用者の別ラッパーかもしれない）。生成時に必ず入る目印を見る。
    """
    if not path.lower().endswith(".cmd"):
        return False
    try:
        return 'uapp-dash install-shims' in Path(path).read_text(encoding="ascii",
                                                                 errors="replace")
    except OSError:
        return False


def _ps1_versions(script: Path) -> dict[str, str | None]:
    """併設 `.ps1` を**利用できる PowerShell すべてで実走**し、シェルごとの版を返す。

    **存在確認では足りない**。`.cmd` と `.ps1` は別々のファイルなので、更新が途中で
    失敗すれば「`.cmd` は新版・`.ps1` は旧版」という混在が残る。PowerShell は `.ps1` を
    優先するので、その状態では `doctor` が確かめた `.cmd` とは別の版が実運用で動く。

    **最初に見つかったシェルだけで合格にしない**。実行ポリシーや `#requires` の違いで
    「pwsh では動くが Windows PowerShell 5.1 では動かない」が起きうる。5.1 でも `.ps1` は
    `.cmd` より優先されるので、5.1 の利用者はコマンドを実行できないままになる。
    """
    results: dict[str, str | None] = {}
    for shell in ("pwsh", "powershell"):
        exe = shutil.which(shell)
        if not exe:
            continue
        try:
            proc = _run([exe, "-NoProfile", "-NonInteractive", "-File", str(script), "--version"])
        except (OSError, subprocess.SubprocessError):
            results[shell] = None
            continue
        results[shell] = (proc.stdout or "").strip() if proc.returncode == 0 else None
    return results


def _shim_limitation(shim_paths: list[str]) -> str:
    """シム利用時の注意文。**`.ps1` が隣にあるかで危険度がまるで違う**ので書き分ける。"""
    has_ps1 = all(Path(p).with_suffix(".ps1").exists() for p in shim_paths)
    return SHIM_LIMITATION_CMD + "。" + (SHIM_HAS_PS1 if has_ps1 else SHIM_NO_PS1)


def _check_ps1_shims(shim_paths: list[str]) -> list[dict]:
    """併設 `.ps1` が `.cmd` と同じ版を実行するか（混在の検出）。"""
    checks = []
    for cmd_path in shim_paths:
        ps1 = Path(cmd_path).with_suffix(".ps1")
        if not ps1.exists():
            continue
        name = ps1.stem
        expected = f"{name} {__version__}"
        results = _ps1_versions(ps1)
        if not results:
            continue                       # PowerShell が無い環境（`.cmd` だけで運用する）
        broken = [sh for sh, v in results.items() if v is None]
        mismatched = {sh: v for sh, v in results.items() if v is not None and v != expected}
        if broken:
            checks.append(_check(NG, f"併設の {name}.ps1 が動く",
                                 f"{ps1} ― {'、'.join(broken)} で実行できない"
                                 f"（試したシェル: {'、'.join(results)}）",
                                 "PowerShell は `.cmd` より `.ps1` を優先するので、"
                                 "動かない `.ps1` があるとそのシェルからはコマンドが使えない"
                                 "（実行ポリシーの可能性）。"
                                 "`install-shims --force` で作り直すか、`.ps1` を削除する"))
        elif mismatched:
            detail = "、".join(f"{sh}={v or '版を答えない'}" for sh, v in mismatched.items())
            checks.append(_check(NG, f"併設の {name}.ps1 が同じ版を実行する",
                                 f"{ps1} ― {detail}（期待: {expected}）",
                                 "**`.cmd` と `.ps1` で違う版が動いている**"
                                 "（更新が途中で失敗した可能性）。PowerShell は `.ps1` を"
                                 "優先するので、実運用ではこちらが動く。"
                                 "`install-shims --force` で作り直す"))
        else:
            checks.append(_check(OK, f"併設の {name}.ps1 が同じ版を実行する",
                                 f"{ps1}（{'、'.join(results)}）"))
    return checks


def _check_commands() -> list[dict]:
    checks = []
    shim_paths = []
    # **診断の土台が壊れている環境では launcher を「壊れている」と言わない**。
    # `_run` は一時ディレクトリを要求するので、それが無いと健全な launcher まで
    # 「起動できない」に化ける（実走確認の失敗と launcher の故障を混ぜない）
    probe_ok = _probe_env_ok()
    if not probe_ok:
        checks.append(_check(NG, "診断の土台（一時ディレクトリ）が使える",
                             "一時ディレクトリを作成できない ― コマンドの実走確認ができない",
                             "TMP / TEMP（POSIX は TMPDIR）が実在する書き込み可能な場所を"
                             "指しているか確認する。この状態で launcher の健全性は判定できない"
                             "（launcher の故障とは限らない）"))
    for name in ("uapp-dash", "uapp-dash-emit"):
        exe = shutil.which(name)
        if not exe:
            checks.append(_check(NG, f"コマンド {name} が使える", "PATH に見つからない",
                                 _missing_hint(name)))
            continue
        if not probe_ok:
            checks.append(_check(INFO, f"コマンド {name} が使える",
                                 f"{exe} ― 見つかったが、実走確認はできなかった（上の [未] 参照）"))
            continue
        ok, detail = _responds(exe, f"usage: {name}")
        if not ok:
            checks.append(_check(NG, f"コマンド {name} が使える", f"{exe} ― {detail}",
                                 _broken_hint(name, exe)))
            continue
        if name == "uapp-dash":
            works, why = _round_trip(exe)
            if not works:
                checks.append(_check(NG, "コマンド uapp-dash が実際に動く", f"{exe} ― {why}",
                                     _broken_hint(name, exe, suggest_probe=False)))
                continue
        checks.append(_check(OK, f"コマンド {name} が使える", exe))
        if _is_shim(exe):
            shim_paths.append(exe)
    if shim_paths:
        # **detail に入れる**（表示は NG のときしか hint を出さないため。
        # ここは異常ではないので NG にはできないが、毎回目に入る必要がある）。
        # 2 コマンド分を 1 件にまとめる（同じ長文を二度読ませない）
        checks.append(_check(INFO, "コマンドはシム（制限あり）",
                             ", ".join(shim_paths) + " ― " + _shim_limitation(shim_paths)))
        checks.extend(_check_ps1_shims(shim_paths))
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
    # git ルートは上位へ辿って探す（Unity プロジェクトがリポジトリのサブディレクトリにある
    # `<repo>/<unity-project>` 構成で「git リポジトリではない」と誤判定しない）。
    # 除外記述は プロジェクト直下 / git ルートの .gitignore / .git/info/exclude のどこでもよい
    git_root, candidates = gitignore_candidates(project_root)
    if git_root is None:
        return [_check(INFO, ".gitignore に .agent-status/ がある",
                       "git リポジトリではない（上位にも .git が無い・対象外）")]
    for file in candidates:
        if has_status_dir_entry(file):
            return [_check(OK, ".gitignore に .agent-status/ がある", str(file))]
    return [_check(NG, ".gitignore に .agent-status/ がある", str(git_root / ".gitignore"),
                   "`uapp-dash init` が追記する（ホスト名・絶対パス・pid を含むのでコミットしない。"
                   "diff を汚したくなければ `init --git-exclude` でローカル除外へ書ける）")]


def _check_registry(project_root: Path) -> list[dict]:
    known = {str(Path(p).resolve()) for p in aggregate.load_registry()}
    if str(Path(project_root).resolve()) in known:
        return [_check(OK, "レジストリに登録済み（uapp-dash view の走査対象）", str(aggregate.registry_path()))]
    return [_check(NG, "レジストリに登録済み（uapp-dash view の走査対象）", str(aggregate.registry_path()),
                   "`uapp-dash init` か `uapp-dash begin` を一度実行すると自動登録される。"
                   "登録しなくても `uapp-dash view --project <path>` なら表示できる")]


def _agent_hint(project_root: Path, agent: str, path: Path) -> str:
    hint = (f"`uapp-dash --project {agents_mod.quote_for_cmd(project_root)} init --agents {agent}` を実行する")
    if path.exists():
        hint += ("（既存ファイルは自動で書き換えないので、表示されるスニペットを begin/end マーカーごと"
                 "そのまま統合する。古い規約ブロックが残っていれば消す）")
    return hint


def _check_agent_rules(project_root: Path, status_dir: Path) -> list[dict]:
    """申告規約の配置。**`--agents both` と要求したなら両方が揃って初めて [済]**。

    片方でも [済] にすると、既存の AGENTS.md があるプロジェクト（Codex 利用者の普通の状態）で
    「Claude 側だけ入って Codex 側は未統合」を見逃し、ダッシュボードが空のまま導入成功に見える。
    """
    conv = agents_mod.convention_state(project_root)
    found = {agent: st == agents_mod.CONVENTION_OK for agent, st in conv.items()}
    paths = {agent: project_root / relpath for agent, relpath in agents_mod.RELPATHS.items()}

    def _detail_and_hint(agent, *, requested=True):
        """「無い」と「古い」で、状況の書き方も直し方も変える。"""
        if conv[agent] == agents_mod.CONVENTION_OUTDATED:
            return (f"**旧版が配置されている**: {paths[agent]}（ファイルはあるが規約が現行と違う）",
                    f"`uapp-dash --project {agents_mod.quote_for_cmd(project_root)} init --agents {agent} --replace-marker-block` で"
                    "マーカー間だけ差し替える（マーカー外の記述は触らない）")
        # 要求していない種別まで「要求済みなのに置かれていない」と書かない（読み手が混乱する）
        note = "（要求済みなのに置かれていない）" if requested else ""
        return (f"未配置: {paths[agent]}{note}",
                _agent_hint(project_root, agent, paths[agent]))
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
                            f"`uapp-dash --project {agents_mod.quote_for_cmd(project_root)} init --agents <claude|codex|both>` で"
                            "作り直す（既存ファイルは書き換えない）"))
    if requested:
        checks = list(extra)
        for agent in requested:
            if found[agent]:
                checks.append(_check(OK, f"申告規約が配置済み（{agent}）", str(paths[agent])))
            else:
                detail, hint = _detail_and_hint(agent)
                checks.append(_check(NG, f"申告規約が配置済み（{agent}）", detail, hint))
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
                detail, hint = _detail_and_hint(agent, requested=False)
                checks.append(_check(INFO, f"申告規約（{agent}）", detail, hint))
        return checks
    # 記録も要求も無くても、**現物を見れば「旧版が置かれている」ことは分かる**。
    # `.agent-status/` は丸ごと gitignore される＝別クローンや掃除のあとは記録が無いのが普通なので、
    # ここを一般論の「配置されていない」に丸めると、その状況では必ず
    # 「100 行を手で貼り直す」案内に戻ってしまう（§14 で無くしたはずの作業）
    if any(st == agents_mod.CONVENTION_OUTDATED for st in conv.values()):
        checks = list(extra)
        for agent in agents_mod.AGENT_NAMES:
            outdated = conv[agent] == agents_mod.CONVENTION_OUTDATED
            detail, hint = _detail_and_hint(agent, requested=False)
            checks.append(_check(NG if outdated else INFO,
                                 f"申告規約が配置済み（{agent}）", detail, hint))
        return checks
    return [*extra, _check(NG, "申告規約が配置済み（AI が uapp-dash を打つ導線）",
                           "これが無いと AI は申告すべきだと知らず、ダッシュボードは埋まらない",
                           _agent_hint(project_root, "both", paths["codex"]))]


EMITTER_HINT = ("ビルド・テスト・E2E のラッパーの末尾から "
                "`uapp-dash-emit evidence.test --set passed=… --set failed=… --set exitCode=…` を呼ぶ"
                "（`.agent-status` が無いプロジェクトでは完全な no-op なので、他所へ影響しない）")


def _tool_evidence(store: StatusStore) -> tuple[dict | None, int]:
    """**実際に記録された** `producer: "tool"` のイベントを探す。

    配線の形（キット同梱・自前ラッパー・CI）を問わずに判定するため、特定パスの
    ファイル探しはしない。ファイルの存在で判定していた頃は、自前ラッパーから
    正しく記録できている環境が「配線なし」と表示されていた。
    """
    if not store.exists():
        return None, 0
    unit_ids = [unit.get("unitId") for unit in store.list_units(include_done=True)]
    unit_ids += store.orphan_journals()
    latest, count = None, 0
    for unit_id in unit_ids:
        if not unit_id:
            continue
        for event in store.read_events(unit_id):
            if event.get("producer") != P.PRODUCER_TOOL:
                continue
            count += 1
            if latest is None or str(event.get("at") or "") > str(latest.get("at") or ""):
                latest = event
    return latest, count


def _kit_present(project_root: Path) -> Path | None:
    """uapp_e2e キットの導入痕跡。導入先レイアウトとキット開発リポの両方を見る。"""
    for relative in (Path("uapp_e2e"), Path("Assets") / "uapp_e2e",
                     Path("uapp_e2e") / "scripts" / "emit-status.ps1",
                     Path("scripts") / "emit-status.ps1"):
        candidate = project_root / relative
        if candidate.exists():
            return candidate
    return None


def _check_evidence_binding(store: StatusStore) -> list[dict]:
    """**進行中の単位に客観エビデンスが 1 件も無い**状態を炙り出す。

    ラッパーが別プロセスで動くと unitId が届かず、記録は ambient に落ちる。
    記録自体は残るので「配線は [済]」に見えるが、単位レベルで申告と実測を
    突き合わせられない（二層化の目的が成立しない）。繋がっていないと気づけること自体に価値がある。
    """
    active = [unit for unit in store.list_units(include_done=False)
              if unit.get("unitId") and unit.get("state") not in P.TERMINAL_STATES]
    if len(active) != 1:
        return []                      # 0 件は対象外、2 件以上は ambient が正しい挙動
    unit = active[0]
    # **成果のエビデンスだけを数える**。デバイス負荷や資源取得の記録は配線の証明にはなるが、
    # 「この単位で何を作って何が通ったか」の証明にはならない（device 1 件で警告が消えると、
    # テストを一度も走らせていない単位が緑に見える）
    outcome_kinds = ("evidence.test", "evidence.e2e", "evidence.build", "evidence.git")
    has_outcome = any(event.get("producer") == P.PRODUCER_TOOL and event.get("kind") in outcome_kinds
                      for event in store.read_events(unit["unitId"]))
    if has_outcome:
        return []
    # **「まだ記録が無い」だけでは原因が分からない**。自動結びつけは「ハートビートが切れて
    # いない単位だけ」という条件で働くので、TTL 切れならそれが理由だと言い切る
    # （運用では TTL 切れに気づけず、記録が ambient に落ち続けた）
    window = attention.heartbeat_window(unit)
    if window["overdue"]:
        return [_check(NG, "進行中の単位にツールの記録が結びついている",
                       f"{unit['unitId']}（{unit.get('label') or ''}）は **TTL 切れ**"
                       f"（{-window['remainingSec']}秒超過）。この状態ではツールの記録は"
                       "自動で結びつかず ambient に落ちる",
                       f"`uapp-dash heartbeat --unit-id {unit['unitId']} --ttl <秒>` で伸ばす"
                       "（長時間処理の直前に伸ばす。Android ビルド 2400 / Unity テスト 900 が目安）")]
    return [_check(INFO, "進行中の単位にツールの記録が結びついている",
                   f"{unit['unitId']}（{unit.get('label') or ''}）にはまだ客観エビデンスが無い"
                   f"（TTL 残り {window['remainingSec']}秒）",
                   "テスト/ビルドのラッパーから `uapp-dash-emit … --unit-id "
                   f"{unit['unitId']}` を撃つ。渡せない場合でも、進行中の単位が 1 件だけなら自動で結びつく")]


def _check_claim_targets(store: StatusStore) -> list[dict]:
    """claims の**書き方の事故**を拾う。

    claims は「重なりを警告する」ための情報なので、壊れていても何も起きない
    （警告が出ないだけ）＝**動いていないことに気づく手段が無い**。区切りの取り違え
    （`;` 連結）で衝突検出が静かに無効化されていた実例があるため、気づく道を用意する。

    **どちらも `info`（終了コードを汚さない）**。人が読んで判断できる材料を出すのが目的で、
    機械的に「誤り」と断定できないため:

    - `;` を含む claim は取り違えの可能性が高いが、**`;` はファイル名に使える文字**なので
      （`\\;` とエスケープして意図的に宣言できる）誤りと断定できない
    - 存在しない領域の宣言も**正常**（「編集する前に宣言する」のが規約なので、
      これから作るファイル・ディレクトリを宣言する）。衝突判定はパターン同士の比較で、
      ファイルの実在に依存しない＝実在しなくても検出は働く

    ファイルシステムの全走査はしない（大きな Unity プロジェクトで重い）。
    グロブの**ワイルドカードより前の部分**が実在するかだけを見る。
    """
    if not store.exists():
        return []
    project_root = store.project_root
    separators: list[str] = []
    missing: list[str] = []
    checked = 0
    for unit in store.list_units(include_done=False):
        if unit.get("state") in P.TERMINAL_STATES:
            continue
        for claim in unit.get("claims") or []:
            path = claim.get("path") or ""
            if not path:
                continue
            if claims_mod.SEPARATOR in path:
                separators.append(f"{unit.get('unitId')}: {path}")
                continue
            prefix = claims_mod.static_prefix(path).rstrip("/")
            if not prefix:
                continue          # 先頭からワイルドカード（**/*.cs 等）は判定できない
            checked += 1
            if not (project_root / prefix).exists():
                missing.append(f"{unit.get('unitId')}: {path}")
    checks: list[dict] = []
    if separators:
        checks.append(_check(INFO, "claims の区切り",
                             "`;` を含む claim がある（1 本のパスとして登録されている）: "
                             + _sample(separators),
                             "区切りの取り違えなら宣言し直す（`--claims` は空白区切り。"
                             "`;` 区切りは `--tasks` だけ）。`\\;` でエスケープして"
                             "意図的に宣言したパスなら、このままで正しい"))
    if missing:
        checks.append(_check(INFO, "claims が指す領域の実在",
                             "まだ存在しない領域を宣言している: " + _sample(missing),
                             "これから作るなら正常（衝突判定はパターン比較なので実在に依存しない）。"
                             "心当たりが無ければタイポを疑う"))
    elif checked:
        checks.append(_check(OK, "claims が指す領域の実在", f"{checked} 件を確認"))
    return checks


def _sample(items: list[str], limit: int = 5) -> str:
    head = " / ".join(items[:limit])
    return head if len(items) <= limit else f"{head} ほか{len(items) - limit}件"


def _check_emitter(project_root: Path, store: StatusStore) -> list[dict]:
    """ツール側の配線は**実績で判定する**（キットの有無と配線の有無は別物）。"""
    latest, count = _tool_evidence(store)
    if latest is not None:
        checks = [_check(OK, "ツール側エミッタの配線（客観エビデンス）",
                         f"{count} 件記録済み・直近 {latest.get('kind')} {latest.get('at')}")]
        checks += _check_evidence_binding(store)
        return checks
    kit = _kit_present(project_root)
    if kit:
        return [_check(NG, "ツール側エミッタの配線（客観エビデンス）",
                       f"uapp_e2e キットはあるが記録が 1 件も無い（{kit}）",
                       "キット v0.1.4 以降の `scripts/emit-status.ps1` を使うか、" + EMITTER_HINT)]
    return [_check(INFO, "ツール側エミッタの配線（客観エビデンス）",
                   "ツールからの記録がまだ無い（自前で配線する）", EMITTER_HINT)]


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
    checks += _check_emitter(project_root, store)
    checks += _check_claim_targets(store)
    checks += _check_records(store)
    return checks


def format_checks(project_root: Path, checks: list[dict]) -> str:
    lines = [f"診断対象: {project_root}", ""]
    for check in checks:
        line = f"{_LABEL[check['status']]} {check['title']}"
        if check["detail"]:
            line += f" ― {check['detail']}"
        lines.append(line)
        if check["status"] == NG and check["hint"]:
            lines.append(f"      → {check['hint']}")
    ng = [c for c in checks if c["status"] == NG]
    lines.append("")
    lines.append("すべて満たしている" if not ng else f"未了 {len(ng)} 件（上の → の手順で解消する）")
    return "\n".join(lines)
