import { defineStore } from "pinia"
import { ref, computed } from "vue"

export interface ServiceInfo {
  status: string
  provider: string
  provider_name: string
  uptime_seconds: number
  pid: number
  memory_bytes: number
  browser_ready: boolean
  client_ready: boolean
}

export interface ProviderItem {
  id: string
  name: string
  port: number
  status: "online" | "offline"
  models: string[]
  memory_bytes: number
  active: number
  capacity: number
  sessions: number
}

export interface QueueSession {
  session_key: string
  locked: boolean
  open: boolean
  url: string
  idle_seconds: number
}

export interface QueueData {
  enabled: boolean
  active: number
  waiting: number
  capacity: number
  persistent_sessions: QueueSession[]
  ephemeral_idle: number
}

export interface ConversationCounts {
  sessions: number
  responses: number
}

export interface MetricData {
  counters: Record<string, number>
  totals: Record<string, number>
  averages: Record<string, number>
}

export interface VncInfo {
  available: boolean
  chatgpt_open: boolean
  gemini_open: boolean
  mode: 'docker' | 'native_macos'
  ports: { chatgpt: number; gemini: number }
  direct_ports: { chatgpt: number; gemini: number }
  status_text: string
}

export interface OverviewData {
  service: ServiceInfo
  providers: ProviderItem[]
  queue: QueueData
  conversations: ConversationCounts
  metrics: MetricData
  vnc?: VncInfo
}

export interface SessionItem {
  project_key: string
  app_key: string
  conversation_key: string
  thread_id?: string
  transcript_json?: string
  message_hashes_json?: string
  contract_hash?: string
  revision?: number
  created_at?: number
  updated_at?: number
}

export interface SettingItem {
  name: string
  value: any
  type: "text" | "int" | "bool" | "choice"
  choices?: string[] | null
  min?: number | null
  max?: number | null
  category: string
  restart_required: boolean
  description: string
}

export interface ToastMessage {
  id: string
  type: "success" | "error" | "info"
  text: string
}

