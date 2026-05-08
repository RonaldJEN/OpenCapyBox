import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from 'vitest';
import { AuthenticatedImage } from '../../components/AuthenticatedImage';

// ── Object URL mock helpers ──

const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');

function mockObjectUrlApis() {
  const createMock = vi.fn(() => 'blob:http://localhost/fake-blob');
  const revokeMock = vi.fn();
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, writable: true, value: createMock });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, writable: true, value: revokeMock });
  return { createMock, revokeMock };
}

function restoreObjectUrlApis() {
  if (originalCreateObjectURL) {
    Object.defineProperty(URL, 'createObjectURL', originalCreateObjectURL);
  } else {
    delete (URL as { createObjectURL?: unknown }).createObjectURL;
  }

  if (originalRevokeObjectURL) {
    Object.defineProperty(URL, 'revokeObjectURL', originalRevokeObjectURL);
  } else {
    delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL;
  }
}

// ── apiService mock ──

const { getAuthHeadersMock } = vi.hoisted(() => ({
  getAuthHeadersMock: vi.fn(() => ({ Authorization: 'Bearer test-token' })),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    getAuthHeaders: getAuthHeadersMock,
  },
}));

// ── Tests ──

describe('AuthenticatedImage', () => {
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  afterAll(() => {
    restoreObjectUrlApis();
  });

  it('对 /api/ 图片使用 fetch + Authorization 并显示 blob URL', async () => {
    const { createMock } = mockObjectUrlApis();
    const fakeBlob = new Blob(['img'], { type: 'image/png' });
    fetchSpy.mockResolvedValueOnce(new Response(fakeBlob, { status: 200 }));

    render(<AuthenticatedImage src="/api/sessions/s1/files/photo.png?preview=true" alt="photo" />);

    await waitFor(() => {
      const img = screen.getByAltText('photo');
      expect(img).toBeInTheDocument();
      expect(img.getAttribute('src')).toBe('blob:http://localhost/fake-blob');
    });

    expect(fetchSpy).toHaveBeenCalledWith(
      '/api/sessions/s1/files/photo.png?preview=true',
      expect.objectContaining({
        headers: { Authorization: 'Bearer test-token' },
      }),
    );
    expect(createMock).toHaveBeenCalledTimes(1);
  });

  it('fetch 失败时显示 fallback', async () => {
    mockObjectUrlApis();
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 401 }));

    render(
      <AuthenticatedImage
        src="/api/sessions/s1/files/photo.png"
        alt="photo"
        fallback={<div data-testid="fallback">failed</div>}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId('fallback')).toBeInTheDocument();
    });
    expect(screen.queryByAltText('photo')).not.toBeInTheDocument();
  });

  it('网络错误时显示 fallback', async () => {
    mockObjectUrlApis();
    fetchSpy.mockRejectedValueOnce(new Error('network error'));

    render(
      <AuthenticatedImage
        src="/api/sessions/s1/files/photo.png"
        alt="photo"
        fallback={<span data-testid="net-err">err</span>}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTestId('net-err')).toBeInTheDocument();
    });
  });

  it('卸载时 revoke blob URL', async () => {
    const { revokeMock } = mockObjectUrlApis();
    const fakeBlob = new Blob(['x'], { type: 'image/png' });
    fetchSpy.mockResolvedValueOnce(new Response(fakeBlob, { status: 200 }));

    const { unmount } = render(
      <AuthenticatedImage src="/api/sessions/s1/files/a.png" alt="a" />,
    );

    await waitFor(() => {
      expect(screen.getByAltText('a')).toBeInTheDocument();
    });

    unmount();

    await waitFor(() => {
      expect(revokeMock).toHaveBeenCalled();
    });
  });

  it('非 API 图片直接渲染，不调用 fetch', () => {
    render(<AuthenticatedImage src="https://example.com/cat.jpg" alt="cat" />);

    const img = screen.getByAltText('cat');
    expect(img.getAttribute('src')).toBe('https://example.com/cat.jpg');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('data: URL 直接渲染，不调用 fetch', () => {
    const dataUrl = 'data:image/png;base64,iVBOR';
    render(<AuthenticatedImage src={dataUrl} alt="base64" />);

    const img = screen.getByAltText('base64');
    expect(img.getAttribute('src')).toBe(dataUrl);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('无 fallback 时 fetch 失败渲染空', async () => {
    mockObjectUrlApis();
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 403 }));

    const { container } = render(
      <AuthenticatedImage src="/api/sessions/s1/files/x.png" alt="x" />,
    );

    await waitFor(() => {
      // failed 状态，无 fallback → 空
      expect(screen.queryByAltText('x')).not.toBeInTheDocument();
    });
    expect(container.innerHTML).toBe('');
  });
});
