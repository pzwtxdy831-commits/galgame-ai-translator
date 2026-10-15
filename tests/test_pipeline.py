import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import gal_translate as gt  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(__file__), '..', 'samples', 'first.ks')


class ParseTests(unittest.TestCase):
    def test_edge_tags_are_kept_out_of_text(self):
        part = gt.split_text_line("今日から新しい学期が始まる。[l][r]")
        self.assertEqual(part["text"], "今日から新しい学期が始まる。")
        self.assertEqual(part["suffix"], "[l][r]")

    def test_inline_tags_become_placeholders(self):
        part = gt.split_text_line('彼女は[ruby text="はにか"]照れた。[p]')
        self.assertEqual(part["text"], "彼女は{1}照れた。")
        self.assertEqual(part["tags"], ['[ruby text="はにか"]'])

    def test_commands_labels_and_ascii_are_skipped(self):
        for line in ["@bg storage=a", "*start|プロローグ", "; コメント", "#さくら", "[l][r]", "hello[p]"]:
            self.assertIsNone(gt.split_text_line(line), line)

    def test_parse_script_skips_iscript_and_tracks_speakers(self):
        with open(SAMPLE, encoding='utf-8') as f:
            entries, speakers = gt.parse_script("first.ks", f.read())
        texts = [e["text"] for e in entries]
        self.assertFalse(any("翻訳しない" in t for t in texts))
        self.assertIn("さくら", speakers)
        self.assertIn("悠斗", speakers)
        greeting = next(e for e in entries if e["text"].startswith("「先輩、おはよう"))
        self.assertEqual(greeting["speaker"], "さくら")
        bento = next(e for e in entries if "お弁当" in e["text"])
        self.assertEqual(bento["speaker"], "悠斗")


class CheckTests(unittest.TestCase):
    def test_flags(self):
        e = {"text": "彼女は{1}照れた。", "trans": "她害羞了。"}
        self.assertIn("tags", gt.check_entry(e))
        e["trans"] = "她{1}害羞了。"
        self.assertEqual(gt.check_entry(e), [])
        e["trans"] = "她{1}はにかんだ。"
        self.assertIn("kana", gt.check_entry(e))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        game = os.path.join(self.tmp, 'game')
        os.makedirs(game)
        with open(SAMPLE, encoding='utf-8') as f:
            raw = f.read().replace('\n', '\r\n').encode('cp932')
        gt.xp3_write([("first.ks", raw)], os.path.join(game, 'data.xp3'))
        self.game = game
        self.project = os.path.join(self.tmp, 'proj')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_extract_translate_pack_roundtrip(self):
        self.assertEqual(gt.main(['extract', self.game, self.project]), 0)
        glossary = gt.load_json(os.path.join(self.project, gt.GLOSSARY_FILE))
        self.assertIn("さくら", glossary)
        glossary["さくら"] = "小樱"
        gt.save_json(os.path.join(self.project, gt.GLOSSARY_FILE), glossary)

        self.assertEqual(gt.main(['translate', self.project, '--backend', 'dummy', '--batch', '3']), 0)
        out = os.path.join(self.tmp, 'patch2.xp3')
        self.assertEqual(gt.main(['pack', self.project, '-o', out]), 0)

        index = gt.xp3_read_index(out)
        self.assertEqual([n for n, _ in index], ["first.ks"])
        with open(out, 'rb') as f:
            data = gt.xp3_read_file(f, index[0][1])
        self.assertTrue(data.startswith(b'\xff\xfe'))
        text = data[2:].decode('utf-16le')
        self.assertIn("【译】今日から新しい学期が始まる。[p]\r\n", text)
        self.assertIn("\r\n【悠斗】\r\n", text)  # no glossary entry -> name kept, not sent to the LLM
        self.assertIn('[ruby text="はにか"]', text)
        self.assertIn("#小樱\r\n", text)
        self.assertIn("@bg storage=school_gate\r\n", text)
        self.assertIn("「これは翻訳しない」", text)

    def test_review_roundtrip(self):
        gt.main(['extract', self.game, self.project])
        gt.main(['translate', self.project, '--backend', 'dummy'])
        tsv = os.path.join(self.tmp, 'review.tsv')
        gt.main(['export', self.project, tsv])
        with open(tsv, encoding='utf-8-sig') as f:
            content = f.read()
        content = content.replace("【译】今日から新しい学期が始まる。", "新学期从今天开始了。")
        with open(tsv, 'w', encoding='utf-8-sig') as f:
            f.write(content)
        gt.main(['import', self.project, tsv])
        entries = gt.load_json(os.path.join(self.project, gt.ENTRIES_FILE))
        self.assertTrue(any(e["trans"] == "新学期从今天开始了。" and not e["flags"] for e in entries))


if __name__ == '__main__':
    unittest.main()
