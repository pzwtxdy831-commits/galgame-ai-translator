# 🌸 Galgame AI Translator（AI 汉化助手）

[中文](#中文) | [English](#english)

> **原型阶段（v0.1）**：目前只支持吉里吉里（Kirikiri / KAG）引擎的 `.ks` 剧本。

---

## 中文

把民间汉化里最费时间的几步串成一条流水线：

```
提取剧本 → LLM 批量翻译（带术语表） → 自动检查 → 表格校对 → 打包成补丁 XP3
```

- **提取**：直接读游戏目录里的 XP3 封包（按引擎加载顺序，后加载的覆盖先加载的），也可以读已经用 GARbro 解出来的 `.ks` 文件夹。
  - 自动跳过指令行、标签行、注释和 `[iscript]` 代码块。
  - 行首、行尾的标签（`[l][r]`、`[p]`）不送给模型；行中的标签（如 `[ruby]`）换成 `{1}` 占位符。
  - 自动收集角色名（`#名字`、`【名字】`），生成术语表模板。
- **翻译**：支持两种后端。
  - **Gemini API**：术语表和指令固定放在请求开头，能命中 Gemini 隐式缓存，降低重复部分的费用。运行结束会打印缓存命中率。
  - **OpenAI 兼容接口**：比如本地 llama.cpp 跑的 [SakuraLLM](https://github.com/SakuraLLM/SakuraLLM) 模型，加 `--sakura` 使用 Sakura 专用提示词格式。
  - 每批翻译完都会保存进度，中断后重新运行会接着翻。
- **检查**：标出残留假名、占位符丢失、译文异常变长等可疑行。
- **校对**：导出 UTF-8 的双语 TSV，可以直接用 Excel 编辑，改完再导回来。
- **打包**：生成独立的 `patch_chs.xp3`，剧本统一存为 UTF-16LE（带 BOM）。不改动原版封包，删掉补丁就能恢复原样。

### 使用

需要 Python 3.8 以上，没有第三方依赖。

```bash
# 1. 提取（游戏目录 / 单个 XP3 / 解包后的 .ks 文件夹都可以）
python gal_translate.py extract "D:\Games\XXX" myproject

# 2. 编辑 myproject/glossary.json，填好角色名译名，例如 {"さくら": "小樱"}

# 3a. 用 Gemini 翻译（先只翻 50 行试试效果）
set GEMINI_API_KEY=你的key
python gal_translate.py translate myproject --limit 50

# 3b. 或者用本地 SakuraLLM（先启动 llama-server）
python gal_translate.py translate myproject --backend openai --base-url http://127.0.0.1:8080/v1 --sakura

# 4. 查看进度和可疑行
python gal_translate.py status myproject

# 5. 导出表格校对，改完再导入
python gal_translate.py export myproject review.tsv
python gal_translate.py import myproject review.tsv

# 6. 打包，复制进游戏目录，命名为下一个没被占用的 patchN.xp3
python gal_translate.py pack myproject -o patch2.xp3
```

### 实测效果

用 `samples/first.ks`（为本项目写的测试剧本）和本地 Sakura 1.5B 模型实测：

| 原文 | 译文 |
|---|---|
| 桜の花びらが、風に乗ってゆっくりと舞い落ちていく。 | 樱花花瓣乘着风缓缓飘落。 |
| 「おはよう、さくら。朝から元気だな」 | 「早啊，小樱。一大早就这么有精神。」 |
| 「……お弁当、また作ってきたのか？」 | 「……你又做了便当吗？」 |
| 彼女はそう言って、`{1}`照れくさそうに笑った。 | 她这么说完，害羞地笑了。 ⚠️ 被标记：`[ruby]` 占位符丢失 |

### 已知限制

- 只支持 Kirikiri / KAG；加密的 XP3 需要先用 GARbro 等工具解包。
- 被 TJS 简单加密（文件头 `FE FE`）的剧本会被跳过。
- 占位符丢失的行，打包时会去掉行中的标签（多半是注音、字体这类装饰），这些行建议人工校对。
- Gemini 后端按官方 REST 接口实现，目前只用本地模型实测过完整流程。
- 打包后的游戏需要能显示中文的字体，字体问题请参考 Kirikiri 汉化排查笔记。

### 开发

```bash
python -m unittest discover -s tests
```

---

## English

A translation pipeline for Kirikiri / KAG visual novels: **extract → LLM batch translation with a glossary → automatic checks → spreadsheet proofreading → patch XP3**.

- Reads XP3 archives in the engine's load order, or a folder of `.ks` scripts. Skips commands, labels, comments and `[iscript]` blocks. Inline tags become `{n}` placeholders.
- Backends: **Gemini API** (glossary and instructions form a stable prefix for implicit caching; cache hit rate is reported) or any **OpenAI-compatible** server such as llama.cpp running SakuraLLM (`--sakura`).
- Resumable: progress is saved after every batch.
- Flags leftover kana, lost placeholders and suspiciously long lines. Exports and imports a bilingual TSV for review.
- Packs a standalone `patch_chs.xp3` (UTF-16LE scripts) without touching the original archives.

Pure Python standard library (3.8+). See the Chinese section for commands. **Status:** prototype; Kirikiri only.

## License

[MIT](LICENSE)
