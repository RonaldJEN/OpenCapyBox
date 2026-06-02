import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../utils/test-utils';
import { ChatV2 } from '../../components/ChatV2';
import { apiService } from '../../services/api';
import { makeChatV2DefaultProps } from '../utils/chatv2-helpers';

let lastChatInputProps: any = null;
let lastArtifactsPanelProps: any = null;

vi.mock('../../services/api', () => ({
  apiService: {
    getSessionHistoryV2: vi.fn(),
    getSessionFiles: vi.fn(),
    sendMessageStreamV2: vi.fn(),
    uploadFile: vi.fn(),
    getRunningSessions: vi.fn(),
    createSession: vi.fn(),
    getUserId: vi.fn(() => 'demo-session'),
  },
}));

vi.mock('../../components/ChatInput', () => ({
  ChatInput: (props: any) => {
    lastChatInputProps = props;
    return <div data-testid="chat-input-mock" />;
  },
}));

vi.mock('../../components/Round', () => ({
  Round: (props: any) => (
    <button
      type="button"
      data-testid="round-open-file"
      onClick={() => props.onOpenFileInPanel?.({
        name: 'quick_sort.py',
        path: 'quick_sort.py',
        size: 0,
        modified: '',
        type: 'py',
      })}
    >
      open file
    </button>
  ),
}));

vi.mock('../../components/ArtifactsPanel', () => ({
  ArtifactsPanel: (props: any) => {
    lastArtifactsPanelProps = props;
    return <div data-testid="artifacts-panel" data-open={String(props.isOpen)} />;
  },
}));

vi.mock('../../components/FilePreview', () => ({
  FilePreview: () => <div data-testid="file-preview" />,
}));

describe('ChatV2 preview callback wiring', () => {
  const defaultProps = makeChatV2DefaultProps();

  beforeEach(() => {
    vi.clearAllMocks();
    lastChatInputProps = null;
    lastArtifactsPanelProps = null;
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'test-session',
      total: 0,
    });
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({ files: [], total: 0 });
  });

  it('no sessionId: should not pass onPreviewAttachment to ChatInput', async () => {
    render(<ChatV2 sessionId="" {...defaultProps} />);

    await waitFor(() => {
      expect(lastChatInputProps).toBeTruthy();
      expect(lastChatInputProps.onPreviewAttachment).toBeUndefined();
    });
  });

  it('with sessionId: should pass onPreviewAttachment to ChatInput', async () => {
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(lastChatInputProps).toBeTruthy();
      expect(typeof lastChatInputProps.onPreviewAttachment).toBe('function');
    });
  });

  it('should pass onInputDropHandled to ChatInput', async () => {
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(lastChatInputProps).toBeTruthy();
      expect(typeof lastChatInputProps.onInputDropHandled).toBe('function');
    });
  });

  it('assistant file callback should open Files panel with target file', async () => {
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({
      total: 1,
      files: [{
        name: 'quick_sort.py',
        path: 'quick_sort.py',
        size: 256,
        modified: '2026-04-22T10:20:00Z',
        type: 'py',
        is_directory: false,
      }],
    });
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session',
      total: 1,
      rounds: [
        {
          round_id: 'round-1',
          user_message: '写个快排给我',
          final_response: '文件位置： quick_sort.py',
          steps: [],
          step_count: 0,
          status: 'completed',
          created_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        },
      ],
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByTestId('round-open-file')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('round-open-file'));

    await waitFor(() => {
      expect(apiService.getSessionFiles).toHaveBeenCalledWith('test-session', undefined);
      expect(lastArtifactsPanelProps.isOpen).toBe(true);
      expect(lastArtifactsPanelProps.targetFile).toMatchObject({
        name: 'quick_sort.py',
        path: 'quick_sort.py',
        session_id: 'test-session',
      });
      expect(lastArtifactsPanelProps.targetFileNonce).toBe(1);
    });
  });

  it('assistant file callback should not open Files panel when target file is missing', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session',
      total: 1,
      rounds: [
        {
          round_id: 'round-1',
          user_message: '写个快排给我',
          final_response: '文件位置： quick_sort.py',
          steps: [],
          step_count: 0,
          status: 'completed',
          created_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        },
      ],
    });
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({ files: [], total: 0 });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByTestId('round-open-file')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('round-open-file'));

    await waitFor(() => {
      expect(apiService.getSessionFiles).toHaveBeenCalledWith('test-session', undefined);
      expect(lastArtifactsPanelProps.isOpen).toBe(false);
    });
    expect(screen.getByText('文件不存在或尚未生成：quick_sort.py')).toBeInTheDocument();
  });

  it('assistant file callback should clear stale missing-file error after a later success', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session',
      total: 1,
      rounds: [
        {
          round_id: 'round-1',
          user_message: '写个快排给我',
          final_response: '文件位置： quick_sort.py',
          steps: [],
          step_count: 0,
          status: 'completed',
          created_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        },
      ],
    });
    vi.mocked(apiService.getSessionFiles)
      .mockResolvedValueOnce({ files: [], total: 0 })
      .mockResolvedValueOnce({
        total: 1,
        files: [{
          name: 'quick_sort.py',
          path: 'quick_sort.py',
          size: 256,
          modified: '2026-04-22T10:20:00Z',
          type: 'py',
          is_directory: false,
        }],
      });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(screen.getByTestId('round-open-file')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('round-open-file'));

    await waitFor(() => {
      expect(screen.getByText('文件不存在或尚未生成：quick_sort.py')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('round-open-file'));

    await waitFor(() => {
      expect(lastArtifactsPanelProps.isOpen).toBe(true);
      expect(screen.queryByText('文件不存在或尚未生成：quick_sort.py')).not.toBeInTheDocument();
    });
  });
});
