import { parse as parseMicromark, preprocess } from 'micromark';
import { factorySpace } from 'micromark-factory-space';
import remarkCjkFriendly from 'remark-cjk-friendly';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';

const BACKSLASH = 92;
const CLOSE_BY_OPEN: Record<number, number> = { 40: 41, 91: 93 }; // `(`→`)`，`[`→`]`

// micromark 的 CR / LF / CRLF 为 -5 / -4 / -3。
const isLineEnding = (code: number | null) => code !== null && code < -2;

// `\]` 之后到行尾只允许空格、Tab。
const lineEndAhead = {
  partial: true,
  tokenize(effects: any, ok: any, nok: any) {
    return factorySpace(effects, (code: number | null) => (code === null || isLineEnding(code) ? ok(code) : nok(code)), 'whitespace');
  },
};

/**
 * `\(…\)`、`\[…\]` 行内构造：产出与 `$$…$$` 相同的 mathText token，由 remark-math 生成 inlineMath。
 * 作为 micromark 语法构造运行，代码块、行内代码、链接与图片目标不会进入这里。
 * 公式内 `\x` 成对读取，因此 `\\[2pt]`、`\\` 不会被当成闭合符；未闭合时回退为普通转义字符。
 * `\[…\]` 必须独占一行：`\[` 在行首（段首或换行后的缩进之后），`\]` 之后到行尾只有空白；
 * 与文字同一行的 `见\[1\]`、`\[1\] 张三` 是 Markdown 转义的方括号。
 */
const latexDelimiterMath = {
  name: 'latexDelimiterMath',
  add: 'before',
  tokenize(this: any, effects: any, ok: any, nok: any) {
    // 文本 tokenizer 每个段落、标题、表格单元格各自从空事件开始；行首之前只有换行与其后的缩进。
    const tail = this.events[this.events.length - 1];
    const atLineStart = !tail || tail[1].type === 'lineEnding' || tail[1].type === 'linePrefix';
    let close: number;
    let token: any;

    const start = (code: number) => {
      effects.enter('mathText');
      effects.enter('mathTextSequence');
      effects.consume(code);
      return open;
    };

    const open = (code: number) => {
      if (code !== 40 && !(code === 91 && atLineStart)) return nok(code);
      close = CLOSE_BY_OPEN[code];
      effects.consume(code);
      effects.exit('mathTextSequence');
      return between;
    };

    const between = (code: number | null): any => {
      if (code === null) return nok(code);
      if (isLineEnding(code)) {
        effects.enter('lineEnding');
        effects.consume(code);
        effects.exit('lineEnding');
        return between;
      }
      if (code === BACKSLASH) {
        token = effects.enter('mathTextSequence');
        effects.consume(code);
        return afterBackslash;
      }
      effects.enter('mathTextData');
      return data(code);
    };

    const afterBackslash = (code: number | null) => {
      if (code === close) {
        effects.consume(code);
        effects.exit('mathTextSequence');
        effects.exit('mathText');
        return close === 93 ? effects.check(lineEndAhead, ok, nok) : ok;
      }
      token.type = 'mathTextData';
      if (code === null || isLineEnding(code)) {
        effects.exit('mathTextData');
        return between(code);
      }
      effects.consume(code);
      return data;
    };

    const data = (code: number | null): any => {
      if (code === null || code === BACKSLASH || isLineEnding(code)) {
        effects.exit('mathTextData');
        return between(code);
      }
      effects.consume(code);
      return data;
    };

    return start;
  },
};

const nonLazyContinuation = {
  partial: true,
  tokenize(this: any, effects: any, ok: any, nok: any) {
    const lineStart = (code: number | null) => (this.parser.lazy[this.now().line] ? nok(code) : ok(code));
    return (code: number | null) => {
      if (code === null) return nok(code);
      effects.enter('lineEnding');
      effects.consume(code);
      effects.exit('lineEnding');
      return lineStart;
    };
  },
};

