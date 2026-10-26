# ナンクル番頭（仮）— 開発ルール（AIコーディング担当向け）

このファイルは、このフォルダでコードを書くAI（ChatGPT / Codex など）への指示書です。
作業の前に必ず全部読み、ここに書かれたルールを破る変更はしないでください。

## 何を作っているか

台風で帰りの便が欠航した宿泊客に、小さな宿が「延泊」を提案し、PayPal で請求・確定するまでを
AIエージェントが手伝う仕組み。PayPal AI Hackathon（Devpost）への提出作品。締切 2026-11-13 05:00 JST。

流れ：AI（頭）が調べて提案 → 安全装置（gate）がルール違反を止める → 人（責任者）が承認
→ PayPal で請求書を客に送る（手）→ 客が支払うと延泊が確定。

## ファイルの役割

| ファイル | 役割 |
|---|---|
| agent.py | 頭。Gemini の function calling で道具を使い、提案を data/approvals.json に入れる。gate もここ（tool_propose_stay_extension） |
| approve.py | 人の承認（ターミナル版）。承認したものだけ paypal_tools を呼ぶ |
| check_payments.py | 支払い確認。PAID なら status を confirmed にする |
| paypal_tools.py | 手。PayPal Invoicing v2（sandbox 固定） |
| common.py | env.txt 読み込み、JSON 読み書き、判断ログ（logs/decision_log.jsonl） |
| data/guests.json | 架空の宿泊客5名と宿のルール（hotel_policy.typhoon_discount_percent） |
| data/flights.json | 架空の公式運航情報と、出典不明のSNS投稿（デマのテスト用） |
| env.txt | 秘密のキー。絶対に読まない・表示しない・コミットしない |

承認リストの status の流れ（extension）：pending → approved（下書きのみ）/ sent → confirmed、または rejected。
staff_note：pending → acknowledged / dismissed。

## 絶対に守るルール（破る変更は不可）

1. **AI はお金を動かせない。** AI に渡す道具（TOOLS）に、請求・送信・返金・支払いの道具を追加しない。
   PayPal を呼ぶのは、人が承認ボタンを押した処理（approve 系）、支払い確認、
   人が押す支払い記録（sandbox デモ用）だけ。
2. **金額はコードが計算する。** AI に金額や割引率を決めさせない。割引率は hotel_policy から読む。
3. **gate のルールを緩めない。** 欠航のみ／直接予約のみ（OTA はスタッフへ）／必要な泊数まで（最大3泊）／
   空室数まで／重複禁止／案内文必須。ルールを変えるときは人に確認する。
4. **PayPal は sandbox 固定。** API_BASE を live に変えない。
5. **秘密を扱わない。** env.txt の中身を読まない・ログに出さない・画面に出さない。キーをコードに書かない。
6. **実在の人や宿のデータを入れない。** データはすべて架空。
7. **天気を予報しない。** 気象業務法の関係で、自前の予報は作らない。公式発表を表示するだけ。
8. **出典不明の情報（SNS）は判断に使わない。** 表示するときは「未確認・判断に不使用」と明記する。
9. **判断はすべて log_decision で記録する**（AI の提案、gate の却下、人の承認・却下、支払い確認）。
10. 新しいライブラリは最小限に。追加したら requirements.txt に書く。

Web 画面は実装済み（app.py / templates / static）。変更時も上のルールを守ること。
