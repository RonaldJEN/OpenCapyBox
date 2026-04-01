"""JSON Parser 模块测试

测试 robust_json_parse 函数的各种边界情况和修复能力
"""
import pytest
from src.agent.llm.json_parser import (
    robust_json_parse,
    _extract_key_value_pairs,
    JsonTokenizer,
    _normalize_quotes,
)


class TestRobustJsonParse:
    """測試健壯的 JSON 解析器"""

    def test_valid_json(self):
        """測試正常 JSON 解析"""
        result = robust_json_parse('{"name": "test", "value": 123}')
        assert result == {"name": "test", "value": 123}

    def test_empty_string(self):
        """測試空字符串"""
        result = robust_json_parse("")
        assert result == {}

    def test_whitespace_only(self):
        """測試只有空白字符"""
        result = robust_json_parse("   \n\t   ")
        assert result == {}

    def test_none_input(self):
        """測試 None 輸入"""
        result = robust_json_parse(None)
        assert result == {}

    def test_unclosed_quote(self):
        """測試未閉合的引號"""
        result = robust_json_parse('{"name": "test')
        assert result is not None
        assert result.get("name") == "test"

    def test_unclosed_brace(self):
        """測試未閉合的花括號"""
        result = robust_json_parse('{"name": "test"')
        assert result is not None
        assert result.get("name") == "test"

    def test_unclosed_bracket(self):
        """測試未閉合的方括號"""
        result = robust_json_parse('{"items": [1, 2, 3')
        assert result is not None
        assert "items" in result

    def test_multiple_unclosed_brackets(self):
        """測試多個未閉合的括號"""
        result = robust_json_parse('{"items": [[1, 2], [3, 4')
        assert result is not None

    def test_nested_unclosed(self):
        """測試嵌套的未閉合結構"""
        result = robust_json_parse('{"outer": {"inner": "value"')
        assert result is not None

    def test_boolean_values(self):
        """測試布爾值"""
        result = robust_json_parse('{"enabled": true, "disabled": false}')
        assert result == {"enabled": True, "disabled": False}

    def test_null_value(self):
        """測試 null 值"""
        result = robust_json_parse('{"value": null}')
        assert result == {"value": None}

    def test_number_values(self):
        """測試數字值"""
        result = robust_json_parse('{"int": 42, "float": 3.14}')
        assert result == {"int": 42, "float": 3.14}

    def test_negative_numbers(self):
        """測試負數"""
        result = robust_json_parse('{"value": -123}')
        assert result == {"value": -123}

    def test_array_of_strings(self):
        """測試字符串數組"""
        result = robust_json_parse('{"tags": ["a", "b", "c"]}')
        assert result == {"tags": ["a", "b", "c"]}

    def test_with_tool_name_logging(self):
        """測試工具名稱日誌記錄"""
        result = robust_json_parse('{"key": "value"}', tool_name="test_tool")
        assert result == {"key": "value"}

    def test_deeply_nested_structure(self):
        """測試深度嵌套結構"""
        result = robust_json_parse('{"a": {"b": {"c": {"d": "deep"}}}}')
        assert result == {"a": {"b": {"c": {"d": "deep"}}}}

    def test_escaped_quotes(self):
        """測試轉義引號"""
        result = robust_json_parse('{"text": "He said \\"hello\\""}')
        assert result == {"text": 'He said "hello"'}

    def test_unicode_characters(self):
        """測試 Unicode 字符"""
        result = robust_json_parse('{"message": "你好世界"}')
        assert result == {"message": "你好世界"}


