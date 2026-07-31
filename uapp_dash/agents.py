"""監視対象プロジェクトへ「申告規約」を配置する（`uapp-dash init --agents`）。

**これが無いとダッシュボードは埋まらない**。AI エージェントは、自分が `uapp-dash begin` を
打つべきだと知らなければ何も申告しないため、規約をプロジェクト側の自動読込パスへ置く。

- Claude Code … `.claude/rules/agent-dash.md`（ルールは常に読み込まれる）
- Codex …… ルートの `AGENTS.md`（**既存ファイルは絶対に書き換えない**。無いときだけ新規作成し、
  在るときは統合用のスニペットを表示するだけに留める）

所有権はファイル中のマーカーでは決めない（マーカーは統合用スニペットにも入る＝手で貼った
ユーザーのファイルを「自分の物」と誤認して丸ごと消す）。**書いた時のハッシュを
`.agent-status/agents.json` に記録し、現物のハッシュが一致するときだけ更新する**
（キットの kit-manifest と同じ方式）。手で編集された生成物も上書きしない。

マーカーは「規約がそこに在る」ことを doctor が見つけるための**検出用**であり、
手で統合した場合にも残してもらう（そのために統合用スニペットにも含める）。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path

from . import protocol as P
from .store import StatusStore, file_lock, write_json_atomic

# 管理領域は開始と終了で挟む。1 行のマーカーだけだと、テンプレート更新後に新しい
# スニペットを**追記**した場合に旧規約が残ったままでも「現行の全文が在る」ことになってしまう
BEGIN_MARKER = "<!-- uapp-dash:convention:begin -->"
END_MARKER = "<!-- uapp-dash:convention:end -->"
MARKER = BEGIN_MARKER      # 後方互換の別名（呼び手は begin/end を使うこと）
# 1 行マーカーだけだった頃の形式。**残っていれば「古い規約が同居している」ことを意味する**
LEGACY_MARKER = "<!-- uapp-dash:convention -->"

AGENT_CHOICES = ("claude", "codex", "both")
AGENT_NAMES = ("claude", "codex")

CLAUDE_RULE_RELPATH = Path(".claude") / "rules" / "agent-dash.md"
CODEX_AGENTS_RELPATH = Path("AGENTS.md")
RELPATHS = {"claude": CLAUDE_RULE_RELPATH, "codex": CODEX_AGENTS_RELPATH}

MANIFEST_NAME = "agents.json"

# 結果コード（表示と終了コードの決定は呼び手の仕事）
CREATED = "created"
UPDATED = "updated"
UNCHANGED = "unchanged"
SKIPPED_FOREIGN = "skipped-foreign"      # 自分が書いた記録が無いファイル
SKIPPED_MODIFIED = "skipped-modified"    # 自分が書いたが、その後手で編集されている

_CONVENTION_BODY = """\
このプロジェクトは **uapp-dash**（ローカルのエージェント開発ダッシュボード）で監視されている。
人が席に戻ったときに「どの作業に手を貸せばいいか」を数秒で判断するための唯一の情報源なので、
**作業の開始・節目・終了を必ず申告すること**。

コマンドは `uapp-dash`（`pip install` 済み）。入っていなければ `python -m uapp_dash` でも同じ。

## 毎回やること

