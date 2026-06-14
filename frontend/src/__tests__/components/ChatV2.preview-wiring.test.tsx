import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../utils/test-utils';
import { ChatV2 } from '../../components/ChatV2';
import { apiService } from '../../services/api';
import { makeChatV2DefaultProps } from '../utils/chatv2-helpers';

let lastChatInputProps: any = null;
let lastArtifactsPanelProps: any = null;
let lastRoundProps: any = null;

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
  Round: (props: any) => {
    lastRoundProps = props;
    return (
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
    );
  },
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
    lastRoundProps = null;
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

  it('should verify assistant file refs against session files before passing matches to Round', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session',
      total: 1,
      rounds: [
        {
          round_id: 'round-1',
          user_message: '整理一下文件',
          final_response: [
            '文件位置： `results/report.md`',
            '文件位置： `missing.py`',
          ].join('\n'),
          steps: [],
          step_count: 0,
          status: 'completed',
          created_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        },
      ],
    });
    vi.mocked(apiService.getSessionFiles).mockImplementation(async (_sessionId, path) => {
      if (path === 'results') {
        return {
          total: 1,
          files: [{
            name: 'report.md',
            path: 'results/report.md',
            size: 128,
            modified: '2026-06-12T10:20:00Z',
            type: 'md',
            is_directory: false,
          }],
        };
      }
      return { files: [], total: 0 };
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(apiService.getSessionFiles).toHaveBeenCalledWith('test-session', 'results');
      expect(apiService.getSessionFiles).toHaveBeenCalledWith('test-session', undefined);
    });

    await waitFor(() => {
      expect(lastRoundProps.assistantFileMatches['results/report.md']).toMatchObject({
        name: 'report.md',
        path: 'results/report.md',
        session_id: 'test-session',
      });
      expect(lastRoundProps.assistantFileMatches['missing.py']).toBeNull();
    });
  });

  it('should keep assistant file refs unknown when verification request fails', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session',
      total: 1,
      rounds: [
        {
          round_id: 'round-1',
          user_message: '生成文件',
          final_response: '文件位置： `unstable.md`',
          steps: [],
          step_count: 0,
          status: 'completed',
          created_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        },
      ],
    });
    vi.mocked(apiService.getSessionFiles).mockRejectedValue(new Error('temporary failure'));
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(apiService.getSessionFiles).toHaveBeenCalledWith('test-session', undefined);
    });

    expect(
      Object.prototype.hasOwnProperty.call(lastRoundProps.assistantFileMatches, 'unstable.md'),
    ).toBe(false);
    warnSpy.mockRestore();
  });

  it('should recheck a missing assistant file ref after later rounds are added', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session',
      total: 1,
      rounds: [
        {
          round_id: 'round-1',
          user_message: '先生成文件',
          final_response: '文件位置： `later.md`',
          steps: [],
          step_count: 0,
          status: 'completed',
          created_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        },
      ],
    });

    let fileExists = false;
    vi.mocked(apiService.getSessionFiles).mockImplementation(async () => (
      fileExists
        ? {
            total: 1,
            files: [{
              name: 'later.md',
              path: 'later.md',
              size: 64,
              modified: '2026-06-12T11:00:00Z',
              type: 'md',
              is_directory: false,
            }],
          }
        : { files: [], total: 0 }
    ));
    vi.mocked(apiService.sendMessageStreamV2).mockImplementationOnce(async (_sessionId, _content, callbacks) => {
      callbacks.onRunFinished?.(
        'test-session',
        'round-2',
        { finalResponse: '', stepCount: 0 },
        'success',
      );
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(lastRoundProps.assistantFileMatches['later.md']).toBeNull();
    });

    fileExists = true;
    act(() => {
      lastChatInputProps.onChange('再检查一次');
    });
    act(() => {
      lastChatInputProps.onSend();
    });

    await waitFor(() => {
      expect(vi.mocked(apiService.getSessionFiles).mock.calls.length).toBeGreaterThanOrEqual(2);
      expect(lastRoundProps.assistantFileMatches['later.md']).toMatchObject({
        name: 'later.md',
        path: 'later.md',
        session_id: 'test-session',
      });
    });
  });

  it('should clear stale missing assistant file refs while later-round recheck is unknown', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session',
      total: 1,
      rounds: [
        {
          round_id: 'round-1',
          user_message: '先生成文件',
          final_response: '文件位置： `later.md`',
          steps: [],
          step_count: 0,
          status: 'completed',
          created_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        },
      ],
    });

    let fileListCalls = 0;
    vi.mocked(apiService.getSessionFiles).mockImplementation(async () => {
      fileListCalls += 1;
      if (fileListCalls === 1) {
        return { files: [], total: 0 };
      }
      throw new Error('temporary failure');
    });
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.mocked(apiService.sendMessageStreamV2).mockImplementationOnce(async (_sessionId, _content, callbacks) => {
      callbacks.onRunFinished?.(
        'test-session',
        'round-2',
        { finalResponse: '', stepCount: 0 },
        'success',
      );
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(lastRoundProps.assistantFileMatches['later.md']).toBeNull();
    });

    act(() => {
      lastChatInputProps.onChange('再检查一次');
    });
    act(() => {
      lastChatInputProps.onSend();
    });

    await waitFor(() => {
      expect(apiService.getSessionFiles).toHaveBeenCalledTimes(2);
      expect(
        Object.prototype.hasOwnProperty.call(lastRoundProps.assistantFileMatches, 'later.md'),
      ).toBe(false);
    });
    warnSpy.mockRestore();
  });

  it('should ignore stale pending assistant file verification after later rounds are added', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session',
      total: 1,
      rounds: [
        {
          round_id: 'round-1',
          user_message: '先生成文件',
          final_response: '文件位置： `race.md`',
          steps: [],
          step_count: 0,
          status: 'completed',
          created_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        },
      ],
    });

    let resolveFirst: (value: { files: any[]; total: number }) => void = () => {};
    let resolveSecond: (value: { files: any[]; total: number }) => void = () => {};
    const firstRequest = new Promise<{ files: any[]; total: number }>((resolve) => {
      resolveFirst = resolve;
    });
    const secondRequest = new Promise<{ files: any[]; total: number }>((resolve) => {
      resolveSecond = resolve;
    });
    vi.mocked(apiService.getSessionFiles)
      .mockImplementationOnce(async () => firstRequest)
      .mockImplementationOnce(async () => secondRequest);
    vi.mocked(apiService.sendMessageStreamV2).mockImplementationOnce(async (_sessionId, _content, callbacks) => {
      callbacks.onRunFinished?.(
        'test-session',
        'round-2',
        { finalResponse: '', stepCount: 0 },
        'success',
      );
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(apiService.getSessionFiles).toHaveBeenCalledTimes(1);
    });

    act(() => {
      lastChatInputProps.onChange('继续');
    });
    act(() => {
      lastChatInputProps.onSend();
    });

    await waitFor(() => {
      expect(apiService.getSessionFiles).toHaveBeenCalledTimes(2);
    });

    await act(async () => {
      resolveFirst({ files: [], total: 0 });
      await firstRequest;
    });
    await act(async () => {
      resolveSecond({
        total: 1,
        files: [{
          name: 'race.md',
          path: 'race.md',
          size: 32,
          modified: '2026-06-12T12:00:00Z',
          type: 'md',
          is_directory: false,
        }],
      });
      await secondRequest;
    });

    await waitFor(() => {
      expect(lastRoundProps.assistantFileMatches['race.md']).toMatchObject({
        name: 'race.md',
        path: 'race.md',
        session_id: 'test-session',
      });
    });
  });
});