class TestExtractKeyValuePairs:
    """測試鍵值對提取函數"""

    def test_extract_string_value(self):
        """測試提取字符串值"""
        result = _extract_key_value_pairs('"name": "John"', "test")
        assert result is not None
        assert result.get("name") == "John"

    def test_extract_integer_value(self):
        """測試提取整數值"""
        result = _extract_key_value_pairs('"age": 25', "test")
        assert result is not None
        assert result.get("age") == 25

    def test_extract_float_value(self):
        """測試提取浮點數值"""
        result = _extract_key_value_pairs('"price": 19.99', "test")
        assert result is not None
        assert result.get("price") == 19.99

    def test_extract_boolean_true(self):
        """測試提取 true 值"""
        result = _extract_key_value_pairs('"active": true', "test")
        assert result is not None
        assert result.get("active") is True

    def test_extract_boolean_false(self):
        """測試提取 false 值"""
        result = _extract_key_value_pairs('"active": false', "test")
        assert result is not None
        assert result.get("active") is False

    def test_extract_null(self):
        """測試提取 null 值"""
        result = _extract_key_value_pairs('"value": null', "test")
        assert result is not None
        assert result.get("value") is None

    def test_extract_multiple_pairs(self):
        """測試提取多個鍵值對"""
        result = _extract_key_value_pairs(
            '"name": "Alice", "age": 30, "active": true', "test"
        )
        assert result is not None
        assert result.get("name") == "Alice"
        assert result.get("age") == 30
        assert result.get("active") is True

    def test_extract_from_malformed_json(self):
        """測試從損壞的 JSON 中提取"""
        malformed = '{"name": "Bob", broken here, "value": 123}'
        result = _extract_key_value_pairs(malformed, "test")
        assert result is not None
        assert result.get("name") == "Bob"
        assert result.get("value") == 123

    def test_empty_string_returns_none(self):
        """測試空字符串返回 None"""
        result = _extract_key_value_pairs("", "test")
        assert result is None

    def test_no_valid_pairs_returns_none(self):
        """測試無有效鍵值對時返回 None"""
        result = _extract_key_value_pairs("random text without json", "test")
        assert result is None

    def test_negative_number_extraction(self):
        """測試負數提取"""
        result = _extract_key_value_pairs('"temp": -15', "test")
        assert result is not None
        assert result.get("temp") == -15


class TestEdgeCases:
    """測試邊界情況"""

    def test_only_opening_brace(self):
        """測試只有左花括號"""
        result = robust_json_parse("{")
        # 應該能修復並返回空對象
        assert result is not None or result == {}

    def test_truncated_string_value(self):
        """測試截斷的字符串值"""
        result = robust_json_parse('{"content": "This is a long text that gets cut')
        assert result is not None

    def test_mixed_types_array(self):
        """測試混合類型數組"""
        result = robust_json_parse('{"data": [1, "two", true, null]}')
        assert result == {"data": [1, "two", True, None]}

    def test_empty_object(self):
        """測試空對象"""
        result = robust_json_parse("{}")
        assert result == {}

    def test_empty_array(self):
        """測試空數組"""
        result = robust_json_parse('{"items": []}')
        assert result == {"items": []}

    def test_whitespace_in_values(self):
        """測試值中的空白"""
        result = robust_json_parse('{"text": "  hello  world  "}')
        assert result == {"text": "  hello  world  "}

    def test_special_characters_in_string(self):
        """測試字符串中的特殊字符"""
        result = robust_json_parse('{"path": "/usr/local/bin"}')
        assert result == {"path": "/usr/local/bin"}

    def test_newline_in_string(self):
        """測試字符串中的換行符"""
        result = robust_json_parse('{"text": "line1\\nline2"}')
        assert result == {"text": "line1\nline2"}


