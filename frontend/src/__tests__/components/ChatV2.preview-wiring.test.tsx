import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '../utils/test-utils';
import { ChatV2 } from '../../components/ChatV2';
import { apiService } from '../../services/api';
import { makeChatV2DefaultProps } from '../utils/chatv2-helpers';

let lastChatInputProps: any = null;

vi.mock('../../services/api', () => ({
  apiService: {
    getSessionHistoryV2: vi.fn(),
    getSessionFiles: vi.fn(),
    sendMessageStreamV2: vi.fn(),
    uploadFile: vi.fn(),
    getRunningSession: vi.fn(),
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
  Round: () => <div data-testid="round" />,
}));

vi.mock('../../components/ArtifactsPanel', () => ({
  ArtifactsPanel: () => <div data-testid="artifacts-panel" />,
}));

vi.mock('../../components/FilePreview', () => ({
  FilePreview: () => <div data-testid="file-preview" />,
}));

describe('ChatV2 preview callback wiring', () => {
  const defaultProps = makeChatV2DefaultProps();

  beforeEach(() => {
    vi.clearAllMocks();
    lastChatInputProps = null;
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
});
