// Same shapes as TurnStats, ToolUse and GuardHit in backend/models/chat.py

export type ToolUse = {
  agent: string
  name: string
  ok: boolean
  duration_ms: number
}

export type GuardHit = {
  agent: string
  name: string
  detail: string
}

export type TurnStats = {
  duration_ms: number
  llm_calls: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
  models: string[]
  fallbacks: number
  tools: ToolUse[]
  guards: GuardHit[]
}

export type Message = {
  role: 'user' | 'assistant'
  content: string
  stats?: TurnStats | null
  traceUrl?: string | null
}

export type ChatResponse = {
  reply: string
  session_id: string
  stats: TurnStats | null
  trace_url: string | null
}