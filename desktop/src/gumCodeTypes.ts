export type GumCodeSkill = { name: string; displayName: string; description: string };
export type ModelProfile = { id: string; name: string; baseUrl: string; model: string };
export type GumCodeState = {
  progress: { id: string; label: string; detail: string; status: "running" | "completed" | "failed" }[];
  plan: { id: string; text: string; status: "pending" | "running" | "completed" }[];
  turnStartedAt: number | null;
  turnEndedAt: number | null;
  workspaces: { id: string; name: string; path: string }[];
  sessions: { id: string; projectId?: string; workspaceId?: string; workspace: string; title: string; updatedAt: number; providerId?: string; pendingInputs?: { id: string; text: string }[] }[];
  selectedId: string | null;
  messages: { role: "user" | "assistant"; content: string; streamId?: string; streaming?: boolean; interrupted?: boolean; inputId?: string; delivery?: "pending" | "received" }[];
  status: "idle" | "running" | "waiting" | "stopping";
  statusText: string;
  approval: { requestId: string; title: string; detail: string } | null;
  error: string | null;
};

export type GumCodeAttachment = { id: string; name: string; size: number };
export type GumCodeInstructions = { path: string; content: string; revision: string; error?: never } | { error: string };
export type GumCodeBridge = {
  gumCodeInstructions: (request: { sessionId: string }) => Promise<GumCodeInstructions>;
  gumCodeSaveInstructions: (request: { sessionId: string; content: string; revision: string }) => Promise<GumCodeInstructions>;
  gumCodePickAttachments: (request: { sessionId: string }) => Promise<{ files: GumCodeAttachment[]; error?: never } | { error: string }>;

  gumCodeMemoryState: (request: { sessionId: string }) => Promise<GumCodeMemoryState>;
  gumCodeMemoryAction: (request: GumCodeMemoryAction) => Promise<GumCodeMemoryState>;
  gumCodeSkills: () => Promise<{ skills: GumCodeSkill[]; error?: string }>;
  gumCodeState: () => Promise<GumCodeState>;
  gumCodeNew: (request: { projectId?: string; workspaceId?: string; providerId?: string }) => Promise<GumCodeState>;
  gumCodePickWorkspace: (request?: { providerId?: string }) => Promise<GumCodeState>;
  gumCodeModels: () => Promise<{ models: ModelProfile[]; error?: string }>;
  gumCodeRename: (request: { sessionId: string; title: string }) => Promise<GumCodeState>;
  gumCodeSelect: (request: { sessionId: string }) => Promise<GumCodeState>;
  gumCodeSend: (request: { sessionId: string; text: string; attachmentIds?: string[]; pendingInputId?: string }) => Promise<GumCodeState>;
  gumCodeApprove: (request: { requestId: string; approved: boolean }) => Promise<GumCodeState>;
  gumCodeStop: () => Promise<GumCodeState>;
  onGumCode: (callback: (state: GumCodeState) => void) => () => void;
};

export type GumCodeMemoryItem = {
  id: string; scope: "workspace" | "global"; kind: string; body: string;
  status: "candidate" | "enabled"; revision: number;
  source: { sessionId: string; turnId: string; inputId?: string };
  reason: string; verified: boolean; skillRef?: string;
};
export type GumCodeMemoryState = {
  enabled: boolean; revision: number; items: GumCodeMemoryItem[];
  jobs: { id: string; status: string; error?: string }[]; error?: string;
  sourceView?: { sessionId: string; turnId: string; available: boolean; evidence?: { toolCallId: string; label: string; content: string }[]; messages: { role: "user" | "assistant"; content: string }[] };
};
export type GumCodeMemoryAction = {
  sessionId: string; action: "enable" | "approve" | "edit" | "delete" | "reject" | "retry" | "source";
  enabled?: boolean; id?: string; revision?: number; body?: string; scope?: "workspace" | "global";
};
