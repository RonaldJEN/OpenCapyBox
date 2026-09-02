export type WorkspaceEntryKind = 'file' | 'directory';
export type WorkspaceEntryStatus = 'active';

export interface WorkspaceEntry {
  entry_id: string;
  parent_id: string | null;
  name: string;
  kind: WorkspaceEntryKind;
  path: string;
  size_bytes: number;
  mime_type: string | null;
  sha256: string | null;
  revision: number;
  current_version_id?: string | null;
  tree_revision?: number;
  status: WorkspaceEntryStatus;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceEntriesResponse {
  items: WorkspaceEntry[];
  next_cursor: string | null;
  workspace_revision: number;
}

export type WorkspaceMutationStatus =
  | 'CREATED'
  | 'UPDATED'
  | 'NO_CHANGE'
  | 'MOVED'
;

export interface WorkspaceMutationResult {
  status: WorkspaceMutationStatus;
  entry: WorkspaceEntry;
  mutation_id: string;
  auto_merged?: boolean;
}

export interface WorkspaceDeleteResult {
  status: 'DELETED';
  mutation_id: string;
  affected_entry_ids: string[];
  root_count: number;
  entry_count: number;
}

export interface WorkspaceVersion {
  version_id: string;
  entry_id: string;
  sequence: number;
  parent_version_id: string | null;
  restored_from_version_id: string | null;
  sha256: string | null;
  size_bytes: number;
  mime_type: string | null;
  actor: string;
  state: string;
  pinned: boolean;
  checkpoint_kind: string | null;
  created_at: string;
}

export interface WorkspaceImportSessionFileRequest {
  session_id: string;
  source_path: string;
  source_revision: string;
  destination_parent_id: string | null;
  destination_name: string;
  conflict_policy: 'fail' | 'overwrite';
  expected_destination_revision: number | null;
  idempotency_key: string;
}

export type WorkspaceErrorCode =
  | 'NAME_CONFLICT'
  | 'REVISION_CONFLICT'
  | 'SOURCE_REVISION_CONFLICT'
  | 'QUOTA_EXCEEDED'
  | string;

export interface WorkspaceErrorDetail {
  code: WorkspaceErrorCode;
  message: string;
  entry?: WorkspaceEntry;
  current_revision?: string;
  mutation_id?: string;
  mutation_state?: string;
  outcome?: 'pending' | 'not_applied' | 'unknown' | string;
}
