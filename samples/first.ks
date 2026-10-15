; Sample script for testing (original text written for this project)
*start|プロローグ
@bg storage=school_gate
@playbgm storage=bgm01

桜の花びらが、風に乗ってゆっくりと舞い落ちていく。[l][r]
今日から新しい学期が始まる。[p]

#さくら
「先輩、おはようございます！　今日もいい天気ですね」[p]

#悠斗
「おはよう、さくら。朝から元気だな」[p]

#さくら
「はい！　だって、先輩と一緒に登校できるんですから」[p]

[iscript]
var count = 0; // 「これは翻訳しない」
[endscript]

彼女はそう言って、[ruby text="はにか"]照れくさそうに笑った。[p]

【悠斗】
「……お弁当、また作ってきたのか？」[p]

*choice|選択肢
[link target=*yes]一緒に食べる[endlink][r]
[link target=*no]今日は遠慮する[endlink][s]
