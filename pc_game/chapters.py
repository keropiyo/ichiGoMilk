"""位置GO MILK — Maker Faire 展示用ストーリー定義。"""

CHAPTERS = [
    {
        "id": 2,
        "chapter": "タンバリン審査に挑戦！",
        "name": "アイドル候補生のタンバリン審査",
        "image": "story_tambourine.png",
        "type": "story",
        "story": [
            "あなたはアイドル候補生。",
            "位置GO MILKタンバリンで、デビューをつかもう！",
        ],
        "action_label": "操作をおぼえる",
        "next": 3,
    },
    {
        "id": 3,
        "chapter": "ノーツは3種類！",
        "name": "位置GO MILKの操作説明",
        "image": "note_guid.png",
        "type": "story",
        "story": [
            "いちご：タンバリンを横に振る",
            "ROLL：すばやく連続で振る",
            "いちご牛乳：タンバリンを上へ振り上げる",
        ],
        "action_label": "3種類を練習する",
        "practice": True,
        "next": 4,
    },
    {
        "id": 4,
        "chapter": "オーディション本番！",
        "name": "デビュー曲『恋の電子回路』",
        "image": "story_practice.png",
        "type": "rhythm",
        "song_id": "koi_no_denshi_kairo",
        "intro": [
            "課題曲はデビュー曲の『恋の電子回路』。タンバリンで合格をつかもう！",
        ],
        "action_label": "審査スタート！",
        "next": 5,
    },
    {
        "id": 5,
        "chapter": "オーディション合格！",
        "name": "ユニット15369誕生",
        "image": "story_unitname.png",
        "type": "story",
        "story": [
            "あなたを新メンバーに迎え、",
            "アイドルユニット15369（いちごみるく）がデビュー！",
        ],
        "action_label": "次のステージへ",
        "next": 6,
    },
    {
        "id": 6,
        "chapter": "セカンドシングル！",
        "name": "2曲目『きらめきラブタンバリン』",
        "image": "story_second_single.png",
        "type": "rhythm",
        "song_id": "kirameki_love_tambourine",
        "intro": [
            "『きらめきラブタンバリン』。今度はあなたのタンバリンが主役！",
        ],
        "action_label": "ステージスタート！",
        "next": 7,
    },
    {
        "id": 7,
        "chapter": "運命のラストステージ！",
        "name": "3曲目『真夏の恋のトライアングル』",
        "image": "story_finale.png",
        "type": "rhythm",
        "song_id": "manatsu_triangle",
        "intro": [
            "『真夏の恋のトライアングル』。",
            "この曲のあと、15369の運命が決まる！",
        ],
        "action_label": "ラストステージへ！",
        "next": 8,
    },
    {
        "id": 8,
        "chapter": "運命のランキング発表",
        "name": "ベストテン第1位か解散か",
        "image": "story_photo.png",
        "type": "tambourine",
        "intro": [
            "15369の運命は、ベストテン第1位か、それとも解散か――。",
        ],
        "action_label": "タンバリンで運命を決める！",
        "outcomes": {
            1: {"title": "ベストテン第1位！", "desc": "15369はついに第1位を獲得！ 解散を回避し、伝説のステージへ！", "goal": True},
            2: {"title": "解散", "desc": "第1位には届かなかった。15369は約束どおり解散することに……", "dead": True},
        },
    },
]


def get_chapter(chapter_id):
    for chapter in CHAPTERS:
        if chapter["id"] == chapter_id:
            return chapter
    return None


BOARD = []
GOAL_INDEX = 0


def get_space(pos):
    return {}