1. **開始**: 作業単位を作る。**出力された unitId を必ず控える**

   ```powershell
   uapp-dash begin --label "issue #12 の実装" --tasks "調査;実装;テスト" `
     --claims "Assets/Scripts/**" "Assets/Tests/**" "docs/**"
   ```

   **`--claims` は値ごとに分ける（空白区切り）**。`;` 区切りは `--tasks` だけなので、
   つい `--claims "a/**;b/**"` と書きたくなるが、それは本来「`;` を含む 1 本のパス」を意味する。
   CLI はほどいて登録したうえで**警告を出す**ので、警告が出たら書き方を直すこと
   （本当に `;` を含むパスを宣言したいときだけ `\\;` とエスケープする）。

   標準出力は unitId だけ（例: `u-20260726-011500-a1b2`）。
   **以後のコマンドには毎回 `--unit-id <控えた値>` を付けること。**
   コマンドは 1 回ごとに別のプロセス・別のシェルで動くことが多く、
   `UAPP_DASH_UNIT_ID` に入れても次の呼び出しには残らない
   （同じシェルを使い続ける場合に限り環境変数でも代用できる）。

2. **節目ごと**: 今やっていることを更新する。長時間処理の直前は TTL を伸ばす
   （既定 300 秒。Android ビルド 2400 / Unity テスト 900 が目安。伸ばさないと「停滞」に見える）

   ```powershell
   uapp-dash heartbeat --unit-id <unitId> --activity "EditMode テスト実行中" --ttl 900
   ```

   **TTL が切れるとツール側の記録が自分の単位に結びつかなくなる**（ambient に落ちる）。
   `uapp-dash units` に「TTL 残り N 秒」/「TTL 切れ」が出るので、長引いたら伸ばし直す。

3. **タスク消化**: `uapp-dash task t1 --done --unit-id <unitId>`
   （やめたものは `--drop --note "理由"`）

4. **人待ちになったら即座に**: 承認・入力・資源のどれ待ちかを付けて申告する
   （`needs approval` は表示の最上位＝人が最初に見る）

   ```powershell
   uapp-dash blocked --unit-id <unitId> --reason "push の承認待ち" --needs approval
   ```

   待ちが解けたら `uapp-dash heartbeat --unit-id <unitId> --state running --activity "再開"`。

5. **終了**: 必ず閉じる（閉じないと TTL 超過で「停滞」として人の注意を奪い続ける）

   ```powershell
   uapp-dash end --unit-id <unitId> --result success --summary "テストまで通過"
   ```

   失敗・中断も正直に `--result failure` / `--result aborted` で閉じる。
   **unitId が分からなくなったら `uapp-dash units`**（進行中の単位を label 付きで一覧する。
   `--all` で完了分も、`--json` で機械可読）。

   **`end` が終了コード 3 を返したら閉じられていない**（掴んだ排他資源を解放できなかった）。
   放置すると資源が塞がったままになるので、時間をおいて `end` をやり直すか、
   `uapp-dash resource release <資源ID> --unit-id <unitId>` で明示的に解放する。

## 約束

- **テスト件数やビルド結果を自分で書かない**。数字はツール側のエミッタ（`uapp-dash-emit`）だけが書く。
  自己申告と客観エビデンスを分けているのは、食い違いを人に見せるため
  （`--summary` / `--activity` に件数らしき数字を書くと警告が出る。**書く瞬間に「親切だろう」と
  思っても書かない**。実際にそれで規約を破った報告がある）
- **編集する前に `--claims` を宣言する**（これから作るファイル・ディレクトリでよい。
  重なりの判定はパターン同士の比較なので、実在しなくても検出は働く）。
  シーン・プレハブ・アセット（マージ困難な YAML）は指定に関わらずファイル丸ごと排他として
  扱われ、他の作業単位と重なれば警告が出る。書き方が怪しいと思ったら `uapp-dash doctor`
  （`;` の混入と、実在しない領域の宣言を報告する）で確かめる
- **排他資源は取ってから使う**。エディタ Play・重量ビルド・デバイスの待受ポートが対象

  ```powershell
  uapp-dash resource acquire "editor-play:<プロジェクトの絶対パス>" --unit-id <unitId>
  uapp-dash resource release "editor-play:<プロジェクトの絶対パス>" --unit-id <unitId>
  ```

  取得できないときは終了コード 3 と保持者が返る。**奪わずに待つか、別の作業を先にやる**
- **持続プロセスから使うなら pid を渡す**（`uapp-dash begin --pid <pid>` か環境変数 `UAPP_DASH_PID`）。
  渡しておくと、応答が止まったときに「停滞」と「プロセス消失」を区別できる。
  コマンドを毎回別プロセスで叩く AI は渡せないので、その場合は**常に「停滞」止まり**になる（それでよい）
- **失敗を無視してよいのは申告系だけ**（`begin` / `heartbeat` / `task` / `blocked`、および
  終了コード 3 以外で失敗した `end`）。申告は補助であり本流ではないので、
  これらが失敗しても作業そのものは止めなくてよい。**排他に関わる失敗は別**:
  `resource acquire` の失敗を無視して資源を使ってはならず、`end` の終了コード 3
  （資源を解放できていない）も放置してはならない
  （どちらも排他機構が無効になる。エディタ Play・ビルド・デバイスの取り合いが実害になる）

## 人が見る側

```powershell
uapp-dash view --serve --open     # 5 秒ごとに更新される表示
uapp-dash view --out fleet.html   # 1 ファイルで完結する HTML
```

**`view --serve` を AI が自分のバックグラウンドタスクとして起動しない**
（AI のセッション終了と同時にダッシュボードも落ち、落ちたことに誰も気づけない。
人に見せたいときは `view --out` で HTML を書き出すか、別ターミナルでの常駐を人に頼む）。
"""


def render(target: str) -> str:
    """配置先ごとの本文を作る（中身は同一・見出しだけ変える）。

    手で統合する場合にもそのまま貼れるようにする（マーカーは検出用なので残してもらう。
    貼られても所有権はハッシュ記録で判定するため、ユーザーのファイルを消すことはない）。
    """
    if target == "codex":
        # 本文の見出しが `##` なので、ルートの AGENTS.md では `#` を 1 本だけ立てる
        head = "# AGENTS.md — エージェント開発ステータスの申告規約\n\n"
    else:
        head = "# エージェント開発ステータスの申告規約\n\n"
    return f"{BEGIN_MARKER}\n{head}{_CONVENTION_BODY}{END_MARKER}\n"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest_path(status_dir: Path) -> Path:
    return Path(status_dir) / MANIFEST_NAME


