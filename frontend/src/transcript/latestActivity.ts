export type LatestActivityEntry = {
  key: string;
  step: number;
  process: boolean;
} & (
  | { kind: 'text'; node?: { interrupted?: boolean } }
  | { kind: 'tool'; item?: { status: string } }
  | { kind: 'file' }
);

/** Select the live activity preview without discarding its ordered history. */
export function selectLatestActivityKeys(entries: readonly LatestActivityEntry[]): Set<string> {
  let latestTextIndex = -1;
  let latestToolStep: number | undefined;
  entries.forEach((entry, index) => {
    if (!entry.process) return;
    if (entry.kind === 'text' && !entry.node?.interrupted) {
      latestTextIndex = index;
      // Earlier completed tools belong to the previous progress paragraph.
      latestToolStep = undefined;
    } else if (entry.kind === 'tool') {
      latestToolStep = entry.step;
    }
  });

  return new Set(entries.filter((entry, index) => entry.process && (
    index === latestTextIndex
    || (entry.kind === 'tool' && (entry.item?.status === 'running'
      || (index > latestTextIndex && entry.step === latestToolStep)))
  )).map((entry) => entry.key));
}