export const useRelayStore = defineStore("relay", () => {
  const isConnected = ref(false)
  const isRefreshing = ref(false)
  const lastUpdated = ref<Date | null>(null)
  const activeTab = ref<"queue" | "sessions" | "logs" | "companions" | "settings">("queue")

  const overview = ref<OverviewData>({
    service: {
      status: "connecting",
      provider: "chatgpt",
      provider_name: "ChatGPT",
      uptime_seconds: 0,
      pid: 0,
      memory_bytes: 0,
      browser_ready: false,
      client_ready: false,
    },
    providers: [],
    queue: {
      enabled: true,
      active: 0,
      waiting: 0,
      capacity: 3,
      persistent_sessions: [],
      ephemeral_idle: 0,
    },
    conversations: { sessions: 0, responses: 0 },
    metrics: { counters: {}, totals: {}, averages: {} },
  })

  const queueItems = ref<QueueSession[]>([])
  const sessions = ref<SessionItem[]>([])
  const logs = ref<string[]>([])
  const logFile = ref<string | null>(null)
  const settings = ref<SettingItem[]>([])
  const envPath = ref<string>("")
  const modifiedSettings = ref<Record<string, any>>({})

  const logFilter = ref("")
  const logLevel = ref<string>("ALL")
  const autoScroll = ref(true)
  const toasts = ref<ToastMessage[]>([])

  function showToast(text: string, type: "success" | "error" | "info" = "success") {
    const id = Math.random().toString(36).substring(2, 9)
    toasts.value.push({ id, type, text })
    setTimeout(() => {
      toasts.value = toasts.value.filter(t => t.id !== id)
    }, 3500)
  }

  async function fetchOverview() {
    try {
      const res = await fetch("/admin/overview")
      if (res.ok) {
        const data = await res.json()
        overview.value = data
        isConnected.value = true
        lastUpdated.value = new Date()
      }
    } catch {
      isConnected.value = false
    }
  }

  async function fetchQueue() {
    try {
      const res = await fetch("/admin/queue")
      if (res.ok) {
        const data = await res.json()
        queueItems.value = data.persistent_sessions || []
        if (data.capacity) {
          overview.value.queue = data
        }
      }
    } catch {
      // Ignored
    }
  }

  async function fetchSessions() {
    try {
      const res = await fetch("/admin/sessions?limit=500")
      if (res.ok) {
        const data = await res.json()
        sessions.value = data.items || []
      }
    } catch {
      // Ignored
    }
  }

  async function fetchLogs() {
    try {
      const res = await fetch("/admin/logs?lines=300")
      if (res.ok) {
        const data = await res.json()
        logs.value = data.lines || []
        logFile.value = data.file || null
      }
    } catch {
      // Ignored
    }
  }

  async function fetchSettings() {
    try {
      const res = await fetch("/admin/settings")
      if (res.ok) {
        const data = await res.json()
        settings.value = data.items || []
        envPath.value = data.env_path || ""
      }
    } catch {
      // Ignored
    }
  }

  async function refreshAll() {
    isRefreshing.value = true
    await Promise.allSettled([
      fetchOverview(),
      fetchQueue(),
      fetchSessions(),
      fetchLogs(),
      fetchSettings(),
    ])
    isRefreshing.value = false
  }

  async function clearIdleQueue() {
    try {
      const res = await fetch("/admin/queue/clear-idle", { method: "POST" })
      if (res.ok) {
        const data = await res.json()
        showToast(`Closed ${data.closed || 0} idle session tab(s)`)
        await fetchQueue()
        await fetchOverview()
      } else {
        showToast("Failed to clear idle queue", "error")
      }
    } catch (e: any) {
      showToast(`Network error: ${e.message}`, "error")
    }
  }

  async function deleteSession(projectKey: string, appKey: string, convKey: string) {
    try {
      const res = await fetch(
        `/admin/sessions/${encodeURIComponent(projectKey)}/${encodeURIComponent(appKey)}/${encodeURIComponent(convKey)}`,
        { method: "DELETE" }
      )
      if (res.ok) {
        showToast("Conversation route deleted")
        sessions.value = sessions.value.filter(
          s => !(s.project_key === projectKey && s.app_key === appKey && s.conversation_key === convKey)
        )
        await fetchOverview()
      } else {
        const err = await res.text()
        showToast(`Failed to delete session: ${err}`, "error")
      }
    } catch (e: any) {
      showToast(`Error deleting session: ${e.message}`, "error")
    }
  }

  async function saveSettings(valuesToSave?: Record<string, any>) {
    const payload = valuesToSave || modifiedSettings.value
    if (Object.keys(payload).length === 0) {
      showToast("No settings have been changed", "info")
      return false
    }
    try {
      const res = await fetch("/admin/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values: payload }),
      })
      if (res.ok) {
        const data = await res.json()
        showToast(`Saved ${data.saved?.length || 0} setting(s) to .env!`)
        modifiedSettings.value = {}
        await fetchSettings()
        return true
      } else {
        const err = await res.json().catch(() => ({ detail: "Unknown error" }))
        showToast(`Save failed: ${err.detail || JSON.stringify(err)}`, "error")
        return false
      }
    } catch (e: any) {
      showToast(`Save error: ${e.message}`, "error")
      return false
    }
  }

  async function restartService() {
    try {
      const res = await fetch("/admin/service/restart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      })
      if (res.ok) {
        showToast("Relay restart initiated! Reconnecting in a few moments...", "info")
        setTimeout(() => refreshAll(), 2000)
        return true
      } else {
        showToast("Failed to schedule restart", "error")
        return false
      }
    } catch (e: any) {
      showToast(`Restart error: ${e.message}`, "error")
      return false
    }
  }

  const filteredLogs = computed(() => {
    let result = logs.value
    if (logLevel.value !== "ALL") {
      result = result.filter(l => l.includes(`| ${logLevel.value}`))
    }
    if (logFilter.value.trim()) {
      const q = logFilter.value.toLowerCase()
      result = result.filter(l => l.toLowerCase().includes(q))
    }
    return result
  })

  return {
    isConnected,
    isRefreshing,
    lastUpdated,
    activeTab,
    overview,
    queueItems,
    sessions,
    logs,
    logFile,
    settings,
    envPath,
    modifiedSettings,
    filteredLogs,
    logFilter,
    logLevel,
    autoScroll,
    toasts,
    showToast,
    refreshAll,
    fetchOverview,
    fetchQueue,
    fetchSessions,
    fetchLogs,
    fetchSettings,
    clearIdleQueue,
    deleteSession,
    saveSettings,
    restartService,
  }
})
