"""健壮的 JSON 解析器，用于修复流式传输中的不完整/不规范 JSON

设计原则：
1. 保守修复 - 只修复明确可识别的问题，不破坏正确的结构
2. 状态机解析 - 正确区分字符串内外的特殊字符
3. 渐进式尝试 - 先尝试标准解析，再逐步尝试修复
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 中文引号到英文引号的映射
CHINESE_QUOTE_MAP = {
    "\u201c": '"',  # " LEFT DOUBLE QUOTATION MARK
    "\u201d": '"',  # " RIGHT DOUBLE QUOTATION MARK
    "\u2018": "'",  # ' LEFT SINGLE QUOTATION MARK
    "\u2019": "'",  # ' RIGHT SINGLE QUOTATION MARK
    "\u300c": '"',  # 「 LEFT CORNER BRACKET
    "\u300d": '"',  # 」 RIGHT CORNER BRACKET
    "\u300e": '"',  # 『 LEFT WHITE CORNER BRACKET
    "\u300f": '"',  # 』 RIGHT WHITE CORNER BRACKET
}


def _normalize_quotes(json_str: str) -> str:
    """将中文引号转换为标准 JSON 引号"""
    result = json_str
    for chinese_quote, english_quote in CHINESE_QUOTE_MAP.items():
        result = result.replace(chinese_quote, english_quote)
    return result


class JsonTokenizer:
    """JSON 字符串分词器，正确处理字符串内的特殊字符"""

    def __init__(self, json_str: str):
        self.json_str = json_str
        self.pos = 0
        self.length = len(json_str)

    def _skip_whitespace(self):
        """跳过空白字符"""
        while self.pos < self.length and self.json_str[self.pos] in ' \t\n\r':
            self.pos += 1

    def _read_string(self) -> tuple[str, bool]:
        """读取字符串值，返回 (内容, 是否完整闭合)"""
        if self.pos >= self.length or self.json_str[self.pos] != '"':
            return "", False

        result = []
        self.pos += 1  # 跳过开头引号

        while self.pos < self.length:
            char = self.json_str[self.pos]

            if char == '\\' and self.pos + 1 < self.length:
                next_char = self.json_str[self.pos + 1]
                # 处理转义序列
                escape_map = {
                    '"': '"', '\\': '\\', '/': '/', 'b': '\b',
                    'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'
                }
                if next_char in escape_map:
                    result.append(escape_map[next_char])
                    self.pos += 2
                elif next_char == 'u' and self.pos + 5 < self.length:
                    # Unicode 转义 \uXXXX
                    hex_digits = self.json_str[self.pos + 2:self.pos + 6]
                    if all(c in '0123456789abcdefABCDEF' for c in hex_digits):
                        result.append(chr(int(hex_digits, 16)))
                        self.pos += 6
                    else:
                        # 无效的 Unicode 转义，保留原样
                        result.append('\\')
                        result.append(next_char)
                        self.pos += 2
                else:
                    # 未知转义，保留原样
                    result.append('\\')
                    result.append(next_char)
                    self.pos += 2
            elif char == '"':
                self.pos += 1  # 跳过结束引号
                return ''.join(result), True
            else:
                result.append(char)
                self.pos += 1

        # 未找到结束引号
        return ''.join(result), False

    def analyze_structure(self) -> dict:
        """分析 JSON 结构，统计不在字符串内的括号"""
        stats = {
            'open_braces': 0,    # {
            'close_braces': 0,   # }
            'open_brackets': 0,  # [
            'close_brackets': 0, # ]
            'unclosed_string': False,
            'last_string_complete': True,
        }

        self.pos = 0
        while self.pos < self.length:
            self._skip_whitespace()
            if self.pos >= self.length:
                break

            char = self.json_str[self.pos]

            if char == '"':
                _, complete = self._read_string()
                stats['last_string_complete'] = complete
                if not complete:
                    stats['unclosed_string'] = True
            elif char == '{':
                stats['open_braces'] += 1
                self.pos += 1
            elif char == '}':
                stats['close_braces'] += 1
                self.pos += 1
            elif char == '[':
                stats['open_brackets'] += 1
                self.pos += 1
            elif char == ']':
                stats['close_brackets'] += 1
                self.pos += 1
            else:
                self.pos += 1

        return stats


def _try_fix_trailing(json_str: str, stats: dict) -> str | None:
    """尝试修复尾部缺失的闭合符号

    只在确定是尾部截断的情况下修复，避免破坏正确的结构
    """
    fixed = json_str.rstrip()

    # 如果有未闭合的字符串，先尝试闭合
    if stats['unclosed_string'] and not stats['last_string_complete']:
        # 检查是否是字符串被截断（以引号开始但未结束）
        fixed = fixed + '"'
        # 重新分析
        new_stats = JsonTokenizer(fixed).analyze_structure()
        if new_stats['unclosed_string']:
            # 闭合引号没有帮助，回退
            fixed = json_str.rstrip()
        else:
            stats = new_stats

    # 计算需要的闭合符号
    need_brackets = stats['open_brackets'] - stats['close_brackets']
    need_braces = stats['open_braces'] - stats['close_braces']

    # 只有当缺少闭合符号时才添加（保守策略）
    if need_brackets < 0 or need_braces < 0:
        # 多余的闭合符号，可能是结构错误，不处理
        return None

    # 分析尾部来决定闭合顺序
    # 简单策略：先闭合括号，再闭合花括号（适用于大多数情况）
    if need_brackets > 0:
        fixed = fixed + ']' * need_brackets
    if need_braces > 0:
        fixed = fixed + '}' * need_braces

    return fixed


def _extract_string_value_greedy(json_str: str, start: int) -> tuple[str, int]:
    """贪婪地提取字符串值，处理模型未正确转义引号的情况

    当模型输出 {"command": "python "path" arg"} 这种未转义引号的 JSON 时，
    尝试找到真正的字符串结束位置（在下一个键名或对象结束符之前）。

    Args:
        json_str: JSON 字符串
        start: 开始位置（引号位置）

    Returns:
        (提取的值, 结束位置)
    """
    if start >= len(json_str) or json_str[start] != '"':
        return "", start

    # 查找可能的结束位置候选
    # 策略：找到最后一个 " 后面紧跟 , 或 } 或 ] 或字符串结尾的位置
    length = len(json_str)
    i = start + 1
    last_valid_end = -1
    result_chars = []

    while i < length:
        char = json_str[i]

        if char == '\\' and i + 1 < length:
            # 正常的转义序列
            next_char = json_str[i + 1]
            escape_map = {
                '"': '"', '\\': '\\', '/': '/', 'b': '\b',
                'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'
            }
            if next_char in escape_map:
                result_chars.append(escape_map[next_char])
                i += 2
                continue
            elif next_char == 'u' and i + 5 < length:
                hex_digits = json_str[i + 2:i + 6]
                if all(c in '0123456789abcdefABCDEF' for c in hex_digits):
                    result_chars.append(chr(int(hex_digits, 16)))
                    i += 6
                    continue
            # 未知转义，保留原样
            result_chars.append(char)
            i += 1
        elif char == '"':
            # 检查这个引号后面是什么
            # 跳过空白
            j = i + 1
            while j < length and json_str[j] in ' \t\n\r':
                j += 1

            if j >= length:
                # 字符串在文件末尾结束
                last_valid_end = i
                break
            elif json_str[j] in ',}]':
                # 这是一个有效的字符串结束位置
                last_valid_end = i
                break
            elif json_str[j] == '"':
                # 下一个键开始了，当前位置是结束
                last_valid_end = i
                break
            else:
                # 这个引号可能是字符串内部的未转义引号，继续
                result_chars.append(char)
                i += 1
        else:
            result_chars.append(char)
            i += 1

    if last_valid_end > 0:
        # 找到了有效的结束位置
        # 重新从 start+1 到 last_valid_end 提取内容（保留内部引号）
        value = json_str[start + 1:last_valid_end]
        # 处理转义字符 - 使用正则一次性处理，避免顺序问题
        def unescape(match):
            escape_char = match.group(1)
            escape_map = {
                'n': '\n', 't': '\t', 'r': '\r',
                '\\': '\\', '"': '"', '/': '/',
                'b': '\b', 'f': '\f'
            }
            return escape_map.get(escape_char, '\\' + escape_char)
        value = re.sub(r'\\(.)', unescape, value)
        return value, last_valid_end + 1
    else:
        # 没找到有效结束，返回已解析的内容
        return ''.join(result_chars), i


def _extract_key_value_pairs(json_str: str, tool_name: str) -> dict[str, Any] | None:
    """从损坏的 JSON 中提取键值对（最后的尝试）

    使用状态机正确处理字符串内的特殊字符，
    并处理模型未正确转义引号的情况。
    """
    result = {}
    tokenizer = JsonTokenizer(json_str)

    # 查找 "key": 模式
    i = 0
    length = len(json_str)

    while i < length:
        # 跳过空白
        while i < length and json_str[i] in ' \t\n\r,{':
            i += 1

        if i >= length:
            break

        # 期望找到 key
        if json_str[i] != '"':
            i += 1
            continue

        # 读取 key
        tokenizer.pos = i
        key, key_complete = tokenizer._read_string()

        if not key:
            i += 1
            continue

        i = tokenizer.pos

        # 跳过空白和冒号
        while i < length and json_str[i] in ' \t\n\r':
            i += 1

        if i >= length or json_str[i] != ':':
            continue

        i += 1  # 跳过冒号

        # 跳过空白
        while i < length and json_str[i] in ' \t\n\r':
            i += 1

        if i >= length:
            break

        # 读取值
        char = json_str[i]

        if char == '"':
            # 字符串值 - 使用贪婪提取处理未转义引号
            value, new_pos = _extract_string_value_greedy(json_str, i)
            result[key] = value
            i = new_pos

        elif char in '-0123456789':
            # 数字值
            num_match = re.match(r'-?\d+\.?\d*(?:[eE][+-]?\d+)?', json_str[i:])
            if num_match:
                value_str = num_match.group()
                try:
                    if '.' in value_str or 'e' in value_str.lower():
                        result[key] = float(value_str)
                    else:
                        result[key] = int(value_str)
                    i += len(value_str)
                except ValueError:
                    i += 1
            else:
                i += 1

        elif json_str[i:i+4] == 'true':
            result[key] = True
            i += 4

        elif json_str[i:i+5] == 'false':
            result[key] = False
            i += 5

        elif json_str[i:i+4] == 'null':
            result[key] = None
            i += 4

        elif char in '[{':
            # 数组或对象 - 尝试找到匹配的闭合符号
            close_char = ']' if char == '[' else '}'
            depth = 1
            j = i + 1
            in_str = False

            while j < length and depth > 0:
                c = json_str[j]
                if in_str:
                    if c == '\\' and j + 1 < length:
                        j += 2
                        continue
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c == char:
                        depth += 1
                    elif c == close_char:
                        depth -= 1
                j += 1

            if depth == 0:
                # 找到完整的数组/对象
                try:
                    result[key] = json.loads(json_str[i:j])
                except json.JSONDecodeError:
                    pass
            i = j
        else:
            i += 1

    return result if result else None


def robust_json_parse(json_str: str, tool_name: str = "unknown") -> dict[str, Any] | None:
    """尝试解析可能不完整或不规范的 JSON 字符串

    Args:
        json_str: 待解析的 JSON 字符串
        tool_name: 工具名称（用于日志）

    Returns:
        解析后的字典，如果无法修复则返回 None
    """
    if not json_str or not json_str.strip():
        return {}

    # 🔍 调试日志：记录原始输入
    logger.debug(f"[JSON_PARSER] [{tool_name}] Raw input: {repr(json_str[:500])}")

    # 1. 规范化引号（将中文引号转换为英文引号）
    original_json_str = json_str
    json_str = _normalize_quotes(json_str)
    if json_str != original_json_str:
        logger.info(f"Normalized Chinese quotes in JSON for tool '{tool_name}'")
        logger.debug(f"[{tool_name}] After quote normalization: {repr(json_str[:500])}")

    # 2. 首先尝试标准解析（最快路径）
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.debug(f"Standard JSON parse failed for tool '{tool_name}': {e}")

    # 2.1 修复 GLM 等模型在 JSON 末尾多输出 {} 的问题
    # 例如: {"command": "pwd"}{} -> {"command": "pwd"}
    if json_str.rstrip().endswith('{}'):
        trimmed = json_str.rstrip()[:-2].rstrip()
        try:
            result = json.loads(trimmed)
            logger.info(f"Fixed trailing '{{}}' in JSON for tool '{tool_name}'")
            return result
        except json.JSONDecodeError:
            pass

    # 3. 分析 JSON 结构
    stats = JsonTokenizer(json_str).analyze_structure()
    logger.debug(f"JSON structure analysis for tool '{tool_name}': {stats}")

    # 4. 尝试修复尾部截断
    fixed_json = _try_fix_trailing(json_str, stats)
    if fixed_json:
        try:
            result = json.loads(fixed_json)
            logger.info(f"Successfully fixed truncated JSON for tool '{tool_name}'")
            return result
        except json.JSONDecodeError as e:
            logger.debug(f"Fixed JSON still failed for tool '{tool_name}': {e}")

    # 5. 最后尝试：提取键值对
    try:
        result = _extract_key_value_pairs(json_str, tool_name)
        if result:
            logger.warning(
                f"Extracted partial data from malformed JSON for tool '{tool_name}': "
                f"keys={list(result.keys())}"
            )
            return result
    except Exception as e:
        logger.debug(f"Key-value extraction failed for tool '{tool_name}': {e}")

    # 6. 所有修复尝试失败
    logger.error(
        f"Failed to parse JSON for tool '{tool_name}' after all attempts. "
        f"Input (truncated): {json_str[:200]}..."
    )
    return None
