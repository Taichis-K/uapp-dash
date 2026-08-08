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

#### 1-0. 先に「どこへ入れるか」を決める（**ウイルス対策対策。飛ばさない**）

**この項は Windows の話**（launcher が未署名 exe になるのは Windows だけ。
macOS / Linux の launcher は素の Python スクリプトで隔離対象にならない —
そちらで典型なのは「入ったのに PATH に無い」で、1-3 の切り分けを見る）。

`pip install` は `uapp-dash.exe` / `uapp-dash-emit.exe` という**未署名の launcher exe** を作る。
これは実在のマルウェアと同じ特徴（署名なし・レピュテーションなし・毎回ハッシュが変わる）を
持つため、**ウイルス対策に隔離・ブロック・解析されることがある**。厄介なのは、どれも
「`pip install` は成功したのにコマンドが使えない」という同じ形で出ることと、
未知の未署名 exe をクラウドへ送って解析する種類の製品では、**その間マシンが数分止まる**ことがある点。

**インストール先を確認し、可能ならウイルス対策の除外へ先に登録する。**

```powershell
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"   # exe が置かれる場所
```

| 状況 | 選ぶもの |
|---|---|
| 除外に登録できる（自分の PC・管理者権限がある） | **そのまま `pip install .`**。上のパスを除外に登録しておく |
| 除外の運用が決まっている（開発用フォルダだけ許可、等） | **その配下に venv を作って**そこへ入れる（下記） |
| 除外に触れない（会社支給 PC など） | **exe を作らない運用**にする（手順 1-2 の「入れずに使う」） |

除外済みの場所へ venv を作って入れる場合:

```powershell
python -m venv <除外済みフォルダ>\uapp-dash-venv
<除外済みフォルダ>\uapp-dash-venv\Scripts\python.exe -m pip install <このリポジトリ or 展開先>
# 以降 `uapp-dash` は <除外済みフォルダ>\uapp-dash-venv\Scripts\uapp-dash.exe を指す
```

- **除外はパス単位で登録する**。pip の launcher は埋め込み zip のタイムスタンプを含むため、
  同じ版を入れ直すたびにハッシュが変わる（ファイル内容を信頼登録する方式は次回無効になる）
- **`.gitignore` 済みの領域に venv を作る**（リポジトリ内に置くならなおさら）

#### 1-1. インストール

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

#### 1-2. 入れずに使う（exe を作れない環境）

ウイルス対策の除外に触れない環境では、**exe を一切作らずに運用できる**:

```powershell
python -m uapp_dash --version          # uapp-dash の代わり
python -m uapp_dash.emit --version     # uapp-dash-emit の代わり
```

- 機能は同じ（CLI 表面も引数もそのまま）。以降の手順とダッシュボードの申告規約に出てくる
  `uapp-dash …` を `python -m uapp_dash …` に読み替える

**読み替えたくない場合はシムを置く**（Windows のみ）。申告規約もツール側の
ラッパーも `uapp-dash …` と書かれているので、同じ名前で呼べれば読み替えが要らない。
`.cmd` と `.ps1` の**両方**が作られ、**PowerShell は `.ps1` を優先する**
（＝申告規約やキットのラッパーから呼ぶ実運用の経路では、引数が壊れない）:

```powershell
python -m uapp_dash install-shims --dir <PATH に入れる場所>   # 例: D:\tools\bin
$env:PATH = "<その場所>;$env:PATH"        # PATH の**前方**へ（後方だと壊れた exe が先に見つかる）
uapp-dash --version                       # 版が返れば成功
```

**永続化は GUI で行う。PATH を書き換えるワンライナーは案内しない**（どれも既存の PATH を
壊しうる。`setx` は 1024 文字で切り捨てて保存し、PowerShell の `$env:PATH` を渡すと
Machine の項目まで User へ複製される。`[Environment]::SetEnvironmentVariable(…,"User")` も、
User PATH が `REG_EXPAND_SZ` で `%USERPROFILE%\…` を含む場合に**参照が固定値へ化ける**）:

```powershell
rundll32 sysdm.cpl,EditEnvironmentVariables   # 「ユーザー環境変数」の Path の先頭へ追加する
```

- 生成先は **exe のある場所とは別のディレクトリ**にする（`PATHEXT` は同一ディレクトリ内で
  `.exe` を `.cmd` より優先するため、隣に置いても意味が無い。同じ場所を指定すると拒否される。
  `pip install --user` の置き場も拒否対象）
- 中身は ASCII のみ・終了コードをそのまま返すので、`doctor` の判定も
  `resource acquire` の終了コード 3 による分岐も従来どおり働く