class TestWindowsPathEscaping:
    """測試 Windows 路徑中的轉義字符處理（修復 python \\ 問題）"""

    def test_windows_path_with_backslashes(self):
        """測試包含反斜杠的 Windows 路徑"""
        json_str = '{"command": "python C:\\\\Users\\\\test\\\\script.py", "timeout": 120}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert result.get("command") == "python C:\\Users\\test\\script.py"
        assert result.get("timeout") == 120

    def test_windows_path_truncated(self):
        """測試截斷的 Windows 路徑（模擬原始 bug 場景）"""
        # 模擬模型輸出被截斷的情況
        json_str = '{"command": "python C:\\\\Users\\\\test\\\\'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        # 即使截斷，也應該能提取到部分路徑
        assert "command" in result
        assert "python" in result.get("command", "")

    def test_mixed_escaped_characters(self):
        """測試混合轉義字符"""
        json_str = '{"path": "C:\\\\path\\\\to\\\\file.txt", "content": "line1\\nline2\\ttab"}'
        result = robust_json_parse(json_str, "test")
        assert result is not None
        assert result.get("path") == "C:\\path\\to\\file.txt"
        assert result.get("content") == "line1\nline2\ttab"

    def test_double_backslash_at_end(self):
        """測試結尾的雙反斜杠"""
        json_str = '{"command": "python \\\\"}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        # 應該正確解析為單個反斜杠
        assert result.get("command") == "python \\"

    def test_complex_windows_command(self):
        """測試複雜的 Windows 命令"""
        json_str = '{"command": "python C:\\\\Users\\\\renyx\\\\scripts\\\\read_pdf.py \\"Engram_paper.pdf\\"", "timeout": 120}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert "python" in result.get("command", "")
        assert result.get("timeout") == 120

    def test_bash_command_with_path(self):
        """測試實際的 bash 工具調用場景"""
        # 模擬 GLM 模型生成的實際工具調用
        json_str = '''{"command": "python C:\\\\Users\\\\renyx\\\\PycharmProjects\\\\git\\\\CodeCraft-OWL\\\\backend\\\\AgnetSkills\\\\src\\\\agent\\\\skills\\\\document-skills\\\\pdf\\\\scripts\\\\read_pdf.py Engram_paper.pdf", "timeout": 120}'''
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert "python" in result.get("command", "")
        assert "read_pdf.py" in result.get("command", "")
        assert result.get("timeout") == 120

    def test_incomplete_json_with_windows_path(self):
        """測試包含 Windows 路徑的不完整 JSON"""
        # 模擬流式傳輸中途截斷的情況
        json_str = '{"command": "python C:\\\\Users\\\\test\\\\script.py", "time'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert result.get("command") == "python C:\\Users\\test\\script.py"

    def test_extract_key_value_with_escaped_backslash(self):
        """直接測試 _extract_key_value_pairs 處理轉義反斜杠"""
        from src.agent.llm.json_parser import _extract_key_value_pairs

        json_str = '"command": "python C:\\\\path\\\\script.py"'
        result = _extract_key_value_pairs(json_str, "bash")
        assert result is not None
        assert result.get("command") == "python C:\\path\\script.py"

    def test_unicode_escape_sequence(self):
        """測試 Unicode 轉義序列（雖然不常見但應該不破壞解析）"""
        json_str = '{"message": "Hello \\u4e16\\u754c"}'
        result = robust_json_parse(json_str, "test")
        # 標準 JSON 解析器會處理 unicode 轉義
        assert result is not None

    def test_forward_slash_escape(self):
        """測試正斜杠轉義（JSON 標準允許但不強制）"""
        json_str = '{"url": "http:\\/\\/example.com\\/path"}'
        result = robust_json_parse(json_str, "test")
        assert result is not None
        assert "example.com" in result.get("url", "")


