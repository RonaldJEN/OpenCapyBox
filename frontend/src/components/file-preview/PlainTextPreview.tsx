export function PlainTextPreview({ text }: { text: string }) {
  return (
    <pre
      data-testid="plain-text-content"
      className="m-0 min-w-0 whitespace-pre-wrap font-sans text-[15px] leading-[1.7] text-claude-text [overflow-wrap:anywhere]"
    >
      {text}
    </pre>
  );
}
