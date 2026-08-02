import unittest

from gongkao.answer_formatting import (
    answer_paragraph_alignments,
    normalize_answer_format_json,
)


class AnswerFormattingTest(unittest.TestCase):
    def test_alignment_is_bounded_to_answer_lines_and_known_values(self):
        self.assertEqual(
            answer_paragraph_alignments('["center","bogus","right","center"]', "标题\n正文\n落款"),
            ["center", "left", "right"],
        )
        self.assertEqual(normalize_answer_format_json('["center","left"]', "标题\n正文"), '["center"]')

    def test_invalid_alignment_payload_becomes_default_layout(self):
        self.assertEqual(normalize_answer_format_json('{"center":true}', "正文"), "[]")
        self.assertEqual(normalize_answer_format_json("not-json", "正文"), "[]")
