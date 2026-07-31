"""導入検証用の最小スモーク（**配布リポジトリ／zip に同梱する想定のファイル**）。

導入した AI が「本当に入ったか」を自分で確かめられるようにする。
Unity もデバイスも要らず、数秒で終わる。開発用の詳細テストは配布しない。

    python -m pytest tests/test_installed_smoke.py -q

**インストールされた成果物を検証する**のが目的なので、パッケージを直接 import せず、
ソースツリーの外（一時ディレクトリ）を作業ディレクトリにして `uapp-dash` / `uapp-dash-emit` を
別プロセスで起動する。同一プロセスで `main()` を呼ぶと、作業ツリーのコードを読んでいるだけでも
通ってしまう。未インストールは skip でなく**失敗**にする（skip だと終了コード 0 になり、
自動チェックが「入った」と誤認する）。このファイルは conftest.py に依存しない。
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

STATUS_DIR = ".agent-status"

DASH_EXE = shutil.which("uapp-dash")
EMIT_EXE = shutil.which("uapp-dash-emit")

INSTALL_HINT = ('uapp-dash が未インストール（または PATH に出ていない）。'
                '`pip install ".[test]"` を実行してから再実行する（SETUP.md 手順 1）')

# 未インストールなら test_entry_points_are_installed が落ちる。以降のテストは
# 同じ理由での失敗を重ねても情報が増えないので skip する
needs_install = pytest.mark.skipif(not (DASH_EXE and EMIT_EXE), reason=INSTALL_HINT)

DASH = [DASH_EXE] if DASH_EXE else []
DASH_EMIT = [EMIT_EXE] if EMIT_EXE else []


@pytest.fixture
def env(tmp_path):
    """レジストリも状態も一時領域に閉じ込める（実運用の記録を汚さない）。"""
    environ = dict(os.environ)
    environ["UAPP_DASH_HOME"] = str(tmp_path / "dash-home")
    environ["PYTHONIOENCODING"] = "utf-8"
    for key in ("UAPP_DASH_UNIT_ID", "UAPP_E2E_UNIT_ID", "UAPP_DASH_STATUS_DIR",
                "UAPP_E2E_STATUS_DIR", "UAPP_DASH_PID",
                # ソースツリーへのフォールバックを断つ（PYTHONPATH にリポジトリが入っていると、
                # PATH のコマンドが壊れていても作業ツリーの実装で通ってしまう）
                "PYTHONPATH", "PYTHONHOME"):
        environ.pop(key, None)
    return environ


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    return root


def run(argv, env, *, cwd, check=True):
    """ソースツリーの外を作業ディレクトリにして実行する（作業ツリーを暗黙に import させない）。"""
    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                            env=env, cwd=str(cwd), timeout=120)
    if check:
        assert result.returncode == 0, f"{argv}\nstdout={result.stdout}\nstderr={result.stderr}"
    return result


SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _expected_convention(agent: str) -> str:
    """同梱ソースが生成する規約全文（期待値）。

    版だけの照合では、**同じ版番号のまま中身が変わった配布物**（開発版の連続コミット）を
    区別できない。インストール済みコマンドが実際に書いたものと、同梱ソースが書くものを
    突き合わせれば、成果物の中身そのものを比較できる。
    **算出できない場合は検証を飛ばさず失敗させる**（飛ばすと古い成果物で緑になる）。
    """
    source = SOURCE_ROOT / "uapp_dash" / "agents.py"
    assert source.exists(), f"同梱ソースが見つからない: {source}（配布物が壊れている）"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    # 期待値もパイプ経由で受け取るので UTF-8 を強制する（env フィクスチャと同じ理由。
    # 無指定だと日本語 Windows の既定 cp932 で書かれ、utf-8 デコードに失敗して期待値が作れない）
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-c",
         "from uapp_dash import agents; import sys; "
         f"sys.stdout.write(agents.render({agent!r}))"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(SOURCE_ROOT), timeout=120)
    assert result.returncode == 0 and result.stdout, \
        f"同梱ソースから期待値を作れない: {result.stderr.strip()[:400]}"
    return result.stdout


def _expected_version() -> str:
    """同梱ソースが宣言している版（PATH のコマンドがこの配布物かを見分けるため）。

    実行はソースツリー外から行うが、**期待値の読み取り**は同梱ファイルから行う。
    これが無いと「PATH に居た別の（古い）インストール」でスモークが緑になる。
    """
    source = SOURCE_ROOT / "uapp_dash" / "__init__.py"
    assert source.exists(), f"同梱ソースが見つからない: {source}（配布物が壊れている）"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', source.read_text(encoding="utf-8"))
    assert match, f"同梱ソースが版を宣言していない: {source}"
    return match.group(1)


def test_entry_points_are_installed(env, tmp_path):
    """`uapp-dash` / `uapp-dash-emit` が入っていて、起動して**この配布物の版**を答えること。"""
    assert DASH_EXE and EMIT_EXE, INSTALL_HINT
    expected = _expected_version()
    for name, argv in (("uapp-dash", DASH), ("uapp-dash-emit", DASH_EMIT)):
        result = run([*argv, "--help"], env, cwd=tmp_path)
        assert f"usage: {name}" in result.stdout
        version = run([*argv, "--version"], env, cwd=tmp_path)
        reported = (version.stdout + version.stderr).strip()
        assert reported == f"uapp-dash {expected}", \
            f"PATH の {name} は別のインストール（{reported}）を指している"


@needs_install
def test_init_creates_status_dir_and_rules(env, project, tmp_path):
    run([*DASH, "--project", str(project), "init", "--agents", "both"], env, cwd=tmp_path)
    assert (project / STATUS_DIR / "units").is_dir()
    assert (project / STATUS_DIR / "resources").is_dir()
    assert f"{STATUS_DIR}/" in (project / ".gitignore").read_text(encoding="utf-8")
    for agent, relpath in (("claude", Path(".claude") / "rules" / "agent-dash.md"),
                           ("codex", Path("AGENTS.md"))):
        text = (project / relpath).read_text(encoding="utf-8")
        assert "uapp-dash begin" in text and "uapp-dash end" in text
        # 現行契約: 更新系は必ず --unit-id を渡す形で書かれていること
        # （環境変数方式だった古いインストールをここで落とす）
        for command in ("uapp-dash heartbeat", "uapp-dash end"):
            line = next(l for l in text.splitlines() if l.strip().startswith(command))
            assert "--unit-id" in line, line
        # 同梱ソースが書くはずの規約と**中身まで一致**すること。
        # 版番号が動かない開発版では、これが「入ったのは今の成果物か」の唯一の判定材料になる
        assert text == _expected_convention(agent), \
            f"{relpath} が同梱ソースの規約と違う（PATH のコマンドが別の配布物の可能性）"


@needs_install
def test_doctor_reports_state(env, project, tmp_path):
    # 何もしていない状態では未了があり、終了コードで分かる
    before = run(DASH + ["--project", str(project), "doctor"], env, cwd=tmp_path, check=False)
    assert before.returncode == 1 and "[未]" in before.stdout

    run([*DASH, "--project", str(project), "init", "--agents", "both"], env, cwd=tmp_path)
    after = run(DASH + ["--project", str(project), "doctor"], env, cwd=tmp_path, check=False)
    # 導入後は「すべて満たしている」＝終了コード 0 になること（表示だけでなく結果で確かめる）
    assert after.returncode == 0, after.stdout
    assert "[未]" not in after.stdout


@needs_install
def test_begin_emit_view_round_trip(env, project, tmp_path):
    run([*DASH, "--project", str(project), "init"], env, cwd=tmp_path)
    unit_id = run([*DASH, "--project", str(project), "begin", "--label", "導入確認",
                   "--tasks", "疎通"], env, cwd=tmp_path).stdout.strip()
    assert unit_id.startswith("u-")

    # ツール側のエビデンス（申告とは別レーン）
    run([*DASH_EMIT, "evidence.test", "--set", "suite=unit", "--set", "passed=1",
         "--set", "failed=0", "--set", "exitCode=0", "--project", str(project),
         "--unit-id", unit_id, "--strict"], env, cwd=tmp_path)
    events = [json.loads(line) for line in
              (project / STATUS_DIR / "units" / f"{unit_id}.ndjson").read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in events] == ["claim.begin", "evidence.test"]
    assert events[-1]["producer"] == "tool"

    run([*DASH, "--project", str(project), "task", "t1", "--done", "--unit-id", unit_id], env, cwd=tmp_path)
    run([*DASH, "--project", str(project), "end", "--result", "success",
         "--unit-id", unit_id, "--summary", "導入確認"], env, cwd=tmp_path)

    out = tmp_path / "fleet.html"
    run([*DASH, "view", "--project", str(project), "--out", str(out)], env, cwd=tmp_path)
    html = out.read_text(encoding="utf-8")
    assert "導入確認" in html                       # 自己完結 HTML に載っている
    assert "src=\"http" not in html and "<script src" not in html


@needs_install
def test_emitter_is_a_no_op_without_status_dir(env, tmp_path):
    """ダッシュボード未導入のプロジェクトでは、ツール側が何も作らないこと。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run([*DASH_EMIT, "evidence.build", "--set", "target=Android", "--set", "exitCode=0",
                  "--project", str(plain)], env, cwd=tmp_path)
    assert result.stdout == "" and result.stderr == ""
    assert not (plain / STATUS_DIR).exists()
