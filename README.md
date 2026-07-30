# uapp-dash

複数の Unity プロジェクトで AI エージェントが並行開発している状況を、**人手が要る順**に見るための
ローカル・ダッシュボード。サーバーもデータベースも要らず、各プロジェクトの `.agent-status/` に
書かれたファイルを読んで自己完結 HTML を作る。

- **要注意ファースト**: 承認待ち → ブロック → プロセス消失 → 失敗 → 停滞 → レビュー待ち → 実行中 → 完了
- **自己申告と客観エビデンスの二層化**: エージェントの申告（`uapp-dash`）と、ツールが自動記録する事実（`uapp-dash-emit`）を
  別レーンに並べ、食い違いを警告する
- **Unity 並行開発固有のリソースパネル**: エディタ Play の占有・ビルドの直列化・デバイスの負荷とポート衝突

プロトコルの正本は [docs/protocol-v0.md](docs/protocol-v0.md)。
**導入手順は [SETUP.md](SETUP.md)**（AI が読んで自律導入できる粒度で書いてある）。

## 使い方

```bash
# 監視したいプロジェクトで一度だけ
# （.agent-status を作り・.gitignore に追記し・AI 向けの申告規約を配置する）
python -m uapp_dash --project <projectRoot> init --agents both

# 導入状況の自己診断（[済]/[未] 表示・未了があれば終了コード 1）
python -m uapp_dash --project <projectRoot> doctor

# エージェント側（自己申告）。begin の出力が unitId で、以後のコマンドに毎回渡す
# （AI のコマンド実行は 1 回ごとに別プロセスになることが多く、環境変数 UAPP_DASH_UNIT_ID は
#  同じシェルの中でしか効かない＝開始だけ成功して以降が全部失敗する）
# --claims は値ごとに分ける（空白区切り。";" 区切りは --tasks だけ。混同したら警告が出る）
python -m uapp_dash begin --label "issue #12 の実装" --tasks "設計;実装;テスト" `
    --claims "Assets/Scenes/Main.unity" "Assets/Scripts/**"
python -m uapp_dash heartbeat --unit-id <unitId> --activity "ビルド中" --ttl 2400
python -m uapp_dash task t1 --done --unit-id <unitId>
python -m uapp_dash blocked --unit-id <unitId> --reason "push の承認待ち" --needs approval
python -m uapp_dash end --unit-id <unitId> --result success --summary "テストまで通過"
python -m uapp_dash units                      # unitId を忘れたとき（残り TTL も出る。--all で完了分も）

# ツール側（客観エビデンス。ラッパーから呼ぶ。.agent-status が無ければ何もしない）
python -m uapp_dash.emit evidence.test --set suite=unity-editmode --set passed=12 --set failed=0 --set exitCode=0