MANIFEST_OK = "ok"
MANIFEST_MISSING = "missing"
MANIFEST_BROKEN = "broken"


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _load_manifest(status_dir: Path) -> tuple[str, dict]:
    """記録を読み、**状態と検証済みの中身を同じ場所で決める**。

    `.agent-status/` は丸ごと gitignore される一時状態なので、記録は消えうる。
    消えたことを黙って「未要求」に戻すと `--agents both` の必須判定が静かに退行するため、
    「無い」「壊れている」「正常」を区別する。判定基準を読み取り側と分けると、
    片方が正常扱いしたものをもう片方が捨てる（＝所有ハッシュが黙って消える）。
    """
    empty = {"requested": [], "files": {}}
    path = _manifest_path(status_dir)
    if not path.exists():
        return MANIFEST_MISSING, empty
    try:
        # UnicodeDecodeError も ValueError（JSONDecodeError の親）に含まれる
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MANIFEST_BROKEN, empty
    if not isinstance(data, dict):
        return MANIFEST_BROKEN, empty
    requested, files = data.get("requested"), data.get("files")
    ok = (isinstance(requested, list) and requested
          and all(agent in AGENT_NAMES for agent in requested)
          and isinstance(files, dict)
          and all(isinstance(k, str) and isinstance(v, str) and _SHA256.match(v)
                  for k, v in files.items()))
    valid = {
        **data,
        "requested": [a for a in requested if a in AGENT_NAMES] if isinstance(requested, list) else [],
        "files": ({k: v for k, v in files.items()
                   if isinstance(k, str) and isinstance(v, str) and _SHA256.match(v)}
                  if isinstance(files, dict) else {}),
    }
    return (MANIFEST_OK if ok else MANIFEST_BROKEN), valid


def manifest_state(status_dir: Path) -> str:
    return _load_manifest(status_dir)[0]


def read_manifest(status_dir: Path) -> dict:
    """**検証済みの形だけを返す**入口。

    壊れた記録（`requested: 1` など）をそのまま返すと、読み取る側が軒並み例外で落ちる
    ＝ doctor が [未] を表示できず、案内した修復コマンド（`init --agents`）まで実行不能になる。
    """
    return _load_manifest(status_dir)[1]


def _write_manifest(status_dir: Path, manifest: dict) -> None:
    """記録は原子的に置き換える（途中で落ちても半端な JSON を残さない）。"""
    write_json_atomic(_manifest_path(status_dir), manifest)


def requested_agents(status_dir: Path) -> list[str]:
    """`init --agents` で要求された種別（doctor が必須判定に使う）。"""
    return list(read_manifest(status_dir)["requested"])


