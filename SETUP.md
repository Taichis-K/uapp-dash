# SETUP.md — AI向け導入ランブック

**これは AI エージェント（Claude Code / Codex 等）が読んで実行するための導入手順書。**
ユーザーから「このプロジェクトを uapp-dash で監視できるようにして」と依頼されたら、
この手順を上から順に実行し、各ステップの検証コマンドが通ったことを確認してから次へ進む。

このツールが何をするものかは [README.md](README.md)、ファイル契約の正本は
[docs/protocol-v0.md](docs/protocol-v0.md)。

## 前提の確認（最初に必ず）

1. **Python 3.10 以降**があること

   ```powershell
   python --version
   ```

   無い / 古い場合は**勝手にインストールせず**ユーザーに報告して指示を待つ。

2. **監視対象プロジェクトの場所**を特定する（Unity なら `Assets/` と `ProjectSettings/` がある場所）。
   不明ならユーザーに確認する。複数を並行監視する場合は、それぞれで手順 3 以降を繰り返す。

3. このツール自体は監視対象プロジェクトの中に**コピーしない**。1 か所へインストールし、
   各プロジェクトには `.agent-status/`（実行時の状態）と申告規約だけが置かれる。

## 手順

### 1. インストール

```powershell
cd <このリポジトリ or 展開先>
pip install .
```

検証（`uapp-dash` と `uapp-dash-emit` の 2 つが解決できること）:

```powershell
uapp-dash --version        # uapp-dash <版> と出る
uapp-dash-emit --version
```

- **コマンドはこの 2 つだけ**。短い `dash` は別名としても置かない — **Debian 系の
  POSIX シェル `/usr/bin/dash` と衝突する**ため（実際に別環境の AI が `dash begin` で
  シェルを起動して失敗した）。古い手順に `dash …` とあれば `uapp-dash …` に読み替える

- **失敗時**: `pip install --user .` を試す。それでも `uapp-dash` が PATH に出ない場合は
  `python -m uapp_dash` / `python -m uapp_dash.emit` で全機能を使える
  （以降の手順の `uapp-dash` を読み替える）。**PATH に出ないことを黙って放置しない** — 手順 4 の
  `uapp-dash doctor` が [未] として報告するので、ユーザーにどちらの運用にするか伝える。

- **Smart App Control / アプリ制御ポリシーに exe がブロックされる場合**
  （「アプリケーション制御ポリシーによってこのファイルがブロックされました」。
  片方の exe だけ起動できる、という形でも起きる）: pip が生成する launcher exe は未署名で、
  ハッシュ単位のレピュテーション判定に落ちることがある。**launcher を作り直すと解消しうる**:

  ```powershell
  pip install --force-reinstall --no-deps .
  ```

  launcher は埋め込み zip のタイムスタンプでバイト列が変わるため、同一版でも再生成で
  ハッシュが変わり判定が覆る（導入先で実測）。直らなければ `python -m uapp_dash.emit` へ
  フォールバックする。**同名の `.cmd` シムを exe の隣に置く回避は効かない** —
  `PATHEXT` は同一ディレクトリ内で `.exe` を `.cmd` より優先するし、`.cmd` 内の非 ASCII が
  OEM コードページで化けて stderr に出るだけで `doctor` の `--version` 照合が [未] に落ちる。

### 2. 監視対象の登録

```powershell
uapp-dash --project <対象プロジェクト> init --agents both
```

これで次が行われる:

| 生成物 | 内容 | 既存ファイルの扱い |
|---|---|---|
| `<対象>/.agent-status/` | 状態とイベントの置き場（units / resources） | 既存の記録は消さない |
| `<対象>/.gitignore` へ 1 行追記 | `.agent-status/`（ホスト名・絶対パス・pid を含むのでコミットしない） | git リポジトリのときだけ・重複追記しない |
| `<対象>/.claude/rules/agent-dash.md` | Claude Code 向けの**申告規約** | 自分が書いた通りのままのファイルだけ更新する |
| `<対象>/AGENTS.md` | Codex 等向けの**申告規約** | **既存の AGENTS.md は絶対に書き換えない**（統合用スニペットを表示するだけ） |

- `--agents` は `claude` / `codex` / `both`。片方だけ導入した後で他方を足したくなったら、
  該当値で再実行すればよい（再実行安全。既に置いたものは自動削除しない）。
  要求した種別は `.agent-status/agents.json` に積み上がり、**doctor がそれぞれを必須として点検する**
  （要求を減らすときは同ファイルの `requested` を編集する）