class TestChineseQuoteNormalization:
    """測試中文引號規範化（修復 MiniMax M2 中文引號問題）

    Unicode 轉義參考：
    - \u201c = " (LEFT DOUBLE QUOTATION MARK)
    - \u201d = " (RIGHT DOUBLE QUOTATION MARK)
    - \u2018 = ' (LEFT SINGLE QUOTATION MARK)
    - \u2019 = ' (RIGHT SINGLE QUOTATION MARK)
    - \u300c = 「 (LEFT CORNER BRACKET)
    - \u300d = 」 (RIGHT CORNER BRACKET)
    - \u300e = 『 (LEFT WHITE CORNER BRACKET)
    - \u300f = 』 (RIGHT WHITE CORNER BRACKET)
    """

    def test_left_double_quotation_mark(self):
        """測試左雙引號 U+201C"""
        # 使用中文左雙引號 \u201c 替換正常的 "
        json_str = '{\u201ccommand\u201c: \u201cpython script.py\u201c}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert result.get("command") == "python script.py"

    def test_right_double_quotation_mark(self):
        """測試右雙引號 U+201D"""
        # 使用中文右雙引號 \u201d 替換正常的 "
        json_str = '{\u201dcommand\u201d: \u201dpython script.py\u201d}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert result.get("command") == "python script.py"

    def test_mixed_chinese_quotes(self):
        """測試混合中文引號（左引號開始，右引號結束）"""
        # 模擬 MiniMax M2 的實際輸出：使用中文引號對 \u201c...\u201d
        json_str = '{\u201ccommand\u201d: \u201cpython -c \\"import sys; print(sys.version)\\"\u201d}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert "python" in result.get("command", "")

    def test_chinese_corner_brackets(self):
        """測試中文角引號「」(\u300c \u300d)"""
        json_str = '{"text": \u300chello world\u300d}'
        result = robust_json_parse(json_str, "test")
        assert result is not None
        assert result.get("text") == "hello world"

    def test_chinese_white_corner_brackets(self):
        """測試中文白角引號『』(\u300e \u300f)"""
        json_str = '{"text": \u300ehello world\u300f}'
        result = robust_json_parse(json_str, "test")
        assert result is not None
        assert result.get("text") == "hello world"

    def test_single_chinese_quotes(self):
        """測試中文單引號'' (\u2018 \u2019)"""
        # 使用中文單引號 - 注意這會轉換為英文單引號，但 JSON 標準不支持
        json_str = "{\u2018key\u2019: \u2018value\u2019}"
        # 單引號不是標準 JSON，轉換後仍會失敗，但不應崩潰
        result = robust_json_parse(json_str, "test")
        # 結果可能為 None 或提取到部分值

    def test_realistic_minimax_output(self):
        """測試模擬 MiniMax M2 實際輸出的情況"""
        # 這是從日誌中看到的實際問題：中文引號導致命令執行失敗
        # 原始錯誤：File "<string>", line 1 "import ^ SyntaxError: unterminated string literal
        json_str = '{"command": "python -c \\"\u201cfrom pypdf import PdfReader; print(OK)\u201d\\""}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        # 確保解析成功，即使有中文引號
        assert "command" in result

    def test_complex_bash_command_with_chinese_quotes(self):
        """測試包含複雜 bash 命令的中文引號情況"""
        # 模擬原始 bug 場景
        json_str = '{"command": "python -c \\"\u201cimport pypdf; import pdfplumber; print(OK)\u201d\\""}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert "command" in result

    def test_multiple_chinese_quote_pairs(self):
        """測試多個中文引號對"""
        # 使用中文引號對 \u201c...\u201d
        json_str = '{\u201ckey1\u201d: \u201cvalue1\u201d, \u201ckey2\u201d: \u201cvalue2\u201d}'
        result = robust_json_parse(json_str, "test")
        assert result is not None
        assert result.get("key1") == "value1"
        assert result.get("key2") == "value2"

    def test_nested_values_with_chinese_quotes(self):
        """測試嵌套結構中的中文引號"""
        json_str = '{\u201couter\u201d: {\u201cinner\u201d: \u201cvalue\u201d}}'
        result = robust_json_parse(json_str, "test")
        assert result is not None
        assert result.get("outer", {}).get("inner") == "value"

    def test_truncated_json_with_chinese_quotes(self):
        """測試截斷的 JSON 中的中文引號"""
        # 模擬流式傳輸中途截斷
        json_str = '{\u201ccommand\u201d: \u201cpython script.py'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert "command" in result

    def test_normalize_quotes_function(self):
        """直接測試 _normalize_quotes 函數"""
        from src.agent.llm.json_parser import _normalize_quotes

        # 測試所有類型的中文引號（使用 Unicode 轉義避免語法問題）
        # \u201c = " (LEFT DOUBLE QUOTATION MARK)
        # \u201d = " (RIGHT DOUBLE QUOTATION MARK)
        # \u2018 = ' (LEFT SINGLE QUOTATION MARK)
        # \u2019 = ' (RIGHT SINGLE QUOTATION MARK)
        # \u300c = 「 (LEFT CORNER BRACKET)
        # \u300d = 」 (RIGHT CORNER BRACKET)
        # \u300e = 『 (LEFT WHITE CORNER BRACKET)
        # \u300f = 』 (RIGHT WHITE CORNER BRACKET)
        test_cases = [
            ("\u201chello\u201d", '"hello"'),  # 左右雙引號
            ("\u2018hello\u2019", "'hello'"),  # 左右單引號
            ("\u300chello\u300d", '"hello"'),  # 角引號
            ("\u300ehello\u300f", '"hello"'),  # 白角引號
            ("\u201c混合\u201dquotes", '"混合"quotes'),  # 混合
        ]

        for input_str, expected in test_cases:
            assert _normalize_quotes(input_str) == expected, f"Failed for input: {repr(input_str)}"