// `\]` 加行尾空白，之后必须是行尾或文末。
function closingSequence(effects: any, ok: any, nok: any) {
  const closeBracket = (code: number | null) => {
    if (code !== 93) return nok(code);
    effects.consume(code);
    effects.exit('mathFlowFenceSequence');
    return factorySpace(effects, afterClose, 'whitespace');
  };
  const afterClose = (code: number | null) => {
    if (code !== null && !isLineEnding(code)) return nok(code);
    effects.exit('mathFlowFence');
    return ok(code);
  };
  return (code: number | null) => {
    if (code !== BACKSLASH) return nok(code);
    effects.enter('mathFlowFence');
    effects.enter('mathFlowFenceSequence');
    effects.consume(code);
    return closeBracket;
  };
}

// 独占行的 `\]`。
const latexDisplayClosingFence = {
  partial: true,
  tokenize(effects: any, ok: any, nok: any) {
    return factorySpace(effects, closingSequence(effects, ok, nok), 'linePrefix', 4);
  },
};

// 写在公式最后一行行尾的 `\]`。
const latexDisplayTrailingFence = {
  partial: true,
  tokenize: closingSequence,
};

// 行尾是 `\]`（后面只允许空格、Tab）且其前反斜杠成对（`\\]` 是 LaTeX 换行加 `]`，不算闭合）。
const CLOSING_LINE = /(?:^|[^\\])(?:\\\\)*\\\][ \t]*$/;

interface ContainerContinuation {
  construct: any;
  state: Record<string, any>;
}

/**
 * document 探针不消费字符，总是回退给原生容器构造。记录的 state 引用会由 micromark
 * 填入列表项宽度等信息；flow 预判时只复制实际仍打开的容器，绝不修改真实 document。
 */
function createContainerTracker() {
  const documents = new WeakMap<object, { context: any; containers: Map<number, ContainerContinuation> }>();
  const observer = {
    partial: true,
    tokenize(this: any, _effects: any, _ok: any, nok: any) {
      return (code: number) => {
        let document = documents.get(this.parser);
        if (!document) {
          document = { context: this, containers: new Map() };
          documents.set(this.parser, document);
        }
        const construct = this.parser.constructs.document[code]?.find((candidate: any) => candidate.continuation);
        if (construct) {
          document.containers.set(this.now().offset, { construct, state: this.containerState });
        }
        return nok(code);
      };
    },
  };
  return {
    // 原生列表、引用、GFM 脚注的起始字符，以及公式起始字符。
    document: Object.fromEntries([42, 43, 45, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 62, 91, BACKSLASH].map((code) => [code, observer])),
    snapshot(parser: object): ContainerContinuation[] {
      const document = documents.get(parser);
      const stack: any[] = [];
      for (const [kind, token] of document?.context.events ?? []) {
        if (token._container) {
          if (kind === 'enter') stack.push(token);
          else stack.pop();
        }
      }
      return stack.map((token) => {
        const container = document!.containers.get(token.start.offset)!;
        return { construct: container.construct, state: { ...container.state } };
      });
    },
  };
}

/**
 * 在独立 tokenizer 上复用原生 continuation，检查闭合是否仍在同一组容器内。
 * 列表 continuation 的 `_closeFlow` 表示切换列表项，成功返回也必须拒绝；引用前缀、
 * 列表缩进和 Tab 均由 micromark 处理。容器后面的 `-`、`1.`、`>` 只作为公式正文。
 */
