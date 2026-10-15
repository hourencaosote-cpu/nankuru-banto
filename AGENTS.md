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
   PayPal を呼ぶのは、人が承認ボタンを押した処理（approve 系）と支払い確認だけ。
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

## 次のタスク：責任者向けの Web 画面

デザインの見本：Claude で作った「ナンクル番頭 承認画面」（Main＝責任者画面、Guest＝客のスマホ）。
見本の配置・文言・色に合わせる。画面の文字は英語。

### 作るもの
- Flask の `app.py` を1つ追加。`python3 app.py` で http://localhost:5001 に画面が出る（macOS は 5000 番を AirPlay が使うため）。
- テンプレートは `templates/`、CSS は `static/`。JavaScript は最小限（フォーム送信で十分）。

### 画面の中身（見本の Main に合わせる）
- 上部：宿名、台風の状況、データ時刻と「official notices only」、「Run AI check」ボタン
- 数字4つ：チェックアウト予定人数／欠航数／延泊に使える残り空室／延泊売上（支払い済み・未払い）
- 左：宿泊客一覧（便の状態、今の対応状況）
- 中央：承認待ちの提案カード（金額・内訳・根拠・AIの理由・客に届く案内文。システムが足した段落は印を付ける）
  と「Approve & send PayPal invoice」「Reject」「Later」ボタン。スタッフ連絡カードには「Mark as handled」
- 右：Safety gate（gate が止めた提案と理由、無視した SNS 投稿、ルール一覧）と AI の作業ログ
- 「Check payments」ボタン：check_payments と同じ処理で、支払い済みを confirmed にする

### 作り方の条件
- 既存の処理を**再利用**する。approve.py と check_payments.py の中身は関数に分け（例：
  `approve_extension(item_id)`、`reject(item_id)`、`check_payments()`）、ターミナル版と Web 版の両方から呼ぶ。
  同じ処理をコピーして2か所に書かない。
- 「Run AI check」は agent.run_agent(agent.call_gemini, env) を呼ぶ。完了まで数十秒かかるので、
  処理中の表示を出す。AI の作業ログは logs/decision_log.jsonl から表示する。
- gate の却下理由は decision_log の `rejected_proposal` から表示する。
- Approve を押したときだけ PayPal を呼ぶ。GET リクエストでお金が動く処理を書かない（POST のみ）。
- エラー（Gemini の混雑、PayPal の失敗）は画面に日本語ではなく英語で分かりやすく出し、処理は止めない。

### 完成の条件（これを満たしたら完了）
1. `python3 app.py` で画面が開く
2. Run AI check → 提案3件・スタッフ連絡1件・gate の却下・SNS 無視が画面に出る
3. Approve → PayPal sandbox に請求書が送られ、カードが「Invoice sent」に変わる
4. 客側で支払い → Check payments → 「Paid · extension confirmed」に変わる
5. ターミナル版（agent.py / approve.py / check_payments.py）も今まで通り動く
6. 上の「絶対に守るルール」を1つも破っていない

作業が終わったら、変えたファイルと変更内容の一覧を出してください（監査に使います）。