class TestJsonTokenizer:
    """测试 JsonTokenizer 状态机"""

    def test_analyze_simple_object(self):
        """测试简单对象的结构分析"""
        tokenizer = JsonTokenizer('{"key": "value"}')
        stats = tokenizer.analyze_structure()
        assert stats['open_braces'] == 1
        assert stats['close_braces'] == 1
        assert stats['unclosed_string'] is False

    def test_analyze_nested_object(self):
        """测试嵌套对象的结构分析"""
        tokenizer = JsonTokenizer('{"outer": {"inner": "value"}}')
        stats = tokenizer.analyze_structure()
        assert stats['open_braces'] == 2
        assert stats['close_braces'] == 2

    def test_analyze_with_array(self):
        """测试包含数组的结构分析"""
        tokenizer = JsonTokenizer('{"items": [1, 2, 3]}')
        stats = tokenizer.analyze_structure()
        assert stats['open_brackets'] == 1
        assert stats['close_brackets'] == 1

    def test_analyze_brackets_inside_string(self):
        """测试字符串内的括号不被计数（关键修复）"""
        # 这是旧版本的 bug：字符串内的 { 和 [ 被错误计数
        tokenizer = JsonTokenizer('{"text": "hello {world} [test]"}')
        stats = tokenizer.analyze_structure()
        # 只有外层的一个花括号对
        assert stats['open_braces'] == 1
        assert stats['close_braces'] == 1
        # 字符串内的方括号不应计数
        assert stats['open_brackets'] == 0
        assert stats['close_brackets'] == 0

    def test_analyze_unclosed_string(self):
        """测试未闭合的字符串检测"""
        tokenizer = JsonTokenizer('{"key": "unclosed')
        stats = tokenizer.analyze_structure()
        assert stats['unclosed_string'] is True
        assert stats['last_string_complete'] is False

    def test_analyze_escaped_quote_in_string(self):
        """测试字符串中的转义引号"""
        tokenizer = JsonTokenizer('{"text": "He said \\"hello\\""}')
        stats = tokenizer.analyze_structure()
        assert stats['unclosed_string'] is False
        assert stats['open_braces'] == 1
        assert stats['close_braces'] == 1

    def test_analyze_truncated_json(self):
        """测试截断的 JSON"""
        tokenizer = JsonTokenizer('{"key": "value", "arr": [1, 2')
        stats = tokenizer.analyze_structure()
        assert stats['open_braces'] == 1
        assert stats['close_braces'] == 0
        assert stats['open_brackets'] == 1
        assert stats['close_brackets'] == 0

    def test_read_string_with_unicode_escape(self):
        """测试读取包含 Unicode 转义的字符串"""
        tokenizer = JsonTokenizer('"hello \\u4e16\\u754c"')
        value, complete = tokenizer._read_string()
        assert complete is True
        assert value == "hello 世界"

    def test_read_string_with_all_escapes(self):
        """测试所有转义序列"""
        tokenizer = JsonTokenizer('"\\n\\r\\t\\\\\\/\\b\\f"')
        value, complete = tokenizer._read_string()
        assert complete is True
        assert value == "\n\r\t\\/\b\f"