function closesBeforeBlankLine(source: string, lineEndOffset: number, containers: ContainerContinuation[]) {
  let closed: boolean | undefined;
  const check = {
    tokenize(this: any, effects: any, ok: any) {
      let index: number;
      let body: any;
      const discardContent = (code: number | null): any => {
        if (code === null) {
          effects.exit('latexDisplayCheckDiscard');
          return ok(code);
        }
        effects.consume(code);
        return discardContent;
      };
      const discard = (code: number | null) => {
        if (code === null) return ok(code);
        effects.enter('latexDisplayCheckDiscard');
        return discardContent(code);
      };
      const fail = (code: number | null) => {
        closed = false;
        return discard(code);
      };
      const nextContainer = (code: number | null): any => {
        if (index === containers.length) {
          if (code === null || isLineEnding(code)) return fail(code);
          body = effects.enter('latexDisplayCheckBody');
          return content(code);
        }
        const container = containers[index++];
        this.containerState = container.state;
        return effects.attempt(container.construct.continuation, (next: number | null) => (
          this.containerState._closeFlow ? fail(next) : nextContainer(next)
        ), fail)(code);
      };
      const lineStart = (code: number | null) => {
        index = 0;
        return nextContainer(code);
      };
      const content = (code: number | null): any => {
        if (code === null || isLineEnding(code)) {
          effects.exit('latexDisplayCheckBody');
          const value = this.sliceSerialize(body);
          if (/^[ \t]*$/.test(value)) return fail(code);
          if (CLOSING_LINE.test(value)) {
            closed = true;
            return discard(code);
          }
          if (code === null) return fail(code);
          effects.enter('lineEnding');
          effects.consume(code);
          effects.exit('lineEnding');
          return lineStart;
        }
        effects.consume(code);
        return content;
      };
      return lineStart;
    },
  };
  // 在 flowInitial 的空白入口先做预检，保留 codeIndented 配置，使原生引用 continuation 仍限制前导缩进。
  const tokenizer = parseMicromark({ extensions: [{ flowInitial: { [-2]: check, [-1]: check, [32]: check, null: check } }] }).flow();
  const prepare = preprocess();
  const lines = /[^\r\n]*(?:\r\n|\r|\n|$)/g;
  lines.lastIndex = lineEndOffset + (source.startsWith('\r\n', lineEndOffset) ? 2 : 1);
  let line: RegExpExecArray | null;
  while ((line = lines.exec(source)) && line[0]) {
    if (/^[ \t]*(?:\r\n|\r|\n)?$/.test(line[0])) return false;
    tokenizer.write(prepare(line[0], undefined, lines.lastIndex === source.length));
    if (closed !== undefined) return closed;
  }
  return false;
}

/**
 * 独占行的 `\[` … `\]` 块构造：与 `$$` 围栏一样按块收集原文行，内部的 `- y`、`1.`、`>` 等不会被当成 Markdown 块语法。
 * 产出与 `$$` 围栏相同的 mathFlow token，由 remark-math 生成 math 节点。
 * micromark 进入块构造后不再为后续行识别列表、引用等容器，因此在 `\[` 行先用原文预判：
 * 下一个空行之前没有闭合的 `\]`（流式中尚未写完或写法错误）就不进入块构造，后文结构不受影响。
 * 闭合符可独占一行，也可写在最后一行行尾，遇到第一个闭合符即结束。
 */
function createLatexDisplayFlow(getSource: () => string, tracker: ReturnType<typeof createContainerTracker>) {
  return {
    name: 'latexDisplayFlow',
    concrete: true,
    tokenize(this: any, effects: any, ok: any, nok: any) {

      const openBracket = (code: number | null) => {
        if (code !== 91) return nok(code);
        effects.consume(code);
        effects.exit('mathFlowFenceSequence');
        return factorySpace(effects, afterOpen, 'whitespace');
      };

      const afterOpen = (code: number | null) => {
        if (code === null || !isLineEnding(code)) return nok(code);
        if (!closesBeforeBlankLine(getSource(), this.now().offset, tracker.snapshot(this.parser))) return nok(code);
        effects.exit('mathFlowFence');
        if (this.interrupt) return ok(code);
        return effects.attempt(nonLazyContinuation, lineStart, nok)(code);
      };

      const lineStart = (code: number | null) => effects.attempt(latexDisplayClosingFence, after, contentChunkStart)(code);

      const contentChunkStart = (code: number | null): any => {
        if (code === null) return nok(code);
        if (isLineEnding(code)) return effects.attempt(nonLazyContinuation, lineStart, nok)(code);
        return contentPiece(code);
      };

      // 遇到反斜杠先试行尾闭合符；不是闭合符时把 `\x` 成对计入公式原文。
      const contentPiece = (code: number | null): any => {
        if (code === BACKSLASH) return effects.attempt(latexDisplayTrailingFence, after, escapedPair)(code);
        effects.enter('mathFlowValue');
        return contentChunk(code);
      };

      const contentChunk = (code: number | null): any => {
        if (code === null || isLineEnding(code) || code === BACKSLASH) {
          effects.exit('mathFlowValue');
          return code === BACKSLASH ? contentPiece(code) : contentChunkStart(code);
        }
        effects.consume(code);
        return contentChunk;
      };

      const escapedPair = (code: number) => {
        effects.enter('mathFlowValue');
        effects.consume(code);
        return escapedNext;
      };

      const escapedNext = (code: number | null) => {
        if (code === null || isLineEnding(code)) {
          effects.exit('mathFlowValue');
          return contentChunkStart(code);
        }
        effects.consume(code);
        return contentChunk;
      };

      const after = (code: number | null) => {
        effects.exit('mathFlow');
        return ok(code);
      };

      return (code: number) => {
        effects.enter('mathFlow');
        effects.enter('mathFlowFence');
        effects.enter('mathFlowFenceSequence');
        effects.consume(code);
        return openBracket;
      };
    },
  };
}

