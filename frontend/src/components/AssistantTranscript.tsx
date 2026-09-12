import { Component, type ReactNode } from 'react';
import type { AssistantFileReference, FileInfo } from '../types';
import type { TranscriptTextNode } from '../transcript/projectRoundTranscript';
import { AssistantMarkdown } from './AssistantMarkdown';
import FeedbackMessage from './FeedbackMessage';

class MessageErrorBoundary extends Component<{
  node: TranscriptTextNode;
  children: ReactNode;
}, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (!this.state.failed) return this.props.children;
    return <>
      <FeedbackMessage tone="error" messageKey={this.props.node.id} className="not-prose text-xs text-claude-error">此段格式显示失败，已显示原文。</FeedbackMessage>
      <div data-reading-block={`answer:${this.props.node.id}:plain`} className="whitespace-pre-wrap break-words">
        {this.props.node.text}
      </div>
    </>;
  }
}

export function AssistantTextMessage({ node, fileReferences, onOpenFile }: {
  node: TranscriptTextNode;
  fileReferences: AssistantFileReference[];
  onOpenFile?: (file: FileInfo) => void;
}) {
  return <>
    <MessageErrorBoundary node={node}>
      <AssistantMarkdown content={node.text} readingNodeId={node.id} fileReferences={fileReferences} onOpenFile={onOpenFile} />
    </MessageErrorBoundary>
    {node.interrupted && <p className="not-prose text-xs text-claude-muted">本段输出已中断</p>}
    {node.streaming && <span data-stream-cursor aria-hidden="true" className="inline-block w-0.5 h-4 bg-claude-muted ml-0.5 animate-blink align-middle" />}
  </>;
}
