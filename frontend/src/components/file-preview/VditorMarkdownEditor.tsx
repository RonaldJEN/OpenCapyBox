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

export const VditorMarkdownEditor = forwardRef<VditorMarkdownEditorHandle, VditorMarkdownEditorProps>(function VditorMarkdownEditor({
  markdown,
  onChange,
  filePath,
  buildSessionFileUrl,
  toolbarOpen,
}, forwardedRef) {
  const mountRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<Vditor | null>(null);
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
      return restoreSessionImageSources(current, sourceByObjectUrlRef.current);
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
        delay: 120,
        markdown: {
          footnotes: true,
          gfmAutoLink: true,
          mark: true,
          sanitize: true,
          toc: true,
        },
      },
      input: (value) => {
        const restored = restoreSessionImageSources(value, sourceByObjectUrl);
        emittedMarkdownRef.current = restored;
        onChangeRef.current(restored);
      },
      after: () => {
        if (disposed) return;
        const scrollSurface = mount.querySelector<HTMLElement>('.vditor-wysiwyg');
        const editable = mount.querySelector<HTMLElement>('.vditor-wysiwyg > .vditor-reset');
        scrollSurface?.classList.add('file-preview-vditor-scroll');
        editable?.classList.add('file-preview-report', 'file-preview-vditor-content');
        editable?.setAttribute('role', 'textbox');
        editable?.setAttribute('aria-label', 'Markdown 所见即所得编辑器');
        editable?.setAttribute('aria-multiline', 'true');
        editable?.setAttribute('spellcheck', 'false');

        const latestMarkdown = latestMarkdownRef.current;
        if (editor.getValue() !== latestMarkdown) editor.setValue(latestMarkdown, true);

        observer = new MutationObserver((mutations) => {
          mutations.forEach((mutation) => {
            if (mutation.type === 'attributes' && mutation.target instanceof HTMLImageElement) {
              void hydrateImage(mutation.target);
            }
            mutation.addedNodes.forEach((node) => {
              if (node instanceof Element) hydrateImages(node);
            });
          });
        });
        observer.observe(mount, { childList: true, subtree: true, attributes: true, attributeFilter: ['src'] });
        hydrateImages(mount);
        setReady(true);
      },
    });
    editorRef.current = editor;

    return () => {
      disposed = true;
      observer?.disconnect();
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
