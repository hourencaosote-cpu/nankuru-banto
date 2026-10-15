ナンクル番頭（仮）— いまのファイルの役割

  create_invoice.py  … 1日目のテスト用（請求書を1枚作るだけ）。もう使わなくてOK
  agent.py           … 頭：AIが宿泊客と運航情報を調べて、延泊の「提案」を承認待ちリストに入れる
  approve.py         … 安全装置：責任者が提案を1件ずつ確認し、承認したものだけPayPalに請求書（下書き）を作る
  paypal_tools.py    … 手：PayPal sandboxに請求書を作る部品（approve.py からだけ使われる）
  common.py          … 共通の部品（設定の読み込み、ログ）
  data/guests.json   … 架空の宿泊客5名
  data/flights.json  … 架空の運航情報と、出典不明のSNS投稿（デマのテスト用）
  data/approvals.json … 承認待ちリスト（agent.py を動かすと自動でできる）
  logs/decision_log.jsonl … 誰が何を判断したかの記録（自動でできる）
  env.txt            … キーを書くファイル。誰にも見せない

動かし方（ターミナルで）
  cd ~/Developer/nankuru
  python3 agent.py --reset     （AIが提案を作る）
  python3 approve.py           （人が承認する）

請求書を客（テスト用の買い手）に送るところまで試すとき
  env.txt の BUYER_EMAIL= に、PayPal sandbox の「Personal」アカウントのメールアドレスを入れる
  （developer.paypal.com → Testing Tools → Sandbox Accounts に表示されている、末尾が @personal.example.com のもの）
  空のままなら、請求書は下書きを作るだけで止まる

宿のルール
  台風割引の割合は data/guests.json の hotel_policy で宿が決める（AIは変えられない）

客が支払ったかの確認
  python3 check_payments.py   （支払われた延泊は「確定」になり、一覧に出る）

責任者向け Web 画面
  python3 -m venv .venv
  source .venv/bin/activate
  python3 -m pip install -r requirements.txt
  python3 app.py

  ブラウザで http://localhost:5001 を開く
  Run AI check と Check payments は処理中の表示が出ます。
  承認・却下・支払い確認など、状態を変える操作はすべて POST で実行されます。
  Reset demo は現在の承認データと判断ログを data/archive/ に移してから空にします。
  PayPal sandbox 側の請求書は変更しません。
