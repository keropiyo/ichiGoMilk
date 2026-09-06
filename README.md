# 位置GO MILK 〜アイドルライフゲーム〜

## このゲームについて

この物語は、デビュー曲から3曲目まででベストテン第1位を取れなければ解散という条件で、アイドルとしてデビューし奮闘するアイドルライフゲームである！

タンバリンを使ってリズム審査に挑戦し、アイドルとしてのデビューを目指します。

## 遊び方

- いちご：タンバリンを横に振る／叩く
- ROLL：タンバリンをすばやく連続で振る
- いちご牛乳：タンバリンを上に振り上げる

## 起動方法

### キーボードで遊ぶ場合

```bash
cd pc_game
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py --keyboard
```

### タンバリンを使う場合

まず、タンバリン基板をUSB接続して、ポート名を確認します。

```bash
.venv/bin/python main.py --list-ports
```

表示された`/dev/cu.usbmodem...`のポート名を使ってゲームを起動します。

```bash
.venv/bin/python main.py --port /dev/cu.usbmodemXXXXXXXX
```

`/dev/cu.usbmodemXXXXXXXX`の部分は、`--list-ports`で表示された自分の環境のポート名に置き換えてください。

例：

```bash
.venv/bin/python main.py --port /dev/cu.usbmodemSQTFB2IFOWSVL3
```

※ポート名は、タンバリン基板を接続するたびに変わる場合があります。

## フォルダ構成

- `pc_game/`：ゲーム本体
- `zephyr_tambourine/`：タンバリン基板用ファームウェア
- `docs/`：通信仕様

## ライセンス・クレジット

このゲームは位置GO MILKの公開用配布版です。

配布版ではランキングサイトへの自動送信は無効です。