def _write_atomic(path: Path, text: str) -> None:
    """同一ディレクトリの一時ファイル経由で置き換える。

    直接書くと、途中で落ちたときに半端なファイルが残る。それは記録とも一致しないため
    「手が入った」と判定され、以後の自動更新が永久に止まる。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.replace(tmp, path)
    except OSError as exc:
        # Windows では置換先を他プロセス（エディタ・同期ソフト・ウイルス対策）が開いていると
        # ここで失敗する。**一時ファイルを残さない**: 残すと次の導入で見慣れないファイルとして
        # 人を混乱させ、リポジトリにも紛れ込む（実際に `AGENTS.md.tmp<pid>` が残った）
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise RuntimeError(
            f"{path} を更新できなかった（{exc.__class__.__name__}: {exc}）。"
            "このファイルを開いているエディタや同期ソフトを閉じて `uapp-dash init` をやり直すこと"
        ) from exc


def _place(path: Path, text: str, recorded: str | None) -> str:
    """自分が書いた記録があり、現物がその通りのときだけ更新する。"""
    if not path.exists():
        _write_atomic(path, text)
        return CREATED
    current = path.read_text(encoding="utf-8", errors="replace")
    # 中身がこれから書く内容と完全一致するなら、記録の状態に関わらず自分の生成物として扱ってよい
    # （書き込み後・記録の保存前に落ちた場合の回復経路。書き換えは起きないので無害）
    if current == text:
        return UNCHANGED
    if recorded is None:
        return SKIPPED_FOREIGN                      # ユーザーのファイル（マーカーの有無で判断しない）
    if _digest(current) != recorded:
        return SKIPPED_MODIFIED                     # 自分の生成物だが手が入っている
    _write_atomic(path, text)
    return UPDATED


def install(project_root: Path, agents: str, *, status_dir: Path) -> list[dict]:
    """申告規約を配置する。戻り値は `{agent, path, status}` の一覧（表示は呼び手の仕事）。

    記録の読み書きと配置は **OS のファイルロックで囲んだ排他区間**で行う。囲まないと、
    `--agents claude` と `--agents codex` を同時に走らせたときに後勝ちで記録の片方が消える。
    なお「ハッシュ確認 → 書き込み」の間に人がエディタで保存した場合までは防げない
    （プロセス外の編集は排他できない）。窓を最小にするに留める。
    """
    if agents not in AGENT_CHOICES:
        raise ValueError(f"--agents は {AGENT_CHOICES} のいずれか: {agents!r}")
    project_root, status_dir = Path(project_root), Path(status_dir)
    targets = AGENT_NAMES if agents == "both" else (agents,)

    manifest_path = _manifest_path(status_dir)
    with file_lock(StatusStore(status_dir).lock_path(manifest_path)):
        manifest = read_manifest(status_dir)       # 検証済みの形だけが返る
        files = dict(manifest["files"])
        known = set(manifest["requested"])
        results: list[dict] = []
        for agent in targets:
            relpath = RELPATHS[agent]
            path = project_root / relpath
            text = render(agent)
            key = relpath.as_posix()
            status = _place(path, text, files.get(key))
            if status in (CREATED, UPDATED, UNCHANGED):
                files[key] = _digest(text)
            results.append({"agent": agent, "path": path, "status": status})
            # **配置ごとに記録する**。最後にまとめて書くと、途中で落ちたときに
            # 「ファイルは在るが所有記録が無い」＝以後ずっと更新できない状態が残る
            known.add(agent)
            manifest = {**manifest, "requested": sorted(known), "files": files,
                        "updatedAt": P.now_iso()}
            _write_manifest(status_dir, manifest)
    return results


def _managed_blocks(text: str) -> list[str]:
    """begin/end で挟まれた管理領域を取り出す（対応が壊れていれば空）。"""
    blocks: list[str] = []
    cursor = 0
    while True:
        begin = text.find(BEGIN_MARKER, cursor)
        if begin < 0:
            break
        end = text.find(END_MARKER, begin)
        if end < 0:
            return []                      # 開始だけあって終端が無い＝壊れている
        stop = end + len(END_MARKER)
        blocks.append(text[begin:stop])
        cursor = stop
    return blocks


def convention_present(project_root: Path) -> dict[str, bool]:
    """**現行の規約が過不足なくそこにあるか**（doctor 用）。手で統合された場合も見つける。

    マーカーや骨の文言だけを見ると、古い版の規約や、heartbeat・排他・エビデンス分離を
    削った本文でも「配置済み」になる。逆に「現行の全文を含むか」だけを見ると、
    **更新時に新しいスニペットを追記して旧規約が残った状態**を成功にしてしまう
    （矛盾する 2 つの規約を AI が読むことになる）。管理領域がちょうど 1 つで、
    その中身が現行と一致することまで確かめる。
    """
    project_root = Path(project_root)
    found = {}
    for agent, relpath in RELPATHS.items():
        path = project_root / relpath
        if not path.exists():
            found[agent] = False
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        blocks = _managed_blocks(text)
        if len(blocks) != 1 or blocks[0] != render(agent).strip():
            found[agent] = False
            continue
        # 旧形式（1 行マーカー）の規約が同居していないか。新形式だけを数えると、
        # 移行時に「旧規約を残して新スニペットを追記した」状態を見逃す
        found[agent] = LEGACY_MARKER not in text.replace(blocks[0], "")
    return found