# 表示
python -m uapp_dash view --out fleet.html        # 1 ファイルで完結する HTML
python -m uapp_dash view --serve --open          # 5 秒ごとに更新される表示
```

`--project` は**サブコマンドの前後どちらでもよい**（`uapp-dash --project <path> init` ＝
`uapp-dash init --project <path>`。`uapp-dash-emit` も同じ）。

`pip install -e .` すると **`uapp-dash` / `uapp-dash-emit`** コマンドとして使える。
短い `dash` は別名としても置かない — **Debian 系の POSIX シェル `/usr/bin/dash` と衝突**し
（別環境の AI が `dash begin` でシェルを起動して失敗した実績がある）、名前が一般的すぎて
他者の自作ツールとも当たるため。`uapp_e2e` キット側の名前空間（`UAPP_E2E_*` / `UAPP_DASH_*`）とも揃う。

## 画面

**12 プロジェクトが並んでも、スクロールせずに状況が読めること**を要件に作ってある。

| 領域 | 何を見るところ |
|---|---|
| 上段のスタットバンド | 稼働プロジェクト数 / 実行中の単位数 / **人手が要る件数**（人待ち・障害・要観察の内訳付き）／保持中の排他資源 / デバイス / 直近 1 時間の活動スパークライン |
| フリートストリップ | 全プロジェクトを 1 行のチップで俯瞰（要注意の重い順） |
| プロジェクトマトリクス | プロジェクトごとのカード。固定色＋短縮コードで識別し、実行中・要注意の件数、活動スパークライン、直近のエビデンスを出す |
| 右列 ACTION QUEUE | **P0 人待ち → P1 障害 → P2 要観察** の順に「次に手を貸すもの」だけを並べる |

- **並び順・件数・優先度は集約側（`aggregate.py`）だけが決める**。画面側は数え直さない
  （数えると必ずヘッダーと一覧が食い違う）
- ヘッダー右の切り替え: **テーマ**（ダーク / ライト / OS に合わせる）、**完了**（完了した単位も出す）、
  **常時表示**（TV モード＝文字を拡大し補助情報を畳む。壁掛けディスプレイ用）。選択は
  ブラウザに記憶される
- 幅 820px 以下では 1 列に畳み、ACTION QUEUE を先頭へ繰り上げる（スマホで最初に見るのは
  「手を貸す先」なので）

### 手元以外から見る

`--serve` は **127.0.0.1 にだけ**バインドする（絶対パス・ホスト名・作業ラベルが出るため、
既定で LAN へ晒さない）。外出先のスマホから見たいときは、ローカルのままトンネルを張る:

```powershell
uapp-dash view --serve            # 127.0.0.1:8788
cloudflared tunnel --url http://127.0.0.1:8788
```

表示される一時 URL を開く。**URL を知っている人は誰でも見られる**ので、社内の
プロジェクト名・パスが出ることを承知した上で使い、不要になったら `cloudflared` を止める。

## 環境変数

| 変数 | 用途 |
|---|---|
| `UAPP_DASH_UNIT_ID` | 操作対象の開発単位（`begin` の出力） |
| `UAPP_E2E_UNIT_ID` | 同上（E2E キット側のラッパーから渡される場合） |
| `UAPP_DASH_STATUS_DIR` / `UAPP_E2E_STATUS_DIR` | `.agent-status` の位置を明示する |
| `UAPP_DASH_PID` | 生存判定に使う持続プロセスの pid（未設定なら生存判定しない） |
| `UAPP_DASH_AGENT` / `UAPP_DASH_SESSION` | 表示用のエージェント名・セッション識別子 |
| `UAPP_DASH_HOME` | レジストリと設定の置き場（既定: `%LOCALAPPDATA%\uapp-dash`） |

## 設計上の要点

- **単位ごとにファイルを分ける**: 1 プロジェクトに複数の開発単位が同時に居るため、共有ファイルへの
  read-modify-write を避ける。書き手は自分のスナップショットを原子的に置換し、ジャーナルには追記だけする
- **停滞は TTL で判定する**: 長いビルド（20 分超）を停滞と誤判定しないよう、書き手が `ttlSec` を宣言する。
  `stalled` / `crashed` はダッシュボードだけが付ける（自己申告できる停滞は停滞ではない）。
  **期限の判定は 1 か所に集約**してあり（表示・停滞判定・エビデンスの自動結びつけが同じ結論になる）、
  `units` は残り TTL と期限切れを出す（`state: running` と期限切れは両立するため）
- **申告に数字を書かせない**: `end --summary` / `heartbeat --activity` にテスト件数らしき数字があると
  警告する（止めない）。自己申告と客観エビデンスを分けている意味は、食い違いを人に見せることにある
- **人待ちの状態は停滞にしない**: `blocked` / `waiting-approval` / `review` は止まっているのが正常
- **claims は勧告のみ**: 編集領域の重なりを警告するだけでブロックしない。シーン・プレハブ・アセット
  （マージ困難な YAML）はファイル丸ごと排他へ自動昇格し、`.meta` も同伴する
- **申告規約を配る導線を持つ**（`init --agents claude|codex|both`）: AI は「`uapp-dash begin` を打つべきだ」と
  知らなければ何も申告しない。規約は `.claude/rules/agent-dash.md` と ルートの `AGENTS.md` へ置く。
  **既存の `AGENTS.md` は書き換えず**、統合用のスニペットを表示するだけに留める。
  所有権は書き込み時ハッシュの記録（`.agent-status/agents.json`）で判定し、手が入ったファイルは上書きしない

## 開発

```bash
python -m pytest -q                                # 全量・デバイスも Unity も不要
python -m pytest tests/test_installed_smoke.py -q  # 導入検証用の最小スモーク（配布同梱）
```

スモークは**インストール済みの `uapp-dash` / `uapp-dash-emit` を PATH から別プロセスで起動して**検証する
（作業ツリーを暗黙に import して「入ったことにする」のを避けるため）。したがって
`pip install -e ".[test]"` した仮想環境で実行すること。**未インストールならスキップではなく失敗する**
（スキップだと終了コードが 0 になり、自動チェックが「入った」と誤認するため）。

## ライセンスと商標

MIT（同梱の `LICENSE`）。実行時の外部依存はゼロで、第三者のコード・フォント・ライブラリを
同梱していない（ビューアーも自己完結 HTML）。

Unity は Unity Technologies の商標。**このツールは Unity Technologies 公式の製品ではなく、
提携・承認も受けていない**。監視対象のプロジェクトやそこで使うツールには、それぞれの提供元の
ライセンス・利用規約が適用される。