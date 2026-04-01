"""終端工具函數測試

測試 terminal_utils 中的顯示寬度計算和文本處理函數
"""
import pytest
from src.agent.utils.terminal_utils import (
    calculate_display_width,
    truncate_with_ellipsis,
    pad_to_width,
    ANSI_ESCAPE_RE,
)


class TestCalculateDisplayWidth:
    """測試顯示寬度計算"""

    def test_ascii_text(self):
        """測試 ASCII 文本"""
        assert calculate_display_width("Hello") == 5
        assert calculate_display_width("Hello World") == 11

    def test_empty_string(self):
        """測試空字符串"""
        assert calculate_display_width("") == 0

    def test_chinese_characters(self):
        """測試中文字符（寬字符）"""
        assert calculate_display_width("你好") == 4  # 每個中文字符佔 2 列
        assert calculate_display_width("世界") == 4

    def test_mixed_ascii_chinese(self):
        """測試混合 ASCII 和中文"""
        assert calculate_display_width("Hello你好") == 9  # 5 + 4

    def test_japanese_characters(self):
        """測試日文字符"""
        assert calculate_display_width("こんにちは") == 10  # 5個字符，每個 2 列

    def test_korean_characters(self):
        """測試韓文字符"""
        assert calculate_display_width("안녕") == 4  # 2個字符，每個 2 列

    def test_emoji(self):
        """測試 Emoji 字符"""
        assert calculate_display_width("🤖") == 2
        assert calculate_display_width("👍👎") == 4

    def test_emoji_mixed_with_text(self):
        """測試 Emoji 與文本混合"""
        assert calculate_display_width("Hello🤖World") == 12  # 5 + 2 + 5

    def test_ansi_escape_codes_ignored(self):
        """測試 ANSI 轉義碼不計入寬度"""
        # 紅色文本
        red_text = "\033[31mRed\033[0m"
        assert calculate_display_width(red_text) == 3

    def test_complex_ansi_codes(self):
        """測試複雜的 ANSI 代碼"""
        # 粗體 + 綠色
        styled_text = "\033[1m\033[32mBold Green\033[0m"
        assert calculate_display_width(styled_text) == 10

    def test_multiple_ansi_codes(self):
        """測試多個 ANSI 代碼"""
        text = "\033[31mRed\033[0m and \033[34mBlue\033[0m"
        assert calculate_display_width(text) == 12  # "Red and Blue"

    def test_fullwidth_numbers(self):
        """測試全形數字"""
        assert calculate_display_width("１２３") == 6  # 全形數字每個 2 列

    def test_fullwidth_latin(self):
        """測試全形拉丁字母"""
        assert calculate_display_width("ＡＢＣ") == 6  # 全形字母每個 2 列

    def test_special_characters(self):
        """測試特殊字符"""
        assert calculate_display_width("!@#$%") == 5

    def test_tabs_and_spaces(self):
        """測試制表符和空格"""
        assert calculate_display_width("a\tb") == 3  # \t 算 1 列
        assert calculate_display_width("a b") == 3

    def test_newlines(self):
        """測試換行符"""
        assert calculate_display_width("a\nb") == 3


class TestTruncateWithEllipsis:
    """測試帶省略號的截斷"""

    def test_no_truncation_needed(self):
        """測試不需要截斷的情況"""
        text = "Hello"
        assert truncate_with_ellipsis(text, 10) == "Hello"

    def test_exact_width(self):
        """測試剛好達到最大寬度"""
        text = "Hello"
        assert truncate_with_ellipsis(text, 5) == "Hello"

    def test_basic_truncation(self):
        """測試基本截斷"""
        text = "Hello World"
        result = truncate_with_ellipsis(text, 8)
        assert len(result) <= 8
        assert result.endswith("…")

    def test_truncation_with_chinese(self):
        """測試中文字符截斷"""
        text = "你好世界"
        result = truncate_with_ellipsis(text, 5)
        # 5 列只能放 2 個中文字符 (4列) + 省略號 (1列)
        assert calculate_display_width(result) <= 5

    def test_zero_max_width(self):
        """測試最大寬度為零"""
        assert truncate_with_ellipsis("Hello", 0) == ""

    def test_negative_max_width(self):
        """測試負數最大寬度"""
        assert truncate_with_ellipsis("Hello", -1) == ""

    def test_max_width_less_than_ellipsis(self):
        """測試最大寬度小於省略號"""
        result = truncate_with_ellipsis("Hello World", 1)
        assert len(result) == 1

    def test_custom_ellipsis(self):
        """測試自定義省略號"""
        result = truncate_with_ellipsis("Hello World", 8, ellipsis="...")
        assert result.endswith("...")

    def test_ansi_codes_removed(self):
        """測試 ANSI 代碼被移除"""
        text = "\033[31mRed Text Here\033[0m"
        result = truncate_with_ellipsis(text, 8)
        # ANSI 代碼在截斷時被移除
        assert "\033" not in result

    def test_emoji_truncation(self):
        """測試 Emoji 截斷"""
        text = "🤖Hello🤖"
        result = truncate_with_ellipsis(text, 5)
        assert calculate_display_width(result) <= 5


