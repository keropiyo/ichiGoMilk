位置GO MILK リズムタンバリン（配布用）

このフォルダはゲーム体験に必要なソース・譜面・画像・音声だけをまとめたものです。


【Macでキーボード操作する場合】
1. ターミナルでこのフォルダの pc_game に移動
   cd pc_game
2. 仮想環境を作成（最初の1回だけ）
   python3 -m venv .venv
3. 必要なライブラリをインストール（最初の1回だけ）
   .venv/bin/python -m pip install -r requirements.txt
4. 起動
   .venv/bin/python main.py --keyboard

【タンバリン基板を使う場合】
必要なファームウェアを書き込んだ基板をUSB接続し、次のコマンドでポートを確認します。
   .venv/bin/python main.py --list-ports
表示された /dev/cu.usbmodem... を使って起動します。
   .venv/bin/python main.py --port /dev/cu.usbmodemXXXXXXXX

配布版ではランキングサイトへの自動送信は無効です。