- **所有権はファイル中のマーカーではなく、書き込み時のハッシュ記録で判定する**
  （`.agent-status/agents.json`）。手で編集された生成物も上書きしない
- **既存の `AGENTS.md` があった場合**は「変更しなかった」と表示され、統合用のスニペットが出力される。
  勝手に追記せず、**ユーザーに提示して統合の可否を確認する**。統合するときは
  **表示されたスニペットを `begin` / `end` のマーカー行ごと一字一句そのまま貼る**
  ── doctor は「管理領域がちょうど 1 つあり、その中身が現行の規約と一致するか」で判定するため、
  要約・節の削除・**古い規約を残したまま新しいものを追記**すると [未] になる。
  貼っても次回以降に上書きされることはない
- 将来キット側の規約テンプレートが更新されると、既存の導入先は [未] に落ちる。
  `init --agents` を再実行すれば**このツールが作ったファイルは自動更新**され、
  手で統合したファイルは新しいスニペットを貼り直す
- **申告規約こそがこのツールの本体**。これが無いと AI は `uapp-dash begin` を打つべきだと知らず、
  ダッシュボードは永久に空のままになる

検証:

```powershell
uapp-dash --project <対象プロジェクト> doctor
```

### 3. 疎通確認（実際に 1 単位を作って消す）

```powershell
$unit = uapp-dash --project <対象> begin --label "導入疎通確認" --tasks "疎通"
uapp-dash --project <対象> task t1 --done --unit-id $unit
uapp-dash --project <対象> end --result success --summary "導入確認" --unit-id $unit
```

- `begin` は **unitId だけ**を標準出力に出す
- **以後のコマンドには `--unit-id` を渡す**。環境変数 `UAPP_DASH_UNIT_ID` でも同じことができるが、
  それが効くのは同じシェルの中だけ。AI のコマンド実行は 1 回ごとに別プロセスになることが多く、
  そこで環境変数に頼ると「開始はできたのに以降が全部失敗する」ことになる
  （配置される申告規約もこの前提で書かれている）
- **失敗時**: `.agent-status/` に書けていない可能性がある。`uapp-dash doctor` の
  「.agent-status/ へ書き込める」を確認する（ウイルス対策ソフトや権限で弾かれることがある）

ツール側のエビデンス経路も確認する（**エージェントからは書けない**別レーン）:

```powershell
uapp-dash-emit evidence.test --set suite=unit --set passed=1 --set failed=0 --set exitCode=0 --project <対象> --verbose
```

`.agent-status/` が無いプロジェクトでは**何も起きないのが正しい**（完全な no-op）。

- `--project` は**サブコマンドの前後どちらに置いてもよい**（`uapp-dash --project <対象> init` と
  `uapp-dash init --project <対象>` は同じ。`uapp-dash-emit` も同様）。AI がコマンドを組み立てるときに
  位置を気にしなくてよい
- **`doctor` の「ツール側エミッタの配線」は記録の実績で判定する**。この疎通で 1 件記録すれば [済] になり、
  以後は自前ラッパーからでも CI からでも、記録さえ出ていれば配線の形は問わない

### 4. 自己診断

```powershell
uapp-dash --project <対象> doctor
```

`[済]` / `[未]` / `[--]`（該当なし）で表示され、**`[未]` が 1 件でもあれば終了コード 1**。
`[未]` には対処コマンドが `→` 付きで出るので、それを実行してから再診断する。

診断は名前解決だけで済ませない: PATH の `uapp-dash` / `uapp-dash-emit` を実際に起動し、版がこの診断と
一致するかを見て、さらに一時ディレクトリで `init` を通してから [済] を出す（1〜2 秒かかる）。

### 5. 表示の確認

```powershell
uapp-dash view --out fleet.html        # 1 ファイルで完結する HTML（外部参照ゼロ）
uapp-dash view --serve --open          # 5 秒ごとに更新される表示（人が見る用）
```

手順 3 で作った単位が「完了」として出ていれば導入は成功
（完了した単位はヘッダー右の**完了**にチェックを入れると出る）。
何も出ない場合は `uapp-dash view --project <対象>` と明示して切り分ける（レジストリ未登録の可能性）。

- ヘッダー右で**テーマ**（ダーク / ライト / OS に合わせる）と**常時表示**（TV モード＝
  壁掛けディスプレイ用に拡大）を切り替えられる。選択はブラウザに記憶される
