# StockSignalForge

**easymoneysniper**

[English](README.md) | [中文](README_zh.md) | **日本語** | [한국어](README_ko.md) | [العربية](README_ar.md)

[![CI](https://github.com/JohnCharlesFields/StockSignalForge/actions/workflows/test.yml/badge.svg)](https://github.com/JohnCharlesFields/StockSignalForge/actions/workflows/test.yml)

[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) を基盤とするAI株式リサーチ・ワークベンチ。相場、ニュース、押し目候補、個別銘柄分析、保有銘柄の検証、戦略バックテストをまとめます。

> **主な対象は米国株と米国上場株式オプションです。中国本土A株と香港株は現在対応作業中で、全面対応ではありません。** 上流フレームワークにアダプターがあっても、本プロジェクトの全工程が検証済みとは限りません。

## 対応市場

| 市場 | 現在の範囲 |
| --- | --- |
| 米国株 | 日次候補、チャート、企業情報、ニュース、ウォッチリスト、戦略研究が中心 |
| 米国株式オプション | Call/Put/様子見の方向研究、取得可能なIV/Greeks、満期60–90日のCallと退出シナリオの手動計算。データ権限と品質に依存 |
| 中国本土A株 | 対応作業中。全工程の検証は未完了 |
| 香港株 | 対応作業中。全工程の検証は未完了 |

研究支援ツールであり、利益や売買判断を保証しません。ブローカーへの接続や実注文の自動発注は行いません。SOXLの実行実験はシミュレーションです。

## 主な機能

- 押し目候補の順位、継続監視、予測と後日の結果の照合。
- 個別銘柄のローソク足、トレンド構造、因果的研究マーカー、企業・業種・サプライチェーンとリスク水準。
- オプションの方向とボラティリティ研究、取得済み相場による契約・退出シナリオ比較。
- 優先順位付きニュースと、実際の字幕・文稿に基づく動画投稿者の意見。
- ウォッチリスト、保有銘柄レビュー、三層シグナル、イベント/GEXの品質表示。
- 因子研究、バックテスト、バージョン別の前向き検証、日次バッチとキャッシュの鮮度。

欠けたIV、OI、出来高、字幕、決算日、過去の構成銘柄は「不明」のまま表示し、ゼロや架空の証拠に置き換えません。

## 画面

![日足の実験的研究マーカー](assets/screenshots/desktop/13-v-swing-chart.png)

[画面一覧](SCREENSHOTS.md)。画像は過去の研究スナップショットで、利益の証明ではありません。多言語ドキュメントは、アプリ全画面の翻訳を意味しません。

## Dockerで起動

Docker Engine/Compose、またはLinuxコンテナー・モードのDocker Desktopを使用します。リポジトリーのルートで実行し、既存の秘密設定を上書きしないでください。

```powershell
if (!(Test-Path agent/.env)) { Copy-Item agent/.env.example agent/.env }
# Configure agent/.env locally before starting the application.
docker compose up -d --build vibe-trading
```

起動前に `agent/.env` をローカルで編集します。[ワークベンチ](http://127.0.0.1:19090) と [ヘルスチェック](http://127.0.0.1:19090/health) を確認してください。

```sh
docker compose logs --tail 100 vibe-trading
docker compose stop vibe-trading
```

既定の公開ポートはlocalhost限定です。リモートアクセスには認証と信頼できるOriginの設定が必要です。

### 既存環境の更新

スキャン、校正、情報補完などの実行中タスクが完了してから、SQLiteのオンラインバックアップAPIで整合性のあるバックアップを取り、名前付きボリュームを保持します。

```sh
docker compose build vibe-trading
docker compose up -d --no-deps vibe-trading
```

`docker compose down -v` は実行しないでください。Gitのcloneだけでは履歴DB、キャッシュ、秘密設定、Cookiesは移行されません。

## データと秘密設定

| 設定 | ローカル変数・資料 |
| --- | --- |
| AIモデル | `agent/.env` の `LANGCHAIN_PROVIDER`、`LANGCHAIN_MODEL_NAME`、プロバイダーの認証情報 |
| GilData MCP | `GILDATA_MCP_URL` と `GILDATA_MCP_TOKEN` を別々に設定。[接続資料](GILDATA_MCP.md)（中国語） |
| その他の相場データ | 自分のAPIキーと市場別の利用権限 |
| API認証 | `API_AUTH_KEY` |
| 外部MCP | `config/agent.example.json`、`config/mcp-client.example.json` |

GilDataは任意の証拠データ源で、共同ブランドや利益の保証ではありません。株価の契約にOPRAオプションが含まれるとは限りません。日時と代替データ源を確認してください。

キー、認証付きMCP URL、Cookies、DB、業者の履歴データ、非公開学習ラベルは配布しません。バックグラウンド更新・学習は明示設定が必要で、新しいモデルは自動的には有効化されません。

## 検証上の制約

研究スコア、株式の校正確率、取引の純利益率、SPY比alpha、オプションの利益確率は別の指標です。方向が正しくても、プレミアム、スプレッド、時間価値とIV変化でオプションは損失になり得ます。シナリオは確率加重期待値ではありません。

研究マーカーは約定・利益の証明ではありません。日足確定後に同じ終値で約定したと仮定しません。株式分割、実際の情報取得時刻、構成銘柄とイベントの履歴には証拠が必要です。

厳格な5/8取引日の校正再構築は、時点が確認できる履歴標本が不足し、新曲線を有効化していません。旧数値は未検証の研究参考です。固定ルールの再検証でも安定した正の期待値は確認されておらず、閲覧済みの履歴を未使用テストとは呼びません。ソフトウェアテスト合格は、あらゆる先読みや過学習の不存在を証明しません。

## 更新・テスト・開発

10月20日の更新はブランド、株式分割会計、履歴の可知時刻、イベント履歴、校正の信頼性とCIの不具合を修正しました。公開済み `162054e` のGitHub CIは3321テスト・45サブテスト成功、既存2件スキップ。現在の状態は上のCIバッジを参照し、投資成績と混同しないでください。

[変更履歴](CHANGELOG.md)（英語） · [実測テスト記録](PUBLIC_RELEASE.md)（中国語） · [修正の詳細](wiki/RELEASE_20261020_ZH.md)（中国語） · [Callの方法論](wiki/CALL_PLAN_RESEARCH_ZH.md)（中国語）

Python 3.11+、Node.js 20+。開発APIは `VITE_API_URL` で指定します。

```sh
python -m pip install -e ".[dev]"
pytest --ignore=agent/tests/e2e_backtest --ignore=agent/tests/test_e2e_harness_v2.py --tb=short -q
cd frontend
npm ci
npm run build
```

実モデルのE2Eテストは別途有効化と認証が必要です。隔離テストに本番ボリュームを接続しないでください。

## 謝辞・ライセンス

[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) のフレームワークを使用しています。[上流の日本語資料](README_ja.upstream.md)、[英語資料](README.upstream.md)、[中国語資料](README_zh.upstream.md) を保持しています。

[MIT License](LICENSE) と [NOTICE](NOTICE)。ソフトウェアライセンスは第三者データへのアクセスや再配布の許可を含みません。