// 段落里只有一个 `\[…\]` 时提升为独立公式块，与 `$$` 围栏一致；段内其余写法保持行内。
function remarkLatexDelimiters(this: any) {
  // 块构造需要预读原文；remark-parse 已注册 parser，这里记下每次解析的原文。
  let source = '';
  const parse = this.parser;
  this.parser = (doc: string, file: any) => {
    source = doc;
    return parse(doc, file);
  };
  const tracker = createContainerTracker();
  this.data().micromarkExtensions.push({
    document: tracker.document,
    flow: { [BACKSLASH]: createLatexDisplayFlow(() => source, tracker) },
    text: { [BACKSLASH]: latexDelimiterMath },
  });
  return (tree: any, file: any) => {
    const source = String(file.value);
    const visit = (node: any) => {
      node.children?.forEach((child: any, index: number) => {
        const only = child.type === 'paragraph' && child.children.length === 1 ? child.children[0] : null;
        if (only?.type === 'inlineMath' && source.startsWith('\\[', only.position.start.offset)) {
          node.children[index] = {
            type: 'math',
            value: only.value,
            position: child.position,
            data: {
              hName: 'pre',
              hChildren: [{
                type: 'element',
                tagName: 'code',
                properties: { className: ['language-math', 'math-display'] },
                children: [{ type: 'text', value: only.value }],
              }],
            },
          };
          return;
        }
        visit(child);
      });
    };
    visit(tree);
  };
}

// 单个 `$` 会和正文金额冲突（`$100 到 $200`），只接受 `$$`、`\(...\)`、`\[...\]`。
export const markdownRemarkPlugins = [
  remarkGfm,
  remarkCjkFriendly,
  [remarkMath, { singleDollarTextMath: false }],
  remarkLatexDelimiters,
] as any[];

/**
 * 独立公式（`$$`/`\[` 块、` ```math ` 围栏、提升的段落）的 `pre` 换成带源位置的 div：提供阅读锚点，并让长公式在块内横向滚动。
 * 公式本身保留为 `code.language-math`（行内另带 `math-inline`、独立带 `math-display`），由 `MarkdownMath` 组件渲染 KaTeX。
 */
function wrapDisplayMath() {
  return (tree: any) => {
    const visit = (node: any) => {
      node.children?.forEach((child: any, index: number) => {
        const code = child.type === 'element' && child.tagName === 'pre' ? child.children?.[0] : null;
        if (code?.tagName === 'code' && code.properties?.className?.includes('language-math')) {
          node.children[index] = {
            type: 'element',
            tagName: 'div',
            properties: { className: ['markdown-math-display'] },
            position: child.position,
            children: [{ ...code, properties: { className: ['language-math', 'math-display'] } }],
          };
          return;
        }
        visit(child);
      });
    };
    visit(tree);
  };
}

export const markdownMathRehypePlugins = [wrapDisplayMath] as any[];