class TestPreserveCorrectStructure:
    """测试不破坏正确结构的保守修复策略"""

    def test_valid_json_unchanged(self):
        """测试有效 JSON 不被修改"""
        valid_json = '{"command": "echo hello", "timeout": 30}'
        result = robust_json_parse(valid_json)
        assert result == {"command": "echo hello", "timeout": 30}

    def test_complex_nested_unchanged(self):
        """测试复杂嵌套结构不被修改"""
        complex_json = '''
        {
            "name": "test",
            "config": {
                "enabled": true,
                "items": [1, 2, {"nested": "value"}]
            },
            "tags": ["a", "b", "c"]
        }
        '''
        result = robust_json_parse(complex_json)
        assert result is not None
        assert result["config"]["items"][2]["nested"] == "value"

    def test_dont_add_extra_braces(self):
        """测试不会添加多余的花括号"""
        # 完整的 JSON，不应该被修改
        json_str = '{"a": 1}'
        result = robust_json_parse(json_str)
        assert result == {"a": 1}

    def test_extra_close_brace_not_fixed(self):
        """测试多余的闭合括号不会被错误处理"""
        # 这种情况应该尝试提取键值对
        json_str = '{"a": 1}}'
        result = robust_json_parse(json_str)
        # 标准解析会失败，但不应崩溃
        assert result is None or "a" in result

    def test_brackets_in_string_preserved(self):
        """测试字符串中的括号被正确保留"""
        json_str = '{"pattern": "[a-z]{2,4}", "regex": "\\\\{\\\\}"}'
        result = robust_json_parse(json_str)
        assert result is not None
        assert result["pattern"] == "[a-z]{2,4}"

    def test_real_world_tool_call(self):
        """测试真实世界的工具调用场景"""
        # 模拟 Agent 工具调用的真实数据
        json_str = '{"file_path": "/home/user/test.py", "content": "def hello():\\n    print(\\"Hello, World!\\")\\n"}'
        result = robust_json_parse(json_str, "write_file")
        assert result is not None
        assert "def hello():" in result["content"]
        assert 'print("Hello, World!")' in result["content"]


class TestUnescapedQuotes:
    """测试模型输出未转义引号的情况（MiniMax M2 等模型可能出现）"""

    def test_single_unescaped_quote_in_path(self):
        """测试路径中有未转义引号的情况"""
        # 模型可能输出: {"command": "python "C:/path/script.py" arg"}
        # 而不是正确的: {"command": "python \"C:/path/script.py\" arg"}
        json_str = '{"command": "python "C:/Users/test.py" arg1"}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert "python" in result.get("command", "")
        assert "C:/Users/test.py" in result.get("command", "")
        assert "arg1" in result.get("command", "")

    def test_unescaped_quote_followed_by_brace(self):
        """测试未转义引号后跟花括号的情况（更常见）"""
        # 这种情况下，引号后跟 } 是明确的结束标志
        json_str = '{"command": "python "script.py" arg"}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert "python" in result.get("command", "")
        assert "script.py" in result.get("command", "")

    def test_unescaped_with_timeout_separated(self):
        """测试带有其他键的未转义引号（键之间有明确分隔）"""
        # 注意：这种模糊情况下，解析器会尽力提取
        json_str = '{"command": "python script.py", "timeout": 120}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert "python" in result.get("command", "")
        # 标准 JSON 应该正确解析 timeout
        assert result.get("timeout") == 120

    def test_real_pdf_reader_scenario(self):
        """测试真实的 PDF 读取场景"""
        json_str = '{"command": "python "C:/Users/renyx/PycharmProjects/git/CodeCraft-OWL/backend/AgnetSkills/src/agent/skills/document-skills/pdf/scripts/read_pdf.py" Engram_paper.pdf"}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        cmd = result.get("command", "")
        assert "python" in cmd
        assert "read_pdf.py" in cmd
        assert "Engram_paper.pdf" in cmd

    def test_properly_escaped_still_works(self):
        """确保正确转义的引号仍然正常工作"""
        json_str = '{"command": "python \\"C:/Users/test.py\\" arg1"}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert result.get("command") == 'python "C:/Users/test.py" arg1'