class TestPadToWidth:
    """測試填充到指定寬度"""

    def test_left_align(self):
        """測試左對齊"""
        result = pad_to_width("Hello", 10, align="left")
        assert result == "Hello     "
        assert len(result) == 10

    def test_right_align(self):
        """測試右對齊"""
        result = pad_to_width("Hello", 10, align="right")
        assert result == "     Hello"
        assert len(result) == 10

    def test_center_align(self):
        """測試居中對齊"""
        result = pad_to_width("Test", 10, align="center")
        assert result == "   Test   "
        assert len(result) == 10

    def test_center_align_odd_padding(self):
        """測試居中對齊（奇數填充）"""
        result = pad_to_width("Hi", 7, align="center")
        # 7 - 2 = 5, 左邊 2, 右邊 3
        assert result == "  Hi   "

    def test_no_padding_needed(self):
        """測試不需要填充"""
        result = pad_to_width("Hello", 5)
        assert result == "Hello"

    def test_text_exceeds_target(self):
        """測試文本超過目標寬度"""
        result = pad_to_width("Hello World", 5)
        assert result == "Hello World"  # 不截斷

    def test_custom_fill_char(self):
        """測試自定義填充字符"""
        result = pad_to_width("Hi", 6, fill_char="-")
        assert result == "Hi----"

    def test_chinese_text_padding(self):
        """測試中文文本填充"""
        result = pad_to_width("你好", 10)
        # 你好佔 4 列，需要 6 個空格
        assert calculate_display_width(result) == 10
        assert result == "你好      "

    def test_invalid_align_raises_error(self):
        """測試無效的對齊方式拋出異常"""
        with pytest.raises(ValueError) as exc_info:
            pad_to_width("Hello", 10, align="invalid")
        assert "invalid" in str(exc_info.value).lower()

    def test_default_is_left_align(self):
        """測試默認左對齊"""
        result = pad_to_width("X", 5)
        assert result == "X    "

    def test_mixed_width_characters(self):
        """測試混合寬度字符"""
        result = pad_to_width("a你", 8)  # a=1, 你=2, 共 3 列
        assert calculate_display_width(result) == 8


class TestAnsiEscapeRegex:
    """測試 ANSI 轉義碼正則表達式"""

    def test_basic_color_codes(self):
        """測試基本顏色代碼"""
        text = "\033[31mRed\033[0m"
        clean = ANSI_ESCAPE_RE.sub("", text)
        assert clean == "Red"

    def test_multiple_codes(self):
        """測試多個代碼"""
        text = "\033[1m\033[32mBold Green\033[0m"
        clean = ANSI_ESCAPE_RE.sub("", text)
        assert clean == "Bold Green"

    def test_256_color_codes(self):
        """測試 256 色代碼"""
        text = "\033[38;5;196mRed\033[0m"
        clean = ANSI_ESCAPE_RE.sub("", text)
        assert clean == "Red"

    def test_no_ansi_codes(self):
        """測試無 ANSI 代碼"""
        text = "Plain text"
        clean = ANSI_ESCAPE_RE.sub("", text)
        assert clean == "Plain text"


class TestEdgeCases:
    """測試邊界情況"""

    def test_only_ansi_codes(self):
        """測試只有 ANSI 代碼"""
        text = "\033[31m\033[0m"
        assert calculate_display_width(text) == 0

    def test_only_emoji(self):
        """測試只有 Emoji"""
        text = "🎉🎊🎁"
        assert calculate_display_width(text) == 6

    def test_very_long_text(self):
        """測試非常長的文本"""
        text = "a" * 10000
        assert calculate_display_width(text) == 10000

    def test_unicode_combining_characters(self):
        """測試 Unicode 組合字符"""
        # 組合字符不應該增加寬度
        # e + 組合重音符 = é
        text = "e\u0301"  # e + combining acute accent
        width = calculate_display_width(text)
        # 組合字符寬度為 0
        assert width == 1

    def test_zero_width_joiner(self):
        """測試零寬連接符"""
        # 常見於 Emoji 序列中
        text = "👨\u200D👩\u200D👧"  # 家庭 emoji
        # 這取決於具體實現，但應該處理而不崩潰
        calculate_display_width(text)  # 不應拋出異常