- **壊れる引数（`.cmd` から呼んだ場合）**。`cmd` がコマンドラインを解釈するため、
  **引数の値に `&` `|` `<` `>` `^` `%` が入っていると壊れる**（実測）:

  | 渡した値 | `.cmd` 経由で届く値 | `.ps1` 経由 |
  |---|---|---|
  | `--label "A&B"` | `A`（さらに **`B` がコマンドとして実行される**） | そのまま |
  | `--label "a\|b"` | 引数ごと消える（**`b` がコマンドとして実行される**） | そのまま |
  | `--label "car^et"` | `caret` | そのまま |
  | `--label "50%PATH%"` | 環境変数が展開された別の文字列 | そのまま |
  | `--label "a>b"` | 引数が消え、`b` というファイルが作られる | そのまま |

  `&` と `|` は文字化けではなく**自由テキスト経由のコマンド実行**。issue 名や依頼文を
  そのままラベルへ書き写す運用では現実に踏む。**`cmd /c` がコマンドラインを読む時点で
  起きるので `.cmd` の中身では直せない**ため、`.ps1` を併設して PowerShell からは
  そちらが使われるようにしてある（PowerShell は引数を配列のまま渡す）。
  `=` `,` `;` `"` 空白・空文字列は `.cmd` でも素通しする
  （`!` は呼び出し元が `cmd /V:ON` のときだけ展開されるが、これはシムに限らず exe でも同じ）。
  **cmd.exe や Python の `subprocess` から呼ぶ場合は `.cmd` が使われる**ので、そこから
  自由テキストを渡すなら上の制限が生きる。
  `uapp-dash doctor` は、コマンドがシムに解決されている場合にこの制限を毎回表示する
  （`.ps1` が置けているかどうかで文言が変わる）
- `.ps1` は**呼び出し元の PowerShell の中で動く**ので、そこに由来する落とし穴を塞いである:
  **空文字列の引数**（`--label ""`）を保つ / **python を起動できなかったときに成功を返さない**
  / **`PYTHONPATH` / `PYTHONSAFEPATH` を呼び出し元へ残さない**
  （残すと呼ぶたびに増殖し、同じシェルで動かす他の Python の探索規則まで変わる）
- `uapp-dash doctor` は `.ps1` も**実走して版を照合する**。`.cmd` と `.ps1` は別ファイルなので、
  更新が途中で失敗すると新旧が混在しうる（PowerShell は `.ps1` を優先するので、
  存在確認だけでは実運用で動く版を確かめたことにならない）
- **実行ポリシーで `.ps1` が動かない環境では `.ps1` を置かない**（生成後に実走して確かめる）。
  置いてしまうと PowerShell がそれを `.cmd` より優先し、動いていた `.cmd` が使われなくなるため。
  その場合は `install-shims` がその旨を表示し、上の制限が PowerShell からも生きる
- **Python 3.11 以降が要る**。`python -m` はカレントディレクトリを `sys.path` の先頭へ置くので、
  呼び出し先に `uapp_dash` というディレクトリがあるとそちらが実行されてしまう。
  `-P` でこれを止めているが、3.10 以前はこのオプションを持たない
  （`install-shims` が実走で確かめ、効かない解釈系ではシムを作らずに理由を返す）
- シムは、**`install-shims` を実行した実装**を指すように作られる。素の
  `python -m uapp_dash` が別のコピー（古い site-packages 等）を拾う場合や、
  そもそも入っていない場合は `PYTHONPATH` を埋めて固定する。
  **リポジトリを移動・削除したり入れ直したりしたら、シムを作り直す**こと
- シムを使わず読み替える場合は、**ユーザーとチームに明示的に伝えること**。`uapp-dash doctor` は
  「コマンドが使える」を [未] と報告し続ける（PATH に無いのは事実なので、これは正しい表示）。
  何が [未] のままで、なぜそれでよいのかを共有しておかないと、後から見た人が直そうとする。
  ツール側のラッパー（`uapp-dash-emit` を呼ぶビルド/テストスクリプト）も同じ読み替えが要る

#### 1-3. 失敗時の切り分け

- `pip install --user .` を試す。それでも PATH に出ない場合は上の「入れずに使う」へ
- **コマンドが PATH に出ないときは、まず `python -m uapp_dash doctor` を実行する**。
  doctor は「**launcher が無い**」と「**launcher はあるが PATH に無い**」を、
  **確認できる候補（既定と `--user` の置き場）の範囲で**区別し、
  後者では実体のフルパス・PATH へ足す 1 行・`python -m` への読み替えを出す
  （pipx や別 venv などの候補外に入れた場合は自分でその場所を PATH へ通す）。
  **macOS / Linux で `pip install --user` した場合の典型は後者**
  （user base の `bin`（例: `~/Library/Python/3.x/bin`）が PATH に無い。
  ウイルス対策は関係ないので疑わなくてよい）
- **（Windows）`pip install` は成功したのにコマンドが無い / 起動しない** → **ウイルス対策を疑う**
  （隔離ログを確認する）。`uapp-dash doctor` が、exe の在るべき場所と
  「確認した候補には存在しない」ことまで表示するので、隔離か未インストールかを疑う材料になる
  （`--user` で入れた場合の置き場も候補に含めて見るので、`--user` を「存在しない」と誤診しない。
  確定は隔離ログで取る）。
  対処は (1) 除外へ登録して `pip install --force-reinstall --no-deps .` で作り直す
  (2) 除外済みの場所へ venv を作って入れ直す (3) 「入れずに使う」へ切り替える
- **PATH に出ないことを黙って放置しない** — 手順 4 の `uapp-dash doctor` が [未] として
  報告するので、ユーザーにどの運用にするか伝えて決めてもらう

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