class TestGLMTrailingBraces:
    """测试 GLM-4.7 等模型在 JSON 后追加 {} 的问题

    GLM-4.7 走 OpenAI 协议时，工具参数经常出现：
    - {"command": "pwd"}{}
    - {"skill_name": "pdf"}{}
    这种尾部追加空对象的情况需要特殊处理
    """

    def test_trailing_empty_braces(self):
        """测试尾部追加空 {} 的情况"""
        json_str = '{"command": "pwd"}{}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert result.get("command") == "pwd"

    def test_trailing_braces_with_skill_name(self):
        """测试 get_skill 工具调用尾部追加 {} 的情况"""
        json_str = '{"skill_name": "pdf"}{}'
        result = robust_json_parse(json_str, "get_skill")
        assert result is not None
        assert result.get("skill_name") == "pdf"

    def test_trailing_braces_with_complex_command(self):
        """测试复杂命令尾部追加 {} 的情况"""
        json_str = '{"command": "python -c \\"print(1)\\""}{}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert "python" in result.get("command", "")

    def test_trailing_braces_with_multiple_keys(self):
        """测试多键值对尾部追加 {} 的情况"""
        json_str = '{"command": "ls -la", "timeout": 120}{}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert result.get("command") == "ls -la"
        assert result.get("timeout") == 120

    def test_trailing_braces_with_whitespace(self):
        """测试带空白的尾部 {} 情况"""
        json_str = '{"command": "pwd"}  {}  '
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert result.get("command") == "pwd"

    def test_trailing_braces_with_windows_path(self):
        """测试包含 Windows 路径的尾部 {} 情况"""
        json_str = '{"command": "python C:\\\\Users\\\\test\\\\script.py"}{}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert "python" in result.get("command", "")
        assert "C:\\Users\\test\\script.py" in result.get("command", "")

    def test_no_trailing_braces_unchanged(self):
        """测试正常 JSON 不受影响"""
        json_str = '{"command": "pwd"}'
        result = robust_json_parse(json_str, "bash")
        assert result is not None
        assert result.get("command") == "pwd"

    def test_nested_object_not_affected(self):
        """测试嵌套对象不被误处理"""
        # 这种情况不应该被 trailing {} 逻辑影响
        json_str = '{"config": {"nested": "value"}}'
        result = robust_json_parse(json_str, "test")
        assert result is not None
        assert result.get("config", {}).get("nested") == "value"

    def test_empty_braces_only(self):
        """测试只有空 {} 的情况"""
        json_str = '{}'
        result = robust_json_parse(json_str, "test")
        assert result == {}

    def test_double_empty_braces(self):
        """测试双重空 {} 的情况"""
        json_str = '{}{}'
        result = robust_json_parse(json_str, "test")
        # 应该返回空对象而非失败
        assert result == {}


class TestScientificNotation:
    """测试科学计数法数字"""

    def test_positive_exponent(self):
        """测试正指数"""
        result = robust_json_parse('{"value": 1.5e10}')
        assert result == {"value": 1.5e10}

    def test_negative_exponent(self):
        """测试负指数"""
        result = robust_json_parse('{"value": 1.5e-10}')
        assert result == {"value": 1.5e-10}

    def test_uppercase_exponent(self):
        """测试大写 E"""
        result = robust_json_parse('{"value": 1.5E10}')
        assert result == {"value": 1.5e10}

    def test_extract_scientific_number(self):
        """测试从损坏 JSON 中提取科学计数法"""
        result = _extract_key_value_pairs('"rate": 3.14e-5, broken', "test")
        assert result is not None
        assert abs(result.get("rate", 0) - 3.14e-5) < 1e-10
