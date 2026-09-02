import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react';
import Vditor from 'vditor';
import 'vditor/dist/index.css';
import 'vditor/dist/js/i18n/zh_CN.js';
import 'vditor/dist/js/icons/ant.js';
import 'vditor/dist/js/lute/lute.min.js';

import { apiService } from '../../services/api';
import { resolveMarkdownSessionPath } from './MarkdownReportPreview';

interface VditorMarkdownEditorProps {
  markdown: string;
  onChange: (markdown: string) => void;
  filePath: string;
  buildSessionFileUrl: (resolvedPath: string) => string;
  toolbarOpen: boolean;
}

export interface VditorMarkdownEditorHandle {
  getMarkdown: () => string;
}

const EXTERNAL_IMAGE_PATTERN = /^(?:[a-z][a-z\d+.-]*:|\/\/)/i;
const INTENTIONAL_EMPTY_PARAGRAPH_ATTR = 'data-opencapybox-empty-paragraph';
const INTENTIONAL_EMPTY_PARAGRAPH_MARKDOWN = '&nbsp;';

function serializeIntentionalTrailingParagraphs(markdown: string, editable: HTMLElement | null): string {
  if (!editable) return markdown;
  const children = Array.from(editable.children);
  let intendedCount = 0;
  for (let index = children.length - 1; index >= 0; index -= 1) {
    const child = children[index];
    const semanticEmpty = child.hasAttribute(INTENTIONAL_EMPTY_PARAGRAPH_ATTR)
      || (child.matches('p') && child.textContent === '\u00a0');
    if (!semanticEmpty) break;
    intendedCount += 1;
  }
  if (intendedCount === 0) return markdown;

  let probe = markdown.replace(/\n+$/g, '');
  let encodedCount = 0;
  while (encodedCount < intendedCount) {
    const match = probe.match(/(?:^|\n{2,})(?:&nbsp;|&#160;|\u00a0)$/);
    if (!match || match.index === undefined) break;
    encodedCount += 1;
    probe = probe.slice(0, match.index).replace(/\n+$/g, '');
  }
  if (encodedCount >= intendedCount) return markdown;

  let serialized = markdown.replace(/\n+$/g, '');
  for (let index = encodedCount; index < intendedCount; index += 1) {
    serialized += `${serialized ? '\n\n' : ''}${INTENTIONAL_EMPTY_PARAGRAPH_MARKDOWN}`;
  }
  return serialized;
}

function ensureBundledScriptMarker(id: string) {
  if (document.getElementById(id)) return;
  const marker = document.createElement('script');
  marker.id = id;
  marker.dataset.bundled = 'true';
  document.head.appendChild(marker);
}

function restoreSessionImageSources(markdown: string, sourcesByObjectUrl: Map<string, string>) {
  let restored = markdown;
  sourcesByObjectUrl.forEach((source, objectUrl) => {
    restored = restored.split(objectUrl).join(source);
  });
  return restored;
}

function decorateMachineCommentBlocks(root: ParentNode) {
  const candidates = root instanceof HTMLElement && root.matches('.vditor-wysiwyg__block[data-type="html-block"]')
    ? [root]
    : Array.from(root.querySelectorAll<HTMLElement>('.vditor-wysiwyg__block[data-type="html-block"]'));
  candidates.forEach((block) => {
    const raw = block.querySelector('pre:first-child code')?.textContent?.replace(/\u200b/g, '').trim() || '';
    const isCommentOnly = /^<!--[\s\S]*-->$/.test(raw);
    block.classList.toggle('file-preview-vditor-machine-comment', isCommentOnly);
    if (isCommentOnly) block.setAttribute('aria-hidden', 'true');
    else block.removeAttribute('aria-hidden');
  });
}

function decorateIntentionalEmptyParagraphs(root: ParentNode) {
  const paragraphs = root instanceof HTMLParagraphElement ? [root] : Array.from(root.querySelectorAll('p'));
  paragraphs.forEach((paragraph) => {
    if (paragraph.textContent === '\u00a0') {
      paragraph.setAttribute(INTENTIONAL_EMPTY_PARAGRAPH_ATTR, 'true');
    }
  });
}

export const VditorMarkdownEditor = forwardRef<VditorMarkdownEditorHandle, VditorMarkdownEditorProps>(function VditorMarkdownEditor({
  markdown,
  onChange,
  filePath,
  buildSessionFileUrl,
  toolbarOpen,
}, forwardedRef) {
  const mountRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<Vditor | null>(null);
  const editableElementRef = useRef<HTMLElement | null>(null);
  const latestMarkdownRef = useRef(markdown);
  const emittedMarkdownRef = useRef(markdown);
  const onChangeRef = useRef(onChange);
  const objectUrlBySourceRef = useRef(new Map<string, string>());
  const sourceByObjectUrlRef = useRef(new Map<string, string>());
  const [ready, setReady] = useState(false);

  latestMarkdownRef.current = markdown;
  onChangeRef.current = onChange;

  useImperativeHandle(forwardedRef, () => ({
    getMarkdown: () => {
      const current = editorRef.current?.getValue() ?? emittedMarkdownRef.current;
      const serialized = serializeIntentionalTrailingParagraphs(current, editableElementRef.current);
      return restoreSessionImageSources(serialized, sourceByObjectUrlRef.current);
    },
  }), []);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return undefined;

    setReady(false);
    ensureBundledScriptMarker('vditorLuteScript');
    ensureBundledScriptMarker('vditorIconScript');

    let disposed = false;
    let observer: MutationObserver | null = null;
    let editableElement: HTMLElement | null = null;
    const pendingImageSources = new Set<string>();
    const objectUrlBySource = objectUrlBySourceRef.current;
    const sourceByObjectUrl = sourceByObjectUrlRef.current;

    const hydrateImage = async (image: HTMLImageElement) => {
      const source = image.getAttribute('src')?.trim() || '';
      if (!source || EXTERNAL_IMAGE_PATTERN.test(source)) return;

      const existingObjectUrl = objectUrlBySource.get(source);
      if (existingObjectUrl) {
        image.setAttribute('src', existingObjectUrl);
        return;
      }
      if (pendingImageSources.has(source)) return;

      const resolvedPath = resolveMarkdownSessionPath(filePath, source);
      if (!resolvedPath) return;

      pendingImageSources.add(source);
      try {
        const response = await fetch(buildSessionFileUrl(resolvedPath), {
          headers: apiService.getAuthHeaders(),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const objectUrl = URL.createObjectURL(await response.blob());
        if (disposed) {
          URL.revokeObjectURL(objectUrl);
          return;
        }
        objectUrlBySource.set(source, objectUrl);
        sourceByObjectUrl.set(objectUrl, source);
        mount.querySelectorAll<HTMLImageElement>('img').forEach((candidate) => {
          if (candidate.getAttribute('src') === source) candidate.setAttribute('src', objectUrl);
        });
      } catch (error) {
        console.error('Failed to load Markdown session image:', error);
      } finally {
        pendingImageSources.delete(source);
      }
    };

    const hydrateImages = (root: ParentNode) => {
      if (root instanceof HTMLImageElement) void hydrateImage(root);
      root.querySelectorAll<HTMLImageElement>('img').forEach((image) => void hydrateImage(image));
    };

    // FilePreview 的 autosave 已经 debounce；再叠一层会让快速关闭丢掉最后的输入。
    const emitInput = (value: string) => {
      const serialized = serializeIntentionalTrailingParagraphs(value, editableElement);
      const restored = restoreSessionImageSources(serialized, sourceByObjectUrl);
      if (restored === emittedMarkdownRef.current) return;
      emittedMarkdownRef.current = restored;
      if (!disposed) onChangeRef.current(restored);
    };

    const preserveContinuousEnter = (event: KeyboardEvent) => {
      if (
        event.key !== 'Enter'
        || event.ctrlKey
        || event.metaKey
        || event.altKey
        || event.shiftKey
        || event.isComposing
      ) return;

      const selection = window.getSelection();
      if (!selection || selection.rangeCount === 0 || !selection.isCollapsed || !editableElement) return;
      const range = selection.getRangeAt(0);
      const startElement = range.startContainer instanceof Element
        ? range.startContainer
        : range.startContainer.parentElement;
      const block = startElement?.closest<HTMLElement>('p, h1, h2, h3, h4, h5, h6');
      if (!block || block.parentElement !== editableElement) return;

      const trailingRange = range.cloneRange();
      trailingRange.setEnd(block, block.childNodes.length);
      const trailingText = trailingRange.cloneContents().textContent?.replace(/[\u200b\n]/g, '').trim() || '';
      if (trailingText) return;

      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();

      const nextParagraph = document.createElement('p');
      nextParagraph.setAttribute('data-block', '0');
      nextParagraph.setAttribute(INTENTIONAL_EMPTY_PARAGRAPH_ATTR, 'true');
      nextParagraph.appendChild(document.createTextNode('\u00a0'));
      block.insertAdjacentElement('afterend', nextParagraph);

      const nextRange = document.createRange();
      nextRange.selectNodeContents(nextParagraph);
      nextRange.collapse(true);
      selection.removeAllRanges();
      selection.addRange(nextRange);

      // Vditor 的 input 回调不会为这次手改 DOM 触发，不自行投影就拿不到 dirty。
      const currentValue = editorRef.current?.getValue();
      if (currentValue !== undefined) emitInput(currentValue);
    };

    const releaseEmptyParagraphPlaceholder = (event: InputEvent) => {
      if (event.inputType === 'insertParagraph') return;
      const selection = window.getSelection();
      if (!selection || selection.rangeCount === 0) return;
      const container = selection.getRangeAt(0).startContainer;
      const element = container instanceof Element ? container : container.parentElement;
      const paragraph = element?.closest<HTMLElement>(`p[${INTENTIONAL_EMPTY_PARAGRAPH_ATTR}]`);
      if (!paragraph || paragraph.parentElement !== editableElement) return;
      paragraph.removeAttribute(INTENTIONAL_EMPTY_PARAGRAPH_ATTR);
      paragraph.textContent = '';
    };

    const captureNativeInput = (event: Event) => {
      if ((event as InputEvent).isComposing) return;
      const value = editorRef.current?.getValue();
      if (value !== undefined) emitInput(value);
    };

    const editor = new Vditor(mount, {
      value: latestMarkdownRef.current,
      mode: 'wysiwyg',
      height: '100%',
      width: '100%',
      lang: 'zh_CN',
      i18n: window.VditorI18n,
      cache: { enable: false },
      toolbar: [
        'headings',
        'bold',
        'italic',
        'strike',
        '|',
        'list',
        'ordered-list',
        'check',
        'quote',
        'line',
        'table',
        'code',
        'inline-code',
        '|',
        'undo',
        'redo',
      ],
      toolbarConfig: { hide: false },
      counter: { enable: false },
      resize: { enable: false },
      outline: { enable: false, position: 'left' },
      link: { isOpen: false },
      preview: {
        delay: 60,
        markdown: {
          footnotes: true,
          gfmAutoLink: true,
          mark: true,
          sanitize: true,
          toc: true,
        },
      },
      input: emitInput,
      after: () => {
        if (disposed) return;
        const scrollSurface = mount.querySelector<HTMLElement>('.vditor-wysiwyg');
        const editable = mount.querySelector<HTMLElement>('.vditor-wysiwyg > .vditor-reset');
        editableElement = editable;
        editableElementRef.current = editable;
        scrollSurface?.classList.add('file-preview-vditor-scroll');
        editable?.classList.add('file-preview-report', 'file-preview-vditor-content');
        editable?.setAttribute('role', 'textbox');
        editable?.setAttribute('aria-label', 'Markdown 所见即所得编辑器');
        editable?.setAttribute('aria-multiline', 'true');
        editable?.setAttribute('spellcheck', 'false');
        editable?.addEventListener('keydown', preserveContinuousEnter, true);
        editable?.addEventListener('beforeinput', releaseEmptyParagraphPlaceholder, true);
        // Vditor's options.input can wait for undoDelay. Capture the resulting
        // DOM immediately after its native input/composition handlers instead.
        editable?.addEventListener('input', captureNativeInput);
        editable?.addEventListener('compositionend', captureNativeInput);

        const latestMarkdown = latestMarkdownRef.current;
        if (editor.getValue() !== latestMarkdown) editor.setValue(latestMarkdown, true);

        observer = new MutationObserver((mutations) => {
          mutations.forEach((mutation) => {
            if (mutation.type === 'attributes' && mutation.target instanceof HTMLImageElement) {
              void hydrateImage(mutation.target);
            }
            mutation.addedNodes.forEach((node) => {
              if (node instanceof Element) {
                hydrateImages(node);
                decorateMachineCommentBlocks(node);
                decorateIntentionalEmptyParagraphs(node);
              }
            });
          });
        });
        observer.observe(mount, { childList: true, subtree: true, attributes: true, attributeFilter: ['src'] });
        hydrateImages(mount);
        decorateMachineCommentBlocks(mount);
        decorateIntentionalEmptyParagraphs(mount);
        setReady(true);
      },
    });
    editorRef.current = editor;

    return () => {
      disposed = true;
      observer?.disconnect();
      editableElement?.removeEventListener('keydown', preserveContinuousEnter, true);
      editableElement?.removeEventListener('beforeinput', releaseEmptyParagraphPlaceholder, true);
      editableElement?.removeEventListener('input', captureNativeInput);
      editableElement?.removeEventListener('compositionend', captureNativeInput);
      editableElementRef.current = null;
      editor.destroy();
      editorRef.current = null;
      objectUrlBySource.forEach((url) => URL.revokeObjectURL(url));
      objectUrlBySource.clear();
      sourceByObjectUrl.clear();
    };
  }, [buildSessionFileUrl, filePath]);

  useEffect(() => {
    if (!ready || markdown === emittedMarkdownRef.current) return;
    emittedMarkdownRef.current = markdown;
    editorRef.current?.setValue(markdown, true);
  }, [markdown, ready]);

  return (
    <div className="file-preview-vditor-shell" data-toolbar-open={toolbarOpen} aria-busy={!ready}>
      <div ref={mountRef} className="file-preview-vditor-editor" />
      {!ready && (
        <div className="file-preview-vditor-loading" role="status">正在初始化 Markdown 编辑器…</div>
      )}
    </div>
  );
});