- **スマホから見たい場合**: `--serve` は 127.0.0.1 にだけバインドするので、
  `cloudflared tunnel --url http://127.0.0.1:8788` でトンネルを張って表示された URL を開く。
  **URL を知っている人は誰でも見られる**（プロジェクトの絶対パス・ホスト名・作業ラベルが出る）ので、
  ユーザーに確認してから使い、不要になったら止める
- **`--serve` を AI エージェントのセッションに紐づけて起動しない**（AI のバックグラウンド
  タスクとして起動すると、そのセッション終了と同時にダッシュボードも落ちる。落ちたことに
  AI 側が気づく契機も無く、人が見に行って初めて「表示が止まっている」と分かる）。
  常時監視したいなら、人が別ターミナルで常駐させるか、OS のサービス/タスクとして起動する
- **1 プロジェクトだけを見る場合**は `--serve` を常駐させる動機が弱い。検証スクリプトの末尾で
  HTML を更新しておき、見たいときに開くほうが合う:

  ```powershell
  # verify.ps1 / run-e2e.ps1 等の末尾に 1 行
  uapp-dash view --out "$PSScriptRoot\..\Builds\fleet.html"
  ```

### 6. ツール側の自動エビデンス配線（任意）

ビルド・テスト・E2E のラッパーがあるなら、そこから `uapp-dash-emit` を呼ぶと**客観エビデンス**が並ぶ。

```powershell
uapp-dash-emit evidence.build --set target=Android --set exitCode=$LASTEXITCODE --set durationSec=930
uapp-dash-emit evidence.e2e  --set passed=7 --set failed=0 --set exitCode=0 --set journeyReport=Builds\journey\report.html
```

- 種別と data は [docs/protocol-v0.md](docs/protocol-v0.md) の §3 が正本
- **AI にはこの経路を使わせない**（自己申告と客観エビデンスを分けているのは食い違いを検出するため）
- uapp_e2e（Unity E2E キット）を導入済みのプロジェクトは、キット側の `scripts/emit-status.ps1` が
  既にこの配線を持っている。`uapp-dash doctor` が検出して [済] と表示する

### 7. 複数プロジェクトの監視

各プロジェクトで手順 2 を実行すれば、`uapp-dash view` が横断表示する（`init` / `begin` 時に自動登録される）。
まとめて走査したい場合は `%LOCALAPPDATA%\uapp-dash\config.json` に走査ルートを書く:

```json
{ "roots": ["D:\\WinDev\\AI"], "scanDepth": 3 }
```

## 導入後の確認（同梱テスト）

**配布リポジトリ／zip に含まれる**最小テストで、環境側の問題を切り分けられる
（Unity もデバイスも不要・数秒）。**インストールされた `uapp-dash` / `uapp-dash-emit` を PATH から
別プロセスで起動して確かめる**ため、未インストールなら失敗する:

```powershell
pip install pytest        # または最初から pip install ".[test]"
python -m pytest tests/test_installed_smoke.py -q
```

- このファイルは **wheel には入らない**（pip でパッケージだけ入れた利用者の手元には無い）。
  その場合は `uapp-dash doctor`（終了コード 0）で導入状態を確認する。ただし doctor が見るのは
  「PATH のコマンドが起動し、この診断と同じ版で、実際に `init` を通せるか」まで。
  **PATH のコマンドが手元の配布物と同一の中身か**を確かめられるのは同梱スモークだけ
  （生成された規約全文を同梱ソースの出力と突き合わせる）

## トラブル時

| 症状 | 最初に見るところ |
|---|---|
| ダッシュボードが空のまま | 申告規約が配置されているか（`uapp-dash doctor`）。AI が `uapp-dash begin` を打っていない典型 |
| 単位が「停滞」ばかりになる | 長時間処理の前に `--ttl` を伸ばしていない（Android ビルド 2400 等）。または `uapp-dash end` を打たずに放置している |
| 「プロセス消失」と出る | `owner.pid` に**短命なプロセスの pid** を渡していないか（CLI 自身の pid は書かない。持続プロセスの pid を `UAPP_DASH_PID` で渡す） |
| 排他資源が解放できない | `uapp-dash resource release <id>` の結果を見る。`not-owner` は別の保持者、`busy` は操作中。保持者が居ないと確認できた場合のみ `--force` |
| `uapp-dash view` に出ない | レジストリ未登録。`uapp-dash --project <対象> init` を実行するか `uapp-dash view --project <対象>` |

判断に迷う変更（既存 `AGENTS.md` の統合など）は**実行前にユーザーへ提示して指示を仰ぐ**こと。
